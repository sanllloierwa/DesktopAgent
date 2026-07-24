from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.executor import Executor
from src.agent.loop import AgentLoop
from src.agent.state import AgentState
from src.agent.step_guard import GuardDecision, StepGuard
from src.agent.verifier import Verifier
from src.platforms.zhihu import build_zhihu_task
from src.schemas.task import ActionResult, Step, Task
from src.tools.ai import image_gen
from src.tools.base import BaseTool, ToolRegistry, ToolSchema
from src.tools.browser import navigate
from src.ui.events import EventBus


class FakeImages:
    async def generate(self, **_kwargs):
        item = SimpleNamespace(
            b64_json=base64.b64encode(b"fake-png").decode("ascii"),
            revised_prompt="safe editorial illustration",
            url=None,
        )
        return SimpleNamespace(data=[item])


class FakeKimiClient:
    model = "kimi-k2.5"

    def __init__(self) -> None:
        self.messages = self

    async def create(self, **_kwargs):
        text = """{
          "designs": [
            {
              "headline": "智能体如何行动",
              "subheadline": "从感知到验证的完整闭环",
              "keywords": ["感知", "规划", "执行"],
              "palette": {
                "background": "#EFF6FF",
                "primary": "#172554",
                "accent": "#2563EB",
                "secondary": "#7C3AED"
              }
            }
          ]
        }"""
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


@pytest.mark.asyncio
async def test_generate_image_writes_uploadable_files(monkeypatch, tmp_path: Path) -> None:
    config = SimpleNamespace(image_gen=SimpleNamespace(
        provider="openai",
        model="dall-e-3",
        default_size="1024x1024",
        output_dir=str(tmp_path),
    ))
    monkeypatch.setattr(image_gen, "load_config", lambda: config)
    monkeypatch.setattr(
        image_gen,
        "_create_image_client",
        lambda: SimpleNamespace(images=FakeImages()),
    )

    result = await image_gen.GenerateImageTool().execute(
        topic="Agent 自动化",
        article_text="正文内容",
        count=2,
    )

    assert result["success"] is True
    assert result["count"] == 2
    assert all(Path(path).read_bytes() == b"fake-png" for path in result["image_paths"])


@pytest.mark.asyncio
async def test_generate_image_uses_kimi_plan_and_local_png_renderer(
    monkeypatch, tmp_path: Path
) -> None:
    config = SimpleNamespace(image_gen=SimpleNamespace(
        provider="kimi",
        model="kimi-k2.5",
        default_size="1024x1024",
        output_dir=str(tmp_path),
    ))
    monkeypatch.setattr(image_gen, "load_config", lambda: config)
    monkeypatch.setattr(
        image_gen,
        "create_llm_client",
        lambda provider_override: FakeKimiClient(),
    )

    result = await image_gen.GenerateImageTool().execute(
        topic="桌面智能体",
        article_text="智能体通过感知、规划、执行和验证完成任务。",
        count=2,
    )

    assert result["success"] is True
    assert result["provider"] == "kimi"
    assert result["render_method"] == "kimi_visual_plan+pillow_png"
    assert len(result["image_paths"]) == 2
    assert all(Path(path).read_bytes().startswith(b"\x89PNG") for path in result["image_paths"])


class FakeUploadLocator:
    def __init__(self, count: int = 1, multiple: bool = True) -> None:
        self.first = self
        self.last = self
        self._count = count
        self.multiple = multiple
        self.uploaded: list[str] = []
        self.upload_calls: list[list[str]] = []

    async def count(self) -> int:
        return self._count

    async def set_input_files(self, paths: list[str], timeout: int) -> None:
        self.uploaded.extend(paths)
        self.upload_calls.append(paths)

    async def evaluate(self, _script: str) -> bool:
        return self.multiple


class FakeImageCountLocator:
    def __init__(self, page) -> None:
        self.page = page

    async def count(self) -> int:
        self.page.image_count_calls += 1
        return 1 if self.page.image_count_calls == 1 else 3


class FakeUploadPage:
    def __init__(self) -> None:
        self.file_input = FakeUploadLocator()
        self.image_count_calls = 0
        self.url = "https://zhuanlan.zhihu.com/write"

    def locator(self, selector: str):
        if selector == "img":
            return FakeImageCountLocator(self)
        return self.file_input

    async def evaluate(self, _script):
        return 0

    async def wait_for_timeout(self, _timeout: int) -> None:
        pass


