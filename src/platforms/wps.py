"""WPS/Word 平台编排 — 新建文档 → 写文章 → 格式化 → 保存 → 导出 PDF"""

from __future__ import annotations

from src.schemas.task import Task


def build_wps_task(topic: str, filepath: str = "") -> Task:
    """构建一个完整的 WPS 文档创作任务"""
    goal = (
        f"在 WPS 中新建文字文档，撰写一篇关于「{topic}」的文章；"
        "标题单独排版，正文设置字体、两端对齐、1.5 倍行距、段后间距和首行缩进，"
        "需要列举内容时使用自动编号；保存文档并导出为 PDF"
    )
    if filepath:
        goal += f"，文档保存到 {filepath}，PDF 使用同目录同名路径"

    return Task(
        goal=goal,
        context={
            "platform": "wps",
            "topic": topic,
            "filepath": filepath or "",
        },
    )
