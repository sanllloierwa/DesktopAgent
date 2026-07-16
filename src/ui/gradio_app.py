"""Gradio-based web UI for Desktop Agent.

Layout:
  ┌─────────────────────────────────────────────┐
  │  Desktop Agent                               │
  │  [▶ 任务]  [⚙ 设置]                          │
  ├─────────────────────────────────────────────┤
  │  Task tab:                                   │
  │  [Task Input] [▶ Run] [■ Stop] [↺ Reset]    │
  │  ┌─ Execution Plan ──┬── Live View ────────┐ │
  │  │ (step cards)      │  (screenshot / log) │ │
  │  └───────────────────┴─────────────────────┘ │
  │                                              │
  │  Settings tab:                               │
  │  [Provider ▼] [Model]                        │
  │  [DeepSeek Key ********]  [Toggle Show]      │
  │  [OpenAI Key   ********]                     │
  │  [Anthropic Key ********]                    │
  │  [💾 Save Settings]                          │
  └─────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any, Generator

from src.schemas.task import Task
from src.tools.base import ToolRegistry
from src.agent.loop import AgentLoop
from src.ui.events import AgentEvent, EventBus, EventType, task_done_console_label
from src.utils.llm_factory import create_llm_client, DEEPSEEK_BASE_URL
from src.utils.config import load_config
from src.utils.user_settings import (
    UserSettings,
    load_user_settings,
    save_user_settings,
    get_user_settings,
)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """
.container { max-width: 1400px; margin: 0 auto; }
.step-row { padding: 6px 10px; margin: 2px 0; border-radius: 6px; font-family: monospace; font-size: 13px; }
.step-pending { background: #f0f0f0; color: #888; }
.step-running { background: #e3f0ff; color: #1a6fcf; border-left: 3px solid #1a6fcf; }
.step-ok { background: #e6f4ea; color: #1e7e34; }
.step-fail { background: #fce8e6; color: #c5221f; border-left: 3px solid #c5221f; }
.step-retry { background: #fef7e0; color: #b06000; }
.console { font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px; line-height: 1.5; }
.status-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.footer { text-align: center; color: #999; font-size: 12px; margin-top: 16px; }
.settings-section { padding: 16px; background: #fafafa; border-radius: 8px; margin-bottom: 12px; }
.settings-section h3 { margin-top: 0; }
"""


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _build_html(events: list[dict]) -> str:
    if not events:
        return "<div style='color:#999;padding:20px;text-align:center;'>等待任务输入...</div>"

    rows: list[str] = []
    for e in events:
        etype = e["type"]
        msg = e.get("message", "")
        if etype == "plan_done":
            for i, s in enumerate(e.get("data", {}).get("steps", [])):
                rows.append(
                    f"<div class='step-row step-pending'>"
                    f"<span class='status-badge' style='background:#ddd;color:#666;'>待执行</span> "
                    f"<b>Step {i+1}:</b> {s.get('desc', s.get('tool', '?'))}"
                    f"</div>"
                )
        elif etype == "step_start":
            idx = e.get("data", {}).get("index", "?")
            total = e.get("data", {}).get("total", "?")
            rows.append(
                f"<div class='step-row step-running'>"
                f"<span class='status-badge' style='background:#1a6fcf;color:#fff;'>执行中</span> "
                f"<b>[{idx}/{total}]</b> {msg}"
                f"</div>"
            )
        elif etype == "step_done":
            success = e.get("data", {}).get("success", True)
            css_class = "step-ok" if success else "step-fail"
            badge = "成功" if success else "失败"
            badge_style = "background:#1e7e34;color:#fff;" if success else "background:#c5221f;color:#fff;"
            dur = e.get("data", {}).get("duration_ms", 0)
            rows.append(
                f"<div class='step-row {css_class}'>"
                f"<span class='status-badge' style='{badge_style}'>{badge}</span> "
                f"{msg} <span style='color:#999;'>({dur:.0f}ms)</span>"
                f"</div>"
            )
        elif etype == "step_retry":
            rows.append(
                f"<div class='step-row step-retry'>"
                f"<span class='status-badge' style='background:#f9ab00;color:#fff;'>重试</span> "
                f"{msg}"
                f"</div>"
            )
        elif etype == "task_done":
            success = e.get("data", {}).get("success", False)
            total_steps = e.get("data", {}).get("total_steps", 0)
            duration = e.get("data", {}).get("duration_ms", 0)
            emoji = "&#x2705;" if success else "&#x274C;"
            rows.append(
                f"<div class='step-row {'step-ok' if success else 'step-fail'}'>"
                f"<b>{emoji} 任务{'完成' if success else '失败'}</b> &mdash; "
                f"{total_steps} 步, {duration:.0f}ms &mdash; {msg}"
                f"</div>"
            )
        elif etype == "error":
            rows.append(
                f"<div class='step-row step-fail'><b>&#x26A0;</b> {msg}</div>"
            )

    return "\n".join(rows) if rows else "<div style='color:#999;padding:20px;text-align:center;'>等待执行...</div>"


# ---------------------------------------------------------------------------
# UIBridge: 后台线程跑 AgentLoop，队列传输事件
# ---------------------------------------------------------------------------

class UIBridge(threading.Thread):
    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(daemon=True)
        self.registry = registry
        self._task_queue: queue.Queue[str] = queue.Queue()
        self._event_queue: queue.Queue[dict] = queue.Queue()
        self._stop_flag = threading.Event()
        self._ready = threading.Event()
        self._last_error: str = ""

    def submit_task(self, goal: str) -> None:
        self._task_queue.put(goal)

    def stop_task(self) -> None:
        self._stop_flag.set()

    def drain_events(self) -> list[dict]:
        events: list[dict] = []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        return events

    @property
    def last_error(self) -> str:
        return self._last_error

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _setup_and_wait():
            try:
                llm = create_llm_client()
            except Exception as exc:
                self._last_error = str(exc)
                self._event_queue.put({
                    "type": "error",
                    "message": f"LLM 客户端创建失败: {exc}. 请在设置中配置 API Key。",
                    "data": {},
                    "timestamp": time.time(),
                })
                self._ready.set()
                return

            self._agent = AgentLoop(self.registry, llm)
            self._ready.set()

            while not self._stop_flag.is_set():
                try:
                    goal = self._task_queue.get(timeout=0.5)
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue

                self._stop_flag.clear()

                async def handler(event: AgentEvent) -> None:
                    d = {
                        "type": event.type.value,
                        "message": event.message,
                        "data": dict(event.data) if event.data else {},
                        "timestamp": event.timestamp,
                    }
                    self._event_queue.put(d)

                self._agent.events.subscribe(handler)
                try:
                    await self._agent.run(Task(goal=goal))
                except Exception as exc:
                    self._event_queue.put({
                        "type": "error",
                        "message": f"执行异常: {exc}",
                        "data": {},
                        "timestamp": time.time(),
                    })
                finally:
                    self._agent.events.unsubscribe(handler)
                    self._agent.reset()

        try:
            loop.run_until_complete(_setup_and_wait())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Gradio UI
# ---------------------------------------------------------------------------

PROVIDER_INFO = {
    "deepseek": {
        "name": "DeepSeek",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "help": "https://platform.deepseek.com/api_keys",
    },
    "openai": {
        "name": "OpenAI",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
        "help": "https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "name": "Anthropic",
        "models": ["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5"],
        "help": "https://console.anthropic.com/keys",
    },
    "agnes": {
        "name": "Agnes AI",
        "models": ["agnes-2.0-flash"],
        "help": "https://platform.agnes-ai.com/",
    },
}


def create_ui(registry: ToolRegistry | None = None) -> Any:
    import gradio as gr

    if registry is None:
        registry = ToolRegistry()

    user_settings = load_user_settings()
    bridge: UIBridge | None = None
    all_events: list[dict] = []

    with gr.Blocks(css=CSS, title="Desktop Agent", theme=gr.themes.Soft()) as app:
        gr.HTML("<h1 style='text-align:center;'>Desktop Agent</h1>")

        # ==================================================================
        # Settings Tab
        # ==================================================================
        with gr.Tab("任务") as task_tab, gr.Tab("设置") as settings_tab:  # type: ignore[attr-defined]
            pass

        # --- Settings Tab Content ---
        with settings_tab:
            gr.HTML("<h2 style='margin-top:0;'>API 配置</h2>")
            gr.HTML("<p style='color:#666;'>API Key 加密保存在本地 <code>~/.desktop-agent/settings.json</code>，不会上传到任何服务器。</p>")

            with gr.Row():
                provider_dd = gr.Dropdown(
                    label="默认 Provider",
                    choices=list(PROVIDER_INFO.keys()),
                    value=user_settings.default_provider,
                    scale=1,
                )
                model_dd = gr.Dropdown(
                    label="默认模型",
                    choices=PROVIDER_INFO[user_settings.default_provider]["models"],
                    value=user_settings.default_model,
                    scale=1,
                    allow_custom_value=True,
                )

            with gr.Group():
                gr.HTML("<b>API Keys</b>")
                ds_key = gr.Textbox(
                    label="DeepSeek API Key",
                    value=user_settings.get_key("deepseek"),
                    type="password",
                    placeholder="sk-...",
                )
                with gr.Row():
                    ds_show = gr.Checkbox(label="显示密钥", scale=0, min_width=80)
                    ds_status = gr.HTML("", scale=1)

                oa_key = gr.Textbox(
                    label="OpenAI API Key",
                    value=user_settings.get_key("openai"),
                    type="password",
                    placeholder="sk-...",
                )
                with gr.Row():
                    oa_show = gr.Checkbox(label="显示密钥", scale=0, min_width=80)
                    oa_status = gr.HTML("", scale=1)

                an_key = gr.Textbox(
                    label="Anthropic API Key",
                    value=user_settings.get_key("anthropic"),
                    type="password",
                    placeholder="sk-ant-...",
                )
                with gr.Row():
                    an_show = gr.Checkbox(label="显示密钥", scale=0, min_width=80)
                    an_status = gr.HTML("", scale=1)

                ag_key = gr.Textbox(
                    label="Agnes AI API Key",
                    value=user_settings.get_key("agnes"),
                    type="password",
                    placeholder="ag-...",
                )
                with gr.Row():
                    ag_show = gr.Checkbox(label="显示密钥", scale=0, min_width=80)
                    ag_status = gr.HTML("", scale=1)

            with gr.Row():
                save_btn = gr.Button("保存设置", variant="primary", scale=0)
                settings_msg = gr.Textbox(label="", interactive=False, scale=1, show_label=False, container=False)

            gr.HTML("<hr><p style='color:#999;font-size:12px;'>"
                     "密钥优先级：UI 保存的设置 > 环境变量 > .env 文件<br>"
                     "获取 DeepSeek Key: <a href='https://platform.deepseek.com/api_keys' target='_blank'>platform.deepseek.com/api_keys</a><br>"
                     "DeepSeek API 文档: <a href='https://api-docs.deepseek.com' target='_blank'>api-docs.deepseek.com</a>"
                     "</p>")

        # --- Task Tab Content ---
        with task_tab:
            gr.HTML("<p style='text-align:center;color:#666;'>用自然语言描述任务，Agent 自动完成</p>")

            with gr.Row():
                task_input = gr.Textbox(
                    label="任务描述",
                    placeholder="例如：在知乎发布一篇关于 Python 协程的文章，配2张图，发布后搜索并点赞",
                    scale=5,
                    lines=2,
                )
                with gr.Column(scale=1, min_width=120):
                    run_btn = gr.Button("执行", variant="primary")
                    stop_btn = gr.Button("停止", variant="stop")
                    reset_btn = gr.Button("重置", variant="secondary")

            with gr.Row():
                with gr.Column(scale=3):
                    plan_html = gr.HTML(
                        value="<div style='color:#999;padding:20px;text-align:center;'>等待任务输入...</div>",
                        label="执行计划",
                    )
                with gr.Column(scale=2):
                    screenshot = gr.Image(label="实时截图", height=320)
                    console = gr.Textbox(
                        label="控制台输出",
                        lines=8,
                        max_lines=20,
                        interactive=False,
                        elem_classes=["console"],
                    )

            status_bar = gr.HTML("<div class='footer'>就绪 — 请先在「设置」中配置 API Key，再输入任务点击执行</div>")

        # ==================================================================
        # Settings Callbacks
        # ==================================================================

        def on_provider_change(provider: str) -> tuple:
            info = PROVIDER_INFO.get(provider, PROVIDER_INFO["deepseek"])
            return gr.Dropdown(choices=info["models"], value=info["models"][0])

        provider_dd.change(
            on_provider_change,
            inputs=[provider_dd],
            outputs=[model_dd],
        )

        def on_show_key(key: str, show: bool) -> dict:
            if show and key:
                return gr.Textbox(type="text", value=key)
            return gr.Textbox(type="password", value=key)

        ds_show.change(on_show_key, inputs=[ds_key, ds_show], outputs=[ds_key])
        oa_show.change(on_show_key, inputs=[oa_key, oa_show], outputs=[oa_key])
        an_show.change(on_show_key, inputs=[an_key, an_show], outputs=[an_key])
        ag_show.change(on_show_key, inputs=[ag_key, ag_show], outputs=[ag_key])

        def on_save_settings(
            provider: str, model: str,
            ds: str, oa: str, an: str, ag: str,
        ) -> tuple:
            settings = UserSettings(
                deepseek_api_key=ds.strip(),
                openai_api_key=oa.strip(),
                anthropic_api_key=an.strip(),
                agnes_api_key=ag.strip(),
                default_provider=provider,
                default_model=model,
            )
            save_user_settings(settings)

            # 刷新 config 中的 provider/model
            config = load_config()
            config.llm.provider = provider
            config.llm.model = model

            # 更新密钥状态提示
            def key_status(val: str, name: str) -> str:
                if val:
                    masked = val[:4] + "****" + val[-4:] if len(val) > 8 else "****"
                    return f"<span style='color:#1e7e34;'>{name} 已配置: {masked}</span>"
                return f"<span style='color:#999;'>{name} 未配置</span>"

            return (
                f"设置已保存 — Provider: {provider} / Model: {model}",
                key_status(ds.strip(), "DeepSeek"),
                key_status(oa.strip(), "OpenAI"),
                key_status(an.strip(), "Anthropic"),
                key_status(ag.strip(), "Agnes AI"),
            )

        save_btn.click(
            on_save_settings,
            inputs=[provider_dd, model_dd, ds_key, oa_key, an_key, ag_key],
            outputs=[settings_msg, ds_status, oa_status, an_status, ag_status],
        )

        # ==================================================================
        # Task Callbacks
        # ==================================================================

        def on_run(goal: str) -> Generator[tuple, None, None]:
            nonlocal bridge, all_events

            if not goal.strip():
                yield gr.HTML(), gr.Image(), gr.Textbox(), gr.HTML()
                return

            # 检查是否有可用的 API key
            try:
                us = get_user_settings()
                provider = us.default_provider
                if not us.has_key(provider):
                    yield (
                        gr.HTML("<div style='color:#c5221f;padding:20px;text-align:center;'>"
                                f"未配置 {provider.upper()} API Key，请先在「设置」页面配置</div>"),
                        None,
                        f"错误: 未找到 {provider} 的 API Key\n",
                        gr.HTML("<div class='footer' style='color:#c5221f;'>请先在设置中配置 API Key</div>"),
                    )
                    return
            except Exception:
                pass

            all_events = []
            bridge = UIBridge(registry)
            bridge.start()
            bridge._ready.wait(timeout=30)
            bridge.submit_task(goal.strip())

            yield (
                _build_html([]),
                None,
                "任务已提交，等待规划...\n",
                "<div class='footer'>规划中...</div>",
            )

            while bridge.is_alive():
                new_events = bridge.drain_events()
                all_events.extend(new_events)

                plan_html_val = _build_html(all_events)

                latest_screenshot = None
                for e in reversed(all_events):
                    if e.get("data", {}).get("screenshot_base64"):
                        latest_screenshot = e["data"]["screenshot_base64"]
                        break

                console_lines: list[str] = []
                for e in all_events[-15:]:
                    prefix = {
                        "plan_start": "P",
                        "plan_done": "P",
                        "step_start": ">",
                        "step_done": "  OK" if e.get("data", {}).get("success", True) else "  FAIL",
                        "step_retry": "  retry",
                        "error": "ERR",
                        "task_done": task_done_console_label(e.get("data", {})),
                        "log": "  ",
                    }.get(e["type"], "-")
                    console_lines.append(f"[{prefix}] {e['message']}")
                console_text = "\n".join(console_lines) if console_lines else "等待中..."

                done_events = [e for e in all_events if e["type"] == "task_done"]
                if done_events:
                    last = done_events[-1]
                    if last["data"].get("success"):
                        status = f"<div class='footer' style='color:#1e7e34;'>任务成功 — {last['data']['total_steps']} 步, {last['data']['duration_ms']:.0f}ms</div>"
                    else:
                        status = f"<div class='footer' style='color:#c5221f;'>任务失败 — {last['message']}</div>"
                elif any(e["type"] == "step_start" for e in all_events):
                    status = "<div class='footer'>执行中...</div>"
                else:
                    status = "<div class='footer'>规划中...</div>"

                yield (
                    gr.HTML(plan_html_val),
                    latest_screenshot,
                    console_text,
                    gr.HTML(status),
                )
                time.sleep(0.5)

            new_events = bridge.drain_events()
            all_events.extend(new_events)
            yield (
                _build_html(all_events),
                None,
                console_text if "console_text" in dir() else "",
                gr.HTML(status if "status" in dir() else "<div class='footer'>已结束</div>"),
            )

        def on_stop() -> tuple:
            nonlocal bridge
            if bridge:
                bridge.stop_task()
            return (
                _build_html(all_events),
                None,
                "已请求停止\n",
                gr.HTML("<div class='footer' style='color:#c5221f;'>已停止</div>"),
            )

        def on_reset() -> tuple:
            nonlocal bridge, all_events
            if bridge:
                bridge.stop_task()
                bridge = None
            all_events = []
            return (
                gr.HTML("<div style='color:#999;padding:20px;text-align:center;'>等待任务输入...</div>"),
                None,
                "",
                gr.HTML("<div class='footer'>就绪</div>"),
            )

        run_btn.click(
            on_run,
            inputs=[task_input],
            outputs=[plan_html, screenshot, console, status_bar],
        )
        stop_btn.click(on_stop, outputs=[plan_html, screenshot, console, status_bar])
        reset_btn.click(on_reset, outputs=[plan_html, screenshot, console, status_bar])

    return app


def launch_ui(
    registry: ToolRegistry | None = None,
    server_name: str = "127.0.0.1",
    server_port: int = 7860,
    share: bool = False,
) -> None:
    app = create_ui(registry)
    app.queue(default_concurrency_limit=1)
    app.launch(server_name=server_name, server_port=server_port, share=share)