@pytest.mark.asyncio
async def test_upload_image_returns_preview_evidence(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "article.png"
    image_path.write_bytes(b"png")
    page = FakeUploadPage()

    async def fake_get_page():
        return page

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    result = await navigate.UploadImageTool().execute([str(image_path)])

    assert result["success"] is True
    assert result["uploaded_count"] == 1
    assert result["preview_detected"] is True
    assert result["modal_closed"] is True
    assert page.file_input.uploaded == [str(image_path.resolve())]


@pytest.mark.asyncio
async def test_upload_image_sequentially_handles_single_file_input(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"png")
    second.write_bytes(b"png")
    page = FakeUploadPage()
    page.file_input.multiple = False

    async def fake_get_page():
        return page

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    result = await navigate.UploadImageTool().execute([
        str(first),
        str(second),
    ])

    assert result["success"] is True
    assert result["input_supports_multiple"] is False
    assert page.file_input.upload_calls == [
        [str(first.resolve())],
        [str(second.resolve())],
    ]


class FakeImageConfirmPage:
    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, _script):
        self.calls += 1
        if self.calls == 1:
            return {"acted": True, "label": "插入"}
        return {"acted": False}

    async def wait_for_timeout(self, _timeout: int) -> None:
        pass


@pytest.mark.asyncio
async def test_image_upload_confirms_insert_dialog_before_dismissal() -> None:
    actions = await navigate._confirm_image_upload_modals(
        FakeImageConfirmPage()
    )

    assert actions == ["插入"]


class FakeModalPage:
    url = "https://zhuanlan.zhihu.com/write"

    def __init__(self) -> None:
        self.modal_visible = True

    async def evaluate(self, script):
        if "dialogs.sort" in script:
            if not self.modal_visible:
                return {"count": 0, "acted": False}
            self.modal_visible = False
            return {"count": 1, "acted": True, "label": "关闭"}
        return int(self.modal_visible)

    async def wait_for_timeout(self, _timeout: int) -> None:
        pass


@pytest.mark.asyncio
async def test_dismiss_modal_bypasses_backdrop_pointer_interception(
    monkeypatch,
) -> None:
    page = FakeModalPage()

    async def fake_get_page():
        return page

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    result = await navigate.DismissModalTool().execute()

    assert result["success"] is True
    assert result["modal_count_before"] == 1
    assert result["modal_count_after"] == 0
    assert result["modal_actions"] == ["关闭"]


class FakePublishPage:
    url = "https://zhuanlan.zhihu.com/write"

    def __init__(self) -> None:
        self.published = False

    async def wait_for_timeout(self, _timeout: int) -> None:
        pass

    async def evaluate(self, _script):
        return False


class FakePublishButton:
    def __init__(self, page: FakePublishPage) -> None:
        self.page = page

    async def click(self, timeout: int) -> None:
        self.page.published = True
        self.page.url = "https://zhuanlan.zhihu.com/p/123"


@pytest.mark.asyncio
async def test_publish_article_clicks_once_and_waits_for_independent_page(
    monkeypatch,
) -> None:
    page = FakePublishPage()
    tool = navigate.PublishArticleTool()

    async def fake_get_page():
        return page

    async def fake_dismiss(_page):
        return {
            "modal_count_before": 1,
            "modal_count_after": 0,
            "modal_actions": ["关闭"],
            "modal_closed": True,
        }

    async def fake_state(_page, _title):
        return {
            "url": page.url,
            "published": page.published,
            "title_visible": page.published,
            "interactions_ready": page.published,
            "success_link": "",
        }

    async def fake_publish_button(_page):
        return FakePublishButton(page)

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    monkeypatch.setattr(navigate, "_dismiss_visible_modals", fake_dismiss)
    monkeypatch.setattr(tool, "_published_state", fake_state)
    monkeypatch.setattr(tool, "_locate_editor_publish", fake_publish_button)

    result = await tool.execute("测试文章", timeout=1200)

    assert result["success"] is True
    assert result["published"] is True
    assert result["title_visible"] is True
    assert result["page_url"].endswith("/p/123")


