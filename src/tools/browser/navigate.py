"""Browser automation tools — Playwright-based"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.utils.config import load_config


def _text_pattern(selector: str) -> re.Pattern[str]:
    if selector in {"获取验证码", "发送验证码", "获取短信验证码", "发送短信验证码"}:
        return re.compile(r"(获取|发送|收取).{0,6}(验证码|校验码)|短信验证码", re.IGNORECASE)
    return re.compile(re.escape(selector), re.IGNORECASE)


async def _try_connect_cdp(pw, max_retries: int = 10, delay: float = 0.5):
    """尝试通过 CDP 连接到系统 Chrome（由 LaunchAppTool 以 --remote-debugging-port=9222 启动）。

    返回 Browser 对象，连接失败返回 None。
    """
    for i in range(max_retries):
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            logger.info("已通过 CDP 连接到系统 Chrome (localhost:9222)")
            return browser
        except Exception:
            if i < max_retries - 1:
                await asyncio.sleep(delay)
    return None


async def _get_page():
    """获取或创建 Playwright 页面单例。

    优先级：
    1. 复用已有有效页面
    2. 通过 CDP 连接系统 Chrome（LaunchAppTool 启动的带 --remote-debugging-port 的浏览器）
    3. 启动 Playwright 自带 Chromium 作为后备
    """
    if hasattr(_get_page, "_page") and _get_page._page and not _get_page._page.is_closed():  # type: ignore[attr-defined]
        return _get_page._page  # type: ignore[attr-defined]

    from playwright.async_api import async_playwright
    config = load_config()
    browser_cfg = config.browser

    pw = await async_playwright().start()

    # 1) 尝试 CDP 连接系统 Chrome
    browser = await _try_connect_cdp(pw)

    if browser is not None:
        contexts = browser.contexts
        if contexts:
            context = contexts[0]
        else:
            context = await browser.new_context(
                viewport={"width": browser_cfg.viewport_width, "height": browser_cfg.viewport_height},
            )
        # 创建新页面，避免复用可能处于特殊状态的默认页
        page = await context.new_page()
    else:
        # 2) 后备：启动 Playwright 自带 Chromium
        logger.info("CDP 连接失败，启动 Playwright Chromium 作为后备")
        try:
            browser = await pw.chromium.launch(
                headless=browser_cfg.headless,
                args=[f"--window-size={browser_cfg.viewport_width},{browser_cfg.viewport_height}"],
            )
        except Exception as exc:
            error_msg = str(exc)
            if "Executable doesn't exist" in error_msg or "chromium" in error_msg.lower():
                raise RuntimeError(
                    "Playwright Chromium 浏览器未安装。请运行: playwright install chromium"
                ) from exc
            raise

        context = await browser.new_context(
            viewport={"width": browser_cfg.viewport_width, "height": browser_cfg.viewport_height},
        )
        page = await context.new_page()

    _get_page._page = page  # type: ignore[attr-defined]
    _get_page._pw = pw  # type: ignore[attr-defined]
    _get_page._browser = browser  # type: ignore[attr-defined]
    return page


class NavigateTool(BaseTool):
    schema = ToolSchema(
        name="navigate",
        description="导航到指定 URL",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标 URL"},
                "wait_until": {
                    "type": "string",
                    "description": "等待条件: load | domcontentloaded | networkidle",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                },
            },
            "required": ["url"],
        },
    )

    async def execute(self, url: str, wait_until: str = "domcontentloaded") -> dict:
        page = await _get_page()
        try:
            resp = await page.goto(url, wait_until=wait_until, timeout=30000)
            return {
                "success": resp is not None and resp.ok,
                "summary": f"Navigated to {url} (status {resp.status if resp else 'N/A'})",
                "url": page.url,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class ClickTool(BaseTool):
    schema = ToolSchema(
        name="click",
        description="点击页面元素（支持 CSS selector、文本匹配、role）",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector 或文本匹配"},
                "strategy": {
                    "type": "string",
                    "description": "定位策略: css | text | role",
                    "enum": ["css", "text", "role"],
                },
                "timeout": {"type": "number", "description": "超时毫秒，默认 10000"},
            },
            "required": ["selector", "strategy"],
        },
    )

    async def execute(self, selector: str, strategy: str = "css", timeout: int = 10000) -> dict:
        page = await _get_page()
        try:
            if strategy == "text":
                return await self._click_by_text(page, selector, timeout)
            elif strategy == "role":
                locator = page.get_by_role(selector)
            else:
                locator = page.locator(selector)

            await locator.first.click(timeout=timeout)
            return {"success": True, "summary": f"Clicked '{selector}'"}
        except Exception as exc:
            return {"success": False, "error": await self._format_click_error(page, exc)}

    async def _click_by_text(self, page: Any, selector: str, timeout: int) -> dict:
        pattern = _text_pattern(selector)
        marker = await page.evaluate(
            """
            ({ selector, patternSource }) => {
                const matcher = new RegExp(patternSource, 'i');
                const clickableSelector = 'button, a, [role=button], input[type=button], input[type=submit]';
                const elements = Array.from(document.querySelectorAll(clickableSelector));
                const candidates = elements.map((el, index) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const text = (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
                    const visible = style.visibility !== 'hidden' && style.display !== 'none' &&
                        rect.width > 0 && rect.height > 0;
                    const disabled = Boolean(
                        el.disabled ||
                        el.hasAttribute('disabled') ||
                        el.getAttribute('aria-disabled') === 'true' ||
                        /(^|\\s)(disabled|Button--disabled|is-disabled)(\\s|$)/i.test(el.className || '')
                    );
                    let score = 0;
                    if (visible) score += 100;
                    if (!disabled) score += 100;
                    if (text === selector) score += 80;
                    if (text.includes(selector)) score += 40;
                    if (el.tagName.toLowerCase() === 'button') score += 30;
                    if (el.tagName.toLowerCase() === 'input') score += 25;
                    if ((el.getAttribute('type') || '').toLowerCase() === 'submit') score += 20;
                    score -= Math.min(text.length, 80);
                    return { el, index, text, visible, disabled, score };
                }).filter(item => item.visible && item.text && matcher.test(item.text));

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) {
                    return {
                        ok: false,
                        error: `No visible clickable element matched '${selector}'`,
                        candidates: candidates.map(({ text, disabled, score }) => ({ text, disabled, score })),
                    };
                }
                if (best.disabled) {
                    return {
                        ok: false,
                        error: `Best match for '${selector}' is disabled: ${best.text}`,
                        candidates: candidates.map(({ text, disabled, score }) => ({ text, disabled, score })),
                    };
                }
                const marker = `agent-click-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                best.el.setAttribute('data-agent-click-marker', marker);
                return {
                    ok: true,
                    marker,
                    text: best.text,
                    candidates: candidates.slice(0, 8).map(({ text, disabled, score }) => ({ text, disabled, score })),
                };
            }
            """,
            {"selector": selector, "patternSource": pattern.pattern},
        )
        if not marker.get("ok"):
            return {"success": False, "error": f"{marker.get('error')}\nVisible clickable candidates: {marker.get('candidates', [])}"}

        await page.locator(f'[data-agent-click-marker="{marker["marker"]}"]').click(timeout=timeout)
        return {
            "success": True,
            "summary": f"Clicked '{selector}' via candidate '{marker.get('text', '')}'",
            "clicked_text": marker.get("text", ""),
            "candidates": marker.get("candidates", []),
        }

    async def _format_click_error(self, page: Any, exc: Exception) -> str:
        try:
            candidates = await page.evaluate(
                """
                () => Array.from(document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]'))
                    .filter(el => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== 'hidden' && style.display !== 'none' &&
                            rect.width > 0 && rect.height > 0;
                    })
                    .slice(0, 30)
                    .map(el => ({
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80),
                        disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                    }))
                    .filter(item => item.text)
                """
            )
            if candidates:
                return f"{exc}\nVisible clickable candidates: {candidates}"
        except Exception:
            pass
        return str(exc)


class TypeTextTool(BaseTool):
    schema = ToolSchema(
        name="type_text",
        description="在输入框中输入文本。支持 CSS selector、文本匹配、role 三种定位策略。优先使用 text 策略（通过 placeholder 或 label 文本定位），避免猜测 CSS selector",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "定位选择器：CSS selector、可见文本、或 role 名称"},
                "text": {"type": "string", "description": "要输入的文字"},
                "strategy": {
                    "type": "string",
                    "description": "定位策略: css | text | role。优先使用 text（根据 placeholder/label 匹配）",
                    "enum": ["css", "text", "role"],
                },
                "delay": {"type": "number", "description": "每个字符间延迟毫秒"},
            },
            "required": ["selector", "text", "strategy"],
        },
    )

    async def execute(self, selector: str, text: str, strategy: str = "css", delay: int = 50) -> dict:
        page = await _get_page()
        try:
            if strategy == "text":
                name_pattern = re.compile(re.escape(selector), re.IGNORECASE)
                locator = page.get_by_placeholder(name_pattern).or_(
                    page.get_by_label(name_pattern)
                ).or_(
                    page.get_by_role("textbox", name=name_pattern)
                ).or_(
                    page.locator("input, textarea").filter(has_text=name_pattern)
                )
            elif strategy == "role":
                locator = page.get_by_role(selector)
            else:
                locator = page.locator(selector)
            await locator.first.click(timeout=5000)
            await locator.first.fill("")
            await locator.first.type(text, delay=delay)
            return {"success": True, "summary": f"Typed {len(text)} chars into '{selector}'"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class ScreenshotTool(BaseTool):
    schema = ToolSchema(
        name="screenshot",
        description="截取当前页面或元素的截图，返回 base64 编码",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "可选，仅截取指定元素"},
                "full_page": {"type": "boolean", "description": "是否截取整页"},
            },
        },
    )

    async def execute(self, selector: str = "", full_page: bool = False) -> dict:
        page = await _get_page()
        try:
            if selector:
                locator = page.locator(selector)
                data = await locator.screenshot()
            else:
                data = await page.screenshot(full_page=full_page)

            import base64
            b64 = base64.b64encode(data).decode()
            return {
                "success": True,
                "summary": f"Screenshot captured ({len(data)} bytes)",
                "screenshot_base64": b64,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class GetDOMTool(BaseTool):
    schema = ToolSchema(
        name="get_dom",
        description="获取当前页面的 DOM 摘要（精简版，只包含可交互元素）",
        parameters={
            "type": "object",
            "properties": {
                "depth": {"type": "number", "description": "嵌套深度限制，默认 3"},
            },
        },
    )

    async def execute(self, depth: int = 3) -> dict:
        page = await _get_page()
        try:
            script = """
            (maxDepth) => {
                const interactive = ['a', 'button', 'input', 'select', 'textarea', 'form'];
                const usefulAttrs = ['type', 'name', 'placeholder', 'aria-label', 'role', 'autocomplete', 'disabled', 'checked', 'aria-disabled', 'aria-checked', 'value'];
                function walk(el, d) {
                    if (d > maxDepth) return '';
                    const tag = el.tagName ? el.tagName.toLowerCase() : '';
                    if (!tag) return '';
                    let line = '  '.repeat(d) + '<' + tag;
                    if (el.id) line += ' id="' + el.id + '"';
                    if (el.className && typeof el.className === 'string')
                        line += ' class="' + el.className.split(' ').slice(0,3).join(' ') + '"';
                    for (const attr of usefulAttrs) {
                        const value = el.getAttribute && el.getAttribute(attr);
                        if (value) line += ' ' + attr + '="' + value.slice(0, 80) + '"';
                    }
                    const text = (el.textContent || '').trim().slice(0, 60);
                    if (text && interactive.includes(tag))
                        line += '>' + text.replace(/\\s+/g, ' ');
                    line += '\\n';
                    for (const child of el.children)
                        line += walk(child, d + 1);
                    return line;
                }
                return walk(document.body, 0);
            }
            """
            text = await page.evaluate(script, depth)
            return {"success": True, "summary": text[:3000]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class ExtractTextTool(BaseTool):
    schema = ToolSchema(
        name="extract_text",
        description="抓取页面的可见文本内容",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "只提取指定元素内的文本"},
            },
        },
    )

    async def execute(self, selector: str = "") -> dict:
        page = await _get_page()
        try:
            if selector:
                text = await page.locator(selector).inner_text()
            else:
                text = await page.inner_text("body")
            return {"success": True, "summary": text[:5000]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class CheckLoginStatusTool(BaseTool):
    schema = ToolSchema(
        name="check_login_status",
        description=(
            "检查当前网页是否已经登录。用于登录任务中避免重复登录；"
            "如果已登录会返回 task_complete=true，Agent 应提前完成任务。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "description": "平台名称，如 zhihu / generic",
                    "enum": ["zhihu", "generic"],
                },
            },
            "required": ["platform"],
        },
    )

    async def execute(self, platform: str = "generic") -> dict:
        page = await _get_page()
        try:
            status = await page.evaluate(
                """
                (platform) => {
                    const text = document.body ? document.body.innerText : '';
                    const href = location.href;
                    const clickable = Array.from(document.querySelectorAll(
                        'button, a, [role=button], input[type=button], input[type=submit]'
                    )).map(el => (
                        el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || ''
                    ).trim()).filter(Boolean);

                    if (platform === 'zhihu') {
                        const hasLoginDialog = /登录知乎|验证码登录|密码登录|获取验证码|短信验证码/.test(text);
                        const hasLoginEntry = clickable.some(t => /^登录$/.test(t) || t.includes('登录知乎'));
                        const hasUserSignals = Boolean(
                            document.querySelector('[href*="/people/"], [aria-label*="个人"], [aria-label*="头像"], img[alt*="头像"]')
                        ) || /创作中心|消息|私信|我的主页|退出登录/.test(text);
                        const loggedIn = hasUserSignals && !hasLoginDialog;
                        return {
                            loggedIn,
                            reason: loggedIn
                                ? 'Detected Zhihu user controls and no login dialog'
                                : `Not logged in or login dialog visible. loginEntry=${hasLoginEntry}, loginDialog=${hasLoginDialog}`,
                            clickable: clickable.slice(0, 20),
                            href,
                        };
                    }

                    const loginWords = /登录|登陆|sign in|log in/i;
                    const logoutWords = /退出|注销|logout|log out/i;
                    const loggedIn = logoutWords.test(text) || !clickable.some(t => loginWords.test(t));
                    return {
                        loggedIn,
                        reason: loggedIn ? 'Generic login signal detected' : 'Generic login entry still visible',
                        clickable: clickable.slice(0, 20),
                        href,
                    };
                }
                """,
                platform,
            )
            logged_in = bool(status.get("loggedIn"))
            return {
                "success": True,
                "summary": "Already logged in" if logged_in else "Login required",
                "logged_in": logged_in,
                "task_complete": logged_in,
                "status": status,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
