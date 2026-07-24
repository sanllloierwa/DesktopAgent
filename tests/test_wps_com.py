from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tools.desktop import wps_com


def test_wps_com_is_preferred_over_word() -> None:
    assert wps_com._WORD_APP_PROG_IDS == ("KWPS.Application", "Word.Application")


class FakeStyles:
    def __init__(self) -> None:
        self.requested: list[int] = []

    def __call__(self, style_id: int) -> str:
        self.requested.append(style_id)
        return f"style:{style_id}"


class FakeSelection:
    def __init__(self) -> None:
        self.Style = None
        self.typed: list[str] = []
        self.paragraphs = 0

    def TypeText(self, text: str) -> None:
        self.typed.append(text)

    def TypeParagraph(self) -> None:
        self.paragraphs += 1


class FakeListFormat:
    def __init__(self) -> None:
        self.action = ""

    def ApplyBulletDefault(self) -> None:
        self.action = "bullet"

    def ApplyNumberDefault(self) -> None:
        self.action = "number"

    def RemoveNumbers(self) -> None:
        self.action = "none"


class FakeRange:
    def __init__(self, start: int, end: int) -> None:
        self.Start = start
        self.End = end
        self.Style = None
        self.Font = SimpleNamespace(
            Name="",
            NameFarEast="",
            Size=0,
            Bold=None,
            Italic=None,
            Color=None,
        )
        self.ParagraphFormat = SimpleNamespace(
            Alignment=None,
            LineSpacingRule=None,
            LineSpacing=None,
            SpaceBefore=None,
            SpaceAfter=None,
            CharacterUnitFirstLineIndent=None,
            CharacterUnitLeftIndent=None,
            CharacterUnitRightIndent=None,
            KeepWithNext=None,
            PageBreakBefore=None,
        )
        self.ListFormat = FakeListFormat()


class FakeParagraphs:
    def __init__(self, ranges: list[FakeRange]) -> None:
        self._ranges = ranges
        self.Count = len(ranges)

    def __call__(self, index: int) -> SimpleNamespace:
        return SimpleNamespace(Range=self._ranges[index - 1])


class FakeDocument:
    def __init__(self) -> None:
        self.Styles = FakeStyles()
        self.paragraph_ranges = [
            FakeRange(0, 8),
            FakeRange(8, 30),
            FakeRange(30, 55),
        ]
        self.Paragraphs = FakeParagraphs(self.paragraph_ranges)
        self.Content = FakeRange(0, 55)
        self.resolved_ranges: list[FakeRange] = []

    def Range(self, start: int, end: int) -> FakeRange:
        result = FakeRange(start, end)
        self.resolved_ranges.append(result)
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_style", "expected_id"),
    [("body", -1), ("heading", -2)],
)
async def test_write_text_uses_language_independent_builtin_style(
    monkeypatch, requested_style: str, expected_id: int
) -> None:
    styles = FakeStyles()
    selection = FakeSelection()
    app = SimpleNamespace(
        ActiveDocument=SimpleNamespace(Styles=styles),
        Selection=selection,
    )
    monkeypatch.setattr(wps_com, "_get_word_app", lambda: app)

    result = await wps_com.WriteTextTool().execute("大语言模型", style=requested_style)

    assert result["success"] is True
    assert styles.requested == [expected_id]
    assert selection.Style == f"style:{expected_id}"
    assert selection.typed == ["大语言模型"]
    assert selection.paragraphs == 1


@pytest.mark.asyncio
async def test_write_text_supports_title_style(monkeypatch) -> None:
    styles = FakeStyles()
    selection = FakeSelection()
    app = SimpleNamespace(
        ActiveDocument=SimpleNamespace(Styles=styles),
        Selection=selection,
    )
    monkeypatch.setattr(wps_com, "_get_word_app", lambda: app)

    result = await wps_com.WriteTextTool().execute("文章标题", style="title")

    assert result["success"] is True
    assert styles.requested == [-63]
    assert selection.Style == "style:-63"