@pytest.mark.asyncio
async def test_publish_article_recovers_from_navigation_context_destruction(
    monkeypatch,
) -> None:
    page = FakePublishPage()
    tool = navigate.PublishArticleTool()
    state_calls = 0

    async def fake_get_page():
        return page

    async def fake_dismiss(_page):
        return {
            "modal_count_before": 0,
            "modal_count_after": 0,
            "modal_actions": [],
            "modal_closed": True,
        }

    async def fake_state(_page, _title):
        nonlocal state_calls
        state_calls += 1
        if state_calls == 2:
            raise RuntimeError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        return {
            "url": page.url,
            "published": page.published,
            "title_visible": page.published,
            "interactions_ready": page.published,
            "success_link": "",
        }

    async def fake_publish_button(_page):
        return FakePublishButton(page)

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    monkeypatch.setattr(navigate, "_dismiss_visible_modals", fake_dismiss)
    monkeypatch.setattr(tool, "_published_state", fake_state)
    monkeypatch.setattr(tool, "_locate_editor_publish", fake_publish_button)

    result = await tool.execute("测试文章", timeout=1200)

    assert result["success"] is True
    assert state_calls >= 3
    assert result["page_url"].endswith("/p/123")


class FakeStatePage:
    url = "https://zhuanlan.zhihu.com/p/123"

    async def evaluate(self, _script, _argument):
        return {
            "url": self.url,
            "title": "测试文章 - 知乎",
            "actionStates": [
                {"target": "赞同", "found": True, "selected": True},
                {"target": "收藏", "found": True, "selected": False},
            ],
            "textChecks": [{"text": "测试文章", "found": True}],
            "postedCommentChecks": [],
            "notices": [],
        }


@pytest.mark.asyncio
async def test_page_state_enforces_required_selected_actions(monkeypatch) -> None:
    async def fake_get_page():
        return FakeStatePage()

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    tool = navigate.GetPageStateTool()

    success = await tool.execute(
        text_contains=["测试文章"],
        require_selected=["赞同"],
    )
    failure = await tool.execute(require_selected=["收藏"])

    assert success["success"] is True
    assert success["all_required_selected"] is True
    assert failure["success"] is False
    assert "not selected" in failure["error"]


class FakeCommentEditor:
    def __init__(self) -> None:
        self.value = ""
        self.first = self

    async def fill(self, value: str, timeout: int) -> None:
        self.value = value

    async def evaluate(self, _script):
        return self.value

    async def press(self, _key: str) -> None:
        pass


class FakeCommentSubmit:
    def __init__(self, editor: FakeCommentEditor) -> None:
        self.editor = editor
        self.first = self

    async def click(self, timeout: int) -> None:
        self.editor.value = ""


class FakeCommentPage:
    url = "https://zhuanlan.zhihu.com/p/123"

    async def wait_for_timeout(self, _timeout: int) -> None:
        pass


@pytest.mark.asyncio
async def test_submit_comment_requires_visible_non_editor_evidence(
    monkeypatch,
) -> None:
    page = FakeCommentPage()
    editor = FakeCommentEditor()
    submit = FakeCommentSubmit(editor)
    counts = iter([0, 1])
    tool = navigate.SubmitCommentTool()

    async def fake_get_page():
        return page

    async def fake_locate_editor(_page):
        return editor

    async def fake_locate_submit(_page, _editor):
        return submit

    async def fake_count(_page, _comment):
        return next(counts)

    monkeypatch.setattr(navigate, "_get_page", fake_get_page)
    monkeypatch.setattr(tool, "_locate_comment_editor", fake_locate_editor)
    monkeypatch.setattr(tool, "_locate_comment_submit", fake_locate_submit)
    monkeypatch.setattr(tool, "_posted_comment_count", fake_count)

    result = await tool.execute("这是一条真实评论")

    assert result["success"] is True
    assert result["comment_submitted"] is True
    assert result["comment_visible"] is True
    assert result["editor_cleared"] is True


class GetDOMStub(BaseTool):
    schema = ToolSchema(name="get_dom", description="", parameters={})

    async def execute(self, **_kwargs):
        return {"success": True, "summary": "<button>发布成功"}


class CheckLoginStub(BaseTool):
    schema = ToolSchema(name="check_login_status", description="", parameters={})

    async def execute(self, platform: str):
        return {
            "success": True,
            "summary": f"{platform} logged in",
            "logged_in": True,
            "login_satisfied": True,
        }


