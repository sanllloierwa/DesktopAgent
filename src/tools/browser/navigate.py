"""Browser automation tools — Playwright-based"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.utils.config import load_config


def _text_pattern(selector: str) -> re.Pattern[str]:
    if selector in {"获取验证码", "发送验证码", "获取短信验证码", "发送短信验证码"}:
        return re.compile(r"(获取|发送|收取).{0,6}(验证码|校验码)|短信验证码", re.IGNORECASE)
    return re.compile(re.escape(selector), re.IGNORECASE)


_ARIA_ROLES = {
    "button", "checkbox", "combobox", "dialog", "heading", "link",
    "listbox", "menuitem", "option", "radio", "searchbox", "switch",
    "tab", "textbox",
}


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


def _select_reusable_page(pages: list[Any], preferred_url: str = "") -> Any | None:
    available = [page for page in pages if not page.is_closed()]
    if not available:
        return None

    preferred_host = urlparse(preferred_url).hostname or ""
    if preferred_host:
        for page in available:
            if (urlparse(page.url).hostname or "") == preferred_host:
                return page

    for page in available:
        if page.url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
            return page
    for page in available:
        if page.url.startswith(("http://", "https://")):
            return page
    return available[0]


async def _get_page(preferred_url: str = ""):
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
        page = _select_reusable_page(context.pages, preferred_url)
        if page is None:
            page = await context.new_page()
    else:
        # 2) 后备：使用配置的持久化 profile 启动 Playwright Chromium。
        # This keeps login cookies even when the planner starts with navigate
        # directly instead of launch_app.
        logger.info("CDP 连接失败，启动持久化 Playwright Chromium 作为后备")
        try:
            user_data_dir = str(Path(browser_cfg.user_data_dir).expanduser().resolve())
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=browser_cfg.headless,
                viewport={
                    "width": browser_cfg.viewport_width,
                    "height": browser_cfg.viewport_height,
                },
                args=[f"--window-size={browser_cfg.viewport_width},{browser_cfg.viewport_height}"],
            )
        except Exception as exc:
            error_msg = str(exc)
            if "Executable doesn't exist" in error_msg or "chromium" in error_msg.lower():
                raise RuntimeError(
                    "Playwright Chromium 浏览器未安装。请运行: playwright install chromium"
                ) from exc
            raise

        browser = context.browser
        page = _select_reusable_page(context.pages, preferred_url)
        if page is None:
            page = await context.new_page()

    _get_page._page = page  # type: ignore[attr-defined]
    _get_page._pw = pw  # type: ignore[attr-defined]
    _get_page._browser = browser  # type: ignore[attr-defined]
    _get_page._context = context  # type: ignore[attr-defined]
    return page


async def _visible_modal_count(page: Any) -> int:
    return int(await page.evaluate(
        """
        () => {
            const visible = el => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            return Array.from(document.querySelectorAll('.Modal, [role="dialog"]'))
                .filter(visible).length;
        }
        """
    ))


async def _article_editor_image_count(page: Any) -> int:
    return int(await page.evaluate(
        """
        () => {
            const editors = Array.from(document.querySelectorAll(
                '[contenteditable="true"], .DraftEditor-root, .ProseMirror'
            ));
            const images = new Set();
            for (const editor of editors) {
                for (const image of editor.querySelectorAll('img')) images.add(image);
            }
            return images.size;
        }
        """
    ))


async def _open_image_upload_control(page: Any) -> bool:
    marker = await page.evaluate(
        """
        () => {
            const visible = el => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.display !== 'none' && style.visibility !== 'hidden';
            };
            const candidates = Array.from(document.querySelectorAll(
                'button, [role=button], input[type=button]'
            )).map(el => {
                const text = (
                    el.getAttribute('aria-label') || el.getAttribute('title') ||
                    el.innerText || el.textContent || ''
                ).trim();
                const disabled = Boolean(
                    el.disabled || el.hasAttribute('disabled') ||
                    el.getAttribute('aria-disabled') === 'true'
                );
                let score = 0;
                if (text === '图片') score += 1000;
                if (/插入图片|上传图片/.test(text)) score += 500;
                if (el.closest('.Modal, [role="dialog"]')) score -= 1000;
                return {el, disabled, score};
            }).filter(item => visible(item.el) && !item.disabled && item.score > 0);
            candidates.sort((a, b) => b.score - a.score);
            if (!candidates.length) return null;
            const marker = `agent-image-upload-${Date.now()}-${
                Math.random().toString(16).slice(2)
            }`;
            candidates[0].el.setAttribute('data-agent-image-upload', marker);
            return marker;
        }
        """
    )
    if not marker:
        return False
    await page.locator(
        f'[data-agent-image-upload="{marker}"]'
    ).first.click(timeout=5000)
    await page.wait_for_timeout(300)
    return True


async def _confirm_image_upload_modals(
    page: Any,
    max_attempts: int = 4,
) -> list[str]:
    """Commit crop/insert dialogs before dismissing residual upload modals."""

    actions: list[str] = []
    for _ in range(max_attempts):
        action = await page.evaluate(
            """
            () => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const dialogs = Array.from(document.querySelectorAll(
                    '.Modal, [role="dialog"]'
                )).filter(visible);
                const dialog = dialogs[dialogs.length - 1];
                if (!dialog) return {acted: false};
                const context = (dialog.innerText || dialog.textContent || '').trim();
                if (!/图片|裁剪|上传|插入|封面/.test(context)) {
                    return {acted: false};
                }
                const button = Array.from(dialog.querySelectorAll(
                    'button, [role=button], input[type=button], input[type=submit]'
                )).find(el => {
                    const text = (
                        el.innerText || el.textContent || el.value ||
                        el.getAttribute('aria-label') || ''
                    ).trim();
                    return visible(el) &&
                        /^(确定|完成|插入|保存|使用该图片)$/.test(text) &&
                        !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                });
                if (!button) return {acted: false};
                const label = (
                    button.innerText || button.textContent || button.value || ''
                ).trim();
                button.click();
                return {acted: true, label};
            }
            """
        )
        if not isinstance(action, dict) or not action.get("acted"):
            break
        actions.append(str(action.get("label", "confirm")))
        await page.wait_for_timeout(400)
    return actions


async def _dismiss_visible_modals(page: Any, max_attempts: int = 6) -> dict[str, Any]:
    """Close the top-most visible web modal without relying on pointer hit testing."""

    before = await _visible_modal_count(page)
    actions: list[str] = []
    if before == 0:
        return {
            "modal_count_before": 0,
            "modal_count_after": 0,
            "modal_actions": actions,
            "modal_closed": True,
        }
    for _ in range(max_attempts):
        action = await page.evaluate(
            """
            () => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const dialogs = Array.from(document.querySelectorAll(
                    '.Modal, [role="dialog"]'
                )).filter(visible).map((el, index) => ({
                    el,
                    index,
                    z: Number.parseInt(window.getComputedStyle(el).zIndex, 10) || 0,
                }));
                dialogs.sort((a, b) => b.z - a.z || b.index - a.index);
                const dialog = dialogs[0] && dialogs[0].el;
                if (!dialog) return {count: 0, acted: false};
                const buttons = Array.from(dialog.querySelectorAll(
                    'button, [role=button], input[type=button]'
                )).filter(visible);
                const close = buttons.find(el => {
                    const text = (
                        el.getAttribute('aria-label') || el.getAttribute('title') ||
                        el.innerText || el.textContent || el.value || ''
                    ).trim();
                    return /^(关闭|取消|close|cancel)$/i.test(text) ||
                        /Modal-closeButton/.test(String(el.className || ''));
                });
                if (!close) return {count: dialogs.length, acted: false};
                const label = (
                    close.getAttribute('aria-label') || close.innerText ||
                    close.textContent || 'close'
                ).trim();
                close.click();
                return {count: dialogs.length, acted: true, label};
            }
            """
        )
        if int(action.get("count", 0)) == 0:
            break
        if action.get("acted"):
            actions.append(str(action.get("label", "close")))
        else:
            await page.keyboard.press("Escape")
            actions.append("Escape")
        await page.wait_for_timeout(250)

    after = await _visible_modal_count(page)
    return {
        "modal_count_before": before,
        "modal_count_after": after,
        "modal_actions": actions,
        "modal_closed": after == 0,
    }


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
        page = await _get_page(url)
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
                if selector.lower() in _ARIA_ROLES:
                    locator = page.get_by_role(selector.lower())
                else:
                    locator = page.get_by_role(
                        "button", name=_text_pattern(selector)
                    )
            else:
                locator = page.locator(selector)

            await locator.first.click(timeout=timeout)
            return {
                "success": True,
                "summary": f"Clicked '{selector}'",
                "page_url": page.url,
            }
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
        try:
            await page.wait_for_timeout(250)
        except Exception:
            pass
        return {
            "success": True,
            "summary": f"Clicked '{selector}' via candidate '{marker.get('text', '')}'",
            "clicked_text": marker.get("text", ""),
            "candidates": marker.get("candidates", []),
            "page_url": page.url,
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


class DismissModalTool(BaseTool):
    schema = ToolSchema(
        name="dismiss_modal",
        description=(
            "关闭当前网页最上层的可见 Modal，并验证遮罩层对应的弹窗已经消失。"
            "用于图片上传等弹窗；无需截图、视觉坐标或重复普通 click。"
        ),
        parameters={"type": "object", "properties": {}},
    )

    async def execute(self) -> dict:
        page = await _get_page()
        try:
            result = await _dismiss_visible_modals(page)
            success = bool(result["modal_closed"])
            return {
                "success": success,
                "summary": (
                    "All visible web modals were dismissed"
                    if success
                    else "A visible web modal remains after deterministic dismissal"
                ),
                **result,
                "page_url": page.url,
                **({} if success else {
                    "error": "Unable to dismiss all visible web modals",
                }),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


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
                "field_kind": {
                    "type": "string",
                    "description": "字段语义: title=文章标题 | body=文章正文 | generic=普通输入框",
                    "enum": ["title", "body", "generic"],
                },
            },
            "required": ["selector", "text", "strategy"],
        },
    )

    async def execute(
        self,
        selector: str,
        text: str,
        strategy: str = "css",
        delay: int = 50,
        field_kind: str = "generic",
    ) -> dict:
        page = await _get_page()
        try:
            effective_kind = field_kind
            if effective_kind == "generic" and (len(text) > 200 or "\n" in text):
                effective_kind = "body"

            if effective_kind == "body":
                locator = await self._locate_article_body(page)
            elif effective_kind == "title":
                locator = await self._locate_article_title(page)
            elif strategy == "text":
                locator = await self._locate_text_input(page, selector)
            elif strategy == "role":
                if selector.lower() in {"textbox", "searchbox"}:
                    role_locator = page.get_by_role(selector.lower())
                    if await role_locator.count() != 1:
                        return {
                            "success": False,
                            "error": (
                                f"Ambiguous role '{selector}': resolved to "
                                f"{await role_locator.count()} elements; set field_kind=title/body"
                            ),
                        }
                    locator = role_locator
                else:
                    locator = page.get_by_role(
                        "textbox", name=_text_pattern(selector)
                    )
            else:
                locator = page.locator(selector)
            # Playwright fill() does not require a pointer action, so it can
            # populate an editable field even when a validation/error overlay
            # intercepts mouse clicks. It is also much faster for article bodies.
            await locator.first.fill(text, timeout=5000)
            actual_value = await locator.first.evaluate(
                "el => el.isContentEditable ? el.innerText : (el.value ?? el.textContent ?? '')"
            )
            normalized_expected = text.replace("\r\n", "\n").strip()
            normalized_actual = str(actual_value).replace("\r\n", "\n").strip()
            value_matches = normalized_actual == normalized_expected
            field_state = await self._article_field_state(page)
            title_contains_body = (
                effective_kind == "body"
                and normalized_expected
                and field_state.get("title_value_normalized") == normalized_expected
            )
            title_looks_like_body = (
                effective_kind == "body"
                and int(field_state.get("title_length", 0)) > 150
            )
            semantic_match = value_matches and not title_contains_body and not title_looks_like_body
            result = {
                "success": semantic_match,
                "summary": f"Typed {len(text)} chars into '{selector}'",
                "actual_value": actual_value,
                "value_matches": value_matches,
                "field_kind": effective_kind,
                "expected_length": len(text),
                "actual_length": len(str(actual_value)),
                "title_length": field_state.get("title_length", 0),
                "body_length": field_state.get("body_length", 0),
                "title_preview": field_state.get("title_preview", ""),
                "title_contains_body": title_contains_body,
                "title_looks_like_body": title_looks_like_body,
            }
            if title_contains_body or title_looks_like_body:
                result["error"] = (
                    "Article title contains body-sized content; refusing to continue"
                )
            return result
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _locate_article_body(self, page: Any):
        selector = '[contenteditable="true"], [role="textbox"]'
        match = await page.evaluate(
            """
            () => {
                const selector = '[contenteditable="true"], [role="textbox"]';
                const elements = Array.from(document.querySelectorAll(selector));
                const candidates = elements.map((el, index) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const visible = rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                    const descriptors = [
                        el.getAttribute('placeholder'),
                        el.getAttribute('data-placeholder'),
                        el.getAttribute('aria-label'),
                        el.getAttribute('role'),
                    ].filter(Boolean).join(' ');
                    let score = rect.width * rect.height;
                    if (el.isContentEditable) score += 1000000;
                    if (/正文|写文章|请输入正文|content|body/i.test(descriptors)) score += 2000000;
                    if (/标题|title/i.test(descriptors)) score -= 3000000;
                    if (['INPUT', 'TEXTAREA'].includes(el.tagName)) score -= 1000000;
                    return {index, visible, score, descriptors};
                }).filter(item => item.visible);
                candidates.sort((a, b) => b.score - a.score);
                return candidates[0] || null;
            }
            """
        )
        if not match:
            raise RuntimeError("No visible article body editor found")
        return page.locator(selector).nth(int(match["index"]))

    async def _locate_article_title(self, page: Any):
        selector = 'input, textarea, [role="textbox"]'
        match = await page.evaluate(
            """
            () => {
                const selector = 'input, textarea, [role="textbox"]';
                const elements = Array.from(document.querySelectorAll(selector));
                const candidates = elements.map((el, index) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const visible = rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                    const descriptors = [
                        el.getAttribute('placeholder'),
                        el.getAttribute('data-placeholder'),
                        el.getAttribute('aria-label'),
                    ].filter(Boolean).join(' ');
                    let score = 0;
                    if (/标题|title/i.test(descriptors)) score += 2000;
                    if (['INPUT', 'TEXTAREA'].includes(el.tagName)) score += 500;
                    if (el.isContentEditable) score -= 1000;
                    return {index, visible, score, descriptors};
                }).filter(item => item.visible);
                candidates.sort((a, b) => b.score - a.score);
                return candidates[0] || null;
            }
            """
        )
        if not match:
            raise RuntimeError("No visible article title input found")
        return page.locator(selector).nth(int(match["index"]))

    async def _article_field_state(self, page: Any) -> dict[str, Any]:
        try:
            return await page.evaluate(
                """
                () => {
                    const visible = el => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 &&
                            style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const value = el => (
                        el ? (el.isContentEditable ? el.innerText : (el.value || el.textContent || '')) : ''
                    );
                    const titleCandidates = Array.from(document.querySelectorAll(
                        'input, textarea, [role="textbox"]'
                    )).filter(el => visible(el) && /标题|title/i.test([
                        el.getAttribute('placeholder'),
                        el.getAttribute('data-placeholder'),
                        el.getAttribute('aria-label'),
                    ].filter(Boolean).join(' ')));
                    const bodyCandidates = Array.from(document.querySelectorAll(
                        '[contenteditable="true"], [role="textbox"]'
                    )).filter(el => visible(el) && !titleCandidates.includes(el));
                    bodyCandidates.sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (br.width * br.height) - (ar.width * ar.height);
                    });
                    const titleValue = value(titleCandidates[0]);
                    const bodyValue = value(bodyCandidates[0]);
                    return {
                        title_length: titleValue.length,
                        body_length: bodyValue.length,
                        title_preview: titleValue.slice(0, 100),
                        title_value_normalized: titleValue.replace(/\\r\\n/g, '\\n').trim(),
                    };
                }
                """
            )
        except Exception:
            return {}

    async def _locate_text_input(self, page: Any, selector: str):
        marker = await page.evaluate(
            """
            ({ selector }) => {
                const needle = selector.trim().toLowerCase();
                const elements = Array.from(document.querySelectorAll(
                    'input, textarea, [contenteditable="true"], [role="textbox"]'
                ));
                const candidates = elements.map((el, index) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const label = el.labels && el.labels.length
                        ? Array.from(el.labels).map(item => item.innerText || item.textContent || '').join(' ')
                        : '';
                    const descriptors = [
                        el.getAttribute('placeholder'),
                        el.getAttribute('data-placeholder'),
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        label,
                        el.innerText,
                    ].filter(Boolean).map(value => String(value).trim());
                    const normalized = descriptors.map(value => value.toLowerCase());
                    const visible = style.visibility !== 'hidden' && style.display !== 'none' &&
                        rect.width > 0 && rect.height > 0;
                    const disabled = Boolean(
                        el.disabled || el.hasAttribute('disabled') ||
                        el.getAttribute('aria-disabled') === 'true'
                    );
                    let score = 0;
                    if (visible) score += 100;
                    if (!disabled) score += 100;
                    if (normalized.some(value => value === needle)) score += 80;
                    if (normalized.some(value => value.includes(needle))) score += 40;
                    if (el.isContentEditable) score += 15;
                    return { el, index, descriptors, visible, disabled, score };
                }).filter(item =>
                    item.visible && item.descriptors.some(
                        value => value.toLowerCase().includes(needle)
                    )
                );

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best || best.disabled) {
                    return {
                        ok: false,
                        error: best
                            ? `Best input match for '${selector}' is disabled`
                            : `No visible input matched '${selector}'`,
                        candidates: candidates.slice(0, 8).map(item => item.descriptors),
                    };
                }
                return { ok: true, index: best.index };
            }
            """,
            {"selector": selector},
        )
        if not marker.get("ok"):
            raise RuntimeError(
                f"{marker.get('error')}. Input candidates: {marker.get('candidates', [])}"
            )
        # Use the element's position in a stable semantic locator rather than a
        # temporary data attribute. React may replace the input immediately
        # after validation, which previously made the marker locator go stale.
        return page.locator(
            'input, textarea, [contenteditable="true"], [role="textbox"]'
        ).nth(int(marker["index"]))


class SubmitCommentTool(BaseTool):
    """Submit a web comment and verify that it left the editor."""

    schema = ToolSchema(
        name="submit_comment",
        description=(
            "在当前文章页提交评论，并验证编辑器已清空且评论出现在非编辑区域。"
            "知乎评论必须使用此工具，不能把 type_text 或普通 click 成功当作已发表评论。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "comment": {
                    "type": "string",
                    "description": "要发表的完整评论内容",
                },
            },
            "required": ["comment"],
        },
    )

    async def execute(self, comment: str) -> dict:
        page = await _get_page()
        normalized = comment.replace("\r\n", "\n").strip()
        if not normalized:
            return {"success": False, "error": "Comment must not be empty"}

        try:
            existing_count = await self._posted_comment_count(page, normalized)
            if existing_count > 0:
                return {
                    "success": True,
                    "summary": "Comment is already visible in the published comment area",
                    "comment_text": normalized,
                    "comment_submitted": True,
                    "comment_visible": True,
                    "editor_cleared": True,
                    "already_posted": True,
                    "posted_match_count_before": existing_count,
                    "posted_match_count_after": existing_count,
                    "page_url": page.url,
                }

            editor = await self._locate_comment_editor(page)
            if editor is None:
                await self._open_comment_editor(page)
                editor = await self._locate_comment_editor(page)
            if editor is None:
                return {
                    "success": False,
                    "error": "No visible comment editor found after opening the comment area",
                }

            await editor.fill(normalized, timeout=5000)
            actual = await self._editor_value(editor)
            if actual != normalized:
                return {
                    "success": False,
                    "error": "Comment editor did not retain the complete comment text",
                    "comment_text": normalized,
                    "editor_value": actual,
                    "comment_submitted": False,
                    "comment_visible": False,
                }

            submit = await self._locate_comment_submit(page, editor)
            if submit is None:
                return {
                    "success": False,
                    "error": (
                        "No enabled submit button was found in the same comment editor region"
                    ),
                    "comment_text": normalized,
                    "editor_value": actual,
                    "comment_submitted": False,
                    "comment_visible": False,
                }

            await submit.click(timeout=10000)
            await page.wait_for_timeout(800)
            after_count = await self._posted_comment_count(page, normalized)
            editor_value = await self._editor_value(editor)

            # Some rich-text comment widgets expose Ctrl+Enter as the reliable
            # submit route. Only try it while the original draft is still
            # present, so a slow successful click cannot create a duplicate.
            if after_count <= existing_count and editor_value:
                await editor.press("Control+Enter")
                await page.wait_for_timeout(800)
                after_count = await self._posted_comment_count(page, normalized)
                editor_value = await self._editor_value(editor)

            if not editor_value and after_count <= existing_count:
                await page.wait_for_timeout(1200)
                after_count = await self._posted_comment_count(page, normalized)

            comment_visible = after_count > existing_count
            editor_cleared = not editor_value
            submitted = comment_visible and editor_cleared
            result = {
                "success": submitted,
                "summary": (
                    "Comment submitted and verified outside the editor"
                    if submitted
                    else "Comment submission was not verified"
                ),
                "comment_text": normalized,
                "comment_submitted": submitted,
                "comment_visible": comment_visible,
                "editor_cleared": editor_cleared,
                "editor_value": editor_value,
                "already_posted": False,
                "posted_match_count_before": existing_count,
                "posted_match_count_after": after_count,
                "page_url": page.url,
            }
            if not submitted:
                result["error"] = (
                    "Comment was not published: the draft did not both leave the editor "
                    "and appear in a non-editable page region"
                )
            return result
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _open_comment_editor(self, page: Any) -> None:
        opener = page.get_by_role(
            "button",
            name=re.compile(r"添加评论|写评论|发表评论|评论"),
        )
        if await opener.count():
            await opener.first.click(timeout=5000)
            await page.wait_for_timeout(300)

    async def _locate_comment_editor(self, page: Any):
        selector = 'textarea, [contenteditable="true"], [role="textbox"]'
        match = await page.evaluate(
            """
            () => {
                const selector = 'textarea, [contenteditable="true"], [role="textbox"]';
                const elements = Array.from(document.querySelectorAll(selector));
                const candidates = elements.map((el, index) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const visible = rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                    const context = [
                        el.getAttribute('placeholder'),
                        el.getAttribute('data-placeholder'),
                        el.getAttribute('aria-label'),
                        el.parentElement && el.parentElement.innerText,
                        el.parentElement && el.parentElement.parentElement &&
                            el.parentElement.parentElement.innerText,
                    ].filter(Boolean).join(' ').slice(0, 500);
                    let score = 0;
                    if (/评论|回复|comment|写下/i.test(context)) score += 3000;
                    if (/搜索|search|标题|正文|写文章/i.test(context)) score -= 5000;
                    if (el.isContentEditable) score += 500;
                    if (el.tagName === 'TEXTAREA') score += 300;
                    score += Math.min(rect.width * rect.height / 1000, 300);
                    return {index, visible, score};
                }).filter(item => item.visible && item.score > 0);
                candidates.sort((a, b) => b.score - a.score);
                return candidates[0] || null;
            }
            """
        )
        if not match:
            return None
        return page.locator(selector).nth(int(match["index"]))

    async def _locate_comment_submit(self, page: Any, editor: Any):
        marker = await editor.evaluate(
            """
            editor => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const enabled = el => !(
                    el.disabled || el.hasAttribute('disabled') ||
                    el.getAttribute('aria-disabled') === 'true'
                );
                let root = editor.parentElement;
                for (let depth = 0; root && depth < 8; depth += 1, root = root.parentElement) {
                    const candidates = Array.from(root.querySelectorAll(
                        'button, [role=button], input[type=submit]'
                    )).map(el => {
                        const text = (
                            el.innerText || el.textContent || el.value ||
                            el.getAttribute('aria-label') || ''
                        ).trim();
                        let score = 0;
                        if (/^(发布|评论|发送|提交)$/.test(text)) score += 1000;
                        if (/发布|发表评论|发送评论|提交评论/.test(text)) score += 500;
                        if (/Button--primary|Button--blue|primary/i.test(String(el.className || ''))) {
                            score += 200;
                        }
                        return {el, text, score};
                    }).filter(item => visible(item.el) && enabled(item.el) && item.score > 0);
                    candidates.sort((a, b) => b.score - a.score);
                    if (candidates.length) {
                        const marker = `agent-comment-submit-${Date.now()}-${
                            Math.random().toString(16).slice(2)
                        }`;
                        candidates[0].el.setAttribute('data-agent-comment-submit', marker);
                        return {ok: true, marker, text: candidates[0].text};
                    }
                }
                return {ok: false};
            }
            """
        )
        if not marker or not marker.get("ok"):
            return None
        return page.locator(
            f'[data-agent-comment-submit="{marker["marker"]}"]'
        ).first

    async def _editor_value(self, editor: Any) -> str:
        try:
            value = await editor.evaluate(
                "el => el.isContentEditable ? el.innerText : (el.value || '')"
            )
            return str(value).replace("\r\n", "\n").strip()
        except Exception:
            return ""

    async def _posted_comment_count(self, page: Any, comment: str) -> int:
        return int(await page.evaluate(
            """
            comment => {
                const needle = comment.replace(/\\r\\n/g, '\\n').trim();
                const editableSelector =
                    'input, textarea, [contenteditable="true"], [role="textbox"]';
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                return Array.from(document.querySelectorAll('p, span, div, li'))
                    .filter(el => {
                        if (!visible(el)) return false;
                        if (el.closest(editableSelector) || el.querySelector(editableSelector)) {
                            return false;
                        }
                        const text = (el.innerText || el.textContent || '')
                            .replace(/\\r\\n/g, '\\n').trim();
                        return text === needle;
                    }).length;
            }
            """,
            comment,
        ))


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
        description="获取当前页面可交互元素的扁平 DOM 摘要，不受页面嵌套深度影响",
        parameters={
            "type": "object",
            "properties": {
                "depth": {
                    "type": "number",
                    "description": "兼容参数；当前实现会跨层级收集最多160个可交互元素",
                },
            },
        },
    )

    async def execute(self, depth: int = 3) -> dict:
        page = await _get_page()
        try:
            script = """
            () => {
                const selector = [
                    'a', 'button', 'input', 'select', 'textarea',
                    '[contenteditable="true"]', '[role="button"]',
                    '[role="textbox"]', '[role="checkbox"]', '[role="switch"]',
                    '[role="menuitem"]', '[role="option"]', '[tabindex]'
                ].join(',');
                const usefulAttrs = [
                    'type', 'name', 'placeholder', 'data-placeholder', 'contenteditable',
                    'aria-label', 'role', 'autocomplete', 'disabled', 'checked',
                    'aria-disabled', 'aria-checked', 'aria-pressed', 'data-state',
                    'accept', 'multiple', 'value'
                ];
                const elements = Array.from(document.querySelectorAll(selector));
                const rows = [];
                for (const el of elements) {
                    const tag = el.tagName ? el.tagName.toLowerCase() : '';
                    if (!tag) continue;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const visible = style.visibility !== 'hidden' && style.display !== 'none' &&
                        rect.width > 0 && rect.height > 0;
                    let line = '<' + tag;
                    if (el.id) line += ' id="' + el.id + '"';
                    if (el.className && typeof el.className === 'string')
                        line += ' class="' + el.className.split(' ').slice(0,3).join(' ') + '"';
                    for (const attr of usefulAttrs) {
                        const value = el.getAttribute && el.getAttribute(attr);
                        if (value) line += ' ' + attr + '="' + value.slice(0, 80) + '"';
                    }
                    line += ' visible="' + visible + '"';
                    const text = (
                        el.innerText || el.textContent || el.value ||
                        el.getAttribute('aria-label') || ''
                    ).trim().slice(0, 120);
                    if (text)
                        line += '>' + text.replace(/\\s+/g, ' ');
                    rows.push({line, visible});
                }
                rows.sort((a, b) => Number(b.visible) - Number(a.visible));
                return rows.slice(0, 160).map(item => item.line).join('\\n');
            }
            """
            text = await page.evaluate(script)
            return {"success": True, "summary": text[:12000], "element_count": text.count("\n") + bool(text)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class UploadImageTool(BaseTool):
    schema = ToolSchema(
        name="upload_image",
        description=(
            "向当前网页的图片文件输入框上传一张或多张本地图片。"
            "优先自动寻找 accept=image 的 input[type=file]，也可传入已从 DOM 确认的 CSS selector。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本地图片绝对路径列表，通常引用 {{generate_image.image_paths}}",
                },
                "selector": {
                    "type": "string",
                    "description": "可选文件 input 的 CSS selector",
                },
                "timeout": {"type": "number", "description": "等待上传预览的毫秒数"},
            },
            "required": ["image_paths"],
        },
    )

    async def execute(
        self,
        image_paths: list[str],
        selector: str = "",
        timeout: int = 15000,
    ) -> dict:
        page = await _get_page()
        paths = [str(Path(path).expanduser().resolve()) for path in image_paths]
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            return {"success": False, "error": f"Image file not found: {missing[0]}"}
        unsupported = [
            path for path in paths
            if Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        ]
        if unsupported:
            return {"success": False, "error": f"Unsupported image type: {unsupported[0]}"}

        try:
            file_selector = selector or 'input[type="file"][accept*="image"]'
            locator = page.locator(file_selector)
            if not selector and await locator.count() == 0:
                file_selector = 'input[type="file"]'
                locator = page.locator(file_selector)
            if await locator.count() == 0:
                return {
                    "success": False,
                    "error": "No file input found; open the editor image-upload control and retry get_dom",
                }
            input_target = locator.first if selector else locator.last
            before_images = await page.locator("img").count()
            before_editor_images = await _article_editor_image_count(page)
            if before_editor_images >= len(paths):
                dismissal = await _dismiss_visible_modals(page)
                return {
                    "success": bool(dismissal["modal_closed"]),
                    "summary": "Generated images are already present in the article editor",
                    "uploaded_paths": paths,
                    "uploaded_count": len(paths),
                    "image_count_before": before_images,
                    "image_count_after": before_images,
                    "editor_image_count_before": before_editor_images,
                    "editor_image_count_after": before_editor_images,
                    "preview_detected": True,
                    "already_present": True,
                    **dismissal,
                    "page_url": page.url,
                }

            supports_multiple = bool(await input_target.evaluate(
                "el => Boolean(el.multiple || el.hasAttribute('multiple'))"
            ))
            upload_batches = [paths] if supports_multiple else [[path] for path in paths]
            observed_images = before_images
            observed_editor_images = before_editor_images
            uploaded_paths: list[str] = []
            per_batch_attempts = max(
                3,
                min(10, int(timeout / max(len(upload_batches), 1) / 500)),
            )
            for batch_index, batch in enumerate(upload_batches):
                if (
                    not supports_multiple
                    and batch_index > 0
                    and await _visible_modal_count(page) == 0
                ):
                    await _open_image_upload_control(page)
                locator = page.locator(file_selector)
                if await locator.count() == 0:
                    opened = await _open_image_upload_control(page)
                    locator = page.locator(file_selector)
                    if not opened or await locator.count() == 0:
                        return {
                            "success": False,
                            "error": (
                                "The single-file image input disappeared and the "
                                "editor image control could not reopen it"
                            ),
                            "uploaded_paths": uploaded_paths,
                            "uploaded_count": len(uploaded_paths),
                        }

                input_target = locator.first if selector else locator.last
                batch_before_images = await page.locator("img").count()
                batch_before_editor_images = await _article_editor_image_count(page)
                await input_target.set_input_files(batch, timeout=timeout)
                uploaded_paths.extend(batch)
                for _ in range(per_batch_attempts):
                    await page.wait_for_timeout(500)
                    observed_images = await page.locator("img").count()
                    observed_editor_images = await _article_editor_image_count(page)
                    if (
                        observed_images > batch_before_images
                        or observed_editor_images > batch_before_editor_images
                    ):
                        break
                confirm_actions = await _confirm_image_upload_modals(page)
                if confirm_actions:
                    await page.wait_for_timeout(500)

            dismissal = await _dismiss_visible_modals(page)
            after_images = observed_images
            after_editor_images = observed_editor_images
            for _ in range(6):
                await page.wait_for_timeout(500)
                after_images = await page.locator("img").count()
                after_editor_images = await _article_editor_image_count(page)
                if (
                    after_images > before_images
                    or after_editor_images > before_editor_images
                ):
                    break

            preview_detected = (
                after_images - before_images >= len(paths)
                or after_editor_images - before_editor_images >= len(paths)
            )
            success = preview_detected and dismissal["modal_closed"]
            return {
                "success": success,
                "summary": (
                    f"Uploaded {len(paths)} image(s) and closed the upload modal"
                    if success
                    else "Image upload did not reach a verified modal-free editor state"
                ),
                "uploaded_paths": uploaded_paths,
                "uploaded_count": len(uploaded_paths),
                "input_supports_multiple": supports_multiple,
                "upload_confirm_actions": confirm_actions if upload_batches else [],
                "image_count_before": before_images,
                "image_count_after": after_images,
                "editor_image_count_before": before_editor_images,
                "editor_image_count_after": after_editor_images,
                "preview_detected": preview_detected,
                "already_present": False,
                **dismissal,
                "page_url": page.url,
                **({} if success else {
                    "error": (
                        "Image preview was not detected after upload"
                        if not preview_detected
                        else "Image upload modal is still visible"
                    ),
                }),
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class PublishArticleTool(BaseTool):
    """Publish an article once and wait for the independently viewable page."""

    schema = ToolSchema(
        name="publish_article",
        description=(
            "发布当前知乎文章并等待独立文章页加载。自动清理残留上传弹窗，"
            "只在编辑器中点击一次主发布按钮，并以 /p/ URL 和页面标题作为成功证据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "必须在发布后的独立文章页可见的文章标题",
                },
                "timeout": {
                    "type": "number",
                    "description": "等待发布跳转的毫秒数，默认15000",
                },
            },
            "required": ["title"],
        },
    )

    async def execute(self, title: str, timeout: int = 15000) -> dict:
        page = await _get_page()
        normalized_title = title.strip()
        if not normalized_title:
            return {"success": False, "error": "Article title must not be empty"}

        try:
            state = await self._read_published_state(page, normalized_title)
            if state.get("published"):
                result = self._success_result(state, already_published=True)
                result.update(await self._post_publish_cleanup(page))
                return result

            dismissal = await _dismiss_visible_modals(page)
            if not dismissal["modal_closed"]:
                return {
                    "success": False,
                    "error": "A modal still blocks the article publish button",
                    **dismissal,
                }

            settings_opened = await self._open_publish_settings(page)
            if settings_opened:
                await page.wait_for_timeout(350)
            publish = await self._locate_editor_publish(page)
            if publish is None:
                return {
                    "success": False,
                    "error": "No enabled article publish button found in the editor",
                    "settings_opened": settings_opened,
                    **dismissal,
                }

            await publish.click(timeout=10000)
            confirmation_clicked = False
            success_link_opened = False
            attempts = max(3, int(timeout / 400))
            for attempt in range(attempts):
                await page.wait_for_timeout(400)
                state = await self._read_published_state(page, normalized_title)
                if state.get("published"):
                    result = self._success_result(state)
                    result.update(dismissal)
                    result.update(await self._post_publish_cleanup(page))
                    result["settings_opened"] = settings_opened
                    result["confirmation_clicked"] = confirmation_clicked
                    result["success_link_opened"] = success_link_opened
                    return result

                success_link = str(state.get("success_link", ""))
                if success_link and not success_link_opened:
                    await page.goto(success_link, wait_until="domcontentloaded")
                    success_link_opened = True
                    continue

                if attempt >= 2 and not confirmation_clicked:
                    confirmation_clicked = await self._click_publish_confirmation(page)

            return {
                "success": False,
                "summary": "Article publication was not verified before timeout",
                "error": (
                    "Publish did not reach an independent /p/ article page with the "
                    "expected title; refusing to click publish repeatedly"
                ),
                "published": False,
                "title_visible": bool(state.get("title_visible")),
                "page_url": state.get("url", page.url),
                "confirmation_clicked": confirmation_clicked,
                "settings_opened": settings_opened,
                **dismissal,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def _read_published_state(
        self,
        page: Any,
        title: str,
        retries: int = 5,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(retries):
            try:
                return await self._published_state(page, title)
            except Exception as exc:
                message = str(exc).lower()
                if not any(term in message for term in (
                    "execution context was destroyed",
                    "most likely because of a navigation",
                    "cannot find context",
                    "target closed",
                )):
                    raise
                last_error = exc
                await page.wait_for_timeout(300)
        raise last_error or RuntimeError("Unable to read article publication state")

    async def _post_publish_cleanup(self, page: Any) -> dict[str, Any]:
        try:
            cleanup = await _dismiss_visible_modals(page)
            return {
                "post_publish_modal_closed": cleanup.get("modal_closed", False),
                "post_publish_modal_actions": cleanup.get("modal_actions", []),
            }
        except Exception:
            return {
                "post_publish_modal_closed": False,
                "post_publish_modal_actions": [],
            }

    def _success_result(
        self,
        state: dict[str, Any],
        already_published: bool = False,
    ) -> dict:
        return {
            "success": True,
            "summary": "Article is published on an independently viewable page",
            "published": True,
            "title_visible": True,
            "interactions_ready": bool(state.get("interactions_ready")),
            "page_url": state.get("url", ""),
            "already_published": already_published,
        }

    async def _published_state(self, page: Any, title: str) -> dict[str, Any]:
        return await page.evaluate(
            """
            title => {
                const normalize = value => String(value || '')
                    .replace(/\\s+/g, ' ').trim();
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const editableSelector =
                    'input, textarea, [contenteditable="true"], [role="textbox"]';
                const titleVisible = Array.from(document.querySelectorAll(
                    'h1, h2, [class*="Title"], article div'
                )).some(el => (
                    visible(el) &&
                    !el.closest(editableSelector) &&
                    !el.querySelector(editableSelector) &&
                    normalize(el.innerText || el.textContent) === normalize(title)
                ));
                const controlText = Array.from(document.querySelectorAll(
                    'button, [role=button]'
                )).filter(visible).map(el => normalize(
                    el.innerText || el.textContent || el.getAttribute('aria-label')
                )).join('\\n');
                const interactionsReady =
                    /赞同|点赞/.test(controlText) &&
                    /收藏/.test(controlText) &&
                    /喜欢/.test(controlText);
                const independentUrl =
                    /\\/p\\//.test(location.pathname) &&
                    !/\\/write/.test(location.pathname);
                const dialogs = Array.from(document.querySelectorAll(
                    '.Modal, [role="dialog"]'
                )).filter(visible);
                const successLink = dialogs.flatMap(dialog =>
                    Array.from(dialog.querySelectorAll('a[href*="/p/"]'))
                ).find(visible);
                return {
                    url: location.href,
                    published: independentUrl && titleVisible,
                    title_visible: titleVisible,
                    interactions_ready: interactionsReady,
                    success_link: successLink ? successLink.href : '',
                };
            }
            """,
            title,
        )

    async def _locate_editor_publish(self, page: Any):
        marker = await page.evaluate(
            """
            () => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const candidates = Array.from(document.querySelectorAll(
                    'button, [role=button], input[type=submit]'
                )).map(el => {
                    const text = (
                        el.innerText || el.textContent || el.value ||
                        el.getAttribute('aria-label') || ''
                    ).trim();
                    const inModal = Boolean(el.closest('.Modal, [role="dialog"]'));
                    const modalText = inModal ? (
                        el.closest('.Modal, [role="dialog"]').innerText || ''
                    ) : '';
                    const disabled = Boolean(
                        el.disabled || el.hasAttribute('disabled') ||
                        el.getAttribute('aria-disabled') === 'true'
                    );
                    let score = 0;
                    if (/^(发布|发布文章|确认发布|立即发布)$/.test(text)) score += 1000;
                    if (/Button--primary|Button--blue|primary/i.test(
                        String(el.className || '')
                    )) score += 200;
                    if (inModal && /发布|设置/.test(modalText)) score += 500;
                    else if (inModal) score -= 2000;
                    return {el, text, disabled, score};
                }).filter(item =>
                    visible(item.el) && !item.disabled && item.score > 0
                );
                candidates.sort((a, b) => b.score - a.score);
                if (!candidates.length) return null;
                const marker = `agent-article-publish-${Date.now()}-${
                    Math.random().toString(16).slice(2)
                }`;
                candidates[0].el.setAttribute('data-agent-article-publish', marker);
                return {marker};
            }
            """
        )
        if not marker:
            return None
        return page.locator(
            f'[data-agent-article-publish="{marker["marker"]}"]'
        ).first

    async def _open_publish_settings(self, page: Any) -> bool:
        return bool(await page.evaluate(
            """
            () => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const button = Array.from(document.querySelectorAll(
                    'button, [role=button]'
                )).find(el => {
                    const text = (
                        el.innerText || el.textContent ||
                        el.getAttribute('aria-label') || ''
                    ).trim();
                    return visible(el) && /^(发布设置|设置发布|发布选项)$/.test(text) &&
                        !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                });
                if (!button) return false;
                button.click();
                return true;
            }
            """
        ))

    async def _click_publish_confirmation(self, page: Any) -> bool:
        return bool(await page.evaluate(
            """
            () => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' && style.visibility !== 'hidden';
                };
                const dialogs = Array.from(document.querySelectorAll(
                    '.Modal, [role="dialog"]'
                )).filter(visible);
                const dialog = dialogs[dialogs.length - 1];
                if (!dialog) return false;
                const button = Array.from(dialog.querySelectorAll(
                    'button, [role=button], input[type=submit]'
                )).find(el => {
                    const text = (
                        el.innerText || el.textContent || el.value || ''
                    ).trim();
                    return visible(el) &&
                        /^(发布|发布文章|确认发布|立即发布)$/.test(text) &&
                        !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                });
                if (!button) return false;
                button.click();
                return true;
            }
            """
        ))


class GetPageStateTool(BaseTool):
    schema = ToolSchema(
        name="get_page_state",
        description=(
            "读取当前网页 URL、标题、提示文本以及指定按钮的选中/禁用状态。"
            "用于发布、评论、赞同、收藏、喜欢等关键动作后的确定性复验。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要检查的按钮或动作文本，如 赞同、收藏、喜欢",
                },
                "text_contains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "页面正文必须包含的普通文本，如文章标题",
                },
                "posted_comment_contains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "必须出现在非输入框、非富文本编辑器区域的已发布评论文本"
                    ),
                },
                "require_selected": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "必须处于选中状态的动作，如赞同、收藏、喜欢",
                },
            },
        },
    )

    async def execute(
        self,
        targets: list[str] | None = None,
        text_contains: list[str] | None = None,
        posted_comment_contains: list[str] | None = None,
        require_selected: list[str] | None = None,
    ) -> dict:
        page = await _get_page()
        try:
            all_targets = list(dict.fromkeys((targets or []) + (require_selected or [])))
            state = await page.evaluate(
                """
                ({ targets, requiredTexts, requiredPostedComments }) => {
                    const bodyText = document.body ? document.body.innerText : '';
                    const editableSelector =
                        'input, textarea, [contenteditable="true"], [role="textbox"]';
                    const isVisible = el => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 &&
                            style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const controls = Array.from(document.querySelectorAll(
                        'button, a, [role=button], [role=checkbox], [role=switch]'
                    ));
                    const actionStates = targets.map(target => {
                        const needle = target.trim().toLowerCase();
                        const candidates = controls.filter(el => {
                            const text = (
                                el.innerText || el.textContent ||
                                el.getAttribute('aria-label') || ''
                            ).trim().toLowerCase();
                            return text === needle || text.includes(needle);
                        });
                        const el = candidates.find(item => {
                            const rect = item.getBoundingClientRect();
                            const style = window.getComputedStyle(item);
                            return rect.width > 0 && rect.height > 0 &&
                                style.display !== 'none' && style.visibility !== 'hidden';
                        }) || candidates[0];
                        if (!el) return {target, found: false};
                        const className = String(el.className || '');
                        const pressed = el.getAttribute('aria-pressed');
                        const checked = el.getAttribute('aria-checked');
                        const dataState = el.getAttribute('data-state');
                        const selected = pressed === 'true' || checked === 'true' ||
                            ['checked', 'selected', 'active', 'on'].includes(dataState) ||
                            /(^|\\s)(is-active|active|selected|checked)(\\s|$)/i.test(className);
                        return {
                            target,
                            found: true,
                            text: (
                                el.innerText || el.textContent ||
                                el.getAttribute('aria-label') || ''
                            ).trim().slice(0, 120),
                            selected,
                            pressed,
                            checked,
                            dataState,
                            disabled: Boolean(
                                el.disabled || el.hasAttribute('disabled') ||
                                el.getAttribute('aria-disabled') === 'true'
                            ),
                        };
                    });
                    const textChecks = requiredTexts.map(text => ({
                        text,
                        found: bodyText.includes(text),
                    }));
                    const postedCommentChecks = requiredPostedComments.map(text => {
                        const needle = text.replace(/\\r\\n/g, '\\n').trim();
                        const found = Array.from(document.querySelectorAll(
                            'p, span, div, li'
                        )).some(el => {
                            if (!isVisible(el)) return false;
                            if (
                                el.closest(editableSelector) ||
                                el.querySelector(editableSelector)
                            ) return false;
                            const candidate = (el.innerText || el.textContent || '')
                                .replace(/\\r\\n/g, '\\n').trim();
                            return candidate === needle;
                        });
                        return {text, found};
                    });
                    const notices = Array.from(document.querySelectorAll(
                        '[role=alert], [role=status], .Toast, .Notification, .Modal'
                    )).map(el => (el.innerText || el.textContent || '').trim())
                      .filter(Boolean).slice(0, 10);
                    return {
                        url: location.href,
                        title: document.title,
                        actionStates,
                        textChecks,
                        postedCommentChecks,
                        notices,
                    };
                }
                """,
                {
                    "targets": all_targets,
                    "requiredTexts": text_contains or [],
                    "requiredPostedComments": posted_comment_contains or [],
                },
            )
            action_states = state.get("actionStates", [])
            text_checks = state.get("textChecks", [])
            posted_comment_checks = state.get("postedCommentChecks", [])
            selected_by_target = {
                str(item.get("target", "")): bool(item.get("selected"))
                for item in action_states
            }
            required_selected = require_selected or []
            all_required_selected = all(
                selected_by_target.get(target, False) for target in required_selected
            )
            all_text_found = all(item.get("found") for item in text_checks)
            all_posted_comments_found = all(
                item.get("found") for item in posted_comment_checks
            )
            verification_ok = (
                all_text_found
                and all_posted_comments_found
                and all_required_selected
            )
            result = {
                "success": verification_ok,
                "summary": (
                    f"Page state captured at {state.get('url', '')}; "
                    f"{sum(bool(item.get('found')) for item in text_checks)}/{len(text_checks)} text checks found; "
                    f"{sum(bool(item.get('found')) for item in posted_comment_checks)}/"
                    f"{len(posted_comment_checks)} posted comments found; "
                    f"{sum(bool(selected_by_target.get(item)) for item in required_selected)}/"
                    f"{len(required_selected)} required actions selected"
                ),
                "page_url": state.get("url", ""),
                "page_title": state.get("title", ""),
                "action_states": action_states,
                "text_checks": text_checks,
                "posted_comment_checks": posted_comment_checks,
                "all_text_found": all_text_found,
                "all_posted_comments_found": all_posted_comments_found,
                "all_required_selected": all_required_selected,
                "notices": state.get("notices", []),
            }
            if not verification_ok:
                missing_text = [
                    item.get("text") for item in text_checks if not item.get("found")
                ]
                missing_comments = [
                    item.get("text")
                    for item in posted_comment_checks
                    if not item.get("found")
                ]
                missing_selected = [
                    target for target in required_selected
                    if not selected_by_target.get(target, False)
                ]
                result["error"] = (
                    f"Page-state verification failed; missing text={missing_text}, "
                    f"missing posted comments={missing_comments}, "
                    f"not selected={missing_selected}"
                )
            return result
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
            "返回 login_satisfied=true 仅表示登录子流程已经满足，"
            "不能据此提前结束写作、发布等复合任务。"
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
                        const header = document.querySelector(
                            'header, .AppHeader, [role=banner], [data-za-detail-view-path-module="TopNavBar"]'
                        );
                        const hasUserSignals = Boolean(
                            header && header.querySelector(
                                [
                                    '.AppHeader-profileEntry',
                                    '[href*="/people/"]',
                                    '[aria-label*="个人"]',
                                    '[aria-label*="头像"]',
                                    'img[alt*="头像"]'
                                ].join(',')
                            )
                        ) || Boolean(
                            header &&
                            header.querySelector('[aria-label*="通知"]') &&
                            header.querySelector('[aria-label*="私信"], [href*="/messages"]')
                        ) || /退出登录|我的主页/.test(header ? header.innerText : '');
                        const loggedIn = hasUserSignals && !hasLoginDialog && !hasLoginEntry;
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
                "login_satisfied": logged_in,
                "status": status,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
