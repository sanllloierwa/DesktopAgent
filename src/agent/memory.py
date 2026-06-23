"Memory system — 短期/工作/长期三层记忆"

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from src.schemas.task import Step, ActionResult


@dataclass
class MemoryEntry:
    role: str                    # "step" | "observation" | "error" | "reflection"
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class ShortTermMemory:
    """短期记忆：当前任务的滑动窗口，记录最近的步骤和观察"""

    def __init__(self, max_entries: int = 50) -> None:
        self._buffer: deque[MemoryEntry] = deque(maxlen=max_entries)

    def add(self, entry: MemoryEntry) -> None:
        self._buffer.append(entry)

    def recent(self, n: int = 10) -> list[MemoryEntry]:
        """获取最近 n 条"""
        return list(self._buffer)[-n:]

    def to_messages(self) -> list[dict[str, Any]]:
        """转为 LLM 对话消息格式"""
        role_map = {"step": "assistant", "observation": "user", "error": "user", "reflection": "assistant"}
        return [{"role": role_map.get(e.role, "user"), "content": e.content} for e in self._buffer]

    def clear(self) -> None:
        self._buffer.clear()


class WorkingMemory:
    """工作记忆：当前会话的全局上下文，如已完成步骤、环境状态"""

    def __init__(self) -> None:
        self.completed_steps: list[tuple[Step, ActionResult]] = []
        self.errors: list[str] = []
        self.scratchpad: dict[str, Any] = {}  # 随意读写的工作区

    def record_step(self, step: Step, result: ActionResult) -> None:
        self.completed_steps.append((step, result))
        if not result.success and result.error:
            self.errors.append(result.error)

    @property
    def recent_errors(self) -> list[str]:
        return self.errors[-5:]

    @property
    def last_result(self) -> ActionResult | None:
        if self.completed_steps:
            return self.completed_steps[-1][1]
        return None

    def clear(self) -> None:
        self.completed_steps.clear()
        self.errors.clear()
        self.scratchpad.clear()


class LongTermMemory:
    """长期记忆：向量存储，用于跨任务的模式记忆。
    当前使用 ChromaDB，存储历史成功经验和常见失败恢复策略。
    """

    def __init__(self, collection_name: str = "agent_memory") -> None:
        self._collection_name = collection_name
        self._client = None
        self._collection = None
        self._init_chromadb()

    def _init_chromadb(self) -> None:
        try:
            import chromadb
            self._client = chromadb.PersistentClient(
                path="./agent_memory_db",
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception:
            self._client = None
            self._collection = None

    def store(self, text: str, metadata: dict[str, Any] | None = None) -> None:
        if self._collection is None:
            return
        doc_id = f"mem_{int(time.time() * 1000)}"
        self._collection.add(documents=[text], metadatas=[metadata or {}], ids=[doc_id])

    def search(self, query: str, n: int = 5) -> list[str]:
        if self._collection is None:
            return []
        results = self._collection.query(query_texts=[query], n_results=n)
        return results.get("documents", [[]])[0] if results else []

    def clear(self) -> None:
        if self._client and self._collection:
            try:
                self._client.delete_collection(self._collection_name)
                self._collection = self._client.get_or_create_collection(
                    name=self._collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
            except Exception:
                pass


class MemoryHub:
    """记忆系统中枢，组合三层记忆"""

    def __init__(self) -> None:
        self.short_term = ShortTermMemory()
        self.working = WorkingMemory()
        self.long_term = LongTermMemory()

    def commit(self, step: Step, result: ActionResult) -> None:
        self.working.record_step(step, result)
        summary = f"[{'OK' if result.success else 'FAIL'}] {step.description}: {result.summary}"
        self.short_term.add(MemoryEntry(role="step", content=summary))
        if result.success:
            self.long_term.store(
                f"Tool: {step.tool_name}, Desc: {step.description}, Result: {result.summary}",
                {"tool": step.tool_name, "success": True},
            )

    def remember_error(self, error_msg: str) -> None:
        self.short_term.add(MemoryEntry(role="error", content=error_msg))

    def context_for_planner(self) -> str:
        """拼接给 Planner 的最新上下文"""
        recent = self.short_term.recent(10)
        lines = [f"[{e.role}] {e.content}" for e in recent]
        return "\n".join(lines)

    def clear(self) -> None:
        self.short_term.clear()
        self.working.clear()