class DelayedCheckLoginStub(BaseTool):
    schema = ToolSchema(name="check_login_status", description="", parameters={})

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, platform: str):
        self.calls += 1
        logged_in = self.calls >= 2
        return {
            "success": True,
            "summary": f"{platform} login={logged_in}",
            "logged_in": logged_in,
            "login_satisfied": logged_in,
        }


@pytest.mark.asyncio
async def test_browser_action_refreshes_agent_perception() -> None:
    registry = ToolRegistry()
    registry.register(GetDOMStub())
    loop = object.__new__(AgentLoop)
    loop.state = AgentState()
    loop.executor = Executor(registry)

    result = ActionResult(
        step_id="1",
        success=True,
        data={"page_url": "https://zhuanlan.zhihu.com/p/123"},
    )
    await loop._refresh_perception(Step(tool_name="click"), result)

    assert loop.state.last_dom_summary == "<button>发布成功"
    assert loop.state.last_browser_url.endswith("/p/123")


@pytest.mark.asyncio
async def test_guard_skips_already_selected_zhihu_toggle() -> None:
    state_step = Step(tool_name="get_page_state")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[(
            state_step,
            ActionResult(
            step_id=state_step.id,
            success=True,
            data={"action_states": [{"target": "赞同", "selected": True}]},
            ),
        )]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)

    decision = await guard.before_step(
        Task(goal="在知乎点赞文章"),
        Step(tool_name="click", params={"selector": "赞同"}, description="点击赞同"),
    )

    assert decision.decision == GuardDecision.SKIP


@pytest.mark.asyncio
async def test_guard_blocks_zhihu_interaction_until_comment_is_verified() -> None:
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)
    interaction = Step(
        tool_name="click",
        params={"selector": "喜欢"},
        description="点击喜欢",
    )

    blocked = await guard.before_step(
        Task(goal="在知乎评论这篇文章并喜欢"),
        interaction,
    )

    assert blocked.decision == GuardDecision.REPLAN
    assert "submit_comment" in blocked.reason

    comment_step = Step(tool_name="submit_comment")
    memory.working.completed_steps.append((
        comment_step,
        ActionResult(
            step_id=comment_step.id,
            success=True,
            data={
                "comment_submitted": True,
                "comment_visible": True,
                "editor_cleared": True,
            },
        ),
    ))

    allowed = await guard.before_step(
        Task(goal="在知乎评论这篇文章并喜欢"),
        interaction,
    )

    assert allowed.decision == GuardDecision.CONTINUE


@pytest.mark.asyncio
async def test_guard_replaces_generic_zhihu_publish_click() -> None:
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)

    decision = await guard.before_step(
        Task(goal="在知乎写文章并发布"),
        Step(
            tool_name="click",
            params={"selector": "发布"},
            description="点击发布文章",
        ),
    )

    assert decision.decision == GuardDecision.REPLAN
    assert "publish_article" in decision.reason


@pytest.mark.asyncio
async def test_guard_skips_redundant_editor_entry_when_already_editing() -> None:
    navigation = Step(tool_name="navigate")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[(
            navigation,
            ActionResult(
                step_id=navigation.id,
                success=True,
                data={"url": "https://zhuanlan.zhihu.com/write"},
            ),
        )]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)
    task = Task(goal="在知乎写文章并发布")

    write_click = await guard.before_step(
        task,
        Step(tool_name="click", description="点击写文章选项进入编辑器"),
    )
    home_navigation = await guard.before_step(
        task,
        Step(
            tool_name="navigate",
            params={"url": "https://www.zhihu.com"},
            description="返回知乎首页",
        ),
    )
    stale_menu_dom = await guard.before_step(
        task,
        Step(tool_name="get_dom", description="获取下拉菜单DOM确认写文章选项"),
    )
    editor_dom = await guard.before_step(
        task,
        Step(tool_name="get_dom", description="获取编辑器标题和正文DOM"),
    )

    assert write_click.decision == GuardDecision.SKIP
    assert home_navigation.decision == GuardDecision.SKIP
    assert stale_menu_dom.decision == GuardDecision.SKIP
    assert editor_dom.decision == GuardDecision.CONTINUE


