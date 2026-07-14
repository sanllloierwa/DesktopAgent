"""WPS / Word COM automation — 文档创建、编辑、格式、保存、导出 PDF"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from loguru import logger

from src.tools.base import BaseTool, ToolSchema

_WORD_COM_UNAVAILABLE = (
    "[ENV_ERR] Cannot get Word/WPS COM application. Install pywin32 and ensure "
    "Microsoft Word or WPS Writer is installed with COM automation registered."
)

# WdBuiltinStyle values. Use numeric IDs instead of localized names such as
# "Normal" / "Heading 1", which are not guaranteed to exist in Chinese Word.
_WD_STYLE_NORMAL = -1
_WD_STYLE_HEADING_1 = -2


# ---------------------------------------------------------------------------
# COM helper
# ---------------------------------------------------------------------------

def _get_word_app():
    """获取 WPS 或 Word COM Application 对象"""
    try:
        import win32com.client
        # 先尝试 Word
        try:
            app = win32com.client.GetActiveObject("Word.Application")
            return app
        except Exception:
            pass
        # 尝试 WPS
        try:
            app = win32com.client.GetActiveObject("KWPS.Application")
            return app
        except Exception:
            pass
        # 尝试 ET.Application (WPS 表格) 不行
        # 尝试新建
        try:
            app = win32com.client.Dispatch("Word.Application")
            return app
        except Exception:
            pass
        try:
            app = win32com.client.Dispatch("KWPS.Application")
            return app
        except Exception:
            pass
        return None
    except ImportError:
        logger.warning("pywin32 not installed, COM automation unavailable")
        return None


def _word_com_error() -> str:
    try:
        import win32com.client  # noqa: F401
    except ImportError as exc:
        return f"[ENV_ERR] Missing dependency: {exc}. Install pywin32 in the Python environment running Agent."
    return _WORD_COM_UNAVAILABLE


def _markdown_to_word_text(text: str) -> str:
    lines: list[str] = []
    in_code_block = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue

        if not in_code_block:
            heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
            if heading:
                line = heading.group(1).strip()

            line = re.sub(r"^\s*[-*+]\s+", "- ", line)
            line = re.sub(r"^\s*>+\s?", "", line)
            line = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", line)
            line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
            line = re.sub(r"(\*\*|__)(.*?)\1", r"\2", line)
            line = re.sub(r"(\*|_)(.*?)\1", r"\2", line)
            line = re.sub(r"`([^`]+)`", r"\1", line)

            if "|" in line and re.match(r"^\s*\|?[-:|\s]+\|?\s*$", line):
                continue
            if "|" in line:
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                if len(cells) > 1:
                    line = "    ".join(cells)

        lines.append(line.strip() if not in_code_block else line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class CreateDocumentTool(BaseTool):
    schema = ToolSchema(
        name="create_document",
        description="在 WPS/Word 中新建一个空白文档",
        parameters={
            "type": "object",
            "properties": {},
        },
    )

    async def execute(self) -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            app.Visible = True
            doc = app.Documents.Add()
            return {
                "success": True,
                "summary": "Created new document",
                "document_name": doc.Name,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class WriteTextTool(BaseTool):
    schema = ToolSchema(
        name="write_document_text",
        description="向当前 WPS/Word 文档写入文字内容。支持插入标题和正文段落。",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要写入的文字内容",
                },
                "style": {
                    "type": "string",
                    "description": "段落样式: heading | body（默认 body）",
                    "enum": ["heading", "body"],
                },
                "content_format": {
                    "type": "string",
                    "description": "输入文本格式: auto | plain_text | markdown。写入 Word/WPS 时默认自动清理 Markdown 标记。",
                    "enum": ["auto", "plain_text", "markdown"],
                },
            },
            "required": ["text"],
        },
    )

    async def execute(self, text: str, style: str = "body", content_format: str = "auto") -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            looks_like_markdown = bool(
                re.search(r"(^#{1,6}\s)|(\*\*[^*]+\*\*)|(```)|(\[[^\]]+\]\([^)]+\))", text, re.MULTILINE)
            )
            if content_format == "markdown" or (content_format == "auto" and looks_like_markdown):
                text = _markdown_to_word_text(text)

            doc = app.ActiveDocument
            selection = app.Selection

            builtin_style = _WD_STYLE_HEADING_1 if style == "heading" else _WD_STYLE_NORMAL
            selection.Style = doc.Styles(builtin_style)

            selection.TypeText(text)
            selection.TypeParagraph()
            return {
                "success": True,
                "summary": f"Wrote {len(text)} chars as {style}",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class SetFontTool(BaseTool):
    schema = ToolSchema(
        name="set_font",
        description="设置当前选中文本的字体、大小、加粗、颜色",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "字体名称，如 宋体、黑体、Microsoft YaHei",
                },
                "size": {
                    "type": "number",
                    "description": "字号（磅），如 12、16、22",
                },
                "bold": {
                    "type": "boolean",
                    "description": "是否加粗",
                },
                "select_all": {
                    "type": "boolean",
                    "description": "是否应用到全文（默认仅当前光标处）",
                },
            },
            "required": [],
        },
    )

    async def execute(
        self, name: str = "", size: float = 0, bold: bool | None = None, select_all: bool = False
    ) -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            if select_all:
                app.ActiveDocument.Range().Select()

            font = app.Selection.Font
            changes: list[str] = []
            if name:
                font.Name = name
                changes.append(f"font={name}")
            if size > 0:
                font.Size = size
                changes.append(f"size={size}")
            if bold is not None:
                font.Bold = bold
                changes.append(f"bold={bold}")

            return {
                "success": True,
                "summary": f"Applied font: {', '.join(changes)}" if changes else "No changes",
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class SetAlignmentTool(BaseTool):
    schema = ToolSchema(
        name="set_alignment",
        description="设置段落对齐方式",
        parameters={
            "type": "object",
            "properties": {
                "align": {
                    "type": "string",
                    "description": "对齐方式",
                    "enum": ["left", "center", "right", "justify"],
                },
            },
            "required": ["align"],
        },
    )

    async def execute(self, align: str = "left") -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            from win32com.client import constants
            align_map = {
                "left": constants.wdAlignParagraphLeft,
                "center": constants.wdAlignParagraphCenter,
                "right": constants.wdAlignParagraphRight,
                "justify": constants.wdAlignParagraphJustify,
            }
            app.Selection.ParagraphFormat.Alignment = align_map.get(align, 0)
            return {"success": True, "summary": f"Alignment set to {align}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class SaveDocumentTool(BaseTool):
    schema = ToolSchema(
        name="save_document",
        description="保存当前 WPS/Word 文档到指定路径",
        parameters={
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "保存路径，如 C:\\Users\\xxx\\Desktop\\article.docx。不填则保存到桌面。",
                },
            },
            "required": [],
        },
    )

    async def execute(self, filepath: str = "") -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            doc = app.ActiveDocument
            if not filepath:
                filepath = os.path.join(os.path.expanduser("~"), "Desktop", f"{doc.Name or 'document'}.docx")

            doc.SaveAs(filepath)
            return {
                "success": True,
                "summary": f"Document saved to {filepath}",
                "filepath": filepath,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class ExportPDFTool(BaseTool):
    schema = ToolSchema(
        name="export_pdf",
        description="将当前文档导出为 PDF 文件",
        parameters={
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "PDF 保存路径。不填则和文档同名。",
                },
            },
            "required": [],
        },
    )

    async def execute(self, filepath: str = "") -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            doc = app.ActiveDocument
            if not filepath:
                base = os.path.splitext(doc.FullName)[0]
                filepath = base + ".pdf"

            doc.ExportAsFixedFormat(filepath, 17)  # 17 = wdExportFormatPDF
            return {
                "success": True,
                "summary": f"Exported PDF to {filepath}",
                "filepath": filepath,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class InsertImageTool(BaseTool):
    schema = ToolSchema(
        name="insert_image",
        description="在当前文档光标处插入图片",
        parameters={
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "图片文件的完整路径",
                },
            },
            "required": ["image_path"],
        },
    )

    async def execute(self, image_path: str) -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        if not os.path.exists(image_path):
            return {"success": False, "error": f"Image not found: {image_path}"}

        try:
            app.Selection.InlineShapes.AddPicture(image_path)
            return {"success": True, "summary": f"Inserted image: {image_path}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
