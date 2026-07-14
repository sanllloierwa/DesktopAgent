"BaseTool — 所有工具的抽象基类 + ToolRegistry"

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel
from loguru import logger


class ToolSchema(BaseModel):
    """OpenAI function-calling 兼容的工具描述"""
    name: str
    description: str
    parameters: dict[str, Any] = {}  # JSON Schema


class BaseTool(ABC):
    """所有工具的抽象基类。每个工具需定义 schema 并实现 execute / validate。"""

    schema: ToolSchema

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """执行工具，返回结构化结果"""
        ...

    def validate(self, **kwargs) -> bool:
        """参数校验，默认不做校验，子类可按需覆盖"""
        return True

    def to_openai_tool(self) -> dict[str, Any]:
        """转为 OpenAI / Anthropic tool-use 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.schema.name,
                "description": self.schema.description,
                "parameters": self.schema.parameters,
            },
        }

    async def safe_execute(self, **kwargs) -> dict[str, Any]:
        """带异常捕获的安全执行。

        如果工具的 execute() 返回一个包含 success 字段的字典，则将其中的
        success / error / summary 提升到外层，其余字段放入 data。
        """
        try:
            if not self.validate(**kwargs):
                return {"success": False, "error": "Parameter validation failed", "data": None}
            result = await self.execute(**kwargs)
            if isinstance(result, dict) and "success" in result:
                success = result.pop("success")
                error = result.pop("error", None)
                summary = result.pop("summary", "")
                screenshot = result.pop("screenshot_base64", None)
                return {
                    "success": success,
                    "error": error,
                    "summary": summary,
                    "screenshot_base64": screenshot,
                    "data": result,
                }
            return {"success": True, "error": None, "data": result}
        except Exception as exc:
            logger.error(f"[{self.schema.name}] execution failed: {exc}")
            return {"success": False, "error": str(exc), "data": None}


class ToolRegistry:
    """工具注册中心，按名称查找工具实例，支持生成 LLM tool-use schema 列表"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        name = tool.schema.name
        if name in self._tools:
            logger.warning(f"Tool '{name}' already registered, overwriting")
        self._tools[name] = tool
        logger.debug(f"Registered tool: {name}")

    def register_many(self, tools: list[BaseTool]) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def list_schemas(self) -> list[ToolSchema]:
        return [t.schema for t in self._tools.values()]

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [t.to_openai_tool() for t in self._tools.values()]
