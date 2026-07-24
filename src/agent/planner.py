"Planner — 将自然语言目标分解为可执行步骤序列"

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from loguru import logger

from src.schemas.task import Task, Step, RetryPolicy
from src.tools.base import ToolRegistry
from src.utils.config import load_config


def _parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM response.

    JSON mode should normally return a bare object, but this also tolerates
    Markdown fences and a short explanatory prefix from other providers.
    """
    cleaned = (text or "").lstrip("\ufeff\r\n\t ")
    if not cleaned:
        raise ValueError(
            "规划模型返回了空内容；请检查模型输出额度，或关闭 Kimi 思考模式后重试。"
        )

    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            cleaned = cleaned[first_newline + 1:]

    start = cleaned.find("{")
    if start < 0:
        preview = cleaned[:120].replace("\n", " ")
        raise ValueError(f"规划模型未返回 JSON 对象：{preview}")

    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"规划模型返回的 JSON 不完整或格式错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError("规划模型返回的 JSON 顶层必须是对象")
    return value


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
8a. 执行计划是线性的，不支持“若/如果/视情况”条件步骤；每一步都必须可以直接执行。
    只有用户目标明确要求“登录”时，才可预先加入登录人工确认。
    用户只要求操作已安装客户端时，不要额外规划“若未登录则请求用户登录”；
    未登录应由后续实际操作失败触发重新规划。
9. 如果只需一步就能完成任务（如"打开浏览器"），只规划一个步骤
9a. 当用户目标是 Word/WPS/docx 文档时，generate_article 必须使用 {"output_format": "plain_text"}；
    写入 Word/WPS 时使用 write_document_text，content_format 使用 "auto" 或 "plain_text"，
    不要把 Markdown 标记（#、**、```、| 表格等）写入 Word 文档。
9a-1. WPS/Word 排版应使用稳定文档范围，不能依赖写入结束后的当前光标：
    - create_document 会通过 COM 自动连接或启动 WPS/Word 并显示窗口，通常不需要先调用 launch_app；
    - 标题应单独调用 write_document_text(text="{{generate_article.title}}", style="title")；
    - 正文使用 write_document_text(text="{{generate_article.body}}", style="body")；
      禁止把 {{generate_article.article}} 或 {{generate_article}} 同时写入标题和正文；
    - 写入完成后使用 format_document_range(target="title") 设置标题字体、字号和居中；
    - 使用 format_document_range(target="body") 设置正文两端对齐、行距、段间距和首行缩进；
    - 只修改部分段落时使用 target="paragraphs" 并给出 1-based 的起止段落；
    - 项目符号或自动编号使用 apply_list_format，不要把手工数字当作自动编号；
    - 排版完成后再 save_document；只有用户明确给出路径时才传 filepath，否则省略该参数，
      禁止臆造 C:\\Users\\xxx、公共桌面、D 盘等路径；
    - 用户要求 PDF 时，保存成功后调用 export_pdf；若用户未明确 PDF 路径，同样省略 filepath，
      让工具自动使用实际成功保存的 DOCX 同目录同名路径。
9b. 当任务需要看屏幕、识别图形界面或根据截图判断状态时，先调用 desktop_screenshot，
    再调用 analyze_screen，并将 image_base64 设置为 "{{desktop_screenshot.screenshot_base64}}"。
    analyze_screen 会通过 MCP 将图片交给 Agnes 视觉模型；主模型不直接读取 base64 图片。
    微信任务的 desktop_screenshot 必须设置 app_name="wechat"，只截取已聚焦的微信窗口，
    禁止截取整个桌面后把其他应用误判成微信。

关键规则 — 原生桌面应用:
9c. 浏览器工具 click、type_text、get_dom 只能操作 Playwright 浏览器页面，严禁用于微信、Word、记事本等原生桌面窗口。
9d. 操作原生应用前先调用 focus_window；使用 desktop_keypress 发送快捷键/Enter/Tab，使用 desktop_type_text 粘贴文本。
    desktop_keypress 和 desktop_type_text 必须设置 app_name，工具会在每次输入前重新聚焦并验证目标窗口；
    微信统一使用 app_name="wechat"。缺少 app_name 的桌面输入步骤无效。
9e. 需要视觉坐标点击时，必须严格规划以下链路，不能从 analyze_screen 的自然语言回答中猜坐标：
    禁止根据自然语言描述或历史截图臆造坐标。
    1) desktop_screenshot 获取图片及 width/height/left/top；
    2) locate_screen_element 定位目标，参数必须引用：
       image_base64="{{desktop_screenshot.screenshot_base64}}"、
       image_width="{{desktop_screenshot.width}}"、image_height="{{desktop_screenshot.height}}"、
       screen_left="{{desktop_screenshot.left}}"、screen_top="{{desktop_screenshot.top}}"；
    3) desktop_click 的 x/y/confidence 必须分别引用
       "{{locate_screen_element.x}}"、"{{locate_screen_element.y}}"、"{{locate_screen_element.confidence}}"；
       操作微信时 desktop_click 还必须设置 app_name="wechat"，点击前重新验证前台窗口；
    4) 点击后重新 desktop_screenshot + analyze_screen，验证界面变化。
    定位失败、低置信度或坐标越界时不要点击，应重新截图或请求用户确认。
9e-1. desktop_move_mouse 用于悬停，desktop_scroll 用于滚动，desktop_drag 用于拖拽；执行后同样必须重新截图验证。
9e-2. 发送、删除、发布、支付等操作只有在用户目标明确要求时才能执行；否则点击最终确认按钮前调用 request_user_input。
9f. 对微信搜索、进入聊天或发送消息的任务，规划必须形成以下闭环：
    - 全程最多调用一次 launch_app 启动微信；后续失败不得 close_app、重启微信或重复 launch_app；
    - focus_window 聚焦微信，然后使用 desktop_screenshot(app_name="wechat") 截图，
      紧接着调用 check_wechat_login_status；只有 logged_in=true 才能继续搜索；
    - 如果任务明确要求登录，使用 request_user_input 请用户在微信客户端完成扫码或手机确认，完成后仅回复“已登录”；
      不要询问登录方式，不要索要微信账号、手机号或密码；
      如果任务未明确要求登录，不得预先规划“如需登录则等待用户”这种条件步骤；只有工具已确认停在登录页时才能重新规划人工登录；
    - 用户确认后重新 focus_window、定向截图并再次调用 check_wechat_login_status，
      确认 logged_in=true 后才能继续；
    - focus_window 后用 desktop_keypress(keys="ctrl+f", app_name="wechat") 打开搜索，
      desktop_keypress(keys="ctrl+a", app_name="wechat") 清空搜索框，
      desktop_type_text(text=目标名称, app_name="wechat") 输入目标名称，
      desktop_keypress(keys="enter", app_name="wechat") 执行搜索；
      搜索框、搜索图标和搜索按钮不得再用截图坐标定位；
    - 定向截图并分析搜索结果；只有选择具体联系人、群聊、公众号/服务号等结果时才使用视觉定位；
      如果当前是搜索建议下拉层且只显示目标名称，应先定位并点击名称完全匹配的可点击建议，
      不得因为建议层尚未显示“服务号”类型标签就判定目标不存在；进入下一层后再核对账号类型；
      微信名称候选优先使用 open_wechat_search_candidate，将定位和点击作为一个原子操作，
      防止定位成功后又被插入清空或重新搜索步骤；
      新版搜一搜顶部使用“账号”分类承载公众号/服务号结果，旧版才显示“公众号”；
      如果可用标签中有“账号”但没有“公众号”，必须切换“账号”并点击名称完全匹配的结果，
      不得因为缺少“公众号”标签而判定目标不存在；进入资料页后再核验是否为服务号；
    - 进入目标页面后定向截图并分析，确认标题和目标类型正确；
    - focus_window 后用 desktop_type_text(text=消息正文, app_name="wechat") 输入消息，
      再用 desktop_keypress(keys="enter", app_name="wechat") 提交发送；
    - 最后截图并分析消息是否出现在聊天记录中（截图必须绑定微信窗口）。最后一步不能停在登录状态、搜索结果或待发送输入框。

关键规则 — 网页表单操作（DOM 优先）:
9g. 网页任务直接从 navigate 开始。navigate 会复用现有 CDP 页面或自行启动持久化浏览器；
    不要在 navigate 前规划 launch_app 启动 Chrome/Edge，否则可能重复打开浏览器窗口或标签页。
10. 任何需要在网页中填写表单、点击按钮的操作，必须先规划 get_dom 步骤获取页面真实元素
    11. get_dom 之后才能规划 click / type_text 步骤，且选择器必须能从 get_dom 返回的 DOM 中合理推导
    11a. get_dom 只返回可供规划器阅读的 summary 文本，不会返回 login_element、selector 等结构化字段。
         禁止生成 "{{get_dom_login_element}}"、"{{get_dom.selector}}" 等占位符；
         click/type_text 的 selector 必须直接填写从 DOM summary 中看到的真实文本、placeholder、role 或 id。
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
21a. 对任何"打开网页并登录"任务，打开目标网页后必须先调用 check_login_status 检查是否已登录；
     如果已登录，只跳过登录子流程。写作、发布、评论等复合任务必须继续执行，不能提前完成整个任务。
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

关键规则 — 知乎文章闭环:
29. 知乎写文章任务必须使用 generate_article 分别取得 title/body；标题输入
    "{{generate_article.title}}" 且 type_text 必须设置 field_kind="title"；
    正文输入 "{{generate_article.body}}" 且必须设置 field_kind="body"。
    不要把完整 article 同时写入标题和正文，不要用通用 role="textbox" 的第一个元素猜测正文位置。
29a. 知乎文章任务只允许一次进入编辑器：直接 navigate 到
     "https://zhuanlan.zhihu.com/write"；不要点击首页“创作/写文章”菜单。
    一旦进入编辑器，不得再返回首页或重复导航编辑器。generate_article 必须位于所有
     "{{generate_article.*}}" 引用之前，generate_image 必须位于 upload_image 之前。
     type_text 已验证 value_matches 后，不要在编辑器中使用
     get_page_state(text_contains=[标题/正文])；input/contenteditable 值不等于页面正文文本。
30. 用户要求配图时，调用 generate_image：
    topic 使用任务主题，article_text="{{generate_article.body}}"，count 使用用户要求的数量；
    然后先点击编辑器的图片控件并 get_dom 确认 file input，再调用
    upload_image(image_paths="{{generate_image.image_paths}}")。upload_image 会自动关闭残留上传 Modal；
    若网页 file input 不支持 multiple，upload_image 会自动逐张上传，规划器仍须传完整 image_paths；
    不要生成 image_paths[0]、image_paths.[0] 等拆分步骤，也不要向用户索要本地图片路径。
    不要额外规划截图、视觉坐标或重复点击关闭按钮。没有成功上传图片不得发布。
31. 知乎发布、评论、赞同、收藏、喜欢都必须形成“操作前 get_dom/get_page_state → 执行动作
    → get_page_state 复验”的闭环：
    - 发布必须使用 publish_article(title="{{generate_article.title}}")，不得用普通 click 点击发布；
      publish_article 会清理 Modal、等待跳转并返回已验证的 /p/ 独立文章 URL。
      成功后可再用 get_page_state(text_contains=["{{generate_article.title}}"]) 复验；
    - 评论必须使用 submit_comment(comment=实际评论内容)，不得使用 type_text + 普通 click 代替；
      随后使用 get_page_state(posted_comment_contains=[同一条实际评论内容]) 复验。
      只有 submit_comment 返回 comment_submitted=true、comment_visible=true、editor_cleared=true
      后，才能继续赞同、收藏、喜欢；
    - 赞同/收藏/喜欢动作前先 get_page_state(targets=[动作名])；
      动作后使用 get_page_state(require_selected=[动作名])，不能只根据 click 成功判定。
32. 收藏可能打开收藏夹弹窗；看到弹窗后必须 get_dom，选择收藏夹并确认，再验证收藏已选中。
33. 发布后优先使用 get_page_state.page_url 重新打开文章做验证。用户明确要求站内搜索时，
    再用文章标题搜索；搜索结果中必须同时核对标题和当前账号/作者，不能点击相似标题。
34. 如果发布账号无法对自己的文章执行赞同或喜欢，不要重复点击；返回明确 fallback，
    说明该动作需要第二个已授权测试账号，不得伪造成功。
35. 知乎动作文本以真实 DOM 为准：“点赞”可能显示为“赞同”。任何状态切换操作都要先读状态，
    已经 selected 时跳过点击，避免重试将其取消。
36. 知乎“添加话题”是可选操作。只有用户明确要求添加/选择话题或话题标签时才规划；
    未要求时不要添加。若页面已经位于标题可见的 /p/ 独立文章页，说明发布已完成，
    直接继续搜索、评论、赞同、收藏、喜欢，不得返回编辑器补话题或重复发布。

输出格式:
{"steps": [{"tool_name": "...", "params": {...}, "description": "...", "expected_outcome": "...", "retry_policy": "once"}]}"""

    def __init__(self, llm_client: Any, registry: ToolRegistry) -> None:
        self.llm = llm_client
        self.registry = registry
        self.model: str = getattr(llm_client, "model", "claude-sonnet-4-6")
        self.max_tokens = max(2000, int(load_config().llm.max_tokens))

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

        task_context = json.dumps(task.context, ensure_ascii=False, default=str)
        user_prompt = f"""用户目标: {task.goal}

平台/任务结构化上下文:
{task_context if task.context else '(无)'}

当前上下文:
{context or '(无)'}

可用工具:
{tools_text}

请规划执行步骤 (JSON):"""

        try:
            data = await self._call_llm(user_prompt)
            steps, fallback = self._parse_steps(data)
            if fallback.startswith("[PLAN_SCHEMA]"):
                logger.warning(
                    f"Planner returned invalid step schema; requesting one repair: {fallback}"
                )
                repair_prompt = (
                    user_prompt
                    + "\n\n上一次响应的步骤结构无效。请重新输出完整 JSON；"
                      "steps 数组中的每个对象都必须包含非空 tool_name，"
                      "且 tool_name 必须逐字匹配上方可用工具名称。"
                )
                data = await self._call_llm(repair_prompt)
                steps, fallback = self._parse_steps(data)
            if fallback:
                logger.warning(f"Planner cannot fulfill task: {fallback}")
                return [], fallback
            steps = self._normalize_platform_steps(task, steps)
            violation = self._goal_violation(task, steps)
            if violation:
                logger.warning(
                    f"Planner produced incomplete/goal-violating plan; "
                    f"requesting one repair: {violation}"
                )
                repair_prompt = (
                    user_prompt
                    + "\n\n上一次计划没有覆盖用户的最终目标："
                    + violation
                    + "。请重新输出从启动到最终状态的完整 steps；"
                      "不能停在截图、分析、识别或待发送输入框。"
                )
                data = await self._call_llm(repair_prompt)
                steps, fallback = self._parse_steps(data)
                if fallback:
                    return [], fallback
                steps = self._normalize_platform_steps(task, steps)
                violation = self._goal_violation(task, steps)
                if violation:
                    logger.warning(
                        f"Repaired plan still violates goal: {violation}"
                    )
                    return [], violation
            logger.info(f"Planner generated {len(steps)} steps for task '{task.goal[:50]}...'")
            return steps, ""
        except Exception as exc:
            logger.error(f"Planning failed: {exc}")
            return [], str(exc)

    async def replan(
        self,
        task: Task,
        failed_step: Step,
        error_reason: str,
        context: str = "",
        remaining_steps: list[Step] | None = None,
    ) -> tuple[list[Step], str, bool]:
        """重新规划失败步骤，返回 (steps, fallback_reason, preserve_remaining)。"""
        login_recovery = self._wechat_login_recovery(
            task,
            failed_step,
            error_reason,
        )
        if login_recovery is not None:
            logger.info(
                "Using deterministic WeChat login recovery after confirmed login screen"
            )
            return login_recovery, "", True
        suggestion_recovery = self._wechat_search_suggestion_recovery(
            task,
            failed_step,
            error_reason,
        )
        if suggestion_recovery is not None:
            logger.info(
                "Using deterministic WeChat search-suggestion recovery"
            )
            return suggestion_recovery, "", True

        tools_text = self._tool_descriptions()

        # 构建剩余步骤的描述
        remaining_text = ""
        if remaining_steps:
            lines = [f"  {i+1}. [{s.tool_name}] {s.description}" for i, s in enumerate(remaining_steps)]
            remaining_text = "\n".join(lines)
        else:
            remaining_text = "(无)"

        task_context = json.dumps(task.context, ensure_ascii=False, default=str)
        user_prompt = f"""用户目标: {task.goal}

平台/任务结构化上下文:
{task_context if task.context else '(无)'}

一个步骤执行失败了，请规划替代步骤来完成任务。

失败的步骤: {failed_step.description}
使用的工具: {failed_step.tool_name}
参数: {failed_step.params}
失败原因: {error_reason}

原始计划中，失败步骤之后还有 {len(remaining_steps) if remaining_steps else 0} 个步骤:
{remaining_text}

重要规则:
- 规划 1~N 个替代步骤，优先只替换失败的那一步
- 替代步骤最多10个；每个 description/expected_outcome 保持一句话，避免输出冗长解释
- 返回 preserve_remaining=true 表示替代步骤只修复当前失败，原始后续步骤仍有效，应继续执行
- 返回 preserve_remaining=false 表示返回的步骤已经完整替换从失败点开始的全部流程，原始后续步骤必须丢弃
- 如果后续原始步骤仍然有效（选择器/URL/参数不依赖失败步骤的输出），通常只需规划 1 个替代步骤并设置 preserve_remaining=true
- 如果失败原因暴露了整体思路问题（如整个页面结构与预期不符），应规划完整的新后续流程并设置 preserve_remaining=false
- 替代步骤必须真正实现失败步骤的预期结果；只截图、分析或检查状态但没有完成原操作，不是有效替代方案
- 不要使用与失败步骤相同的工具但只改一个参数值（如 wait_until / timeout），这不会解决问题
- 网页表单操作：如果失败原因是选择器找不到（如 "locator resolved to 0 elements"），应先规划 get_dom 获取页面真实元素，再用 text 策略定位输入框
- 如果错误是超时或网络相关（timeout / refused / unreachable / ENOTFOUND）或认证失败（401 / 403 / 无效的令牌），应返回 fallback 说明，不要继续尝试同类操作
- 如果失败原因含 [VISION_TIMEOUT]，不要重复同一个视觉步骤；最多改用一次人工确认并保留后续非视觉操作。
- 如果失败原因含 [VISION_UNAVAILABLE]，说明本任务的视觉熔断器已经打开：禁止继续规划 analyze_screen 或 locate_screen_element；如确有必要，只请求一次人工确认，然后继续 focus_window、desktop_keypress、desktop_type_text 等非视觉步骤。
- 当前上下文中已经有成功的微信登录人工确认时，不要再次规划 request_user_input 确认同一登录状态。
- 微信已成功启动或聚焦后，禁止通过 close_app、强制结束进程、重复 launch_app 来恢复；
  聚焦失败时应再次 focus_window 或请求一次人工置前，保留现有会话。
- 如果失败步骤是 request_user_input 且原因包含"用户取消"，应返回 fallback 说明，不要尝试其他工具替代
- 如果密码输入失败且 DOM/页面是验证码登录，应改走手机号 + 短信验证码流程，不要继续寻找密码框
- 如果"获取验证码/发送验证码"按钮点击失败，不要请求用户输入短信验证码；应先 get_dom 检查按钮真实文本、禁用状态或人机验证提示。若存在 CAPTCHA/滑块/安全验证，则请求用户手动完成验证并确认。
- 如果登录按钮点击后页面无变化，应 get_dom 检查协议勾选、人机验证、按钮禁用状态或错误提示，不要直接判定任务完成。
- 知乎上传图片后若 Modal 阻挡发布，只调用 dismiss_modal 一次或直接调用 publish_article；
  禁止规划截图、视觉坐标、连续 Escape、重复关闭和重复普通 click 发布。
- generate_image 已成功并返回 image_paths 时，上传失败应继续使用完整
  "{{generate_image.image_paths}}" 调用 upload_image；该工具会兼容单选 input 逐张上传。
  禁止把列表拆成 image_paths[0] 占位符，也禁止 request_user_input 索要本地图片路径。
- 原生桌面应用不能使用浏览器 click/type_text/get_dom。微信任务应使用 focus_window、desktop_keypress、desktop_type_text、desktop_screenshot 和 analyze_screen。
- 微信的 desktop_keypress、desktop_type_text、desktop_click 和 desktop_screenshot 必须包含 app_name="wechat"；如果失败原因含 [TARGET_REQUIRED]，修复参数后保留后续步骤。
- 微信搜索框一律用 Ctrl+F、Ctrl+A、输入关键词、Enter；禁止为搜索框/搜索图标/搜索按钮规划视觉定位或坐标点击。
- 视觉坐标操作必须使用 desktop_screenshot → locate_screen_element → desktop_click → desktop_screenshot → analyze_screen；禁止根据自然语言描述或历史截图臆造坐标。
- 微信消息任务的替代计划必须最终进入目标聊天、输入消息并用 Enter 提交；仅截图或判断登录状态不是有效恢复。
- 微信新版搜一搜使用“账号”而非“公众号”作为账号分类；看到“账号”标签和名称完全匹配的
  目标时，应调用 open_wechat_search_candidate 进入资料页，不能继续寻找不存在的“公众号”标签。
- 真正有效的替代方案：换工具、换目标、换定位方式，不是换等待时间

当前上下文:
{context or '(无)'}

可用工具:
{tools_text}

请规划替代步骤，如果实在无法完成则返回 fallback。
JSON 格式：
{{"steps": [{{"tool_name": "...", "params": {{}}, "description": "...", "expected_outcome": "..."}}], "preserve_remaining": true/false}}
或 {{"steps": [], "fallback": "无法恢复的原因", "preserve_remaining": false}}:"""

        try:
            data = await self._call_llm(user_prompt)
            steps, fallback, preserve_remaining = self._parse_replan(data)
            if fallback.startswith("[PLAN_SCHEMA]"):
                logger.warning(
                    f"Replan returned invalid step schema; requesting one repair: {fallback}"
                )
                repair_prompt = (
                    user_prompt
                    + "\n\n上一次响应的步骤结构无效。请重新输出完整 JSON；"
                      "steps 数组中的每个对象都必须包含非空 tool_name，"
                      "且 tool_name 必须逐字匹配上方可用工具名称。"
                )
                data = await self._call_llm(repair_prompt)
                steps, fallback, preserve_remaining = self._parse_replan(data)
            if fallback:
                logger.warning(f"Replan cannot recover: {fallback}")
                return [], fallback, False
            steps = self._normalize_platform_steps(task, steps)
            validation_steps = (
                steps + list(remaining_steps or [])
                if preserve_remaining
                else steps
            )
            violation = self._goal_violation(task, validation_steps)
            if violation:
                logger.warning(f"Replan produced goal-violating plan: {violation}")
                return [], violation, False
            # Safety: cap replanned steps (replan may legitimately replace
            # multiple remaining steps, but LLM shouldn't generate > 10)
            if len(steps) > 10:
                logger.warning(
                    f"Replan returned {len(steps)} steps, capping to first 10."
                )
                steps = steps[:10]
            logger.info(f"Replan generated {len(steps)} alternative steps")
            return steps, "", preserve_remaining
        except Exception as exc:
            logger.error(f"Replan failed: {exc}")
            return [], str(exc), False

    async def _call_llm(self, user_prompt: str) -> dict[str, Any]:
        """调用 LLM，返回解析后的 JSON"""
        prompts = [
            user_prompt,
            (
                user_prompt
                + "\n\n上一次 JSON 响应因过长而被截断。请重新输出更精简的完整 JSON："
                  "替代步骤不超过10个，description 和 expected_outcome 各不超过30字，"
                  "不要输出解释、Markdown 或未在工具结果中定义的占位符。"
            ),
        ]
        last_error: Exception | None = None
        for attempt, prompt in enumerate(prompts):
            resp = await self.llm.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.2,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                json_mode=True,
            )
            text = resp.content[0].text
            try:
                return _parse_json_object(text)
            except ValueError as exc:
                last_error = exc
                if attempt == 0 and "不完整或格式错误" in str(exc):
                    logger.warning("Planner JSON was truncated; retrying with a compact-output prompt")
                    continue
                raise
        raise last_error or ValueError("规划模型未返回可解析的 JSON")

    def _goal_violation(self, task: Task, steps: list[Step]) -> str:
        goal = task.goal.lower()
        if "知乎" in goal or "zhihu" in goal:
            for step in steps:
                if self._is_generic_zhihu_publish_click(step):
                    return "知乎文章发布必须使用 publish_article，不能使用普通 click"
        if any(term in goal for term in ("微信", "wechat", "weixin")):
            wechat_violation = self._wechat_goal_violation(task, steps)
            if wechat_violation:
                return wechat_violation
        requires_word = any(word in goal for word in ("word", "wps", "docx", "word文档", "word 文档"))
        if not requires_word:
            return ""

        for step in steps:
            params_text = json.dumps(step.params, ensure_ascii=False).lower()
            text = f"{step.tool_name} {step.description} {params_text}".lower()
            if "notepad" in text or "记事本" in text:
                return (
                    "用户目标要求 Word/WPS 文档，不能自动降级为记事本。"
                    "请安装/配置 Word 或 WPS，或明确允许改用记事本。"
                )
        return ""

    @staticmethod
    def _wechat_goal_violation(task: Task, steps: list[Step]) -> str:
        goal = task.goal.lower()
        step_texts = [
            f"{step.description} {step.expected_outcome} {step.params}".lower()
            for step in steps
        ]
        search_index = next(
            (
                index
                for index, step in enumerate(steps)
                if step.tool_name == "desktop_keypress"
                and Planner._desktop_keys(step) in {"ctrl+f", "^f"}
            ),
            None,
        )
        if "搜索" in task.goal and search_index is not None:
            login_check_index = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step.tool_name == "check_wechat_login_status"
                ),
                None,
            )
            if (
                login_check_index is None
                or login_check_index > search_index
            ):
                return (
                    "微信搜索前必须截图并调用 check_wechat_login_status，"
                    "只有 logged_in=true 才能继续"
                )

        if "服务号" in goal:
            open_index = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step.tool_name == "open_wechat_search_candidate"
                ),
                None,
            )
            result_index = next(
                (
                    index
                    for index, (step, text) in enumerate(zip(steps, step_texts))
                    if step.tool_name == "locate_screen_element"
                    and "服务号" in text
                ),
                None,
            )
            if result_index is None and open_index is None:
                return "微信计划必须定位类型为服务号的具体结果，不能停在分析或识别"
            if result_index is not None and not any(
                step.tool_name == "desktop_click"
                for step in steps[result_index + 1:]
            ):
                return "微信计划定位服务号后必须点击进入该结果"

        if "关注" in goal and not any(
            step.tool_name == "desktop_click" and "关注" in text
            for step, text in zip(steps, step_texts)
        ):
            return "微信计划缺少点击关注服务号的步骤"

        requires_message = any(
            term in goal for term in ("私信", "发送消息", "发消息", "发送一段文字")
        )
        if requires_message:
            message_input_index = next(
                (
                    index
                    for index, (step, text) in enumerate(zip(steps, step_texts))
                    if step.tool_name == "desktop_type_text"
                    and any(
                        term in text
                        for term in (
                            "私信", "消息正文", "消息内容", "输入消息",
                            "发送内容", "文字内容",
                        )
                    )
                ),
                None,
            )
            if message_input_index is None:
                return "微信计划缺少在目标会话中输入私信正文的步骤"
            if not any(
                step.tool_name == "desktop_keypress"
                and Planner._desktop_keys(step) in {"enter", "return"}
                for step in steps[message_input_index + 1:]
            ):
                return "微信计划输入私信后必须使用 Enter 提交发送"

        return ""

    def _normalize_platform_steps(
        self,
        task: Task,
        steps: list[Step],
    ) -> list[Step]:
        """Repair safe, unambiguous platform-tool mismatches in model plans."""

        goal = task.goal.lower()
        if any(term in goal for term in ("微信", "wechat", "weixin")):
            steps = self._normalize_wechat_steps(task, steps)

        if "知乎" not in goal and "zhihu" not in goal:
            return steps

        normalized: list[Step] = []
        for step in steps:
            if self._is_generic_zhihu_publish_click(step):
                logger.info(
                    "Normalized generic Zhihu publish click to publish_article"
                )
                normalized.append(replace(
                    step,
                    tool_name="publish_article",
                    params={"title": "{{generate_article.title}}"},
                    description="发布知乎文章并等待独立文章页",
                    expected_outcome="返回标题可见的 /p/ 独立文章URL",
                ))
                continue
            normalized.append(step)
        normalized = self._normalize_zhihu_editor_entry(normalized)
        normalized = self._move_producer_before_references(
            normalized,
            "generate_article",
        )
        normalized = self._move_producer_before_references(
            normalized,
            "generate_image",
        )
        return normalized

    def _normalize_wechat_steps(
        self,
        task: Task,
        steps: list[Step],
    ) -> list[Step]:
        """Keep WeChat plans on one foreground-verified keyboard workflow."""

        goal = task.goal.lower()
        goal_requires_login = any(
            term in goal for term in ("登录", "登陆", "login", "sign in")
        )
        goal_requires_close = any(
            term in goal for term in ("关闭微信", "退出微信", "close wechat")
        )
        has_ctrl_f = any(
            step.tool_name == "desktop_keypress"
            and self._desktop_keys(step) in {"ctrl+f", "^f"}
            for step in steps
        )
        first_launch_kept = False
        normalized: list[Step] = []

        for step in steps:
            if self._is_wechat_login_analysis_step(step):
                image_ref = step.params.get(
                    "image_base64",
                    "{{desktop_screenshot.screenshot_base64}}",
                )
                step = replace(
                    step,
                    tool_name="check_wechat_login_status",
                    params={"image_base64": image_ref},
                    description="结构化检查微信登录状态",
                    expected_outcome="仅在微信主界面返回 logged_in=true",
                )
                logger.info(
                    "Normalized free-text WeChat login analysis to structured check"
                )

            if step.tool_name == "launch_app":
                if first_launch_kept:
                    logger.info("Dropped duplicate WeChat launch step")
                    continue
                first_launch_kept = True

            if step.tool_name == "close_app" and not goal_requires_close:
                logger.info("Dropped destructive WeChat restart/close recovery step")
                continue

            if (
                step.tool_name == "request_user_input"
                and not goal_requires_login
                and self._is_wechat_login_prompt(step)
                and not self._is_confirmed_wechat_login_prompt(step)
            ):
                logger.info("Dropped conditional WeChat login prompt")
                continue

            if self._is_wechat_search_control_visual_step(step):
                logger.info("Dropped visual WeChat search-control step")
                continue

            if step.tool_name in {
                "focus_window",
                "desktop_keypress",
                "desktop_type_text",
                "desktop_click",
                "desktop_screenshot",
            }:
                params = dict(step.params)
                params["app_name"] = "wechat"
                step = replace(step, params=params)

            normalized.append(step)

        if "搜索" in task.goal and not has_ctrl_f:
            insert_at = next(
                (
                    index
                    for index, step in enumerate(normalized)
                    if (
                        step.tool_name == "desktop_keypress"
                        and self._desktop_keys(step) in {"ctrl+a", "^a"}
                    )
                    or (
                        step.tool_name == "desktop_type_text"
                        and any(
                            term in f"{step.description} {step.expected_outcome}"
                            for term in ("搜索关键词", "搜索关键字", "目标名称")
                        )
                    )
                ),
                None,
            )
            if insert_at is not None:
                normalized.insert(
                    insert_at,
                    Step(
                        tool_name="desktop_keypress",
                        params={"keys": "ctrl+f", "app_name": "wechat"},
                        description="使用快捷键打开微信搜索",
                        expected_outcome="微信搜索输入框获得焦点",
                    ),
                )
                logger.info("Inserted canonical Ctrl+F WeChat search entry")

        return self._normalize_wechat_candidate_open(task, normalized)

    def _normalize_wechat_candidate_open(
        self,
        task: Task,
        steps: list[Step],
    ) -> list[Step]:
        target_name = self._extract_wechat_search_target(task)
        if not target_name:
            return steps
        result: list[Step] = []
        index = 0
        while index < len(steps):
            step = steps[index]
            following = steps[index + 1] if index + 1 < len(steps) else None
            text = f"{step.description} {step.expected_outcome} {step.params}"
            is_candidate = (
                step.tool_name == "locate_screen_element"
                and target_name in text
                and any(term in text for term in ("服务号", "公众号", "搜索结果", "候选"))
                and not any(term in text for term in ("关注按钮", "发消息", "发送按钮"))
            )
            clicks_located_target = (
                following is not None
                and following.tool_name == "desktop_click"
                and (
                    "locate_screen_element" in json.dumps(
                        following.params,
                        ensure_ascii=False,
                        default=str,
                    )
                    or any(
                        term in f"{following.description} {following.expected_outcome}"
                        for term in ("搜索结果", "服务号结果", "候选项")
                    )
                )
            )
            if is_candidate and clicks_located_target:
                params = {
                    "image_base64": step.params.get(
                        "image_base64",
                        "{{desktop_screenshot.screenshot_base64}}",
                    ),
                    "target_name": target_name,
                    "expected_type": (
                        "service_account"
                        if "服务号" in task.goal
                        else "public_account"
                        if "公众号" in task.goal
                        else "any"
                    ),
                    "image_width": step.params.get(
                        "image_width",
                        "{{desktop_screenshot.width}}",
                    ),
                    "image_height": step.params.get(
                        "image_height",
                        "{{desktop_screenshot.height}}",
                    ),
                    "screen_left": step.params.get(
                        "screen_left",
                        "{{desktop_screenshot.left}}",
                    ),
                    "screen_top": step.params.get(
                        "screen_top",
                        "{{desktop_screenshot.top}}",
                    ),
                }
                result.append(Step(
                    tool_name="open_wechat_search_candidate",
                    params=params,
                    description=f"定位并立即打开“{target_name}”候选项",
                    expected_outcome="名称匹配的候选项已打开，随后核对服务号类型",
                ))
                logger.info(
                    "Collapsed WeChat candidate locate+click into atomic open tool"
                )
                index += 2
                continue
            result.append(step)
            index += 1
        return result

    @staticmethod
    def _desktop_keys(step: Step) -> str:
        return str(
            step.params.get("keys", step.params.get("key", ""))
        ).strip().lower().replace(" ", "")

    @staticmethod
    def _is_wechat_login_prompt(step: Step) -> bool:
        text = (
            f"{step.description} {step.expected_outcome} {step.params}"
        ).lower()
        return any(
            term in text
            for term in (
                "微信登录", "微信客户端是否已登录", "扫码", "手机确认登录",
                "已登录", "wechat login",
            )
        )

    @staticmethod
    def _is_confirmed_wechat_login_prompt(step: Step) -> bool:
        text = (
            f"{step.description} {step.expected_outcome} {step.params}"
        ).lower()
        return "[wechat_login_required]" in text

    def _wechat_login_recovery(
        self,
        task: Task,
        failed_step: Step,
        error_reason: str,
    ) -> list[Step] | None:
        """Resume the same task after UI evidence confirms WeChat is logged out."""

        goal = task.goal.lower()
        if not any(term in goal for term in ("微信", "wechat", "weixin")):
            return None
        if failed_step.tool_name == "request_user_input":
            return None
        reason = error_reason.lower()
        login_evidence = (
            "[wechat_login_required]",
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
        if not any(term in reason for term in login_evidence):
            return None

        if (
            failed_step.tool_name == "desktop_keypress"
            and self._desktop_keys(failed_step) in {"ctrl+f", "^f"}
        ):
            return [
                Step(
                    tool_name="request_user_input",
                    params={
                        "prompt": (
                            "已确认微信当前处于扫码登录页。请在微信客户端完成扫码或"
                            "手机确认登录，进入微信主界面后回复“已登录”。"
                        ),
                    },
                    description="[WECHAT_LOGIN_REQUIRED] 等待用户完成微信扫码登录",
                    expected_outcome="用户确认微信已进入主界面",
                ),
                Step(
                    tool_name="focus_window",
                    params={"app_name": "wechat"},
                    description="登录后重新聚焦微信窗口",
                    expected_outcome="微信主窗口位于前台",
                ),
                Step(
                    tool_name="desktop_screenshot",
                    params={"app_name": "wechat"},
                    description="截取登录后的微信窗口",
                    expected_outcome="获得微信主界面截图",
                ),
                Step(
                    tool_name="check_wechat_login_status",
                    params={
                        "image_base64": "{{desktop_screenshot.screenshot_base64}}",
                    },
                    description="结构化确认微信登录完成",
                    expected_outcome="返回 logged_in=true 后才继续搜索",
                ),
                replace(
                    failed_step,
                    params={
                        **failed_step.params,
                        "app_name": "wechat",
                    },
                    description="登录后打开微信搜索",
                    expected_outcome="微信搜索框获得焦点",
                ),
            ]

        target = self._extract_wechat_search_target(task)
        if not target:
            return [
                Step(
                    tool_name="request_user_input",
                    params={
                        "prompt": (
                            "已确认微信当前处于扫码登录页。请在微信客户端完成扫码或"
                            "手机确认登录，进入微信主界面后回复“已登录”。"
                        ),
                    },
                    description="[WECHAT_LOGIN_REQUIRED] 等待用户完成微信扫码登录",
                    expected_outcome="用户确认微信已进入主界面",
                ),
                Step(
                    tool_name="focus_window",
                    params={"app_name": "wechat"},
                    description="登录后重新聚焦微信窗口",
                    expected_outcome="微信主窗口位于前台",
                ),
            ]

        target_kind = "服务号" if "服务号" in task.goal else (
            "公众号" if "公众号" in task.goal else "目标"
        )
        return [
            Step(
                tool_name="request_user_input",
                params={
                    "prompt": (
                        "已确认微信当前处于扫码登录页。请在微信客户端完成扫码或"
                        "手机确认登录，进入微信主界面后回复“已登录”。"
                    ),
                },
                description="[WECHAT_LOGIN_REQUIRED] 等待用户完成微信扫码登录",
                expected_outcome="用户确认微信已进入主界面",
            ),
            Step(
                tool_name="focus_window",
                params={"app_name": "wechat"},
                description="登录后重新聚焦微信窗口",
                expected_outcome="微信主窗口位于前台",
            ),
            Step(
                tool_name="desktop_screenshot",
                params={"app_name": "wechat"},
                description="截取登录后的微信窗口",
                expected_outcome="获得微信主界面截图",
            ),
            Step(
                tool_name="check_wechat_login_status",
                params={
                    "image_base64": "{{desktop_screenshot.screenshot_base64}}",
                },
                description="结构化确认微信登录完成",
                expected_outcome="返回 logged_in=true 后才继续搜索",
            ),
            Step(
                tool_name="desktop_keypress",
                params={"keys": "ctrl+f", "app_name": "wechat"},
                description="重新打开微信搜索",
                expected_outcome="微信搜索框获得焦点",
            ),
            Step(
                tool_name="desktop_keypress",
                params={"keys": "ctrl+a", "app_name": "wechat"},
                description="清空微信搜索框",
                expected_outcome="搜索框为空",
            ),
            Step(
                tool_name="desktop_type_text",
                params={"text": target, "app_name": "wechat"},
                description=f"重新输入搜索关键词“{target}”",
                expected_outcome="搜索框显示目标名称",
            ),
            Step(
                tool_name="desktop_keypress",
                params={"keys": "enter", "app_name": "wechat"},
                description="重新执行微信搜索",
                expected_outcome="显示微信搜索结果",
            ),
            Step(
                tool_name="desktop_screenshot",
                params={"app_name": "wechat"},
                description="截取重新搜索后的结果",
                expected_outcome="获得包含目标结果的微信截图",
            ),
            Step(
                tool_name="locate_screen_element",
                params={
                    "image_base64": "{{desktop_screenshot.screenshot_base64}}",
                    "target": f'名称为“{target}”且类型为{target_kind}的结果',
                    "image_width": "{{desktop_screenshot.width}}",
                    "image_height": "{{desktop_screenshot.height}}",
                    "screen_left": "{{desktop_screenshot.left}}",
                    "screen_top": "{{desktop_screenshot.top}}",
                },
                description=f"重新定位“{target}”{target_kind}结果",
                expected_outcome="返回目标结果的可靠屏幕坐标",
            ),
        ]

    def _wechat_search_suggestion_recovery(
        self,
        task: Task,
        failed_step: Step,
        error_reason: str,
    ) -> list[Step] | None:
        """Relax an over-specific service-account target on suggestion layers."""

        goal = task.goal.lower()
        if not any(term in goal for term in ("微信", "wechat", "weixin")):
            return None
        if failed_step.tool_name != "locate_screen_element":
            return None
        reason = error_reason.lower()
        suggestion_terms = (
            "搜索建议",
            "建议下拉",
            "相关搜索关键词",
            "未显示任何带有",
            "未显示服务号",
            "类型标识",
        )
        if not any(term in reason for term in suggestion_terms):
            return None
        target = self._extract_wechat_search_target(task)
        if not target:
            return None
        return [
            Step(
                tool_name="locate_screen_element",
                params={
                    "image_base64": "{{desktop_screenshot.screenshot_base64}}",
                    "target": (
                        f'搜索建议或搜索结果中名称与“{target}”完全匹配的'
                        "可点击候选项；此步骤不要求候选项同时显示服务号标签"
                    ),
                    "image_width": "{{desktop_screenshot.width}}",
                    "image_height": "{{desktop_screenshot.height}}",
                    "screen_left": "{{desktop_screenshot.left}}",
                    "screen_top": "{{desktop_screenshot.top}}",
                },
                description=f"定位名称完全匹配的“{target}”候选项",
                expected_outcome="返回可点击候选项坐标，进入后再核对服务号类型",
            ),
        ]

    @staticmethod
    def _extract_wechat_search_target(task: Task) -> str:
        for key in ("account", "target", "query", "name", "keyword"):
            value = task.context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        quoted = re.search(r'[“"「『]([^”"」』]+)[”"」』]', task.goal)
        if quoted:
            return quoted.group(1).strip()
        match = re.search(
            r"搜索\s*[：:]?\s*([^，,。；;\s]+)",
            task.goal,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _is_wechat_search_control_visual_step(step: Step) -> bool:
        if step.tool_name not in {
            "desktop_screenshot",
            "analyze_screen",
            "locate_screen_element",
            "desktop_click",
        }:
            return False
        text = f"{step.description} {step.expected_outcome} {step.params}"
        control_terms = (
            "搜索框", "搜索图标", "搜索按钮", "搜索入口",
            "触发搜索", "打开微信搜索",
        )
        return any(term in text for term in control_terms)

    @staticmethod
    def _is_wechat_login_analysis_step(step: Step) -> bool:
        if step.tool_name != "analyze_screen":
            return False
        text = (
            f"{step.description} {step.expected_outcome} {step.params}"
        ).lower()
        return any(
            term in text
            for term in (
                "微信登录状态",
                "微信是否登录",
                "是否已登录微信",
                "分析微信登录",
                "确认微信登录",
            )
        )

    def _normalize_zhihu_editor_entry(self, steps: list[Step]) -> list[Step]:
        entry_indexes = [
            index for index, step in enumerate(steps)
            if self._is_zhihu_editor_entry_step(step)
        ]
        if not entry_indexes:
            return steps

        first_entry = entry_indexes[0]
        publish_indexes = [
            index for index, step in enumerate(steps)
            if step.tool_name == "publish_article"
        ]
        publish_index = publish_indexes[0] if publish_indexes else len(steps)
        result: list[Step] = []
        for index, step in enumerate(steps):
            if index == first_entry:
                result.append(replace(
                    step,
                    tool_name="navigate",
                    params={
                        "url": "https://zhuanlan.zhihu.com/write",
                        "wait_until": "domcontentloaded",
                    },
                    description="直接进入知乎文章编辑器",
                    expected_outcome="标题和正文编辑区加载完成",
                ))
                continue
            if index in entry_indexes:
                logger.info("Dropped duplicate Zhihu editor-entry step")
                continue
            if (
                first_entry < index < publish_index
                and self._is_zhihu_home_navigation(step)
            ):
                logger.info("Dropped navigation away from an active Zhihu draft")
                continue
            if (
                first_entry < index < publish_index
                and self._is_stale_zhihu_editor_menu_observation(step)
            ):
                logger.info("Dropped stale Zhihu editor-menu observation")
                continue
            if (
                first_entry < index < publish_index
                and self._is_stale_editor_field_page_check(step)
            ):
                logger.info("Dropped invalid editor body-text verification")
                continue
            result.append(step)
        return result

    @staticmethod
    def _is_zhihu_editor_entry_step(step: Step) -> bool:
        text = f"{step.description} {step.expected_outcome} {step.params}"
        if step.tool_name == "navigate":
            return "zhuanlan.zhihu.com/write" in str(step.params.get("url", ""))
        if step.tool_name != "click":
            return False
        if "写文章" in text:
            return True
        return "创作" in text and any(
            term in text for term in ("展开", "下拉", "进入文章编辑器")
        )

    @staticmethod
    def _is_zhihu_home_navigation(step: Step) -> bool:
        if step.tool_name != "navigate":
            return False
        url = str(step.params.get("url", "")).rstrip("/")
        return url in {"https://www.zhihu.com", "https://zhihu.com"}

    @staticmethod
    def _is_stale_zhihu_editor_menu_observation(step: Step) -> bool:
        if step.tool_name != "get_dom":
            return False
        text = f"{step.description} {step.expected_outcome}"
        return "下拉菜单" in text and any(
            term in text for term in ("写文章", "创作")
        )

    @staticmethod
    def _is_stale_editor_field_page_check(step: Step) -> bool:
        if step.tool_name != "get_page_state":
            return False
        text = f"{step.description} {step.expected_outcome} {step.params}"
        return "编辑器" in text and any(term in text for term in ("标题", "正文"))

    @staticmethod
    def _move_producer_before_references(
        steps: list[Step],
        producer_tool: str,
    ) -> list[Step]:
        producer_index = next(
            (
                index for index, step in enumerate(steps)
                if step.tool_name == producer_tool
            ),
            None,
        )
        if producer_index is None:
            return steps
        reference = f"{{{{{producer_tool}."
        consumer_index = next(
            (
                index for index, step in enumerate(steps)
                if index != producer_index
                and reference in json.dumps(
                    step.params,
                    ensure_ascii=False,
                    default=str,
                )
            ),
            None,
        )
        if consumer_index is None or producer_index < consumer_index:
            return steps
        reordered = list(steps)
        producer = reordered.pop(producer_index)
        reordered.insert(consumer_index, producer)
        logger.info(
            f"Moved {producer_tool} before its first parameter reference"
        )
        return reordered

    @staticmethod
    def _is_generic_zhihu_publish_click(step: Step) -> bool:
        if step.tool_name != "click":
            return False
        text = f"{step.description} {step.expected_outcome} {step.params}"
        if "发布" not in text or any(
            term in text for term in ("评论", "想法", "回答")
        ):
            return False
        selector = str(step.params.get("selector", "")).strip()
        return (
            selector == "发布"
            or any(term in text for term in ("文章", "作品", "发布按钮"))
        )

    def _parse_steps(self, data: dict[str, Any]) -> tuple[list[Step], str]:
        """将 LLM 输出转为 Step 对象列表。返回 (steps, fallback_reason)。"""
        fallback = str(data.get("fallback", "") or "")
        raw_steps: Any = data.get("steps")
        if raw_steps is None:
            raw_steps = data.get("plan", data.get("actions", []))
        if isinstance(raw_steps, dict):
            raw_steps = list(raw_steps.values())
        if not isinstance(raw_steps, list):
            return [], (
                fallback
                or "[PLAN_SCHEMA] 规划结果中的 steps 必须是对象数组"
            )

        registry = getattr(self, "registry", None)
        available_tools = (
            set(registry.list_names()) if registry is not None else set()
        )
        steps: list[Step] = []
        invalid: list[str] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                invalid.append(f"第 {index} 步不是对象")
                continue
            rs = raw_step
            nested = rs.get("action")
            if isinstance(nested, dict):
                rs = {**rs, **nested}
            tool_field = (
                rs.get("tool_name")
                or rs.get("tool")
                or rs.get("name")
                or rs.get("工具")
            )
            if isinstance(tool_field, dict):
                tool_field = (
                    tool_field.get("name")
                    or tool_field.get("tool_name")
                )
            tool_name = str(tool_field or "").strip()
            if not tool_name:
                invalid.append(f"第 {index} 步缺少 tool_name")
                continue
            if available_tools and tool_name not in available_tools:
                invalid.append(f"第 {index} 步使用未知工具 {tool_name!r}")
                continue

            params = rs.get(
                "params",
                rs.get("arguments", rs.get("参数", {})),
            )
            if not isinstance(params, dict):
                invalid.append(f"第 {index} 步的 params 不是对象")
                continue
            retry = RetryPolicy.ONCE
            if rs.get("retry_policy") in {"linear", "exponential", "adaptive"}:
                retry = RetryPolicy(rs["retry_policy"])
            steps.append(Step(
                tool_name=tool_name,
                params=params,
                description=str(
                    rs.get("description", rs.get("描述", ""))
                ),
                expected_outcome=str(
                    rs.get(
                        "expected_outcome",
                        rs.get("expected", rs.get("预期结果", "")),
                    )
                ),
                retry_policy=retry,
            ))

        if invalid:
            logger.warning(
                "Ignored invalid planner step objects: " + "; ".join(invalid)
            )
        if not steps and not fallback:
            detail = "; ".join(invalid) or "steps 数组为空"
            fallback = f"[PLAN_SCHEMA] 没有可执行的有效步骤：{detail}"
        return steps, fallback

    def _parse_replan(self, data: dict[str, Any]) -> tuple[list[Step], str, bool]:
        """解析重规划结果；兼容未返回新字段的旧模型响应。"""
        steps, fallback = self._parse_steps(data)
        preserve_remaining = data.get("preserve_remaining")
        if not isinstance(preserve_remaining, bool):
            # 旧响应通常用单步替换失败步骤、多步替换整个剩余流程。
            preserve_remaining = len(steps) == 1 and not fallback
        return steps, fallback, preserve_remaining
