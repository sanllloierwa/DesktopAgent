"""Pre-step guard rules for dynamic pages.

The guard performs cheap deterministic checks before a planned step runs. It
keeps obvious stale-plan cases out of the executor and only asks the planner to
replan when the current page contradicts the next step.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.agent.executor import Executor
from src.agent.memory import MemoryHub
from src.schemas.task import ActionResult, Step, Task


class GuardDecision(str, Enum):
    CONTINUE = "continue"
    SKIP = "skip"
    REPLAN = "replan"
    COMPLETE = "complete"


@dataclass
class GuardResult:
    decision: GuardDecision = GuardDecision.CONTINUE
    reason: str = ""
    action_result: ActionResult | None = None


class StepGuard:
    """Rule-based guard that checks whether the next planned step still fits."""

    def __init__(self, executor: Executor, memory: MemoryHub) -> None:
        self.executor = executor
        self.memory = memory

    async def before_step(self, task: Task, step: Step) -> GuardResult:
        if (
            self._is_zhihu_task(task)
            and self._currently_in_zhihu_editor()
            and self._is_redundant_zhihu_editor_entry(step)
        ):
            return GuardResult(
                decision=GuardDecision.SKIP,
                reason=(
                    "当前已在知乎文章编辑器，跳过重复的首页、创作菜单和"
                    "“写文章”入口步骤，继续处理现有草稿"
                ),
            )

        if (
            self._is_zhihu_task(task)
            and self._currently_in_zhihu_editor()
            and self._is_redundant_editor_field_verification(step)
            and self._editor_fields_already_verified(step)
        ):
            return GuardResult(
                decision=GuardDecision.SKIP,
                reason=(
                    "标题/正文输入工具已经逐字段验证实际值；编辑器 input 的值"
                    "不会出现在 body.innerText，跳过错误的页面正文文本复验"
                ),
            )

        if (
            self._is_zhihu_task(task)
            and self._verified_zhihu_publication()
            and self._is_redundant_post_publish_config(step)
        ):
            return GuardResult(
                decision=GuardDecision.SKIP,
                reason=(
                    "文章已经在独立 /p/ 页面完成发布验证，跳过发布设置、"
                    "协议和原创声明等编辑器阶段步骤，继续搜索和互动"
                ),
            )

        if (
            self._is_zhihu_task(task)
            and step.tool_name == "request_user_input"
            and self._is_local_image_path_request(step)
            and self._generated_image_paths_available()
        ):
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason=(
                    "generate_image 已返回本地 image_paths；不要向用户索要图片路径。"
                    "请直接调用 upload_image(image_paths="
                    "\"{{generate_image.image_paths}}\")，工具会兼容单选文件input逐张上传"
                ),
            )

        if (
            self._is_zhihu_task(task)
            and self._is_zhihu_topic_step(step)
            and not self._goal_requires_zhihu_topic(task)
        ):
            return GuardResult(
                decision=GuardDecision.SKIP,
                reason=(
                    "用户未要求添加知乎话题；话题为可选项，跳过并继续发布、"
                    "搜索、评论和互动流程"
                ),
            )

        if self._is_zhihu_task(task) and self._is_zhihu_publish_step(step):
            if self._verified_zhihu_publication():
                return GuardResult(
                    decision=GuardDecision.SKIP,
                    reason="知乎文章已经在独立文章页完成发布验证，跳过重复发布",
                )
            if step.tool_name == "click":
                return GuardResult(
                    decision=GuardDecision.REPLAN,
                    reason=(
                        "知乎文章发布必须使用 publish_article(title=文章标题)，"
                        "由工具清理残留弹窗并等待 /p/ 独立文章页；不得重复普通 click"
                    ),
                )

        if self._is_zhihu_task(task) and step.tool_name == "click":
            intended_action = self._intended_zhihu_action(step)
            if (
                intended_action
                and "评论" in task.goal
                and not self._verified_zhihu_comment()
            ):
                return GuardResult(
                    decision=GuardDecision.REPLAN,
                    reason=(
                        f"知乎动作“{intended_action}”必须等评论真实提交后执行；"
                        "请先使用 submit_comment，并确认 comment_submitted=true、"
                        "comment_visible=true、editor_cleared=true"
                    ),
                )
            selected_action = self._already_selected_zhihu_action(step)
            if selected_action:
                return GuardResult(
                    decision=GuardDecision.SKIP,
                    reason=f"知乎动作“{selected_action}”已经处于选中状态，跳过点击以避免取消",
                )

        if (
            self._is_wechat_task(task)
            and step.tool_name == "launch_app"
            and self._wechat_session_already_started()
        ):
            return GuardResult(
                decision=GuardDecision.SKIP,
                reason="微信会话已经启动并成功聚焦过，跳过重复启动以保留当前登录和搜索状态",
            )

        if (
            self._is_wechat_task(task)
            and step.tool_name == "close_app"
            and not self._goal_requires_closing_wechat(task)
        ):
            return GuardResult(
                decision=GuardDecision.SKIP,
                reason="用户未要求关闭微信；跳过关闭/重启恢复，保留当前登录会话",
            )

        if (
            self._is_wechat_task(task)
            and step.tool_name
            in {
                "desktop_keypress",
                "desktop_type_text",
                "desktop_click",
                "locate_screen_element",
            }
            and self._recent_wechat_view_is_login()
        ):
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason=(
                    "[WECHAT_LOGIN_REQUIRED] 最近的微信界面分析已确认当前是"
                    "扫码登录页；请请求用户完成登录，随后重新执行搜索并继续原任务"
                ),
            )

        if (
            self._is_wechat_task(task)
            and step.tool_name == "desktop_keypress"
            and str(step.params.get("keys", "")).strip().lower()
            in {"ctrl+f", "^f"}
            and self._latest_structured_wechat_login() is not True
        ):
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason=(
                    "[WECHAT_LOGIN_UNKNOWN] 尚未取得结构化的微信已登录证据；"
                    "搜索前必须截图并调用 check_wechat_login_status，"
                    "只有 logged_in=true 才能继续"
                ),
            )

        if self._is_wechat_task(task) and step.tool_name in {
            "focus_window",
            "desktop_keypress",
            "desktop_type_text",
            "desktop_click",
            "desktop_screenshot",
        }:
            app_name = str(step.params.get("app_name", "")).strip().lower()
            if app_name not in {"wechat", "weixin", "微信"}:
                return GuardResult(
                    decision=GuardDecision.REPLAN,
                    reason=(
                        f"[TARGET_REQUIRED] 微信桌面操作步骤 {step.tool_name} "
                        "必须设置 app_name='wechat'，以便操作前验证微信确实位于前台"
                    ),
                )

        if self._is_login_task(task) and self._login_already_satisfied():
            if self._is_login_flow_step(step):
                return GuardResult(
                    decision=GuardDecision.SKIP,
                    reason="网页登录状态已经满足，跳过重复登录步骤",
                )

        if (
            self._is_login_task(task)
            and self._has_navigated_to_page()
            and not self._has_checked_login_status()
        ):
            platform = self._platform_for_goal(task.goal)
            check = Step(
                tool_name="check_login_status",
                params={"platform": platform},
                description=f"检查 {platform} 是否已登录",
                expected_outcome="如果已登录则无需继续登录流程",
            )
            result = await self.executor.run(check)
            self.memory.commit(check, result)
            if result.success and isinstance(result.data, dict):
                if result.data.get("logged_in"):
                    decision = (
                        GuardDecision.SKIP
                        if self._is_login_flow_step(step)
                        else GuardDecision.CONTINUE
                    )
                    return GuardResult(
                        decision=decision,
                        reason=(
                            "网页登录状态已经满足，跳过当前重复登录步骤"
                            if decision == GuardDecision.SKIP
                            else "网页登录状态已经满足，继续执行复合任务"
                        ),
                        action_result=result,
                    )
            return GuardResult(decision=GuardDecision.CONTINUE, action_result=result)

        if self._is_password_step(step) and self._recent_dom_says_verification_login():
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason="计划要输入密码，但当前 DOM 显示为验证码登录或未发现密码输入框",
            )

        if self._is_sms_code_input_step(step) and not self._sms_code_was_sent():
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason="计划要输入短信验证码，但还没有成功发送验证码",
            )

        if self._is_login_submit_step(step) and self._recent_dom_says_agreement_needed():
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason="登录提交前可能需要先勾选同意协议/隐私政策",
            )

        return GuardResult()

    def _is_login_task(self, task: Task) -> bool:
        goal = task.goal.lower()
        return any(word in goal for word in ("登录", "登陆", "login", "sign in"))

    def _is_wechat_task(self, task: Task) -> bool:
        goal = task.goal.lower()
        return any(word in goal for word in ("微信", "wechat", "weixin"))

    def _goal_requires_closing_wechat(self, task: Task) -> bool:
        goal = task.goal.lower()
        return any(
            term in goal for term in ("关闭微信", "退出微信", "close wechat")
        )

    def _wechat_session_already_started(self) -> bool:
        target_names = {"wechat", "weixin", "微信"}
        for previous_step, result in reversed(
            self.memory.working.completed_steps
        ):
            if not result.success:
                continue
            app_name = str(
                previous_step.params.get("app_name", "")
            ).strip().lower()
            result_app = ""
            if isinstance(result.data, dict):
                result_app = str(
                    result.data.get(
                        "target_app",
                        result.data.get("app_name", ""),
                    )
                ).strip().lower()
            if (
                previous_step.tool_name
                in {
                    "launch_app",
                    "focus_window",
                    "desktop_keypress",
                    "desktop_type_text",
                    "desktop_click",
                    "desktop_screenshot",
                }
                and (app_name in target_names or result_app in target_names)
            ):
                return True
        return False

    def _recent_wechat_view_is_login(self) -> bool:
        for previous_step, result in reversed(
            self.memory.working.completed_steps
        ):
            if not result.success:
                continue
            if previous_step.tool_name == "check_wechat_login_status":
                if isinstance(result.data, dict):
                    logged_in = result.data.get("logged_in")
                    if logged_in is False:
                        return True
                    if logged_in is True:
                        return False
                continue
            if previous_step.tool_name != "analyze_screen":
                continue
            parts = [result.summary or ""]
            if isinstance(result.data, dict):
                answer = result.data.get("answer")
                if isinstance(answer, str):
                    parts.append(answer)
            text = " ".join(parts).lower()
            if any(
                term in text
                for term in (
                    "不是扫码登录",
                    "并非扫码登录",
                    "已进入微信主界面",
                    "已经进入微信主界面",
                    "微信已登录",
                    "登录成功",
                )
            ):
                return False
            return any(
                term in text
                for term in (
                    "微信扫码登录",
                    "扫码登录",
                    "扫码登录窗口",
                    "扫码登录界面",
                    "扫码登录页面",
                    "未登录的扫码",
                    "尚未登录",
                    "当前未登录",
                    "未登录",
                    "未登录状态",
                    "登录/切换账号",
                    "登录弹窗",
                )
            )
        return False

    def _latest_structured_wechat_login(self) -> bool | None:
        for previous_step, result in reversed(
            self.memory.working.completed_steps
        ):
            if (
                previous_step.tool_name != "check_wechat_login_status"
                or not result.success
                or not isinstance(result.data, dict)
            ):
                continue
            logged_in = result.data.get("logged_in")
            if isinstance(logged_in, bool):
                return logged_in
        return None

    def _is_zhihu_task(self, task: Task) -> bool:
        goal = task.goal.lower()
        return "知乎" in goal or "zhihu" in goal

    def _already_selected_zhihu_action(self, step: Step) -> str:
        intended = self._intended_zhihu_action(step)
        if not intended:
            return ""
        aliases = {intended}
        if intended in {"赞同", "点赞"}:
            aliases.update({"赞同", "点赞"})
        for previous_step, result in reversed(self.memory.working.completed_steps):
            if previous_step.tool_name != "get_page_state" or not result.success:
                continue
            if not isinstance(result.data, dict):
                continue
            for state in result.data.get("action_states", []):
                target = str(state.get("target", ""))
                if target in aliases and state.get("selected") is True:
                    return intended
            break
        return ""

    def _intended_zhihu_action(self, step: Step) -> str:
        text = f"{step.description} {step.expected_outcome} {step.params}"
        actions = ("赞同", "点赞", "收藏", "喜欢")
        return next((action for action in actions if action in text), "")

    def _verified_zhihu_comment(self) -> bool:
        for previous_step, result in reversed(self.memory.working.completed_steps):
            if previous_step.tool_name != "submit_comment" or not result.success:
                continue
            if not isinstance(result.data, dict):
                continue
            if (
                result.data.get("comment_submitted") is True
                and result.data.get("comment_visible") is True
                and result.data.get("editor_cleared") is True
            ):
                return True
        return False

    def _is_zhihu_publish_step(self, step: Step) -> bool:
        if step.tool_name == "publish_article":
            return True
        text = f"{step.description} {step.expected_outcome} {step.params}"
        selector = str(step.params.get("selector", "")).strip()
        return (
            step.tool_name == "click"
            and "发布" in text
            and not any(term in text for term in ("评论", "想法", "回答"))
            and (
                selector == "发布"
                or any(term in text for term in ("文章", "作品", "发布按钮"))
            )
        )

    def _verified_zhihu_publication(self) -> bool:
        for previous_step, result in reversed(self.memory.working.completed_steps):
            if not result.success:
                continue
            if not isinstance(result.data, dict):
                continue
            page_url = str(result.data.get("page_url", ""))
            structured_publish = (
                previous_step.tool_name == "publish_article"
                and result.data.get("published") is True
                and result.data.get("title_visible") is True
            )
            visible_title_state = (
                previous_step.tool_name == "get_page_state"
                and result.data.get("all_text_found") is True
                and bool(result.data.get("text_checks"))
            )
            if (
                (structured_publish or visible_title_state)
                and "/p/" in page_url
                and "/write" not in page_url
            ):
                return True
        return False

    def _is_zhihu_topic_step(self, step: Step) -> bool:
        text = f"{step.description} {step.expected_outcome} {step.params}"
        return any(term in text for term in (
            "添加话题", "选择话题", "补充话题", "话题标签",
        ))

    def _goal_requires_zhihu_topic(self, task: Task) -> bool:
        return any(term in task.goal for term in (
            "添加话题", "选择话题", "补充话题", "话题标签",
        ))

    def _is_local_image_path_request(self, step: Step) -> bool:
        text = f"{step.description} {step.expected_outcome} {step.params}"
        return any(term in text for term in (
            "本地图片路径", "提供图片路径", "选择一张本地图片",
            "图片的完整绝对路径",
        ))

    def _generated_image_paths_available(self) -> bool:
        for previous_step, result in reversed(self.memory.working.completed_steps):
            if previous_step.tool_name != "generate_image" or not result.success:
                continue
            if not isinstance(result.data, dict):
                continue
            paths = result.data.get("image_paths")
            if isinstance(paths, list) and paths:
                return True
        return False

    def _platform_for_goal(self, goal: str) -> str:
        if "知乎" in goal or "zhihu" in goal.lower():
            return "zhihu"
        return "generic"

    def _has_checked_login_status(self) -> bool:
        return any(step.tool_name == "check_login_status" for step, _ in self.memory.working.completed_steps)

    def _login_already_satisfied(self) -> bool:
        for step, result in reversed(self.memory.working.completed_steps):
            if step.tool_name != "check_login_status" or not result.success:
                continue
            if isinstance(result.data, dict) and result.data.get("logged_in") is True:
                return True
        return False

    def _is_login_flow_step(self, step: Step) -> bool:
        if step.tool_name == "check_login_status":
            return True
        if step.tool_name not in {
            "request_user_input", "type_text", "click",
            "get_dom", "get_page_state", "navigate",
        }:
            return False
        text = f"{step.description} {step.expected_outcome} {step.params}".lower()
        login_terms = (
            "登录", "登陆", "login", "sign in", "手机号", "手机号码",
            "短信验证码", "获取验证码", "发送验证码", "password", "密码",
            "同意协议", "隐私政策", "服务条款",
        )
        return any(term in text for term in login_terms)

    def _current_browser_url(self) -> str:
        for _step, result in reversed(self.memory.working.completed_steps):
            if not result.success or not isinstance(result.data, dict):
                continue
            for key in ("page_url", "url"):
                value = result.data.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
        return ""

    def _currently_in_zhihu_editor(self) -> bool:
        url = self._current_browser_url().lower()
        return (
            "zhuanlan.zhihu.com/write" in url
            or (
                "zhuanlan.zhihu.com/p/" in url
                and "/edit" in url
            )
        )

    def _is_redundant_zhihu_editor_entry(self, step: Step) -> bool:
        text = f"{step.description} {step.expected_outcome} {step.params}"
        if step.tool_name == "navigate":
            url = str(step.params.get("url", "")).rstrip("/").lower()
            return (
                "zhuanlan.zhihu.com/write" in url
                or url in {"https://www.zhihu.com", "https://zhihu.com"}
            )
        if step.tool_name == "click":
            return (
                "写文章" in text
                or (
                    "创作" in text
                    and any(term in text for term in ("展开", "下拉", "顶部导航"))
                )
            )
        if step.tool_name == "get_dom":
            return "下拉菜单" in text and any(
                term in text for term in ("写文章", "创作")
            )
        return False

    def _is_redundant_editor_field_verification(self, step: Step) -> bool:
        if step.tool_name != "get_page_state":
            return False
        text = f"{step.description} {step.expected_outcome} {step.params}"
        return "编辑器" in text and any(term in text for term in ("标题", "正文"))

    def _editor_fields_already_verified(self, verification_step: Step) -> bool:
        text = f"{verification_step.description} {verification_step.params}"
        required = {
            kind for kind, terms in {
                "title": ("标题",),
                "body": ("正文", "内容"),
            }.items() if any(term in text for term in terms)
        }
        if not required:
            return False
        verified: set[str] = set()
        for previous_step, result in self.memory.working.completed_steps:
            if previous_step.tool_name != "type_text" or not result.success:
                continue
            if not isinstance(result.data, dict):
                continue
            kind = str(result.data.get("field_kind", ""))
            if result.data.get("value_matches") is True and kind in {"title", "body"}:
                verified.add(kind)
        return required.issubset(verified)

    def _is_redundant_post_publish_config(self, step: Step) -> bool:
        if step.tool_name not in {"click", "get_dom", "get_page_state"}:
            return False
        text = f"{step.description} {step.expected_outcome} {step.params}"
        return any(term in text for term in (
            "发布设置", "发布配置", "协议同意", "同意协议",
            "原创声明", "点击同意", "协议复选框",
        ))

    def _has_navigated_to_page(self) -> bool:
        return any(
            step.tool_name == "navigate" and result.success
            for step, result in self.memory.working.completed_steps
        )

    def _recent_dom(self) -> str:
        for step, result in reversed(self.memory.working.completed_steps):
            if step.tool_name == "get_dom" and result.success and result.summary:
                return result.summary
        return ""

    def _recent_text(self) -> str:
        parts: list[str] = []
        for step, result in self.memory.working.completed_steps[-8:]:
            parts.append(step.description)
            parts.append(result.summary or "")
            if isinstance(result.data, dict):
                summary = result.data.get("summary")
                if isinstance(summary, str):
                    parts.append(summary)
        return "\n".join(parts)

    def _is_password_step(self, step: Step) -> bool:
        text = f"{step.description} {step.params}".lower()
        return "密码" in text or "password" in text

    def _recent_dom_says_verification_login(self) -> bool:
        dom = self._recent_dom()
        if not dom:
            return False
        has_password = "password" in dom.lower() or "密码" in dom
        has_verification = any(word in dom for word in ("验证码登录", "获取验证码", "短信验证码", "验证码"))
        return has_verification and not has_password

    def _is_sms_code_input_step(self, step: Step) -> bool:
        text = f"{step.description} {step.params}"
        return "验证码" in text and step.tool_name in {"type_text", "desktop_type_text"}

    def _sms_code_was_sent(self) -> bool:
        sent_words = (
            "发送短信验证码", "发送验证码", "获取短信验证码", "获取验证码",
        )
        for step, result in reversed(self.memory.working.completed_steps):
            text = f"{step.description}\n{result.summary}"
            if step.tool_name == "click" and result.success and any(word in text for word in sent_words):
                return True
        return False

    def _is_login_submit_step(self, step: Step) -> bool:
        text = f"{step.description} {step.params}"
        return step.tool_name == "click" and "登录" in text

    def _recent_dom_says_agreement_needed(self) -> bool:
        dom = self._recent_dom()
        if not dom:
            return False
        has_agreement = any(word in dom for word in ("同意", "协议", "隐私", "服务条款"))
        has_unchecked = any(word in dom.lower() for word in ('type="checkbox"', 'aria-checked="false"', 'checked="false"'))
        return has_agreement and has_unchecked
