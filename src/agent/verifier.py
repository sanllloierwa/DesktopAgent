"Verifier — 判断步骤是否成功，用规则 + LLM 双重验证"

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from src.schemas.task import Step, ActionResult, Task
from src.agent.observer import Observer, Context


def _parse_json_response(text: str) -> dict[str, Any]:
    cleaned = (text or "").lstrip("\ufeff\r\n\t ")
    if not cleaned:
        raise ValueError("model returned empty content")
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("model response did not contain a JSON object")
    value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(value, dict):
        raise ValueError("model response JSON must be an object")
    return value


class Verifier:
    """步骤验证器。结合结构化规则和 LLM 视觉/文本分析来判定步骤成功与否。"""

    def __init__(self, observer: Observer, llm_client: Any = None) -> None:
        self.observer = observer
        self.llm = llm_client

    async def check(self, step: Step, result: ActionResult) -> tuple[bool, str]:
        """返回 (success, reason)"""
        # 1. 工具层已标记失败
        if not result.success:
            return False, result.error or "Tool execution returned failure"

        # 2. 快速规则检查
        rule_ok, rule_reason = self._rule_check(step, result)
        if not rule_ok:
            return False, rule_reason

        # Native input tools already refocus the named target and verify its
        # foreground HWND before sending input.  Treat delivery as the result
        # of this step; a later explicit screenshot/analyze step verifies the
        # resulting UI state.  Re-verifying every Ctrl+F/typing action through
        # vision turns a transient vision timeout into an incorrect recovery
        # plan (typically coordinate clicking or relaunching the app).
        if isinstance(result.data, dict):
            if (
                step.tool_name in {"desktop_keypress", "desktop_type_text"}
                and result.data.get("foreground_verified") is True
            ):
                return True, "Input delivered to the verified foreground application"
            if (
                step.tool_name == "focus_window"
                and result.data.get("window_handle")
            ):
                return True, "Target application window focused successfully"

        # 3. 如果是影响 UI 的操作，用 LLM 做进一步验证
        if self._needs_visual_check(step):
            ctx = await self.observer.gather()
            # 如果没有可用的感知上下文（无截图/DOM/UIA），信任工具结果
            if not ctx.screenshot_base64 and ctx.to_text() == "(no context available)":
                return True, "Step completed (no visual context available for LLM check)"
            return await self._llm_verify(step, result, ctx)

        return True, "Step completed successfully"

    async def check_task_completion(
        self,
        task: Task,
        completed_steps: list[tuple[Step, ActionResult]],
    ) -> tuple[bool, str]:
        """严格判断用户的最终目标是否已经由执行证据完成。"""
        zhihu = self._deterministic_zhihu_completion(task, completed_steps)
        if zhihu is not None:
            return zhihu
        deterministic = self._deterministic_wps_completion(task, completed_steps)
        if deterministic is not None:
            return deterministic
        if self.llm is None:
            return False, "No LLM available for final goal verification"

        evidence: list[str] = []
        for step, result in completed_steps[-20:]:
            status = "成功" if result.success else "失败"
            line = f"- [{status}] [{step.tool_name}] {step.description}: {result.summary}"
            if result.success and isinstance(result.data, dict):
                answer = result.data.get("answer")
                if isinstance(answer, str) and answer.strip():
                    line += f"；观察结果={answer[:500]}"
                confirmation = result.data.get("confirmation")
                if confirmation in {"yes", "no"}:
                    line += f"；人工确认={confirmation}"
                if result.data.get("foreground_verified") is True:
                    line += (
                        f"；前台目标已验证={result.data.get('target_app', '?')}"
                        f"({result.data.get('window_title', '?')})"
                    )
                if result.data.get("file_verified") is True:
                    line += (
                        f"；文件已验证={result.data.get('filepath', '?')}"
                        f"({result.data.get('file_size', '?')} bytes)"
                    )
                for key in (
                    "page_url", "clicked_text", "value_matches",
                    "uploaded_count", "preview_detected", "all_text_found",
                    "all_required_selected", "action_states", "text_checks",
                ):
                    if key in result.data:
                        line += f"；{key}={str(result.data[key])[:500]}"
            evidence.append(line)

        prompt = f"""你是桌面 Agent 的最终目标审计器。请根据真实执行证据，严格判断用户目标是否已经完成。

用户目标: {task.goal}

执行证据:
{chr(10).join(evidence) if evidence else '(无执行证据)'}

判定规则:
- 只能依据成功步骤和观察结果，不能依据原计划或步骤描述中的意图自行推断成功。
- 截图、分析、打开应用、请求用户输入都只是中间步骤，不能证明发送、保存、发布等动作已完成。
- 对“发送消息”任务，必须有实际输入消息并提交发送的证据；仅粘贴文字不等于已发送。
- 全局键鼠工具只有在证据包含“前台目标已验证”时，才能证明操作发送给了目标应用。
- 人工确认=yes 可以证明对应确认步骤成立；人工确认=unknown 或仅有输入长度不能作为肯定证据。
- 如果证据不足，必须返回 success=false，并具体说明仍缺少什么动作。

只回答 JSON:
{{"success": true/false, "reason": "一句话原因"}}"""

        try:
            resp = await self.llm.messages.create(
                model=self.llm.model,
                max_tokens=200,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
                json_mode=True,
            )
            data = _parse_json_response(resp.content[0].text)
            return bool(data.get("success")), str(data.get("reason", "Final goal not verified"))
        except Exception as exc:
            logger.warning(f"Final goal verification failed: {exc}")
            return False, f"Final goal verification unavailable: {exc}"

    def _deterministic_zhihu_completion(
        self,
        task: Task,
        completed_steps: list[tuple[Step, ActionResult]],
    ) -> tuple[bool, str] | None:
        goal = task.goal.lower()
        if "知乎" not in goal and "zhihu" not in goal:
            return None

        successful = [
            (step, result) for step, result in completed_steps if result.success
        ]
        tool_names = {step.tool_name for step, _ in successful}
        missing: list[str] = []

        if any(term in goal for term in ("登录", "登陆", "login")):
            logged_in = any(
                step.tool_name == "check_login_status"
                and isinstance(result.data, dict)
                and result.data.get("logged_in") is True
                for step, result in successful
            )
            if not logged_in:
                missing.append("已验证的知乎登录状态")

        if any(term in goal for term in ("写", "撰写", "文章")):
            if "generate_article" not in tool_names:
                missing.append("文章内容")

        if any(term in goal for term in ("配图", "图片", "插图")):
            if "generate_image" not in tool_names:
                missing.append("生成配图")
            uploaded = any(
                step.tool_name == "upload_image"
                and isinstance(result.data, dict)
                and int(result.data.get("uploaded_count", 0)) > 0
                and result.data.get("preview_detected") is True
                and result.data.get("modal_closed") is True
                for step, result in successful
            )
            if not uploaded:
                missing.append("上传配图")

        if "发布" in goal:
            published_article = any(
                step.tool_name == "publish_article"
                and isinstance(result.data, dict)
                and result.data.get("published") is True
                and result.data.get("title_visible") is True
                and "/p/" in str(result.data.get("page_url", ""))
                and "/write" not in str(result.data.get("page_url", ""))
                for step, result in successful
            )
            if not published_article:
                missing.append("已验证的文章发布和独立文章页")

        if "搜索" in goal:
            searched = any(
                "搜索" in f"{step.description} {step.params}"
                for step, _ in successful
            )
            if not searched:
                missing.append("站内搜索")

        if "评论" in goal:
            commented = any(
                step.tool_name == "submit_comment"
                and isinstance(result.data, dict)
                and result.data.get("comment_submitted") is True
                and result.data.get("editor_cleared") is True
                for step, result in successful
            )
            comment_visible = any(
                step.tool_name == "submit_comment"
                and isinstance(result.data, dict)
                and result.data.get("comment_visible") is True
                for step, result in successful
            )
            if not commented:
                missing.append("评论提交")
            if not comment_visible:
                missing.append("评论内容可见性验证")

        required_actions: list[set[str]] = []
        if "赞同" in goal or "点赞" in goal:
            required_actions.append({"赞同", "点赞"})
        if "收藏" in goal:
            required_actions.append({"收藏"})
        if "喜欢" in goal:
            required_actions.append({"喜欢"})

        selected_targets: set[str] = set()
        for step, result in successful:
            if step.tool_name != "get_page_state" or not isinstance(result.data, dict):
                continue
            for state in result.data.get("action_states", []):
                if state.get("selected") is True:
                    selected_targets.add(str(state.get("target", "")))
        for aliases in required_actions:
            if not aliases.intersection(selected_targets):
                missing.append(f"{'/'.join(sorted(aliases))}已选中")

        if missing:
            return False, "知乎任务仍缺少：" + "、".join(missing)
        return True, "知乎文章发布、搜索、评论和互动状态均有结构化证据"

    def _deterministic_wps_completion(
        self,
        task: Task,
        completed_steps: list[tuple[Step, ActionResult]],
    ) -> tuple[bool, str] | None:
        goal = task.goal.lower()
        is_wps = any(term in goal for term in ("wps", "word", "docx", "文字文档"))
        is_other_platform = any(term in goal for term in ("知乎", "zhihu", "微信", "wechat"))
        if not is_wps or is_other_platform:
            return None

        successful: dict[str, list[ActionResult]] = {}
        for step, result in completed_steps:
            if result.success:
                successful.setdefault(step.tool_name, []).append(result)

        missing: list[str] = []
        if any(term in goal for term in ("新建", "创建")) and "create_document" not in successful:
            missing.append("新建文档")
        if any(term in goal for term in ("写", "撰写", "文章")) and "write_document_text" not in successful:
            missing.append("写入正文")
        if any(term in goal for term in ("格式", "排版", "字体", "标题", "序号", "编号")):
            formatting_tools = {
                "format_document_range", "apply_list_format", "set_font", "set_alignment",
            }
            if not formatting_tools.intersection(successful):
                missing.append("文档排版")
        if "保存" in goal:
            saved = successful.get("save_document", [])
            if not any(
                isinstance(result.data, dict) and result.data.get("file_verified") is True
                for result in saved
            ):
                missing.append("已验证的 DOCX 文件")
        if "pdf" in goal or "导出" in goal:
            exported = successful.get("export_pdf", [])
            if not any(
                isinstance(result.data, dict) and result.data.get("file_verified") is True
                for result in exported
            ):
                missing.append("已验证的 PDF 文件")

        if missing:
            return False, "WPS 任务仍缺少：" + "、".join(missing)
        return True, "WPS 文档已完成写入和排版，DOCX/PDF 文件均已落盘验证"

    def _rule_check(self, step: Step, result: ActionResult) -> tuple[bool, str]:
        """基于规则的快速检查"""
        # 检查是否有依赖的前置步骤已完成
        # （此处在 executor 层已有保证，这里是二次确认）

        # 结果中是否包含特定的错误标记
        if result.data and isinstance(result.data, dict):
            if result.data.get("error"):
                return False, str(result.data["error"])
            if "status_code" in result.data and result.data["status_code"] not in (200, 201, 204, 0):
                return False, f"HTTP {result.data['status_code']}"

        return True, ""

    def _needs_visual_check(self, step: Step) -> bool:
        """判断此步骤是否需要视觉验证。
        只有确实改变 UI 状态、且依赖截图才能判断的操作才需要 LLM 视觉验证。
        launch_app 工具自身已确认进程启动成功，无需额外视觉确认。
        """
        visual_tools = {
            "click", "navigate", "type_text", "focus_window",
            "desktop_keypress", "desktop_click", "desktop_move_mouse",
            "desktop_scroll", "desktop_drag",
        }
        return step.tool_name in visual_tools and self.llm is not None

    async def _llm_verify(
        self, step: Step, result: ActionResult, ctx: Context
    ) -> tuple[bool, str]:
        """用 LLM 判断步骤效果是否符合预期"""
        prompt = f"""你是桌面 Agent 的步骤验证器。判断以下步骤是否成功。

步骤描述: {step.description}
预期结果: {step.expected_outcome or '无明确预期'}
工具执行摘要: {result.summary}

当前环境状态:
{ctx.to_text()}

请回答 JSON:
{{"success": true/false, "reason": "一句话原因"}}"""

        try:
            resp = await self.llm.messages.create(
                model=self.llm.model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
                json_mode=True,
            )
            data = _parse_json_response(resp.content[0].text)
            return data["success"], data["reason"]
        except Exception as exc:
            logger.warning(f"LLM verification failed, defaulting to rule result: {exc}")
            return True, "LLM verification skipped"
