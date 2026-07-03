"""Browser automation tools — Playwright-based"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.utils.config import load_config


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
                locator = page.get_by_text(selector, exact=False)
            elif strategy == "role":
                locator = page.get_by_role(selector)
            else:
                locator = page.locator(selector)

            await locator.first.click(timeout=timeout)
            return {"success": True, "summary": f"Clicked '{selector}'"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class TypeTextTool(BaseTool):
    schema = ToolSchema(
        name="type_text",
        description="在输入框中输入文本",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "输入框的 CSS selector"},
                "text": {"type": "string", "description": "要输入的文字"},
                "delay": {"type": "number", "description": "每个字符间延迟毫秒"},
            },
            "required": ["selector", "text"],
        },
    )

    async def execute(self, selector: str, text: str, delay: int = 50) -> dict:
        page = await _get_page()
        try:
            locator = page.locator(selector)
            await locator.click()
            await locator.fill("")
            await locator.type(text, delay=delay)
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
                function walk(el, d) {
                    if (d > maxDepth) return '';
                    const tag = el.tagName ? el.tagName.toLowerCase() : '';
                    if (!tag) return '';
                    let line = '  '.repeat(d) + '<' + tag;
                    if (el.id) line += ' id="' + el.id + '"';
                    if (el.className && typeof el.className === 'string')
                        line += ' class="' + el.className.split(' ').slice(0,3).join(' ') + '"';
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