@pytest.mark.asyncio
async def test_format_document_range_formats_body_without_selection(monkeypatch) -> None:
    doc = FakeDocument()
    app = SimpleNamespace(
        ActiveDocument=doc,
        Selection=SimpleNamespace(Range=FakeRange(999, 1000)),
    )
    monkeypatch.setattr(wps_com, "_get_word_app", lambda: app)

    result = await wps_com.FormatDocumentRangeTool().execute(
        target="body",
        style="body",
        font_name="宋体",
        font_size=12,
        bold=False,
        alignment="justify",
        line_spacing_rule="one_and_half",
        space_after=6,
        first_line_indent_chars=2,
    )

    assert result["success"] is True
    assert result["range_start"] == 8
    assert result["range_end"] == 55
    formatted = doc.resolved_ranges[-1]
    assert formatted.Style == "style:-1"
    assert formatted.Font.Name == "宋体"
    assert formatted.Font.NameFarEast == "宋体"
    assert formatted.Font.Size == 12
    assert formatted.Font.Bold is False
    assert formatted.ParagraphFormat.Alignment == 3
    assert formatted.ParagraphFormat.LineSpacingRule == 1
    assert formatted.ParagraphFormat.SpaceAfter == 6
    assert formatted.ParagraphFormat.CharacterUnitFirstLineIndent == 2


@pytest.mark.asyncio
async def test_format_document_range_formats_title_color_and_spacing(monkeypatch) -> None:
    doc = FakeDocument()
    app = SimpleNamespace(
        ActiveDocument=doc,
        Selection=SimpleNamespace(Range=FakeRange(999, 1000)),
        LinesToPoints=lambda value: value * 12,
    )
    monkeypatch.setattr(wps_com, "_get_word_app", lambda: app)

    result = await wps_com.FormatDocumentRangeTool().execute(
        target="title",
        style="title",
        font_name="微软雅黑",
        font_size=22,
        bold=True,
        font_color="#1F4E79",
        alignment="center",
        line_spacing_rule="multiple",
        line_spacing_value=1.25,
        keep_with_next=True,
    )

    assert result["success"] is True
    formatted = doc.resolved_ranges[-1]
    assert (formatted.Start, formatted.End) == (0, 8)
    assert formatted.Style == "style:-63"
    assert formatted.Font.Color == 0x794E1F
    assert formatted.ParagraphFormat.Alignment == 1
    assert formatted.ParagraphFormat.LineSpacingRule == 5
    assert formatted.ParagraphFormat.LineSpacing == 15
    assert formatted.ParagraphFormat.KeepWithNext is True


@pytest.mark.asyncio
async def test_apply_numbered_list_to_specific_paragraphs(monkeypatch) -> None:
    doc = FakeDocument()
    app = SimpleNamespace(
        ActiveDocument=doc,
        Selection=SimpleNamespace(Range=FakeRange(999, 1000)),
    )
    monkeypatch.setattr(wps_com, "_get_word_app", lambda: app)

    result = await wps_com.ApplyListFormatTool().execute(
        list_type="number",
        target="paragraphs",
        start_paragraph=2,
        end_paragraph=3,
    )

    assert result["success"] is True
    formatted = doc.resolved_ranges[-1]
    assert (formatted.Start, formatted.End) == (8, 55)
    assert formatted.ListFormat.action == "number"


@pytest.mark.asyncio
async def test_format_document_range_rejects_invalid_paragraph_range(monkeypatch) -> None:
    doc = FakeDocument()
    app = SimpleNamespace(
        ActiveDocument=doc,
        Selection=SimpleNamespace(Range=FakeRange(999, 1000)),
    )
    monkeypatch.setattr(wps_com, "_get_word_app", lambda: app)

    result = await wps_com.FormatDocumentRangeTool().execute(
        target="paragraphs",
        start_paragraph=2,
        end_paragraph=5,
        bold=True,
    )

    assert result["success"] is False
    assert "outside the document" in result["error"]
