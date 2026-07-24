"""WPS / Word COM automation — 文档创建、编辑、格式、保存、导出 PDF"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from src.tools.base import BaseTool, ToolSchema

_WORD_COM_UNAVAILABLE = (
    "[ENV_ERR] Cannot get Word/WPS COM application. Install pywin32 and ensure "
    "Microsoft Word or WPS Writer is installed with COM automation registered."
)

# This module backs the WPS platform workflow, so prefer WPS when both suites
# are installed while retaining Microsoft Word as a compatible fallback.
_WORD_APP_PROG_IDS = ("KWPS.Application", "Word.Application")

# WdBuiltinStyle values. Use numeric IDs instead of localized names such as
# "Normal" / "Heading 1", which are not guaranteed to exist in Chinese Word.
_WD_STYLE_NORMAL = -1
_WD_STYLE_HEADING_1 = -2
_WD_STYLE_HEADING_2 = -3
_WD_STYLE_HEADING_3 = -4
_WD_STYLE_TITLE = -63
_WD_STYLE_SUBTITLE = -75

_STYLE_IDS = {
    "body": _WD_STYLE_NORMAL,
    "heading": _WD_STYLE_HEADING_1,
    "heading1": _WD_STYLE_HEADING_1,
    "heading2": _WD_STYLE_HEADING_2,
    "heading3": _WD_STYLE_HEADING_3,
    "title": _WD_STYLE_TITLE,
    "subtitle": _WD_STYLE_SUBTITLE,
}

_ALIGNMENT_VALUES = {
    "left": 0,
    "center": 1,
    "right": 2,
    "justify": 3,
}

_LINE_SPACING_RULES = {
    "single": 0,
    "one_and_half": 1,
    "double": 2,
    "at_least": 3,
    "exactly": 4,
    "multiple": 5,
}


# ---------------------------------------------------------------------------
# COM helper
# ---------------------------------------------------------------------------

def _get_word_app():
    """获取 WPS 或 Word COM Application 对象"""
    try:
        import win32com.client

        for prog_id in _WORD_APP_PROG_IDS:
            try:
                return win32com.client.GetActiveObject(prog_id)
            except Exception:
                continue
        for prog_id in _WORD_APP_PROG_IDS:
            try:
                return win32com.client.Dispatch(prog_id)
            except Exception:
                continue
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


def _document_range(
    app: Any,
    target: str,
    start_paragraph: int = 0,
    end_paragraph: int = 0,
) -> tuple[Any, Any, str]:
    """Resolve a stable Word Range instead of relying on the current caret."""
    doc = app.ActiveDocument
    target = target.strip().lower()

    if target == "selection":
        return doc, app.Selection.Range, "selection"
    if target == "document":
        return doc, doc.Content, "document"

    paragraph_count = int(doc.Paragraphs.Count)
    if paragraph_count < 1:
        raise ValueError("The active document has no paragraphs")

    if target == "title":
        start_paragraph = end_paragraph = 1
    elif target == "body":
        if paragraph_count < 2:
            raise ValueError("The active document has no body paragraphs")
        start_paragraph = 2
        end_paragraph = paragraph_count
    elif target == "paragraphs":
        if start_paragraph < 1:
            raise ValueError("start_paragraph must be at least 1")
        if end_paragraph <= 0:
            end_paragraph = start_paragraph
    else:
        raise ValueError(
            "target must be selection, document, title, body, or paragraphs"
        )

    if end_paragraph < start_paragraph or end_paragraph > paragraph_count:
        raise ValueError(
            f"Paragraph range {start_paragraph}-{end_paragraph} is outside "
            f"the document (1-{paragraph_count})"
        )

    first = doc.Paragraphs(start_paragraph).Range
    last = doc.Paragraphs(end_paragraph).Range
    resolved = doc.Range(first.Start, last.End)
    return doc, resolved, f"paragraphs {start_paragraph}-{end_paragraph}"


def _hex_to_wd_color(value: str) -> int:
    """Convert #RRGGBB to Word's BGR integer used by Font.Color."""
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if not match:
        raise ValueError("font_color must use #RRGGBB format")
    rgb = match.group(1)
    red, green, blue = (int(rgb[index:index + 2], 16) for index in (0, 2, 4))
    return red | (green << 8) | (blue << 16)


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PATH_PLACEHOLDERS = re.compile(
    r"(<[^>]+>|\{[^}]+\}|\bxxx\b|用户名|user_name)", re.IGNORECASE
)


