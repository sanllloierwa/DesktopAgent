"""AI text generation tools — LLM article writing"""

from __future__ import annotations

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.utils.llm_factory import create_llm_client


class GenerateArticleTool(BaseTool):
    schema = ToolSchema(
        name="generate_article",
        description="使用 LLM 生成一篇结构化文章。支持指定主题、风格和字数。",
        parameters={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "文章主题",
                },
                "style": {
                    "type": "string",
                    "description": "文章风格: professional | casual | academic | tutorial",
                    "enum": ["professional", "casual", "academic", "tutorial"],
                },
                "length": {
                    "type": "string",
                    "description": "篇幅: short(~500字) | medium(~1500字) | long(~3000字)",
                    "enum": ["short", "medium", "long"],
                },
                "outline": {
                    "type": "string",
                    "description": "可选的大纲（分号分隔各段要点）",
                },
            },
            "required": ["topic"],
        },
    )

    async def execute(
        self, topic: str, style: str = "professional", length: str = "medium", outline: str = ""
    ) -> dict:
        try:
            llm = create_llm_client()
        except Exception as exc:
            return {"success": False, "error": f"LLM client init failed: {exc}"}

        length_map = {
            "short": "约500字",
            "medium": "约1500字",
            "long": "约3000字",
        }
        target_length = length_map.get(length, "约1500字")

        style_map = {
            "professional": "专业严谨，逻辑清晰",
            "casual": "轻松易读，富有感染力",
            "academic": "学术风格，引经据典",
            "tutorial": "教程风格，循序渐进，附带示例",
        }
        style_desc = style_map.get(style, "专业严谨")

        outline_instruction = ""
        if outline:
            outline_instruction = f"\n请按照以下大纲组织文章：\n{outline}"

        prompt = f"""请撰写一篇关于「{topic}」的文章。

要求：
- 风格：{style_desc}
- 篇幅：{target_length}
- 使用 Markdown 格式，包含标题、段落、列表等结构
- 如果合适，可以包含代码示例或表格
{outline_instruction}

请直接输出文章正文（Markdown 格式）："""

        try:
            resp = await llm.messages.create(
                model=getattr(llm, "model", ""),
                max_tokens=4096,
                temperature=0.7,
                system="你是一个专业的内容创作助手。",
                messages=[{"role": "user", "content": prompt}],
            )
            article = resp.content[0].text if hasattr(resp, "content") else str(resp)
            return {
                "success": True,
                "summary": f"Generated article about '{topic}' ({len(article)} chars)",
                "article": article,
                "topic": topic,
            }
        except Exception as exc:
            logger.error(f"Article generation failed: {exc}")
            return {"success": False, "error": str(exc)}


class SummarizeTool(BaseTool):
    schema = ToolSchema(
        name="summarize",
        description="使用 LLM 对文本进行摘要",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要摘要的原文"},
                "max_length": {"type": "number", "description": "摘要最大字数，默认 200"},
            },
            "required": ["text"],
        },
    )

    async def execute(self, text: str, max_length: int = 200) -> dict:
        try:
            llm = create_llm_client()
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        prompt = f"请对以下文本进行摘要，不超过{max_length}字：\n\n{text[:10000]}"
        try:
            resp = await llm.messages.create(
                model=getattr(llm, "model", ""),
                max_tokens=max_length * 2,
                temperature=0.2,
                system="你是一个专业的文本摘要助手。",
                messages=[{"role": "user", "content": prompt}],
            )
            summary = resp.content[0].text if hasattr(resp, "content") else str(resp)
            return {"success": True, "summary": f"Summarized to {len(summary)} chars", "result": summary}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
