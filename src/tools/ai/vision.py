"""Vision tools — screenshot analysis via dedicated vision LLM"""

from __future__ import annotations

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.utils.llm_factory import create_vision_client, _AnthropicAdapter


class AnalyzeScreenTool(BaseTool):
    schema = ToolSchema(
        name="analyze_screen",
        description="分析截图内容，识别 UI 元素、文字、按钮等。需要传入 base64 编码的图片。",
        parameters={
            "type": "object",
            "properties": {
                "image_base64": {
                    "type": "string",
                    "description": "截图的 base64 编码",
                },
                "question": {
                    "type": "string",
                    "description": "要询问的问题，如 '发布按钮在哪里？' 或 '页面上的主要内容是什么？'",
                },
            },
            "required": ["image_base64", "question"],
        },
    )

    async def execute(self, image_base64: str, question: str = "描述这个画面中的内容") -> dict:
        try:
            llm = create_vision_client()
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        is_anthropic = isinstance(llm, _AnthropicAdapter)

        try:
            if is_anthropic:
                resp = await llm.messages.create(
                    max_tokens=1024,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image_base64,
                                },
                            },
                            {"type": "text", "text": question},
                        ],
                    }],
                )
            else:
                # OpenAI 兼容格式 — 支持 GPT-4o、Agnes-2.0-flash 等多模态模型
                resp = await llm.messages.create(
                    max_tokens=1024,
                    temperature=0.2,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": question},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_base64}",
                                },
                            },
                        ],
                    }],
                )

            answer = resp.content[0].text if hasattr(resp, "content") else str(resp)
            return {"success": True, "summary": f"Analysis: {answer[:200]}...", "answer": answer}
        except Exception as exc:
            logger.error(f"Vision analysis failed: {exc}")
            return {"success": False, "error": str(exc)}