def _safe_document_stem(doc: Any) -> str:
    raw_name = str(getattr(doc, "Name", "") or "document")
    stem = Path(raw_name).stem.strip()
    stem = _INVALID_FILENAME_CHARS.sub("_", stem).rstrip(". ")
    return stem or "document"


def _desktop_directory() -> Path:
    candidates = [
        Path.home() / "Desktop",
        Path(os.environ.get("OneDrive", "")) / "Desktop"
        if os.environ.get("OneDrive")
        else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate
    return Path.home() / "Documents"


def _normalize_output_path(
    filepath: str,
    doc: Any,
    suffix: str,
    *,
    default_from_document: bool = False,
) -> Path:
    suffix = suffix.lower()
    requested = str(filepath or "").strip().strip('"\'')

    if not requested and default_from_document:
        full_name = str(getattr(doc, "FullName", "") or "").strip()
        if full_name:
            return Path(full_name).with_suffix(suffix)

    if not requested:
        candidate = _desktop_directory() / f"{_safe_document_stem(doc)}{suffix}"
    elif requested.lower() in {"desktop", "桌面", "公共桌面"}:
        candidate = _desktop_directory() / f"{_safe_document_stem(doc)}{suffix}"
    else:
        expanded = os.path.expandvars(os.path.expanduser(requested))
        supplied = Path(expanded)
        if _PATH_PLACEHOLDERS.search(expanded):
            supplied_name = supplied.name
            if _PATH_PLACEHOLDERS.search(supplied_name):
                supplied_name = f"{_safe_document_stem(doc)}{suffix}"
            candidate = _desktop_directory() / supplied_name
        elif supplied.exists() and supplied.is_dir():
            candidate = supplied / f"{_safe_document_stem(doc)}{suffix}"
        elif requested.endswith(("/", "\\")):
            candidate = supplied / f"{_safe_document_stem(doc)}{suffix}"
        else:
            candidate = supplied
            if not candidate.is_absolute():
                candidate = _desktop_directory() / candidate

    if candidate.suffix.lower() != suffix:
        candidate = candidate.with_suffix(suffix)
    safe_stem = _INVALID_FILENAME_CHARS.sub("_", candidate.stem).rstrip(". ")
    candidate = candidate.with_name(f"{safe_stem or _safe_document_stem(doc)}{suffix}")
    return Path(os.path.abspath(str(candidate)))


def _candidate_output_paths(
    filepath: str,
    doc: Any,
    suffix: str,
    *,
    default_from_document: bool = False,
) -> list[Path]:
    primary = _normalize_output_path(
        filepath, doc, suffix, default_from_document=default_from_document
    )
    name = primary.name
    candidates = [primary]

    full_name = str(getattr(doc, "FullName", "") or "").strip()
    if full_name:
        candidates.append(Path(full_name).with_suffix(suffix))
    candidates.extend([
        _desktop_directory() / name,
        Path.home() / "Documents" / name,
        Path.cwd() / name,
    ])

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        absolute = Path(os.path.abspath(str(candidate)))
        key = os.path.normcase(str(absolute))
        if key not in seen:
            seen.add(key)
            unique.append(absolute)
    return unique


def _ensure_parent_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.parent.is_dir():
        raise OSError(f"Output directory is unavailable: {path.parent}")


def _verify_output_file(path: Path) -> None:
    for _ in range(3):
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.1)
    raise OSError(f"Output file was not created or is empty: {path}")


