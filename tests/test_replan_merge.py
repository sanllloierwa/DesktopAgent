from __future__ import annotations

from src.agent.loop import AgentLoop
from src.agent.planner import Planner
from src.schemas.task import Step


def _step(name: str) -> Step:
    return Step(tool_name=name, description=name)


def test_replan_can_preserve_valid_remaining_steps() -> None:
    original = [_step("launch"), _step("write"), _step("save")]
    replacement = [_step("desktop_type_text")]

    merged = AgentLoop._merge_replan(original, 1, replacement, preserve_remaining=True)

    assert [step.tool_name for step in merged] == ["launch", "desktop_type_text", "save"]


def test_replan_can_replace_entire_remaining_flow() -> None:
    original = [_step("launch"), _step("write"), _step("save")]
    replacement = [_step("create_document"), _step("desktop_type_text"), _step("save_as")]

    merged = AgentLoop._merge_replan(original, 1, replacement, preserve_remaining=False)

    assert [step.tool_name for step in merged] == [
        "launch",
        "create_document",
        "desktop_type_text",
        "save_as",
    ]


def test_replan_parser_honors_explicit_preserve_flag() -> None:
    planner = object.__new__(Planner)
    steps, fallback, preserve = planner._parse_replan({
        "steps": [{"tool_name": "inspect"}, {"tool_name": "recover"}],
        "preserve_remaining": True,
    })

    assert [step.tool_name for step in steps] == ["inspect", "recover"]
    assert fallback == ""
    assert preserve is True


def test_replan_parser_uses_safe_legacy_defaults() -> None:
    planner = object.__new__(Planner)

    one_step = planner._parse_replan({"steps": [{"tool_name": "recover"}]})
    multi_step = planner._parse_replan({
        "steps": [{"tool_name": "recover"}, {"tool_name": "save"}]
    })

    assert one_step[2] is True
    assert multi_step[2] is False


def test_planner_prompt_routes_native_wechat_through_desktop_tools() -> None:
    prompt = Planner.SYSTEM_PROMPT

    assert "focus_window" in prompt
    assert 'desktop_keypress(keys="ctrl+f", app_name="wechat")' in prompt
    assert "不要索要微信账号、手机号或密码" in prompt
    assert "最后截图并分析消息是否出现在聊天记录中" in prompt
    assert "不支持“若/如果/视情况”条件步骤" in prompt
    assert "只有用户目标明确要求“登录”时" in prompt


def test_planner_prompt_requires_structured_visual_coordinates() -> None:
    prompt = Planner.SYSTEM_PROMPT

    assert "locate_screen_element 定位目标" in prompt
    assert "不能从 analyze_screen 的自然语言回答中猜坐标" in prompt
    assert '"{{locate_screen_element.x}}"' in prompt
    assert '"{{locate_screen_element.confidence}}"' in prompt
    assert "禁止根据自然语言描述或历史截图臆造坐标" in prompt
    assert '微信统一使用 app_name="wechat"' in prompt


def test_replan_prompt_stops_using_vision_after_circuit_breaker() -> None:
    source = Planner.replan.__code__.co_consts
    prompt_text = "\n".join(value for value in source if isinstance(value, str))

    assert "[VISION_UNAVAILABLE]" in prompt_text
    assert "禁止继续规划 analyze_screen 或 locate_screen_element" in prompt_text
    assert "不要再次规划 request_user_input" in prompt_text
