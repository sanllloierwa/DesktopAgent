from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tools.desktop import wps_com


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
