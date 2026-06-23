"""知乎平台编排 — 登录→写文章→配图→发布→搜索→互动"""

from __future__ import annotations

from src.schemas.task import Task


def build_zhihu_task(topic: str) -> Task:
    return Task(
        goal=f"在知乎发布一篇关于「{topic}」的文章，配2-3张图，发布后搜索并点赞、收藏",
        context={"platform": "zhihu", "topic": topic},
    )
