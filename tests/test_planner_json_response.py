from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.planner import Planner, _parse_json_object
from src.schemas.task import Step, Task
from src.tools.base import ToolRegistry


def test_parse_planner_json_object() -> None:
    assert _parse_json_object('{"steps": []}') == {"steps": []}


def test_parse_planner_json_from_markdown_or_prefix() -> None:
    fenced = '```json\n{"steps": [], "fallback": "x"}\n```'
    prefixed = '规划如下：\n{"steps": []}\n请执行。'

    assert _parse_json_object(fenced)["fallback"] == "x"
    assert _parse_json_object(prefixed) == {"steps": []}


def test_parse_planner_empty_response_has_actionable_error() -> None:
    with pytest.raises(ValueError, match="空内容"):
        _parse_json_object("  \n")


def test_parse_planner_incomplete_json_has_actionable_error() -> None:
    with pytest.raises(ValueError, match="不完整或格式错误"):
        _parse_json_object('{"steps": [')


def test_step_parser_reports_missing_tool_name_without_key_error() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())

    steps, fallback = planner._parse_steps({
        "steps": [{"description": "启动微信客户端"}],
    })

    assert steps == []
    assert fallback.startswith("[PLAN_SCHEMA]")
    assert "缺少 tool_name" in fallback


def test_step_parser_accepts_common_tool_and_arguments_aliases() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())

    steps, fallback = planner._parse_steps({
        "steps": [{
            "tool": "focus_window",
            "arguments": {"app_name": "wechat"},
            "description": "聚焦微信",
        }],
    })

    assert fallback == ""
    assert len(steps) == 1
    assert steps[0].tool_name == "focus_window"
    assert steps[0].params == {"app_name": "wechat"}


def test_wechat_plan_uses_one_targeted_keyboard_search_session() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    task = Task(goal="微信搜索火眼审阅服务号，关注后发送私信")
    steps = [
        Step(tool_name="launch_app", params={"app_name": "wechat"}),
        Step(
            tool_name="request_user_input",
            description="如需登录请扫码，完成后回复已登录",
        ),
        Step(tool_name="desktop_screenshot", description="截取微信主界面"),
        Step(
            tool_name="analyze_screen",
            params={
                "image_base64": "{{desktop_screenshot.screenshot_base64}}",
                "question": "分析微信登录状态",
            },
            description="分析微信登录状态",
        ),
        Step(
            tool_name="desktop_screenshot",
            description="截取界面获取搜索框视觉坐标",
        ),
        Step(
            tool_name="locate_screen_element",
            description="定位顶部搜索图标",
        ),
        Step(
            tool_name="desktop_click",
            params={"x": 100, "y": 80},
            description="点击搜索图标",
        ),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "ctrl+a"},
            description="清空搜索框",
        ),
        Step(
            tool_name="desktop_type_text",
            params={"text": "火眼审阅"},
            description="输入搜索关键词",
        ),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "enter"},
            description="执行搜索",
        ),
        Step(
            tool_name="desktop_screenshot",
            description="截取搜索结果",
        ),
        Step(
            tool_name="locate_screen_element",
            description="定位火眼审阅服务号结果",
        ),
        Step(tool_name="launch_app", params={"app_name": "wechat"}),
        Step(tool_name="close_app", params={"app_name": "wechat"}),
    ]

    normalized = planner._normalize_platform_steps(task, steps)

    assert sum(step.tool_name == "launch_app" for step in normalized) == 1
    assert not any(step.tool_name == "close_app" for step in normalized)
    assert not any(step.tool_name == "request_user_input" for step in normalized)
    assert any(
        step.tool_name == "check_wechat_login_status"
        for step in normalized
    )
    assert not any(
        step.tool_name == "analyze_screen"
        and "登录状态" in step.description
        for step in normalized
    )
    assert not any(
        "搜索图标" in step.description or "搜索框视觉坐标" in step.description
        for step in normalized
    )
    assert any(
        step.tool_name == "locate_screen_element"
        and "服务号结果" in step.description
        for step in normalized
    )
    key_sequence = [
        step.params.get("keys")
        for step in normalized
        if step.tool_name == "desktop_keypress"
    ]
    assert key_sequence == ["ctrl+f", "ctrl+a", "enter"]
    for step in normalized:
        if step.tool_name in {
            "desktop_screenshot",
            "desktop_keypress",
            "desktop_type_text",
            "desktop_click",
            "focus_window",
        }:
            assert step.params["app_name"] == "wechat"


def test_confirmed_wechat_login_prompt_is_not_dropped() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    prompt = Step(
        tool_name="request_user_input",
        params={"prompt": "请完成微信扫码登录后回复已登录"},
        description="[WECHAT_LOGIN_REQUIRED] 等待用户完成微信扫码登录",
    )

    normalized = planner._normalize_platform_steps(
        Task(goal="微信搜索火眼审阅并发送私信"),
        [prompt],
    )

    assert normalized == [prompt]


