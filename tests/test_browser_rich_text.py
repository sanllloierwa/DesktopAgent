from __future__ import annotations

from src.tools.browser import navigate


class FakeLocator:
    def __init__(self) -> None:
        self.first = self
        self.clicks = 0
        self.fills: list[str] = []
        self.typed: list[tuple[str, int]] = []

    async def click(self, timeout: int) -> None:
        self.clicks += 1

    async def fill(self, value: str, timeout: int = 0) -> None:
        self.fills.append(value)

    async def type(self, value: str, delay: int) -> None:
        self.typed.append((value, delay))

    async def evaluate(self, _script: str) -> str:
        return self.fills[-1]

    def nth(self, _index: int):
        return self


class FakeRichTextPage:
    def __init__(self) -> None:
        self.rich_text = FakeLocator()
        self.evaluate_script = ""
        self.evaluate_argument = None
        self.locator_selector = ""

    async def evaluate(self, script: str, argument):
        self.evaluate_script = script
        self.evaluate_argument = argument
        return {"ok": True, "index": 0}

    def locator(self, selector: str) -> FakeLocator:
        self.locator_selector = selector
        return self.rich_text


async def test_type_text_supports_contenteditable_editor(monkeypatch) -> None:
    page = FakeRichTextPage()

    async def fake_get_page():
        return page

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)

    result = await navigate.TypeTextTool().execute(
        selector="文章正文",
        text="这是知乎文章正文",
        strategy="text",
        delay=0,
    )

    assert result["success"] is True
    assert result["value_matches"] is True
    assert '[contenteditable="true"]' in page.evaluate_script
    assert page.evaluate_argument == {"selector": "文章正文"}
    assert page.locator_selector == (
        'input, textarea, [contenteditable="true"], [role="textbox"]'
    )
    assert page.rich_text.fills == ["这是知乎文章正文"]
    assert page.rich_text.typed == []
    assert page.rich_text.clicks == 0


class FakeLoggedInPage:
    async def evaluate(self, script: str, _platform: str):
        assert ".AppHeader-profileEntry" in script
        assert '[aria-label*="私信"]' in script
        return {
            "loggedIn": True,
            "reason": "Detected user controls",
            "clickable": [],
            "href": "https://www.zhihu.com/",
        }


async def test_login_status_only_satisfies_login_subflow(monkeypatch) -> None:
    async def fake_get_page():
        return FakeLoggedInPage()

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)

    result = await navigate.CheckLoginStatusTool().execute("zhihu")

    assert result["success"] is True
    assert result["logged_in"] is True
    assert result["login_satisfied"] is True
    assert "task_complete" not in result


class SemanticField:
    def __init__(self, value: str = "") -> None:
        self.first = self
        self.value = value

    async def fill(self, value: str, timeout: int = 0) -> None:
        self.value = value

    async def evaluate(self, _script: str) -> str:
        return self.value


class SemanticCollection:
    def __init__(self, fields: list[SemanticField]) -> None:
        self.fields = fields
        self.first = fields[0]

    def nth(self, index: int) -> SemanticField:
        return self.fields[index]

    async def count(self) -> int:
        return len(self.fields)


class FakeArticleEditorPage:
    def __init__(self) -> None:
        self.title = SemanticField("正确标题")
        self.body = SemanticField()

    async def evaluate(self, script: str):
        if "titleCandidates" in script:
            return {
                "title_length": len(self.title.value),
                "body_length": len(self.body.value),
                "title_preview": self.title.value[:100],
                "title_value_normalized": self.title.value.strip(),
            }
        if "score = rect.width * rect.height" in script:
            return {"index": 1, "visible": True, "score": 1000}
        raise AssertionError("unexpected script")

    def locator(self, _selector: str) -> SemanticCollection:
        return SemanticCollection([self.title, self.body])


async def test_long_text_automatically_targets_article_body(monkeypatch) -> None:
    page = FakeArticleEditorPage()

    async def fake_get_page():
        return page

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    body = "正文段落。" * 80

    result = await navigate.TypeTextTool().execute(
        selector="textbox",
        text=body,
        strategy="role",
    )

    assert result["success"] is True
    assert result["field_kind"] == "body"
    assert page.title.value == "正确标题"
    assert page.body.value == body


async def test_body_write_rejects_previously_corrupted_title(monkeypatch) -> None:
    page = FakeArticleEditorPage()
    page.title.value = "错误正文" * 100

    async def fake_get_page():
        return page

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)

    result = await navigate.TypeTextTool().execute(
        selector="正文",
        text="新的正文" * 80,
        strategy="text",
        field_kind="body",
    )

    assert result["success"] is False
    assert result["title_looks_like_body"] is True
    assert "body-sized content" in result["error"]