@pytest.mark.asyncio
async def test_guard_skips_invalid_editor_body_text_verification() -> None:
    navigation = Step(tool_name="navigate")
    title_input = Step(tool_name="type_text")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[
            (
                navigation,
                ActionResult(
                    step_id=navigation.id,
                    success=True,
                    data={"url": "https://zhuanlan.zhihu.com/write"},
                ),
            ),
            (
                title_input,
                ActionResult(
                    step_id=title_input.id,
                    success=True,
                    data={"field_kind": "title", "value_matches": True},
                ),
            ),
        ]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)

    decision = await guard.before_step(
        Task(goal="在知乎写文章并发布"),
        Step(
            tool_name="get_page_state",
            params={"text_contains": ["{{generate_article.title}}"]},
            description="验证编辑器中的文章标题",
        ),
    )

    assert decision.decision == GuardDecision.SKIP
    assert "body.innerText" in decision.reason


@pytest.mark.asyncio
async def test_guard_skips_unrequested_optional_zhihu_topic_step() -> None:
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)
    topic_step = Step(
        tool_name="click",
        params={"selector": "添加话题"},
        description="点击添加话题按钮",
    )

    skipped = await guard.before_step(
        Task(goal="在知乎发布文章后搜索、评论、点赞和收藏"),
        topic_step,
    )
    required = await guard.before_step(
        Task(goal="在知乎发布文章并添加话题标签"),
        topic_step,
    )

    assert skipped.decision == GuardDecision.SKIP
    assert "可选项" in skipped.reason
    assert required.decision == GuardDecision.CONTINUE


@pytest.mark.asyncio
async def test_guard_uses_verified_article_page_to_skip_duplicate_publish() -> None:
    state_step = Step(tool_name="get_page_state")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[(
            state_step,
            ActionResult(
                step_id=state_step.id,
                success=True,
                data={
                    "page_url": "https://zhuanlan.zhihu.com/p/123",
                    "all_text_found": True,
                    "text_checks": [{"text": "测试文章", "found": True}],
                },
            ),
        )]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)

    decision = await guard.before_step(
        Task(goal="在知乎发布文章"),
        Step(tool_name="publish_article", params={"title": "测试文章"}),
    )

    assert decision.decision == GuardDecision.SKIP
    assert "重复发布" in decision.reason


@pytest.mark.asyncio
async def test_guard_skips_editor_config_after_publication() -> None:
    published = Step(tool_name="publish_article")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[(
            published,
            ActionResult(
                step_id=published.id,
                success=True,
                data={
                    "published": True,
                    "title_visible": True,
                    "page_url": "https://zhuanlan.zhihu.com/p/123",
                },
            ),
        )]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)

    decision = await guard.before_step(
        Task(goal="在知乎发布文章后点赞"),
        Step(
            tool_name="click",
            params={"selector": "同意"},
            description="如有协议复选框则点击同意",
        ),
    )

    assert decision.decision == GuardDecision.SKIP
    assert "继续搜索和互动" in decision.reason


@pytest.mark.asyncio
async def test_guard_does_not_request_image_path_after_generation() -> None:
    generated = Step(tool_name="generate_image")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[(
            generated,
            ActionResult(
                step_id=generated.id,
                success=True,
                data={"image_paths": ["C:\\generated\\one.png"]},
            ),
        )]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)
    request = Step(
        tool_name="request_user_input",
        params={"prompt": "请提供图片的完整绝对路径"},
        description="请求用户提供本地图片路径",
    )

    decision = await guard.before_step(
        Task(goal="在知乎写文章并自动生成配图"),
        request,
    )

    assert decision.decision == GuardDecision.REPLAN
    assert "{{generate_image.image_paths}}" in decision.reason


@pytest.mark.asyncio
async def test_manual_login_confirmation_triggers_fresh_login_check() -> None:
    registry = ToolRegistry()
    registry.register(CheckLoginStub())
    loop = object.__new__(AgentLoop)
    loop.state = AgentState()
    loop.executor = Executor(registry)
    completed: list[tuple[Step, ActionResult]] = []
    loop.memory = SimpleNamespace(commit=lambda step, result: completed.append((step, result)))
    loop.events = EventBus()
    confirmation_step = Step(
        tool_name="request_user_input",
        params={"prompt": "请手动完成知乎登录后回复已登录"},
        description="等待用户完成知乎登录",
    )
    confirmation_result = ActionResult(
        step_id=confirmation_step.id,
        success=True,
        data={"confirmation": "yes", "user_input": "已登录"},
    )

    checked = await loop._verify_manual_login_confirmation(
        Task(goal="登录知乎后发布文章"),
        confirmation_step,
        confirmation_result,
    )

    assert checked is not None
    assert checked.data["logged_in"] is True
    assert completed[-1][0].tool_name == "check_login_status"


