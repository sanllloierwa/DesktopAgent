from __future__ import annotations

import pytest

from src.agent.planner import _parse_json_object


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