def test_wechat_partial_recognition_plan_is_rejected_before_execution() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    task = Task(
        goal="微信搜索“火眼审阅”，选择服务号、关注并发送一段文字私信"
    )
    partial = [
        Step(
            tool_name="desktop_type_text",
            params={"text": "火眼审阅", "app_name": "wechat"},
            description="输入搜索关键词",
        ),
        Step(
            tool_name="analyze_screen",
            description="识别服务号结果",
        ),
    ]

    violation = planner._goal_violation(task, partial)

    assert "定位类型为服务号" in violation


def test_wechat_complete_interaction_plan_passes_goal_check() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    task = Task(
        goal="微信搜索“火眼审阅”，选择服务号、关注并发送一段文字私信"
    )
    complete = [
        Step(
            tool_name="locate_screen_element",
            params={"target": "火眼审阅服务号"},
            description="定位火眼审阅服务号",
        ),
        Step(
            tool_name="desktop_click",
            description="点击服务号搜索结果",
        ),
        Step(
            tool_name="desktop_click",
            description="点击关注服务号",
        ),
        Step(
            tool_name="desktop_type_text",
            params={"text": "测试私信", "app_name": "wechat"},
            description="输入私信正文",
        ),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "enter", "app_name": "wechat"},
            description="发送私信",
        ),
    ]

    assert planner._goal_violation(task, complete) == ""


@pytest.mark.asyncio
async def test_wechat_login_screen_uses_deterministic_resume_plan() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    failed = Step(
        tool_name="locate_screen_element",
        description='定位"火眼审阅"服务号结果的具体坐标',
    )

    steps, fallback, preserve = await planner.replan(
        Task(
            goal=(
                "微信（客户端）===> 打开客户端 -> 搜索“火眼审阅” -> "
                "选择类型为服务号的结果 -> 关注并发送私信"
            )
        ),
        failed,
        (
            "微信客户端尚未登录，始终停留在扫码登录界面，"
            "无法执行搜索、关注服务号或发送私信"
        ),
        remaining_steps=[Step(tool_name="desktop_click")],
    )

    assert fallback == ""
    assert preserve is True
    assert len(steps) == 10
    assert steps[0].tool_name == "request_user_input"
    assert "[WECHAT_LOGIN_REQUIRED]" in steps[0].description
    assert any(
        step.tool_name == "desktop_type_text"
        and step.params["text"] == "火眼审阅"
        for step in steps
    )
    assert steps[-1].tool_name == "locate_screen_element"
    assert "服务号" in steps[-1].params["target"]


@pytest.mark.asyncio
async def test_wechat_login_guard_resumes_original_search_without_duplication() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    failed = Step(
        tool_name="desktop_keypress",
        params={"keys": "ctrl+f", "app_name": "wechat"},
        description="打开微信搜索",
    )

    steps, fallback, preserve = await planner.replan(
        Task(
            goal="微信搜索“火眼审阅”，选择服务号、关注并发送一段文字私信"
        ),
        failed,
        "[WECHAT_LOGIN_REQUIRED] 当前未登录",
        remaining_steps=[
            Step(
                tool_name="desktop_keypress",
                params={"keys": "ctrl+a", "app_name": "wechat"},
            ),
            Step(
                tool_name="desktop_type_text",
                params={"text": "火眼审阅", "app_name": "wechat"},
            ),
        ],
    )

    assert fallback == ""
    assert preserve is True
    assert len(steps) == 5
    assert [step.tool_name for step in steps] == [
        "request_user_input",
        "focus_window",
        "desktop_screenshot",
        "check_wechat_login_status",
        "desktop_keypress",
    ]
    assert steps[-1].params["keys"] == "ctrl+f"


def test_wechat_candidate_locate_and_click_are_collapsed_atomically() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    task = Task(
        goal="微信搜索“火眼审阅”，选择服务号、关注并发送一段文字私信"
    )
    steps = [
        Step(
            tool_name="locate_screen_element",
            params={
                "image_base64": "{{desktop_screenshot.screenshot_base64}}",
                "image_width": "{{desktop_screenshot.width}}",
                "image_height": "{{desktop_screenshot.height}}",
            },
            description="定位火眼审阅服务号搜索结果",
        ),
        Step(
            tool_name="desktop_click",
            params={
                "x": "{{locate_screen_element.x}}",
                "y": "{{locate_screen_element.y}}",
            },
            description="点击服务号搜索结果",
        ),
        Step(
            tool_name="desktop_click",
            description="点击关注服务号",
        ),
        Step(
            tool_name="desktop_type_text",
            description="输入私信正文",
        ),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "enter"},
            description="发送私信",
        ),
    ]

    normalized = planner._normalize_platform_steps(task, steps)

    assert normalized[0].tool_name == "open_wechat_search_candidate"
    assert normalized[0].params["target_name"] == "火眼审阅"
    assert normalized[0].params["expected_type"] == "service_account"
    assert not any(
        step.tool_name == "locate_screen_element"
        and "搜索结果" in step.description
        for step in normalized
    )


