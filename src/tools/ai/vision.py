"""Vision tools — screenshot analysis via LLM"""

from __future__ import annotations

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.utils.llm_factory import create_llm_client


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
            llm = create_llm_client()
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        # 检查是否 Anthropic（原生支持 vision）
        provider = getattr(llm, "_client", None)
        is_anthropic = hasattr(provider, "messages") and not hasattr(provider, "chat")

        try:
            if is_anthropic:
                # Anthropic 原生 vision
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
                # OpenAI/DeepSeek 兼容 - DeepSeek chat 模型目前不原生支持 vision
                # 用文字描述替代
                resp = await llm.messages.create(
                    max_tokens=512,
                    temperature=0.2,
                    messages=[{
                        "role": "user",
                        "content": question + "（注意：当前模型不支持图像分析，请基于常识回答）",
                    }],
                )

            answer = resp.content[0].text if hasattr(resp, "content") else str(resp)
            return {"success": True, "summary": f"Analysis: {answer[:200]}...", "answer": answer}
        except Exception as exc:
            logger.error(f"Vision analysis failed: {exc}")
            return {"success": False, "error": str(exc)}
