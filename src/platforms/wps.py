"""WPS/Word 平台编排 — 新建文档 → 写文章 → 格式化 → 保存 → 导出 PDF"""

from __future__ import annotations

from src.schemas.task import Task


def build_wps_task(topic: str, filepath: str = "") -> Task:
    """构建一个完整的 WPS 文档创作任务"""
    goal = f"新建 Word 文档，撰写一篇关于「{topic}」的文章，设置标题和正文字体格式，保存文档"
    if filepath:
        goal += f"到 {filepath}"

    return Task(
        goal=goal,
        context={
            "platform": "wps",
            "topic": topic,
            "filepath": filepath or "",
        },
    )
