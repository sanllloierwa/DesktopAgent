"""知乎平台编排 — 登录→写文章→配图→发布→搜索→互动"""

from __future__ import annotations

from src.schemas.task import Task


def build_zhihu_task(topic: str) -> Task:
    return Task(
        goal=(
            f"登录知乎网页版，撰写并发布一篇关于「{topic}」的文章，"
            "根据正文自动生成并插入2张配图；发布后通过站内搜索找到该文章，"
            "发表评论，并确认赞同、收藏、喜欢状态。"
        ),
        context={
            "platform": "zhihu",
            "topic": topic,
            "required_images": 2,
            "required_interactions": ["comment", "upvote", "collect", "like"],
        },
    )