def _save_docx(doc: Any, path: Path) -> None:
    try:
        save_as2 = doc.SaveAs2
    except Exception:
        save_as2 = None
    if callable(save_as2):
        save_as2(str(path), 16)  # wdFormatDocumentDefault (.docx)
    else:
        doc.SaveAs(str(path))


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
                    "description": "段落样式: title | subtitle | heading/heading1 | heading2 | heading3 | body",
                    "enum": [
                        "title", "subtitle", "heading", "heading1",
                        "heading2", "heading3", "body",
                    ],
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

            builtin_style = _STYLE_IDS.get(style)
            if builtin_style is None:
                return {"success": False, "error": f"Unsupported paragraph style: {style}"}
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


class FormatDocumentRangeTool(BaseTool):
    schema = ToolSchema(
        name="format_document_range",
        description=(
            "对 WPS/Word 已有内容按稳定范围排版，不依赖当前光标。"
            "可选择标题、正文、全文、当前选区或指定段落，并设置样式、字体、"
            "字号、字形、颜色、对齐、行距、段间距和中文字符缩进。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["selection", "document", "title", "body", "paragraphs"],
                    "description": "排版范围；指定 paragraphs 时配合 start_paragraph/end_paragraph",
                },
                "start_paragraph": {
                    "type": "integer",
                    "description": "起始段落，1 为第一段，仅 target=paragraphs 时使用",
                },
                "end_paragraph": {
                    "type": "integer",
                    "description": "结束段落（含）；不填时等于 start_paragraph",
                },
                "style": {
                    "type": "string",
                    "enum": ["title", "subtitle", "heading1", "heading2", "heading3", "body"],
                    "description": "可选的 Word/WPS 内置段落样式",
                },
                "font_name": {
                    "type": "string",
                    "description": "中西文字体，如 宋体、黑体、Microsoft YaHei",
                },
                "font_size": {"type": "number", "description": "字号（磅）"},
                "bold": {"type": "boolean", "description": "是否加粗"},
                "italic": {"type": "boolean", "description": "是否斜体"},
                "font_color": {
                    "type": "string",
                    "description": "字体颜色，#RRGGBB 格式，如 #1F4E79",
                },
                "alignment": {
                    "type": "string",
                    "enum": ["left", "center", "right", "justify"],
                    "description": "段落对齐",
                },
                "line_spacing_rule": {
                    "type": "string",
                    "enum": ["single", "one_and_half", "double", "at_least", "exactly", "multiple"],
                    "description": "行距规则；exactly/at_least 需给出磅值，multiple 需给出倍数",
                },
                "line_spacing_value": {
                    "type": "number",
                    "description": "固定/最小行距的磅值，或 multiple 的倍数",
                },
                "space_before": {"type": "number", "description": "段前间距（磅）"},
                "space_after": {"type": "number", "description": "段后间距（磅）"},
                "first_line_indent_chars": {
                    "type": "number",
                    "description": "首行缩进字符数，中文正文通常为 2",
                },
                "left_indent_chars": {"type": "number", "description": "左缩进字符数"},
                "right_indent_chars": {"type": "number", "description": "右缩进字符数"},
                "keep_with_next": {
                    "type": "boolean",
                    "description": "与下一段同页，适合标题",
                },
                "page_break_before": {
                    "type": "boolean",
                    "description": "段前分页",
                },
            },
            "required": ["target"],
        },
    )

    async def execute(
        self,
        target: str,
        start_paragraph: int = 0,
        end_paragraph: int = 0,
        style: str = "",
        font_name: str = "",
        font_size: float = 0,
        bold: bool | None = None,
        italic: bool | None = None,
        font_color: str = "",
        alignment: str = "",
        line_spacing_rule: str = "",
        line_spacing_value: float = 0,
        space_before: float | None = None,
        space_after: float | None = None,
        first_line_indent_chars: float | None = None,
        left_indent_chars: float | None = None,
        right_indent_chars: float | None = None,
        keep_with_next: bool | None = None,
        page_break_before: bool | None = None,
    ) -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            if style and style not in _STYLE_IDS:
                raise ValueError(f"Unsupported paragraph style: {style}")
            if alignment and alignment not in _ALIGNMENT_VALUES:
                raise ValueError(f"Unsupported alignment: {alignment}")
            if line_spacing_rule and line_spacing_rule not in _LINE_SPACING_RULES:
                raise ValueError(f"Unsupported line spacing rule: {line_spacing_rule}")
            if line_spacing_rule in {"at_least", "exactly", "multiple"} and line_spacing_value <= 0:
                raise ValueError(
                    f"line_spacing_value must be positive for {line_spacing_rule}"
                )
            color_value = _hex_to_wd_color(font_color) if font_color else None

            doc, text_range, range_label = _document_range(
                app, target, start_paragraph, end_paragraph
            )
            changes: list[str] = []

            if style:
                text_range.Style = doc.Styles(_STYLE_IDS[style])
                changes.append(f"style={style}")

            font = text_range.Font
            if font_name:
                font.Name = font_name
                try:
                    font.NameFarEast = font_name
                except Exception:
                    pass
                changes.append(f"font={font_name}")
            if font_size > 0:
                font.Size = font_size
                changes.append(f"size={font_size:g}")
            if bold is not None:
                font.Bold = bool(bold)
                changes.append(f"bold={bool(bold)}")
            if italic is not None:
                font.Italic = bool(italic)
                changes.append(f"italic={bool(italic)}")
            if color_value is not None:
                font.Color = color_value
                changes.append(f"color={font_color.upper()}")

            paragraph = text_range.ParagraphFormat
            if alignment:
                paragraph.Alignment = _ALIGNMENT_VALUES[alignment]
                changes.append(f"alignment={alignment}")
            if line_spacing_rule:
                paragraph.LineSpacingRule = _LINE_SPACING_RULES[line_spacing_rule]
                if line_spacing_rule in {"at_least", "exactly"}:
                    paragraph.LineSpacing = float(line_spacing_value)
                elif line_spacing_rule == "multiple":
                    try:
                        paragraph.LineSpacing = app.LinesToPoints(float(line_spacing_value))
                    except Exception:
                        paragraph.LineSpacing = float(line_spacing_value) * 12
                changes.append(
                    f"line_spacing={line_spacing_rule}"
                    + (f"({line_spacing_value:g})" if line_spacing_value > 0 else "")
                )
            if space_before is not None:
                paragraph.SpaceBefore = float(space_before)
                changes.append(f"space_before={space_before:g}")
            if space_after is not None:
                paragraph.SpaceAfter = float(space_after)
                changes.append(f"space_after={space_after:g}")
            if first_line_indent_chars is not None:
                paragraph.CharacterUnitFirstLineIndent = float(first_line_indent_chars)
                changes.append(f"first_line_indent={first_line_indent_chars:g} chars")
            if left_indent_chars is not None:
                paragraph.CharacterUnitLeftIndent = float(left_indent_chars)
                changes.append(f"left_indent={left_indent_chars:g} chars")
            if right_indent_chars is not None:
                paragraph.CharacterUnitRightIndent = float(right_indent_chars)
                changes.append(f"right_indent={right_indent_chars:g} chars")
            if keep_with_next is not None:
                paragraph.KeepWithNext = bool(keep_with_next)
                changes.append(f"keep_with_next={bool(keep_with_next)}")
            if page_break_before is not None:
                paragraph.PageBreakBefore = bool(page_break_before)
                changes.append(f"page_break_before={bool(page_break_before)}")

            if not changes:
                return {"success": False, "error": "No formatting options were provided"}

            return {
                "success": True,
                "summary": f"Formatted {range_label}: {', '.join(changes)}",
                "target": target,
                "range": range_label,
                "range_start": int(text_range.Start),
                "range_end": int(text_range.End),
                "changes": changes,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class ApplyListFormatTool(BaseTool):
    schema = ToolSchema(
        name="apply_list_format",
        description=(
            "为 WPS/Word 的当前选区、正文或指定段落应用项目符号、自动编号，"
            "也可以移除已有列表格式。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "list_type": {
                    "type": "string",
                    "enum": ["bullet", "number", "none"],
                    "description": "项目符号、自动编号或移除列表",
                },
                "target": {
                    "type": "string",
                    "enum": ["selection", "document", "title", "body", "paragraphs"],
                    "description": "要应用列表的范围",
                },
                "start_paragraph": {"type": "integer", "description": "起始段落（从 1 开始）"},
                "end_paragraph": {"type": "integer", "description": "结束段落（含）"},
            },
            "required": ["list_type", "target"],
        },
    )

    async def execute(
        self,
        list_type: str,
        target: str,
        start_paragraph: int = 0,
        end_paragraph: int = 0,
    ) -> dict:
        app = _get_word_app()
        if app is None:
            return {"success": False, "error": _word_com_error()}

        try:
            aliases = {
                "bullet": "bullet",
                "bulleted": "bullet",
                "unordered": "bullet",
                "项目符号": "bullet",
                "number": "number",
                "numbered": "number",
                "numbering": "number",
                "ordered": "number",
                "编号": "number",
                "自动编号": "number",
                "none": "none",
                "remove": "none",
                "clear": "none",
                "移除": "none",
            }
            normalized_type = aliases.get(str(list_type).strip().lower())
            if normalized_type is None:
                raise ValueError("list_type must be bullet, number, or none")
            _, text_range, range_label = _document_range(
                app, target, start_paragraph, end_paragraph
            )
            if normalized_type == "bullet":
                text_range.ListFormat.ApplyBulletDefault()
            elif normalized_type == "number":
                text_range.ListFormat.ApplyNumberDefault()
            else:
                text_range.ListFormat.RemoveNumbers()

            return {
                "success": True,
                "summary": f"Applied list={normalized_type} to {range_label}",
                "target": target,
                "range": range_label,
                "list_type": normalized_type,
                "range_start": int(text_range.Start),
                "range_end": int(text_range.End),
            }
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

        doc = app.ActiveDocument
        errors: list[str] = []
        candidates = _candidate_output_paths(filepath, doc, ".docx")
        for index, output_path in enumerate(candidates):
            try:
                _ensure_parent_directory(output_path)
                _save_docx(doc, output_path)
                _verify_output_file(output_path)
                return {
                    "success": True,
                    "summary": f"Document saved and verified at {output_path}",
                    "filepath": str(output_path),
                    "requested_filepath": filepath,
                    "file_verified": True,
                    "fallback_used": index > 0,
                    "file_size": output_path.stat().st_size,
                }
            except Exception as exc:
                errors.append(f"{output_path}: {exc}")
        return {
            "success": False,
            "error": "Unable to save document. " + " | ".join(errors),
        }


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

        doc = app.ActiveDocument
        full_name = str(getattr(doc, "FullName", "") or "").strip()
        if not filepath and not full_name:
            return {
                "success": False,
                "error": "Document must be saved before exporting PDF",
            }

        errors: list[str] = []
        candidates = _candidate_output_paths(
            filepath, doc, ".pdf", default_from_document=True
        )
        for index, output_path in enumerate(candidates):
            try:
                _ensure_parent_directory(output_path)
                doc.ExportAsFixedFormat(str(output_path), 17)
                _verify_output_file(output_path)
                return {
                    "success": True,
                    "summary": f"PDF exported and verified at {output_path}",
                    "filepath": str(output_path),
                    "requested_filepath": filepath,
                    "file_verified": True,
                    "fallback_used": index > 0,
                    "file_size": output_path.stat().st_size,
                    "source_document": full_name,
                }
            except Exception as exc:
                errors.append(f"{output_path}: {exc}")
        return {
            "success": False,
            "error": "Unable to export PDF. " + " | ".join(errors),
        }


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