@pytest.mark.asyncio
async def test_wechat_suggestion_layer_relaxes_type_label_requirement() -> None:
    planner = Planner(SimpleNamespace(model="test"), ToolRegistry())
    failed = Step(
        tool_name="locate_screen_element",
        description="重新定位火眼审阅服务号结果",
    )

    steps, fallback, preserve = await planner.replan(
        Task(
            goal="微信搜索“火眼审阅”，选择服务号、关注并发送一段文字私信"
        ),
        failed,
        (
            "Target not found: 当前显示的是搜索建议下拉列表，仅显示"
            "火眼审阅相关搜索关键词，未显示服务号类型标识"
        ),
        remaining_steps=[
            Step(tool_name="desktop_click", description="点击搜索结果"),
            Step(tool_name="desktop_click", description="点击关注服务号"),
            Step(
                tool_name="desktop_type_text",
                description="输入私信正文",
            ),
            Step(
                tool_name="desktop_keypress",
                params={"keys": "enter"},
                description="发送私信",
            ),
        ],
    )

    assert fallback == ""
    assert preserve is True
    assert len(steps) == 1
    assert steps[0].tool_name == "locate_screen_element"
    assert "不要求候选项同时显示服务号标签" in steps[0].params["target"]


class PreservedRecoveryLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.messages = self

    async def create(self, **_kwargs):
        return SimpleNamespace(content=[SimpleNamespace(text=(
            '{"steps": [{"tool_name": "locate_screen_element", '
            '"params": {"target": "火眼审阅服务号"}, '
            '"description": "定位火眼审阅服务号"}], '
            '"preserve_remaining": true}'
        ))])


@pytest.mark.asyncio
async def test_replan_goal_check_includes_preserved_remaining_steps() -> None:
    planner = Planner(PreservedRecoveryLLM(), ToolRegistry())
    remaining = [
        Step(tool_name="desktop_click", description="点击服务号结果"),
        Step(tool_name="desktop_click", description="点击关注服务号"),
        Step(tool_name="desktop_type_text", description="输入私信正文"),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "enter"},
            description="发送私信",
        ),
    ]

    steps, fallback, preserve = await planner.replan(
        Task(
            goal="微信搜索“火眼审阅”，选择服务号、关注并发送一段文字私信"
        ),
        Step(tool_name="analyze_screen", description="恢复搜索结果"),
        "需要重新定位目标",
        remaining_steps=remaining,
    )

    assert fallback == ""
    assert preserve is True
    assert len(steps) == 1


class ContextCapturingLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.messages = self
        self.prompt = ""
        self.max_tokens = 0

    async def create(self, **kwargs):
        self.prompt = kwargs["messages"][0]["content"]
        self.max_tokens = kwargs["max_tokens"]
        return SimpleNamespace(content=[SimpleNamespace(text='{"steps": []}')])


class SchemaRepairLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.messages = self
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        text = (
            '{"steps": [{"description": "缺少工具字段"}]}'
            if self.calls == 1
            else (
                '{"steps": [{"tool_name": "focus_window", '
                '"params": {"app_name": "wechat"}}]}'
            )
        )
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


@pytest.mark.asyncio
async def test_planner_repairs_invalid_step_schema_once() -> None:
    from src.tools.desktop.native_control import FocusWindowTool

    llm = SchemaRepairLLM()
    registry = ToolRegistry()
    registry.register(FocusWindowTool())
    planner = Planner(llm, registry)

    steps, fallback = await planner.plan(Task(goal="聚焦桌面应用"))

    assert fallback == ""
    assert llm.calls == 2
    assert len(steps) == 1
    assert steps[0].tool_name == "focus_window"


@pytest.mark.asyncio
async def test_planner_includes_structured_task_context() -> None:
    llm = ContextCapturingLLM()
    planner = Planner(llm, ToolRegistry())

    await planner.plan(Task(
        goal="在微信发送消息",
        context={"platform": "wechat", "account": "火眼审阅"},
    ))

    assert "平台/任务结构化上下文" in llm.prompt
    assert '"platform": "wechat"' in llm.prompt
    assert '"account": "火眼审阅"' in llm.prompt
    assert llm.max_tokens >= 4096


class TruncatedThenCompactLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.messages = self
        self.calls = 0
        self.prompts: list[str] = []

    async def create(self, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs["messages"][0]["content"])
        text = '{"steps": [' if self.calls == 1 else '{"steps": []}'
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


