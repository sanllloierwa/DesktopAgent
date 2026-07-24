"""AI image generation tools.

The tool intentionally writes generated assets to a dedicated directory and
returns concrete file paths. Browser automation can then upload those files
without passing large base64 payloads through the planner.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
import uuid
from typing import Any
from urllib.request import urlopen

from src.tools.base import BaseTool, ToolSchema
from src.utils.config import load_config
from src.utils.llm_factory import create_llm_client
from src.utils.secret import get_api_key


def _create_image_client() -> Any:
    import openai

    return openai.AsyncOpenAI(api_key=get_api_key("openai"))


def _download_bytes(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:
        return response.read()


def _image_bytes(item: Any) -> bytes:
    encoded = getattr(item, "b64_json", None)
    if encoded:
        return base64.b64decode(encoded)
    raise ValueError("Image response did not contain b64_json data")


def _parse_json(text: str) -> dict[str, Any]:
    start = (text or "").find("{")
    if start < 0:
        raise ValueError("Kimi did not return a JSON object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("Kimi image plan must be a JSON object")
    return value


def _hex_color(value: Any, fallback: str) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"#[0-9a-fA-F]{6}", text) else fallback


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    windows_fonts = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for path in windows_fonts:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _wrap_text(text: str, max_chars: int) -> list[str]:
    compact = " ".join(str(text).split())
    if not compact:
        return []
    return [compact[index:index + max_chars] for index in range(0, len(compact), max_chars)]


def _render_kimi_card(
    design: dict[str, Any],
    output_path: Path,
    size: str,
    variant: int,
) -> None:
    """Render a safe editorial PNG from Kimi's structured visual plan."""
    from PIL import Image, ImageDraw

    width, height = (int(item) for item in size.split("x", 1))
    palette = design.get("palette") if isinstance(design.get("palette"), dict) else {}
    background = _hex_color(palette.get("background"), "#F4F7FB")
    primary = _hex_color(palette.get("primary"), "#172554")
    accent = _hex_color(palette.get("accent"), "#2563EB")
    secondary = _hex_color(palette.get("secondary"), "#7C3AED")

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    margin = max(48, width // 14)

    # Abstract network/flow motif. Kimi chooses the palette and semantic labels;
    # local drawing keeps the output deterministic and safe to upload.
    for index in range(7):
        radius = max(24, width // (24 - min(index, 8)))
        x = width - margin - (index % 3) * (radius * 2 + margin // 3)
        y = margin + (index // 3) * (radius * 2 + margin // 2)
        color = accent if (index + variant) % 2 == 0 else secondary
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
        if index:
            previous_x = width - margin - ((index - 1) % 3) * (radius * 2 + margin // 3)
            previous_y = margin + ((index - 1) // 3) * (radius * 2 + margin // 2)
            draw.line((previous_x, previous_y, x, y), fill=primary, width=max(3, width // 250))

    panel_top = height // 3
    draw.rounded_rectangle(
        (margin, panel_top, width - margin, height - margin),
        radius=max(24, width // 36),
        fill="#FFFFFF",
        outline=accent,
        width=max(2, width // 400),
    )
    draw.rectangle((margin, panel_top, margin + max(10, width // 80), height - margin), fill=accent)

    headline = str(design.get("headline") or "知识与实践")
    subheadline = str(design.get("subheadline") or "")
    keywords = design.get("keywords") if isinstance(design.get("keywords"), list) else []
    keywords = [str(item)[:12] for item in keywords[:4] if str(item).strip()]

    title_font = _font(max(34, width // 22), bold=True)
    subtitle_font = _font(max(20, width // 40))
    keyword_font = _font(max(18, width // 46), bold=True)
    text_x = margin + max(36, width // 28)
    text_y = panel_top + max(34, height // 18)
    max_chars = max(8, width // 75)
    for line in _wrap_text(headline, max_chars)[:2]:
        draw.text((text_x, text_y), line, fill=primary, font=title_font)
        text_y += max(52, width // 17)
    if subheadline:
        text_y += max(8, height // 60)
        for line in _wrap_text(subheadline, max_chars + 8)[:2]:
            draw.text((text_x, text_y), line, fill="#475569", font=subtitle_font)
            text_y += max(34, width // 28)

    chip_y = height - margin - max(42, height // 14)
    chip_x = text_x
    for keyword in keywords:
        box = draw.textbbox((0, 0), keyword, font=keyword_font)
        chip_width = box[2] - box[0] + max(30, width // 35)
        draw.rounded_rectangle(
            (chip_x, chip_y, chip_x + chip_width, chip_y + max(34, height // 18)),
            radius=max(12, height // 40),
            fill=background,
        )
        draw.text(
            (chip_x + max(15, width // 70), chip_y + max(5, height // 120)),
            keyword,
            fill=accent,
            font=keyword_font,
        )
        chip_x += chip_width + max(12, width // 90)
        if chip_x > width - margin * 2:
            break

    image.save(output_path, format="PNG", optimize=True)


async def _generate_kimi_images(
    *,
    topic: str,
    article_text: str,
    count: int,
    size: str,
    style: str,
    output_dir: Path,
    configured_model: str,
) -> tuple[list[str], list[str]]:
    client = create_llm_client(provider_override="kimi")
    selected_model = (
        client.model
        if str(getattr(client, "model", "")).startswith("kimi")
        else configured_model
    )
    excerpt = " ".join(article_text.split())[:6000]
    prompt = f"""请为一篇知乎知识文章设计 {count} 张互不重复的编辑配图方案。

主题：{topic}
视觉风格：{style}
正文摘要素材：{excerpt}

只返回 JSON 对象：
{{
  "designs": [
    {{
      "headline": "不超过14个汉字",
      "subheadline": "不超过28个汉字",
      "keywords": ["关键词1", "关键词2", "关键词3"],
      "palette": {{
        "background": "#六位十六进制",
        "primary": "#六位十六进制",
        "accent": "#六位十六进制",
        "secondary": "#六位十六进制"
      }}
    }}
  ]
}}
不得输出品牌标识、营销话术、水印、虚构数据或正文中没有的事实。"""
    response = await client.messages.create(
        model=selected_model,
        max_tokens=1800,
        temperature=0.6,
        system="你是专业的中文知识内容视觉编辑，只输出符合要求的 JSON。",
        messages=[{"role": "user", "content": prompt}],
        json_mode=True,
    )
    plan = _parse_json(response.content[0].text)
    designs = plan.get("designs")
    if not isinstance(designs, list) or not designs:
        raise ValueError("Kimi response did not contain any image designs")

    paths: list[str] = []
    descriptions: list[str] = []
    for index in range(count):
        raw_design = designs[index % len(designs)]
        design = raw_design if isinstance(raw_design, dict) else {}
        path = output_dir / f"article-kimi-{uuid.uuid4().hex[:12]}.png"
        await asyncio.to_thread(_render_kimi_card, design, path, size, index)
        paths.append(str(path))
        descriptions.append(str(design.get("headline") or topic))
    return paths, descriptions


class GenerateImageTool(BaseTool):
    schema = ToolSchema(
        name="generate_image",
        description=(
            "根据文章主题和正文生成1-3张配图，保存为本地 PNG，并返回 image_paths。"
            "支持 OpenAI 图片接口，或使用 Kimi 生成视觉方案并由本地渲染器输出图片。"
            "适合在网页文章编辑器中继续调用 upload_image 上传。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "文章主题或图片主题"},
                "article_text": {
                    "type": "string",
                    "description": "可选文章正文；工具会据此生成与内容一致的编辑插图",
                },
                "count": {
                    "type": "integer",
                    "description": "图片数量，1-3，默认2",
                    "minimum": 1,
                    "maximum": 3,
                },
                "size": {
                    "type": "string",
                    "description": "图片尺寸",
                    "enum": ["1024x1024", "1024x1792", "1792x1024"],
                },
                "style": {
                    "type": "string",
                    "description": "视觉风格补充，例如 editorial illustration",
                },
            },
            "required": ["topic"],
        },
    )

    async def execute(
        self,
        topic: str,
        article_text: str = "",
        count: int = 2,
        size: str = "",
        style: str = "clean editorial illustration",
    ) -> dict:
        config = load_config()
        image_config = config.image_gen
        count = max(1, min(int(count), 3))
        selected_size = size or image_config.default_size
        allowed_sizes = {"1024x1024", "1024x1792", "1792x1024"}
        if selected_size not in allowed_sizes:
            return {"success": False, "error": f"Unsupported image size: {selected_size}"}
        if image_config.provider not in {"openai", "kimi"}:
            return {
                "success": False,
                "error": f"Unsupported image provider: {image_config.provider}",
            }

        excerpt = " ".join(article_text.split())[:3000]
        prompt = (
            f"Create a {style} for a Chinese knowledge article about: {topic}. "
            "The image must communicate one clear idea from the article, use a "
            "professional restrained color palette, contain no logos, watermarks, "
            "UI screenshots, signatures, or readable text."
        )
        if excerpt:
            prompt += f"\nArticle context:\n{excerpt}"

        output_dir = Path(image_config.output_dir).expanduser().resolve()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            if image_config.provider == "kimi":
                paths, descriptions = await _generate_kimi_images(
                    topic=topic,
                    article_text=article_text,
                    count=count,
                    size=selected_size,
                    style=style,
                    output_dir=output_dir,
                    configured_model=image_config.model,
                )
                return {
                    "success": True,
                    "summary": f"Generated {len(paths)} Kimi-designed article image(s)",
                    "image_paths": paths,
                    "count": len(paths),
                    "size": selected_size,
                    "provider": "kimi",
                    "render_method": "kimi_visual_plan+pillow_png",
                    "revised_prompts": descriptions,
                }

            client = _create_image_client()
            paths: list[str] = []
            revised_prompts: list[str] = []
            for index in range(count):
                response = await client.images.generate(
                    model=image_config.model,
                    prompt=f"{prompt}\nIllustration variant {index + 1} of {count}.",
                    size=selected_size,
                    n=1,
                    response_format="b64_json",
                )
                item = response.data[0]
                try:
                    data = _image_bytes(item)
                except ValueError:
                    url = getattr(item, "url", None)
                    if not url:
                        raise
                    data = await asyncio.to_thread(_download_bytes, url)
                if not data:
                    raise ValueError("Image provider returned an empty image")
                path = output_dir / f"article-{uuid.uuid4().hex[:12]}.png"
                path.write_bytes(data)
                paths.append(str(path))
                revised = getattr(item, "revised_prompt", None)
                if revised:
                    revised_prompts.append(str(revised))
            return {
                "success": True,
                "summary": f"Generated {len(paths)} article image(s)",
                "image_paths": paths,
                "count": len(paths),
                "size": selected_size,
                "provider": "openai",
                "render_method": "provider_image_api",
                "revised_prompts": revised_prompts,
            }
        except Exception as exc:
            return {"success": False, "error": f"Image generation failed: {exc}"}
