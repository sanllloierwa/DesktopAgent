"""微信平台编排 — 打开→搜索→关注→私信"""

from __future__ import annotations

from src.schemas.task import Task


def build_wechat_task(account_name: str = "火眼审阅", message: str = "你好") -> Task:
    return Task(
        goal=f"在微信中搜索「{account_name}」服务号，关注后发送私信：「{message}」",
        context={"platform": "wechat", "account": account_name, "message": message},
    )