@pytest.mark.asyncio
async def test_planner_retries_truncated_json_with_compact_prompt() -> None:
    llm = TruncatedThenCompactLLM()
    planner = Planner(llm, ToolRegistry())

    data = await planner._call_llm("规划复杂知乎流程")

    assert data == {"steps": []}
    assert llm.calls == 2
    assert "响应因过长而被截断" in llm.prompts[1]


def test_planner_forbids_invented_get_dom_placeholders() -> None:
    assert "{{get_dom_login_element}}" in Planner.SYSTEM_PROMPT
    assert "禁止生成" in Planner.SYSTEM_PROMPT


def test_planner_requires_explicit_article_title_and_body_fields() -> None:
    assert 'field_kind="title"' in Planner.SYSTEM_PROMPT
    assert 'field_kind="body"' in Planner.SYSTEM_PROMPT
    assert 'role="textbox" 的第一个元素' in Planner.SYSTEM_PROMPT


class GenericZhihuPublishLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.messages = self

    async def create(self, **_kwargs):
        return SimpleNamespace(content=[SimpleNamespace(text="""{
          "steps": [
            {
              "tool_name": "click",
              "params": {"selector": "发布", "strategy": "text"},
              "description": "点击发布文章",
              "expected_outcome": "文章发布成功"
            }
          ],
          "preserve_remaining": true
        }""")])


@pytest.mark.asyncio
async def test_planner_normalizes_generic_zhihu_publish_click() -> None:
    planner = Planner(GenericZhihuPublishLLM(), ToolRegistry())
    task = Task(goal="在知乎写一篇文章并发布")

    steps, fallback = await planner.plan(task)

    assert fallback == ""
    assert len(steps) == 1
    assert steps[0].tool_name == "publish_article"
    assert steps[0].params == {"title": "{{generate_article.title}}"}


@pytest.mark.asyncio
async def test_replanner_normalizes_generic_zhihu_publish_click() -> None:
    planner = Planner(GenericZhihuPublishLLM(), ToolRegistry())
    task = Task(goal="在知乎写一篇文章并发布")

    steps, fallback, preserve = await planner.replan(
        task,
        Step(tool_name="click", description="点击发布文章"),
        "Modal blocked the click",
    )

    assert fallback == ""
    assert preserve is True
    assert steps[0].tool_name == "publish_article"


def test_zhihu_plan_uses_one_editor_entry_and_orders_dependencies() -> None:
    planner = object.__new__(Planner)
    task = Task(goal="在知乎写文章、生成配图并发布")
    steps = [
        Step(
            tool_name="navigate",
            params={"url": "https://www.zhihu.com"},
            description="打开知乎首页",
        ),
        Step(
            tool_name="click",
            params={"selector": "写文章", "strategy": "text"},
            description="点击写文章进入编辑器",
        ),
        Step(
            tool_name="type_text",
            params={
                "selector": "标题",
                "text": "{{generate_article.title}}",
                "strategy": "text",
            },
            description="输入标题",
        ),
        Step(
            tool_name="get_page_state",
            params={"text_contains": ["{{generate_article.title}}"]},
            description="验证编辑器中的标题",
        ),
        Step(
            tool_name="get_dom",
            description="获取创作下拉菜单DOM并查找写文章",
        ),
        Step(tool_name="generate_article", params={"topic": "AI"}),
        Step(
            tool_name="navigate",
            params={"url": "https://www.zhihu.com"},
            description="重新回到知乎首页",
        ),
        Step(
            tool_name="navigate",
            params={"url": "https://zhuanlan.zhihu.com/write"},
            description="再次进入编辑器",
        ),
        Step(
            tool_name="generate_image",
            params={"article_text": "{{generate_article.body}}"},
        ),
        Step(
            tool_name="upload_image",
            params={"image_paths": "{{generate_image.image_paths}}"},
        ),
        Step(
            tool_name="publish_article",
            params={"title": "{{generate_article.title}}"},
        ),
    ]

    normalized = planner._normalize_platform_steps(task, steps)

    editor_entries = [
        step for step in normalized
        if step.tool_name == "navigate"
        and "zhuanlan.zhihu.com/write" in str(step.params.get("url", ""))
    ]
    assert len(editor_entries) == 1
    assert not any(
        step.tool_name == "click" and "写文章" in step.description
        for step in normalized
    )
    assert not any("下拉菜单" in step.description for step in normalized)
    assert not any(
        step.tool_name == "get_page_state" and "编辑器" in step.description
        for step in normalized
    )
    article_index = next(
        index for index, step in enumerate(normalized)
        if step.tool_name == "generate_article"
    )
    title_index = next(
        index for index, step in enumerate(normalized)
        if step.tool_name == "type_text"
    )
    assert article_index < title_index
