"Planner — 将自然语言目标分解为可执行步骤序列"

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from src.schemas.task import Task, Step, RetryPolicy
from src.tools.base import ToolRegistry


class Planner:
    """任务规划器：使用 LLM 将用户的高层目标分解为有序工具调用序列。"""

    SYSTEM_PROMPT = """你是桌面 Agent 的任务规划器。根据用户目标和可用工具，输出 JSON 格式的执行步骤序列。

核心原则 — 完整闭环:
0. 规划时必须以用户目标的最终状态为终点，而非中间分析步骤
   - "在 X 中做 Y" → 最后一步必须是将结果写入/操作到 X 中，不能停在"分析/描述"步骤
   - 如果用户要求在某个应用中完成操作，确保包含启动该应用和在该应用中产出结果的步骤
   - 反面例子：用户要求"在记事本中描述桌面"→ 只有"截图+分析"却缺少"输入到记事本"是错误的

规则:
1. 每个步骤必须使用可用工具列表中的某个工具名
2. 参数必须从用户目标中推导，不能臆造
3. 步骤顺序必须符合操作逻辑（如"点击发布"必须在"写文章"之后）
4. 失败重试策略默认使用 "once"，仅对网络相关操作使用 "exponential"
5. 只在确定需要重试时使用 retry_policy，大多数步骤保持 "once"

关键规则 — 不要规划备选方案:
6. 每个步骤都是达成目标所必需的，不要把备选方案作为独立步骤
7. 禁止生成"如果 X 失败则尝试 Y"或"尝试 A→若不行用 B"这类回退步骤
8. 系统有自动重试和重新规划机制，失败时会自行生成替代方案，不需要你预先准备
9. 如果只需一步就能完成任务（如"打开浏览器"），只规划一个步骤

关键规则 — 网页表单操作（DOM 优先）:
10. 任何需要在网页中填写表单、点击按钮的操作，必须先规划 get_dom 步骤获取页面真实元素
11. get_dom 之后才能规划 click / type_text 步骤，且选择器必须能从 get_dom 返回的 DOM 中合理推导
12. 禁止凭空猜测 CSS selector（如 input[name='xxx']），因为实际页面的属性名通常不在你的训练数据中
13. type_text 和 click 工具支持三种策略，按可靠性排序：text > role > css
    - text 策略：通过可见标签文本或 placeholder 匹配（最稳定，推荐优先使用）
    - role 策略：通过 ARIA role 匹配
    - css 策略：仅当从 get_dom 中已确认元素有明确 id 时才使用
14. 示例 — 错误: "selector": "input[name='username']", "strategy": "css"
         正确: "selector": "手机号", "strategy": "text"

关键规则 — 当工具不匹配时:
15. 如果用户目标需要的操作在可用工具中完全没有对应项，不要强行匹配不相关的工具
16. 此时应返回空步骤列表并给出 fallback 说明:
   {"steps": [], "fallback": "无法完成：<具体原因>。建议：<替代方案或需要补充的工具>"}

关键规则 — 请求用户输入:
17. 如果遇到以下情况，请使用 request_user_input 工具请求用户手动输入:
    - 页面包含 CAPTCHA 验证码或图片验证码，无法自动识别
    - 需要输入手机/邮箱收到的验证码（2FA / 多因素认证步骤）
    - 自动填充失败的表单字段（selector 找不到或 type_text 反复失败）
    - 需要用户确认的选项或操作（如"是否同意服务条款"）
    - 需要用户提供的非公开信息（私人账号、密码等）
18. request_user_input 的 prompt 参数必须清晰说明:
    - 需要输入什么内容和格式
    - 为什么需要输入（遇到验证码、需要短信验证码等）
    - 在哪里可以看到所需信息（如"请查看屏幕中央的验证码图片"）
19. 使用 request_user_input 后，后续步骤可以直接引用返回的 user_input 数据
20. 引用 request_user_input 的返回值时，参数必须写成占位符 "{{user_input}}"，系统会在运行时替换为最近一次用户输入。
    - 例：先请求手机号，下一步 type_text 的 text 写 "{{user_input}}"
    - 如果之后又请求短信验证码，下一步同样写 "{{user_input}}"，它会引用最近一次输入

关键规则 — 登录页面:
21. 登录流程必须以 get_dom 看到的真实页面为准，不要假设一定是账号密码登录。
21a. 对任何"打开网页并登录"任务，打开目标网页后必须先调用 check_login_status 检查是否已登录；如果已登录，该工具会提前完成任务，后续不需要登录步骤。
22. 如果页面显示"验证码登录"、"获取验证码"、"短信验证码"或没有 password 输入框，应按验证码登录规划：
    - 请求用户输入手机号/账号
    - 输入手机号/账号
    - 点击获取验证码
    - 请求用户输入短信验证码
    - 输入短信验证码
    - 点击登录
23. 只有当 get_dom 明确显示 password 输入框、密码 placeholder、或"密码登录"表单已经打开时，才请求并输入密码。
24. 如果当前是验证码登录但用户目标说"登录指定账号"，不要切换到密码登录，优先完成当前页面提供的验证码登录流程。
25. 对知乎网页版登录，默认按手机号/短信验证码登录规划；不要默认请求知乎密码。只有用户明确要求密码登录，或当前 DOM 明确出现密码输入框时，才使用密码登录。
26. 只有在"点击获取/发送短信验证码"步骤已经成功之后，才可以请求用户输入短信验证码。
27. 如果点击获取验证码失败，不要立刻请求短信验证码；应先 get_dom 查看按钮真实文本/禁用状态，或请求用户手动完成人机验证/点击发送验证码。
28. 点击登录提交前，必须根据 get_dom 检查是否存在"同意协议/隐私政策/服务条款"复选框或按钮；如果存在且未勾选，先点击勾选，再点击登录。

输出格式:
{"steps": [{"tool_name": "...", "params": {...}, "description": "...", "expected_outcome": "...", "retry_policy": "once"}]}"""

    def __init__(self, llm_client: Any, registry: ToolRegistry) -> None:
        self.llm = llm_client
        self.registry = registry
        self.model: str = getattr(llm_client, "model", "claude-sonnet-4-6")

    def _tool_descriptions(self) -> str:
        lines: list[str] = []
        for name in self.registry.list_names():
            tool = self.registry.get(name)
            if tool:
                lines.append(f"- {name}: {tool.schema.description}")
                params = tool.schema.parameters
                if params and "properties" in params:
                    for pname, pinfo in params["properties"].items():
                        required = pname in params.get("required", [])
                        req_mark = "*" if required else ""
                        lines.append(f"    {pname}{req_mark}: {pinfo.get('description', '')}")
        return "\n".join(lines)

    async def plan(self, task: Task, context: str = "") -> tuple[list[Step], str]:
        """根据任务目标规划执行步骤。返回 (steps, fallback_reason)。"""
        tools_text = self._tool_descriptions()

        user_prompt = f"""用户目标: {task.goal}

当前上下文:
{context or '(无)'}

可用工具:
{tools_text}

请规划执行步骤 (JSON):"""

        try:
            data = await self._call_llm(user_prompt)
            steps, fallback = self._parse_steps(data)
            if fallback:
                logger.warning(f"Planner cannot fulfill task: {fallback}")
                return [], fallback
            logger.info(f"Planner generated {len(steps)} steps for task '{task.goal[:50]}...'")
            return steps, ""
        except Exception as exc:
            logger.error(f"Planning failed: {exc}")
            return [], str(exc)

    async def replan(self, task: Task, failed_step: Step, error_reason: str, context: str = "", remaining_steps: list[Step] | None = None) -> tuple[list[Step], str]:
        """在某个步骤失败后重新规划后续步骤。返回 (steps, fallback_reason)。"""
        tools_text = self._tool_descriptions()

        # 构建剩余步骤的描述
        remaining_text = ""
        if remaining_steps:
            lines = [f"  {i+1}. [{s.tool_name}] {s.description}" for i, s in enumerate(remaining_steps)]
            remaining_text = "\n".join(lines)
        else:
            remaining_text = "(无)"

        user_prompt = f"""用户目标: {task.goal}

一个步骤执行失败了，请规划替代步骤来完成任务。

失败的步骤: {failed_step.description}
使用的工具: {failed_step.tool_name}
参数: {failed_step.params}
失败原因: {error_reason}

原始计划中，失败步骤之后还有 {len(remaining_steps) if remaining_steps else 0} 个步骤:
{remaining_text}

重要规则:
- 规划 1~N 个替代步骤，优先只替换失败的那一步
- 如果后续原始步骤仍然有效（选择器/URL/参数不依赖失败步骤的输出），只需规划 1 个替代步骤即可，剩余原始步骤会自动保留
- 如果失败原因暴露了整体思路问题（如整个页面结构与预期不符），可以规划多个步骤一次性替换所有剩余步骤
- 不要使用与失败步骤相同的工具但只改一个参数值（如 wait_until / timeout），这不会解决问题
- 网页表单操作：如果失败原因是选择器找不到（如 "locator resolved to 0 elements"），应先规划 get_dom 获取页面真实元素，再用 text 策略定位输入框
- 如果错误是超时或网络相关（timeout / refused / unreachable / ENOTFOUND）或认证失败（401 / 403 / 无效的令牌），应返回 fallback 说明，不要继续尝试同类操作
- 如果失败步骤是 request_user_input 且原因包含"用户取消"，应返回 fallback 说明，不要尝试其他工具替代
- 如果密码输入失败且 DOM/页面是验证码登录，应改走手机号 + 短信验证码流程，不要继续寻找密码框
- 如果"获取验证码/发送验证码"按钮点击失败，不要请求用户输入短信验证码；应先 get_dom 检查按钮真实文本、禁用状态或人机验证提示。若存在 CAPTCHA/滑块/安全验证，则请求用户手动完成验证并确认。
- 如果登录按钮点击后页面无变化，应 get_dom 检查协议勾选、人机验证、按钮禁用状态或错误提示，不要直接判定任务完成。
- 真正有效的替代方案：换工具、换目标、换定位方式，不是换等待时间

当前上下文:
{context or '(无)'}

可用工具:
{tools_text}

请规划替代步骤，如果实在无法完成则返回 fallback (JSON):"""

        try:
            data = await self._call_llm(user_prompt)
            steps, fallback = self._parse_steps(data)
            if fallback:
                logger.warning(f"Replan cannot recover: {fallback}")
                return [], fallback
            # Safety: cap replanned steps (replan may legitimately replace
            # multiple remaining steps, but LLM shouldn't generate > 10)
            if len(steps) > 10:
                logger.warning(
                    f"Replan returned {len(steps)} steps, capping to first 10."
                )
                steps = steps[:10]
            logger.info(f"Replan generated {len(steps)} alternative steps")
            return steps, ""
        except Exception as exc:
            logger.error(f"Replan failed: {exc}")
            return [], str(exc)

    async def _call_llm(self, user_prompt: str) -> dict[str, Any]:
        """调用 LLM，返回解析后的 JSON"""
        resp = await self.llm.messages.create(
            model=self.model,
            max_tokens=2000,
            temperature=0.2,
            system=self.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = resp.content[0].text
        # 处理可能的 markdown code block 包裹
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)

    def _parse_steps(self, data: dict[str, Any]) -> tuple[list[Step], str]:
        """将 LLM 输出转为 Step 对象列表。返回 (steps, fallback_reason)。"""
        # 检查是否有 fallback（工具无法匹配时 LLM 会返回此字段）
        fallback = data.get("fallback", "")
        raw_steps: list[dict] = data.get("steps", [])
        steps: list[Step] = []
        for rs in raw_steps:
            retry = RetryPolicy.ONCE
            if rs.get("retry_policy") in {"linear", "exponential", "adaptive"}:
                retry = RetryPolicy(rs["retry_policy"])
            steps.append(Step(
                tool_name=rs["tool_name"],
                params=rs.get("params", {}),
                description=rs.get("description", ""),
                expected_outcome=rs.get("expected_outcome", ""),
                retry_policy=retry,
            ))
        return steps, fallback
