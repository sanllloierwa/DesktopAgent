"""CustomTkinter native desktop UI — 替代 Gradio 的本地客户端界面

Layout:
  ┌───────────────────────────────────────────────┐
  │  Desktop Agent                         [_][□][X]│
  │  [任务] [设置]                                   │
  │  ┌─任务描述────────────────────────────────────┐ │
  │  │                                              │ │
  │  └──────────────────────────────────────────────┘ │
  │  [执行] [停止] [重置]                               │
  │  ┌─执行计划────────────────────────────────────┐ │
  │  │ ✓ Step 1: 启动 WPS                (350ms)  │ │
  │  │ ▶ Step 2: 生成文章...                      │ │
  │  └──────────────────────────────────────────────┘ │
  │  ┌─控制台──────────────────────────────────────┐ │
  │  │ > 正在规划...                               │ │
  │  └──────────────────────────────────────────────┘ │
  │  状态: 就绪                                       │
  └───────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any

from src.schemas.task import Task
from src.tools.base import ToolRegistry
from src.agent.loop import AgentLoop
from src.ui.events import AgentEvent, task_done_console_label
from src.tools.interactive.user_input import UserInputBridge, PromptRequest
from src.utils.llm_factory import create_llm_client
from src.utils.config import load_config
from src.utils.user_settings import (
    UserSettings,
    load_user_settings,
    save_user_settings,
    save_model_selection,
    get_user_settings,
)

# ---------------------------------------------------------------------------
# Agent 后台线程（与 Gradio 版 UIBridge 逻辑相同）
# ---------------------------------------------------------------------------

class AgentThread(threading.Thread):
    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(daemon=True)
        self.registry = registry
        self._task_queue: queue.Queue[str] = queue.Queue()
        self._event_queue: queue.Queue[dict] = queue.Queue()
        self._stop_flag = threading.Event()
        self._ready = threading.Event()
        self._task_active = threading.Event()
        self._submit_lock = threading.Lock()
        self._last_error: str = ""

    def submit_task(self, goal: str) -> bool:
        with self._submit_lock:
            if self._task_active.is_set() or not self._task_queue.empty():
                return False
            self._task_active.set()
            self._task_queue.put(goal)
            return True

    @property
    def busy(self) -> bool:
        return self._task_active.is_set() or not self._task_queue.empty()

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

        async def _run():
            try:
                llm = create_llm_client()
            except Exception as exc:
                self._last_error = str(exc)
                self._event_queue.put({
                    "type": "error",
                    "message": f"LLM 初始化失败: {exc}。请在设置中配置 API Key。",
                    "data": {},
                    "timestamp": time.time(),
                })
                self._ready.set()
                return

            agent = AgentLoop(self.registry, llm)
            self._ready.set()

            while not self._stop_flag.is_set():
                try:
                    goal = self._task_queue.get(timeout=0.5)
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue

                self._stop_flag.clear()

                async def handler(event: AgentEvent) -> None:
                    self._event_queue.put({
                        "type": event.type.value,
                        "message": event.message,
                        "data": dict(event.data) if event.data else {},
                        "timestamp": event.timestamp,
                    })

                agent.events.subscribe(handler)
                try:
                    await agent.run(Task(goal=goal))
                except Exception as exc:
                    self._event_queue.put({
                        "type": "error",
                        "message": f"执行异常: {exc}",
                        "data": {},
                        "timestamp": time.time(),
                    })
                finally:
                    self._task_active.clear()
                    agent.events.unsubscribe(handler)
                    agent.reset()

        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# CustomTkinter Application
# ---------------------------------------------------------------------------

STEPS_PER_PAGE = 8  # 每页最多显示步骤数

PROVIDER_INFO = {
    "deepseek": {
        "name": "DeepSeek",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "openai": {
        "name": "OpenAI",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
    },
    "anthropic": {
        "name": "Anthropic",
        "models": ["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5"],
    },
    "agnes": {
        "name": "Agnes AI",
        "models": ["agnes-2.0-flash"],
    },
    "kimi": {
        "name": "Kimi",
        "models": ["kimi-k3", "kimi-k2.6"],
    },
}

VISION_PROVIDER_INFO = {
    "agnes": {"name": "Agnes AI", "models": ["agnes-2.0-flash"]},
    "kimi": {"name": "Kimi", "models": ["kimi-k3", "kimi-k2.6"]},
    "openai": {"name": "OpenAI", "models": ["gpt-4o", "gpt-4.1"]},
    "anthropic": {
        "name": "Anthropic",
        "models": ["claude-sonnet-4-6", "claude-opus-4-7"],
    },
}


class CtkDesktopAgent:
    """CustomTkinter 桌面 Agent 主窗口"""

    def __init__(self, registry: ToolRegistry) -> None:
        import customtkinter as ctk

        self.registry = registry
        self.ctk = ctk
        self.user_settings = load_user_settings()
        self.agent_thread: AgentThread | None = None
        self.all_events: list[dict] = []
        self._poll_id: str | None = None
        self._input_bridge = UserInputBridge.get_instance()
        self._input_dialog_active: bool = False
        self._input_dialog: Any = None

        # 窗口
        self.root = ctk.CTk()
        self.root.title("Desktop Agent")
        self.root.geometry("1100x780")
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        # TabView
        self.tabview = ctk.CTkTabview(self.root)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)
        self.task_tab = self.tabview.add("任务")
        self.settings_tab = self.tabview.add("设置")

        self._build_task_tab()
        self._build_settings_tab()
        self._load_settings_to_form()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ==================================================================
    # Task Tab
    # ==================================================================

    def _build_task_tab(self) -> None:
        ctk = self.ctk

        # --- 任务输入区 ---
        input_frame = ctk.CTkFrame(self.task_tab)
        input_frame.pack(fill="x", padx=5, pady=(5, 2))

        ctk.CTkLabel(input_frame, text="任务描述", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=10, pady=(8, 0))
        self.task_input = ctk.CTkTextbox(input_frame, height=60, font=ctk.CTkFont(size=13))
        self.task_input.pack(fill="x", padx=10, pady=(2, 8))
        self.task_input.insert("1.0", "")

        btn_frame = ctk.CTkFrame(input_frame, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=(0, 8))
        self.run_btn = ctk.CTkButton(btn_frame, text="执行", width=90, command=self._on_run)
        self.run_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ctk.CTkButton(btn_frame, text="停止", width=90, fg_color="#c5221f", hover_color="#a50e0c", command=self._on_stop)
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.reset_btn = ctk.CTkButton(btn_frame, text="重置", width=90, fg_color="gray", hover_color="#666", command=self._on_reset)
        self.reset_btn.pack(side="left")

        # --- 执行计划区（可滚动步骤卡片） ---
        plan_label = ctk.CTkLabel(self.task_tab, text="执行计划", font=ctk.CTkFont(size=13, weight="bold"))
        plan_label.pack(anchor="w", padx=15, pady=(8, 2))

        self.plan_scroll = ctk.CTkScrollableFrame(self.task_tab, height=200)
        self.plan_scroll.pack(fill="x", padx=10, pady=(0, 5))
        self.plan_inner = ctk.CTkFrame(self.plan_scroll, fg_color="transparent")
        self.plan_inner.pack(fill="x")
        self.plan_cards: list[ctk.CTkFrame] = []

        # --- 控制台 ---
        console_label = ctk.CTkLabel(self.task_tab, text="控制台", font=ctk.CTkFont(size=13, weight="bold"))
        console_label.pack(anchor="w", padx=15, pady=(8, 2))
        self.console = ctk.CTkTextbox(self.task_tab, height=160, font=ctk.CTkFont(family="Consolas", size=11))
        self.console.pack(fill="x", padx=10, pady=(0, 5))
        self.console.insert("1.0", "就绪 — 请先在「设置」中配置 API Key\n")

        # --- 状态栏 ---
        self.status_bar = ctk.CTkLabel(
            self.task_tab, text="就绪", font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        self.status_bar.pack(anchor="w", padx=15, pady=(0, 10))

    # ==================================================================
    # Settings Tab
    # ==================================================================

    def _build_settings_tab(self) -> None:
        ctk = self.ctk
        config = load_config()
        vision_provider = self.user_settings.vision_provider or config.vision.provider
        vision_model = self.user_settings.vision_model or config.vision.model
        vision_models = VISION_PROVIDER_INFO.get(
            vision_provider,
            {"models": [vision_model]},
        )["models"]

        title = ctk.CTkLabel(self.settings_tab, text="API 配置", font=ctk.CTkFont(size=16, weight="bold"))
        title.pack(anchor="w", padx=15, pady=(15, 5))

        info = ctk.CTkLabel(
            self.settings_tab,
            text="密钥保存在 ~/.desktop-agent/settings.json，不会上传到任何服务器",
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        info.pack(anchor="w", padx=15, pady=(0, 15))

        # Provider + Model
        sel_frame = ctk.CTkFrame(self.settings_tab, fg_color="transparent")
        sel_frame.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(sel_frame, text="Provider", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 8))
        self.provider_var = ctk.StringVar(value=self.user_settings.default_provider)
        self.provider_dd = ctk.CTkOptionMenu(
            sel_frame, values=list(PROVIDER_INFO.keys()),
            variable=self.provider_var, width=140,
            command=self._on_provider_changed,
        )
        self.provider_dd.pack(side="left", padx=(0, 15))

        ctk.CTkLabel(sel_frame, text="Model", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 8))
        self.model_var = ctk.StringVar(value=self.user_settings.default_model)
        self.model_dd = ctk.CTkOptionMenu(
            sel_frame, values=PROVIDER_INFO[self.user_settings.default_provider]["models"],
            variable=self.model_var, width=200,
            command=self._on_model_changed,
        )
        self.model_dd.pack(side="left")

        vision_sel_frame = ctk.CTkFrame(self.settings_tab, fg_color="transparent")
        vision_sel_frame.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(
            vision_sel_frame, text="视觉 Provider", font=ctk.CTkFont(size=12)
        ).pack(side="left", padx=(0, 8))
        self.vision_provider_var = ctk.StringVar(value=vision_provider)
        self.vision_provider_dd = ctk.CTkOptionMenu(
            vision_sel_frame,
            values=list(VISION_PROVIDER_INFO.keys()),
            variable=self.vision_provider_var,
            width=140,
            command=self._on_vision_provider_changed,
        )
        self.vision_provider_dd.pack(side="left", padx=(0, 15))

        ctk.CTkLabel(
            vision_sel_frame, text="视觉模型", font=ctk.CTkFont(size=12)
        ).pack(side="left", padx=(0, 8))
        self.vision_model_var = ctk.StringVar(value=vision_model)
        self.vision_model_dd = ctk.CTkOptionMenu(
            vision_sel_frame,
            values=vision_models,
            variable=self.vision_model_var,
            width=200,
            command=self._on_vision_model_changed,
        )
        self.vision_model_dd.pack(side="left")

        # API Keys
        keys_frame = ctk.CTkFrame(self.settings_tab)
        keys_frame.pack(fill="x", padx=15, pady=(0, 10))

        self._build_key_row(keys_frame, "DeepSeek API Key", "deepseek", 0)
        self._build_key_row(keys_frame, "OpenAI API Key", "openai", 1)
        self._build_key_row(keys_frame, "Anthropic API Key", "anthropic", 2)
        self._build_key_row(keys_frame, "Agnes AI API Key", "agnes", 3)
        self._build_key_row(keys_frame, "Kimi API Key", "kimi", 4)

        # Save button
        save_frame = ctk.CTkFrame(self.settings_tab, fg_color="transparent")
        save_frame.pack(fill="x", padx=15, pady=(5, 10))

        self.save_btn = ctk.CTkButton(save_frame, text="保存设置", width=120, command=self._on_save_settings)
        self.save_btn.pack(side="left", padx=(0, 10))

        self.settings_msg = ctk.CTkLabel(save_frame, text="", font=ctk.CTkFont(size=12))
        self.settings_msg.pack(side="left")

        # 底部信息
        footer = ctk.CTkLabel(
            self.settings_tab,
            text=(
                "密钥优先级：UI 保存 > 环境变量 > .env\n"
                "Kimi Key: platform.kimi.com  |  DeepSeek Key: platform.deepseek.com/api_keys"
            ),
            font=ctk.CTkFont(size=11),
            text_color="gray",
        )
        footer.pack(anchor="w", padx=15, pady=(10, 0))

    def _build_key_row(self, parent, label: str, provider: str, row: int) -> None:
        ctk = self.ctk
        key_frame = ctk.CTkFrame(parent, fg_color="transparent")
        key_frame.pack(fill="x", padx=5, pady=4)

        ctk.CTkLabel(key_frame, text=label, width=140, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 8))

        key_val = self.user_settings.get_key(provider)
        masked = self.user_settings.mask_key(provider) if key_val else ""

        show_var = ctk.BooleanVar(value=False)
        key_var = ctk.StringVar(value=key_val)

        entry = ctk.CTkEntry(key_frame, width=380, show="*", textvariable=key_var)
        entry.pack(side="left", padx=(0, 8))

        def toggle_show():
            entry.configure(show="" if show_var.get() else "*")

        ctk.CTkCheckBox(key_frame, text="显示", variable=show_var, command=toggle_show, width=50).pack(side="left", padx=(0, 8))

        status_text = f"已配置: {masked}" if key_val else "未配置"
        status_color = "#1e7e34" if key_val else "gray"
        status = ctk.CTkLabel(key_frame, text=status_text, font=ctk.CTkFont(size=11), text_color=status_color)
        status.pack(side="left")

        # Store references
        setattr(self, f"key_{provider}", key_var)
        setattr(self, f"show_{provider}", show_var)
        setattr(self, f"status_{provider}", status)

    # ==================================================================
    # Settings Callbacks
    # ==================================================================

    def _on_provider_changed(self, choice: str) -> None:
        models = PROVIDER_INFO.get(choice, {}).get("models", [])
        self.model_dd.configure(values=models)
        self.model_var.set(models[0] if models else "")
        self.user_settings = save_model_selection(
            default_provider=choice,
            default_model=self.model_var.get(),
        )

    def _on_model_changed(self, choice: str) -> None:
        self.user_settings = save_model_selection(
            default_provider=self.provider_var.get(),
            default_model=choice,
        )

    def _on_vision_provider_changed(self, choice: str) -> None:
        models = VISION_PROVIDER_INFO.get(choice, {}).get("models", [])
        self.vision_model_dd.configure(values=models)
        self.vision_model_var.set(models[0] if models else "")
        self.user_settings = save_model_selection(
            vision_provider=choice,
            vision_model=self.vision_model_var.get(),
        )

    def _on_vision_model_changed(self, choice: str) -> None:
        self.user_settings = save_model_selection(
            vision_provider=self.vision_provider_var.get(),
            vision_model=choice,
        )

    def _load_settings_to_form(self) -> None:
        us = self.user_settings
        self.provider_var.set(us.default_provider)
        models = PROVIDER_INFO.get(us.default_provider, {}).get("models", [])
        self.model_dd.configure(values=models)
        self.model_var.set(us.default_model)

        config = load_config()
        vision_provider = us.vision_provider or config.vision.provider
        vision_model = us.vision_model or config.vision.model
        self.vision_provider_var.set(vision_provider)
        vision_models = VISION_PROVIDER_INFO.get(vision_provider, {}).get("models", [])
        self.vision_model_dd.configure(values=vision_models)
        self.vision_model_var.set(vision_model)

        for p in ["deepseek", "openai", "anthropic", "agnes", "kimi"]:
            var = getattr(self, f"key_{p}", None)
            if var:
                var.set(us.get_key(p))
            status = getattr(self, f"status_{p}", None)
            if status:
                key = us.get_key(p)
                if key:
                    status.configure(text=f"已配置: {us.mask_key(p)}", text_color="#1e7e34")
                else:
                    status.configure(text="未配置", text_color="gray")

    def _on_save_settings(self) -> None:
        settings = UserSettings(
            deepseek_api_key=getattr(self, "key_deepseek").get().strip(),
            openai_api_key=getattr(self, "key_openai").get().strip(),
            anthropic_api_key=getattr(self, "key_anthropic").get().strip(),
            agnes_api_key=getattr(self, "key_agnes").get().strip(),
            kimi_api_key=getattr(self, "key_kimi").get().strip(),
            default_provider=self.provider_var.get(),
            default_model=self.model_var.get(),
            vision_provider=self.vision_provider_var.get(),
            vision_model=self.vision_model_var.get(),
        )
        save_user_settings(settings)
        self.user_settings = settings
        self._load_settings_to_form()
        self.settings_msg.configure(
            text=(
                f"已保存 — 主模型: {settings.default_provider}/{settings.default_model}; "
                f"视觉: {settings.vision_provider}/{settings.vision_model}"
            ),
            text_color="#1e7e34",
        )

    # ==================================================================
    # Task Callbacks
    # ==================================================================

    def _on_run(self) -> None:
        goal = self.task_input.get("1.0", "end-1c").strip()
        if not goal:
            return
        if self.agent_thread and self.agent_thread.busy:
            self._set_status("已有任务正在运行，请勿重复提交", "#c5221f")
            return
        if self.agent_thread and self.agent_thread.is_alive():
            self.agent_thread.stop_task()
            self.agent_thread = None

        self.user_settings = save_model_selection(
            default_provider=self.provider_var.get(),
            default_model=self.model_var.get(),
            vision_provider=self.vision_provider_var.get(),
            vision_model=self.vision_model_var.get(),
        )

        # 检查 API key
        try:
            us = get_user_settings()
            if not us.has_key(us.default_provider):
                self._set_status(f"未配置 {us.default_provider.upper()} API Key，请先到「设置」页配置", "red")
                return
        except Exception as e:
            self._set_status(f"配置错误: {e}", "red")
            return

        self.all_events = []
        self._clear_plan_cards()
        self.console.delete("1.0", "end")
        self.console.insert("1.0", "任务已提交，等待规划...\n")
        self._set_status("规划中...", "#1a6fcf")
        self.run_btn.configure(state="disabled")

        self.agent_thread = AgentThread(self.registry)
        self.agent_thread.start()
        self.agent_thread._ready.wait(timeout=30)
        if not self.agent_thread.submit_task(goal):
            self._set_status("任务已经在运行或排队", "#c5221f")
            self.run_btn.configure(state="normal")
            return

        self._start_polling()

    def _on_stop(self) -> None:
        if self.agent_thread:
            self.agent_thread.stop_task()
        self._stop_polling()
        if self._input_bridge.has_pending():
            self._input_bridge.cancel("操作已停止")
        self._close_input_dialog(self._input_dialog)
        self._add_plan_card("已停止", "stopped")
        self._append_console("已请求停止\n")
        self._set_status("已停止", "#c5221f")
        self.run_btn.configure(state="normal")

    def _on_reset(self) -> None:
        if self.agent_thread:
            self.agent_thread.stop_task()
            self.agent_thread = None
        self._stop_polling()
        if self._input_bridge.has_pending():
            self._input_bridge.cancel("操作已重置")
        self._close_input_dialog(self._input_dialog)
        self.all_events = []
        self._clear_plan_cards()
        self.console.delete("1.0", "end")
        self.console.insert("1.0", "就绪\n")
        self.task_input.delete("1.0", "end")
        self._set_status("就绪", "gray")
        self.run_btn.configure(state="normal")

    # ==================================================================
    # 用户输入对话框
    # ==================================================================

    def _show_input_dialog(self, request: PromptRequest) -> None:
        """显示请求用户输入的模态对话框"""
        ctk = self.ctk

        # 如果已有有效对话框，只需更新提示文字
        if self._input_dialog is not None:
            try:
                if self._input_dialog.winfo_exists():
                    self._input_dialog_label.configure(text=request.prompt)
                    self._input_dialog_entry.delete("1.0", "end")
                    self._input_dialog_entry.focus_set()
                    return
            except Exception:
                pass

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("用户输入")
        dialog.geometry("520x320")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        # 居中于父窗口
        dialog.update_idletasks()
        px = self.root.winfo_x()
        py = self.root.winfo_y()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        dw, dh = 520, 320
        dialog.geometry(f"+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")

        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 5))

        ctk.CTkLabel(
            header, text="Agent 需要您的输入",
            font=ctk.CTkFont(size=15, weight="bold"), anchor="w",
        ).pack(anchor="w")

        label = ctk.CTkLabel(
            dialog, text=request.prompt,
            font=ctk.CTkFont(size=13), wraplength=480, justify="left", anchor="w",
        )
        label.pack(fill="x", padx=20, pady=(5, 12))

        entry = ctk.CTkTextbox(dialog, height=90, font=ctk.CTkFont(size=13))
        entry.pack(fill="x", padx=20, pady=(0, 12))
        entry.focus_set()

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 15))

        ctk.CTkButton(
            btn_frame, text="确认 (Enter)", width=110,
            command=lambda: self._on_input_submit(dialog, entry),
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="取消 (Esc)", width=100,
            fg_color="gray", hover_color="#666",
            command=lambda: self._on_input_cancel(dialog),
        ).pack(side="right")

        dialog.bind("<Return>", lambda e: self._on_input_submit(dialog, entry))
        dialog.bind("<Escape>", lambda e: self._on_input_cancel(dialog))
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._on_input_cancel(dialog))

        self._input_dialog = dialog
        self._input_dialog_label = label
        self._input_dialog_entry = entry
        self._input_dialog_active = True

    def _on_input_submit(self, dialog: Any, entry: Any) -> None:
        """对话框确认按钮回调"""
        text = entry.get("1.0", "end-1c").strip()
        if not text:
            return
        self._input_bridge.respond(text)
        self._close_input_dialog(dialog)

    def _on_input_cancel(self, dialog: Any) -> None:
        """对话框取消按钮回调"""
        self._input_bridge.cancel()
        self._close_input_dialog(dialog)

    def _close_input_dialog(self, dialog: Any) -> None:
        """清理对话框资源"""
        try:
            if dialog and dialog.winfo_exists():
                dialog.grab_release()
                dialog.destroy()
        except Exception:
            pass
        self._input_dialog = None
        self._input_dialog_active = False

    # ==================================================================
    # Polling: 从后台线程拉取事件，渲染到 UI
    # ==================================================================

    def _start_polling(self) -> None:
        self._poll()

    def _stop_polling(self) -> None:
        pass  # poll 自己通过 agent_thread.is_alive 停止

    def _poll(self) -> None:
        if self.agent_thread is None:
            return

        new_events = self.agent_thread.drain_events()
        self.all_events.extend(new_events)

        for e in new_events:
            self._render_event(e)

        # 检查是否有待处理的用户输入请求
        if self._input_bridge.has_pending() and not self._input_dialog_active:
            pending = self._input_bridge.get_pending()
            if pending:
                self._input_dialog_active = True
                self._append_console(f"[INPUT] 等待用户输入: {pending.prompt}\n")
                self.root.after(0, self._show_input_dialog, pending)

        if self.agent_thread.is_alive():
            self._poll_id = self.root.after(300, self._poll)
        else:
            # 最后一次拉取
            final = self.agent_thread.drain_events()
            self.all_events.extend(final)
            for e in final:
                self._render_event(e)
            self.run_btn.configure(state="normal")

    def _render_event(self, e: dict) -> None:
        etype = e["type"]
        msg = e["message"]
        data = e.get("data", {})

        if etype == "plan_done":
            for i, s in enumerate(data.get("steps", [])):
                self._add_plan_card(
                    f"Step {i+1}: {s.get('desc', s.get('tool', '?'))}",
                    "pending",
                )
        elif etype == "step_start":
            self._add_plan_card(msg, "running")
        elif etype == "step_done":
            success = data.get("success", True)
            dur = data.get("duration_ms", 0)
            self._add_plan_card(f"{msg} ({dur:.0f}ms)", "ok" if success else "fail")
        elif etype == "step_retry":
            self._add_plan_card(msg, "retry")
        elif etype == "task_done":
            success = data.get("success", False)
            steps = data.get("total_steps", 0)
            dur = data.get("duration_ms", 0)
            self._add_plan_card(f"任务{'完成' if success else '失败'} — {steps}步 {dur:.0f}ms", "ok" if success else "fail")
            self._set_status(f"任务{'成功' if success else '失败'}", "#1e7e34" if success else "#c5221f")
            self.run_btn.configure(state="normal")
        elif etype == "error":
            self._add_plan_card(msg, "fail")

        # 控制台
        prefix = {
            "plan_start": "[P]", "plan_done": "[P]", "step_start": "[>]", "step_done": "  [OK]" if data.get("success", True) else "  [FAIL]",
            "step_retry": "  [RETRY]", "error": "[ERR]",
            "task_done": f"[{task_done_console_label(data)}]",
        }.get(etype, "[-]")
        self._append_console(f"{prefix} {msg}\n")

    # ==================================================================
    # Plan cards helper
    # ==================================================================

    def _add_plan_card(self, text: str, status: str) -> None:
        ctk = self.ctk

        colors = {
            "pending": ("#f0f0f0", "#888"),
            "running": ("#e3f0ff", "#1a6fcf"),
            "ok": ("#e6f4ea", "#1e7e34"),
            "fail": ("#fce8e6", "#c5221f"),
            "retry": ("#fef7e0", "#b06000"),
            "stopped": ("#fce8e6", "#c5221f"),
        }
        emoji = {"pending": "○", "running": "▶", "ok": "✓", "fail": "✗", "retry": "↻", "stopped": "■"}
        bg, fg = colors.get(status, ("#f0f0f0", "#888"))

        card = ctk.CTkFrame(self.plan_inner, fg_color=bg, corner_radius=6)
        card.pack(fill="x", padx=5, pady=2)

        lbl = ctk.CTkLabel(
            card, text=f"  {emoji.get(status, '•')}  {text}",
            font=ctk.CTkFont(size=12), text_color=fg, anchor="w",
        )
        lbl.pack(fill="x", padx=8, pady=5)

        self.plan_cards.append(card)
        # 限制最多显示 20 条，旧的移出
        if len(self.plan_cards) > 20:
            old = self.plan_cards.pop(0)
            old.destroy()

    def _clear_plan_cards(self) -> None:
        for c in self.plan_cards:
            c.destroy()
        self.plan_cards.clear()

    # ==================================================================
    # Helpers
    # ==================================================================

    def _append_console(self, text: str) -> None:
        self.console.insert("end", text)
        self.console.see("end")

    def _set_status(self, text: str, color: str = "gray") -> None:
        self.status_bar.configure(text=text, text_color=color)

    def _on_close(self) -> None:
        if self.agent_thread:
            self.agent_thread.stop_task()
        if self._input_bridge.has_pending():
            self._input_bridge.cancel("窗口关闭")
        self._close_input_dialog(self._input_dialog)
        self.root.destroy()

    # ==================================================================
    # Launch
    # ==================================================================

    def run(self) -> None:
        self.root.mainloop()


def launch_local_ui(registry: ToolRegistry | None = None) -> None:
    if registry is None:
        registry = ToolRegistry()

    # 确保工具已注册
    if len(registry.list_names()) == 0:
        from src.tools.registry_init import register_all_tools
        register_all_tools(registry)

    app = CtkDesktopAgent(registry)
    app.run()
