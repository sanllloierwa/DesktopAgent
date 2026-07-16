"""Interactive tools — 用户手动输入请求

UserInputBridge: 跨线程单例桥接，连接 Agent 线程和 UI 线程
RequestUserInputTool: 请求用户在 UI 对话框中手动输入内容的工具
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from src.tools.base import BaseTool, ToolSchema


class UserInputCancelledError(Exception):
    """用户取消了输入操作"""


def normalize_confirmation(text: str) -> str:
    """Normalize short manual confirmations without exposing arbitrary input."""
    compact = text.strip().lower().replace(" ", "")
    yes_values = {
        "是", "是的", "已登录", "已完成", "已发送", "确认", "确定",
        "yes", "y", "ok", "true", "完成", "成功",
    }
    no_values = {"否", "不是", "没有", "未登录", "未完成", "未发送", "no", "n", "false"}
    if compact in yes_values or compact.startswith(("是，", "是,", "yes,")):
        return "yes"
    if compact in no_values or compact.startswith(("否，", "否,", "no,")):
        return "no"
    return "unknown"


@dataclass
class PromptRequest:
    """一个待处理的用户输入请求"""
    id: str
    prompt: str
    created_at: float = field(default_factory=time.time)


class UserInputBridge:
    """线程安全的用户输入桥接单例。Agent 线程写入请求并等待，UI 线程轮询并响应。"""

    _instance: Optional["UserInputBridge"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Optional[PromptRequest] = None
        self._response_event = threading.Event()
        self._response: Optional[str] = None
        self._cancelled: bool = False

    @classmethod
    def get_instance(cls) -> "UserInputBridge":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    async def request(self, prompt: str, timeout: int = 300) -> str:
        """Agent 线程调用。挂起协程直到用户响应或取消。"""
        with self._lock:
            self._pending = PromptRequest(id=uuid.uuid4().hex[:8], prompt=prompt)
            self._response = None
            self._cancelled = False
            self._response_event.clear()

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._response_event.wait(timeout=timeout))
        finally:
            with self._lock:
                self._pending = None

        with self._lock:
            if self._cancelled:
                raise UserInputCancelledError(self._response or "用户取消了输入对话框")
            if self._response is None:
                raise TimeoutError("用户未在规定时间内响应")
            return self._response

    def has_pending(self) -> bool:
        """UI 线程轮询：是否有待处理的请求"""
        with self._lock:
            return self._pending is not None

    def get_pending(self) -> Optional[PromptRequest]:
        """UI 线程：获取当前待处理的请求"""
        with self._lock:
            return self._pending

    def respond(self, text: str) -> None:
        """UI 线程：提供用户输入，唤醒 Agent 线程"""
        with self._lock:
            self._response = text
            self._cancelled = False
            self._response_event.set()

    def cancel(self, reason: str = "") -> None:
        """UI 线程：取消待处理请求"""
        with self._lock:
            self._cancelled = True
            self._response = reason or "用户取消了输入对话框"
            self._response_event.set()

    def reset(self) -> None:
        """重置状态（会话清理时调用）"""
        with self._lock:
            if self._pending is not None:
                self._cancelled = True
                self._response = "会话已重置"
                self._response_event.set()
                self._pending = None
            self._response_event.clear()


class RequestUserInputTool(BaseTool):
    """请求用户手动输入。适用于 CAPTCHA、验证码、自动填充失败等场景。"""

    schema = ToolSchema(
        name="request_user_input",
        description=(
            "请求用户手动输入内容。适用场景：\n"
            "1. 遇到 CAPTCHA 验证码或图片验证码，无法自动识别时\n"
            "2. 需要输入手机/邮箱收到的验证码（2FA / 多因素认证）\n"
            "3. 自动填充失败的表单字段，需要用户手动填写\n"
            "4. 需要用户确认或选择的操作\n"
            "5. 需要用户提供的非公开信息（如账号密码等）\n"
            "调用此工具会在桌面弹出对话框等待用户输入，输入内容会返回给 Agent。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "向用户显示的提示信息，清晰说明需要输入什么内容及原因。"
                        "例如：'请查看屏幕上的验证码图片，并将验证码输入此处'、"
                        "'请填写手机收到的短信验证码'"
                    ),
                },
            },
            "required": ["prompt"],
        },
    )

    async def execute(self, prompt: str) -> dict:
        bridge = UserInputBridge.get_instance()
        try:
            result = await bridge.request(prompt)
            confirmation = normalize_confirmation(result)
            logger.info(f"User provided input: {len(result)} chars")
            return {
                "success": True,
                "summary": (
                    f"用户确认: {confirmation}"
                    if confirmation != "unknown"
                    else f"用户输入了 {len(result)} 个字符"
                ),
                "user_input": result,
                "confirmation": confirmation,
            }
        except UserInputCancelledError:
            return {
                "success": False,
                "error": "[USER_CANCELLED] 用户取消了输入对话框",
                "summary": "用户取消了输入",
            }
        except TimeoutError:
            return {
                "success": False,
                "error": "[USER_TIMEOUT] 用户输入超时",
                "summary": "用户输入超时，未在规定时间内响应",
            }