@pytest.mark.asyncio
async def test_manual_login_confirmation_waits_for_page_state_to_settle(
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.agent.loop.asyncio.sleep", lambda _delay: _async_noop())
    login = DelayedCheckLoginStub()
    registry = ToolRegistry()
    registry.register(login)
    loop = object.__new__(AgentLoop)
    loop.state = AgentState()
    loop.executor = Executor(registry)
    loop.memory = SimpleNamespace(commit=lambda _step, _result: None)
    loop.events = EventBus()
    step = Step(
        tool_name="request_user_input",
        params={"prompt": "完成知乎登录后回复已登录"},
        description="确认知乎登录",
    )
    result = ActionResult(
        step_id=step.id,
        success=True,
        data={"confirmation": "yes"},
    )

    checked = await loop._verify_manual_login_confirmation(
        Task(goal="登录知乎后发布文章"),
        step,
        result,
    )

    assert checked is not None
    assert checked.data["logged_in"] is True
    assert login.calls == 2


async def _async_noop() -> None:
    return None


def test_sms_guard_recognizes_get_sms_verification_code_wording() -> None:
    click = Step(tool_name="click", description="点击获取短信验证码按钮")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[(
            click,
            ActionResult(step_id=click.id, success=True, summary="Clicked"),
        )]),
    )
    guard = StepGuard(Executor(ToolRegistry()), memory)

    assert guard._sms_code_was_sent() is True


def test_zhihu_quick_task_contains_full_requested_flow() -> None:
    task = build_zhihu_task("智能体")

    for term in ("登录", "配图", "搜索", "评论", "赞同", "收藏", "喜欢"):
        assert term in task.goal


def _completed(tool_name: str, description: str = "", data: dict | None = None):
    step = Step(tool_name=tool_name, description=description)
    return step, ActionResult(step_id=step.id, success=True, data=data or {})


def test_zhihu_final_audit_requires_structured_end_state() -> None:
    verifier = object.__new__(Verifier)
    task = build_zhihu_task("智能体")
    completed = [
        _completed("check_login_status", data={"logged_in": True}),
        _completed("generate_article"),
        _completed("generate_image"),
        _completed(
            "upload_image",
            data={
                "uploaded_count": 2,
                "preview_detected": True,
                "modal_closed": True,
            },
        ),
        _completed(
            "publish_article",
            "发布并验证文章",
            {
                "published": True,
                "title_visible": True,
                "page_url": "https://zhuanlan.zhihu.com/p/123",
            },
        ),
        _completed("type_text", "在知乎站内搜索文章标题"),
        _completed(
            "submit_comment",
            "提交并验证评论",
            {
                "comment_submitted": True,
                "comment_visible": True,
                "editor_cleared": True,
            },
        ),
        _completed(
            "get_page_state",
            "验证互动状态",
            {
                "action_states": [
                    {"target": "赞同", "selected": True},
                    {"target": "收藏", "selected": True},
                    {"target": "喜欢", "selected": True},
                ],
            },
        ),
    ]

    success, reason = verifier._deterministic_zhihu_completion(task, completed)

    assert success is True
    assert "结构化证据" in reason

    without_upload = [item for item in completed if item[0].tool_name != "upload_image"]
    success, reason = verifier._deterministic_zhihu_completion(task, without_upload)

    assert success is False
    assert "上传配图" in reason


def test_zhihu_final_audit_rejects_comment_draft_as_published_comment() -> None:
    verifier = object.__new__(Verifier)
    task = Task(goal="在知乎评论文章")
    legacy_false_positive = [
        _completed("type_text", "在评论框输入评论内容"),
        _completed("click", "点击提交评论"),
        _completed(
            "get_page_state",
            "验证评论内容",
            {
                "all_text_found": True,
                "text_checks": [{"text": "仍在编辑器里的评论", "found": True}],
            },
        ),
    ]

    success, reason = verifier._deterministic_zhihu_completion(
        task,
        legacy_false_positive,
    )

    assert success is False
    assert "评论提交" in reason
    assert "评论内容可见性验证" in reason
