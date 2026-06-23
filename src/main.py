"Desktop Agent — 跨平台 AI 自动化任务执行"

from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中（兼容 `python src/main.py` 和 `python -m src.main`）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger

from src.schemas.task import Task
from src.tools.base import ToolRegistry
from src.tools.registry_init import register_all_tools
from src.agent.loop import AgentLoop
from src.utils.config import load_config
from src.utils.llm_factory import create_llm_client
from src.utils.logger import setup_logging


def _make_registry() -> ToolRegistry:
    return register_all_tools(ToolRegistry())


async def run_task(goal: str, config_path: str | None = None) -> None:
    setup_logging()
    config = load_config(config_path)
    registry = _make_registry()
    llm = create_llm_client(config)

    loop = AgentLoop(registry, llm)
    task = Task(goal=goal)

    result = await loop.run(task)

    if result.success:
        logger.info(f"Task completed: {result.summary}")
        logger.info(f"Steps: {result.total_steps}, Duration: {result.total_duration_ms:.0f}ms")
    else:
        logger.error(f"Task failed: {result.summary}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Desktop Agent — AI-powered task automation")
    parser.add_argument(
        "goal", nargs="?", type=str,
        help="Natural language task description",
    )
    parser.add_argument(
        "--config", "-c", type=str, default=None,
        help="Path to config YAML override",
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true",
        help="Start interactive REPL mode",
    )
    parser.add_argument(
        "--ui", "-u", action="store_true",
        help="Launch Gradio Web UI",
    )
    parser.add_argument(
        "--local-ui", "-l", action="store_true",
        help="Launch native desktop UI (CustomTkinter)",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=7860,
        help="Web UI port (default: 7860)",
    )
    parser.add_argument(
        "--share", action="store_true",
        help="Enable Gradio share link",
    )
    parser.add_argument(
        "--wps", type=str, metavar="TOPIC", default=None,
        help="Quick task: create a Word document about TOPIC",
    )
    parser.add_argument(
        "--zhihu", type=str, metavar="TOPIC", default=None,
        help="Quick task: publish a Zhihu article about TOPIC",
    )
    parser.add_argument(
        "--wechat", type=str, metavar="ACCOUNT", default=None,
        help="Quick task: follow WECHAT account and send message",
    )
    args = parser.parse_args()

    if args.local_ui:
        _run_local_ui(args.config)
    elif args.ui:
        _run_ui(args.config, args.port, args.share)
    elif args.interactive:
        _run_repl(args.config)
    elif args.wps:
        from src.platforms.wps import build_wps_task
        task = build_wps_task(args.wps)
        asyncio.run(_run_with_tools(task.goal, args.config))
    elif args.zhihu:
        from src.platforms.zhihu import build_zhihu_task
        task = build_zhihu_task(args.zhihu)
        asyncio.run(_run_with_tools(task.goal, args.config))
    elif args.wechat:
        from src.platforms.wechat import build_wechat_task
        task = build_wechat_task(args.wechat)
        asyncio.run(_run_with_tools(task.goal, args.config))
    elif args.goal:
        asyncio.run(run_task(args.goal, args.config))
    else:
        parser.print_help()


async def _run_with_tools(goal: str, config_path: str | None = None) -> None:
    """与 run_task 相同，但使用注册了所有工具的 registry"""
    setup_logging()
    config = load_config(config_path)
    registry = _make_registry()
    llm = create_llm_client(config)
    loop = AgentLoop(registry, llm)
    task = Task(goal=goal)
    result = await loop.run(task)

    if result.success:
        logger.info(f"Task completed: {result.summary}")
    else:
        logger.error(f"Task failed: {result.summary}")
        sys.exit(1)


def _run_repl(config_path: str | None = None) -> None:
    """交互式 REPL 模式"""
    setup_logging()
    config = load_config(config_path)
    registry = _make_registry()
    llm = create_llm_client(config)
    agent_loop = AgentLoop(registry, llm)

    print("Desktop Agent REPL — 输入自然语言任务，输入 'quit' 退出")
    print(f"已注册 {len(registry.list_names())} 个工具")
    while True:
        try:
            goal = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not goal:
            continue
        if goal.lower() in ("quit", "exit", "q"):
            break

        result = asyncio.run(agent_loop.run(Task(goal=goal)))
        if result.success:
            print(f"  OK — {result.summary} ({result.total_steps} steps, {result.total_duration_ms:.0f}ms)")
        else:
            print(f"  FAIL — {result.summary}")
        agent_loop.reset()


def _run_local_ui(config_path: str | None = None) -> None:
    """启动 CustomTkinter 本地桌面 UI"""
    from src.ui.ctk_app import launch_local_ui

    setup_logging()
    load_config(config_path)
    registry = _make_registry()

    print(f"\n  Desktop Agent 本地 UI 启动中...")
    print(f"  已注册 {len(registry.list_names())} 个工具\n")
    launch_local_ui(registry=registry)


def _run_ui(config_path: str | None = None, port: int = 7860, share: bool = False) -> None:
    """启动 Gradio Web UI"""
    from src.ui.gradio_app import launch_ui

    setup_logging()
    load_config(config_path)

    registry = _make_registry()

    print(f"\n  Desktop Agent Web UI 启动中...")
    print(f"  已注册 {len(registry.list_names())} 个工具")
    print(f"  打开浏览器访问: http://127.0.0.1:{port}\n")
    launch_ui(registry=registry, server_port=port, share=share)


if __name__ == "__main__":
    main()
