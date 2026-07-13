from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "examples" / "design-domain-shadow" / "apple-cjk-ab.html").as_uri()


def _open_page(browser, **context_options):
    context = browser.new_context(**context_options)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(PAGE, wait_until="load")
    return context, page, errors


def test_design_shadow_interactions_and_mobile_layout():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context, page, errors = _open_page(
            browser, viewport={"width": 1440, "height": 1050}
        )

        assert page.locator(".apple-nav li").first.evaluate(
            "element => getComputedStyle(element).fontSize"
        ) == "13px"
        assert page.locator("#sheet-close").evaluate(
            "element => [getComputedStyle(element).width, getComputedStyle(element).height]"
        ) == ["44px", "44px"]
        assert page.locator("[aria-live]").count() == 0
        assert page.locator(".apple-nav button").count() == 0

        todo = page.locator("#todo-tab")
        overview = page.locator("#overview-tab")
        panel = page.locator("#apple-panel")
        todo.click()
        todo.press("Home")
        page.wait_for_timeout(220)
        assert overview.get_attribute("aria-selected") == "true"
        assert "is-switching" not in (panel.get_attribute("class") or "")
        assert panel.evaluate("element => getComputedStyle(element).opacity") == "1"

        trigger = page.locator("#sheet-trigger")
        close = page.locator("#sheet-close")
        trigger.click()
        assert close.evaluate("element => document.activeElement === element") is True
        assert page.locator(".apple-shell").evaluate("element => element.inert") is True
        close.click()
        page.wait_for_timeout(40)
        trigger.evaluate("element => element.click()")
        assert page.locator("#apple-variant").evaluate(
            "element => element.classList.contains('sheet-open')"
        ) is True
        close.click()
        page.wait_for_timeout(260)
        assert page.locator("#daily-sheet").is_hidden()
        assert trigger.evaluate("element => document.activeElement === element") is True

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="load")
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        ) is True
        page.locator("#sheet-trigger").click()
        bounds = page.locator("#daily-sheet").bounding_box()
        assert bounds is not None
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        assert errors == []
        context.close()

        reduced_context, reduced_page, reduced_errors = _open_page(
            browser,
            viewport={"width": 900, "height": 900},
            reduced_motion="reduce",
        )
        reduced_panel = reduced_page.locator("#apple-panel")
        transition = reduced_panel.evaluate(
            "element => { const style = getComputedStyle(element); return [style.transitionProperty, style.transitionDuration]; }"
        )
        assert transition == ["opacity", "0.16s"]
        reduced_page.locator("#todo-tab").click()
        assert reduced_page.locator("#todo-tab").get_attribute("aria-selected") == "true"
        assert "is-switching" not in (reduced_panel.get_attribute("class") or "")
        reduced_page.locator("#sheet-trigger").click()
        assert reduced_page.locator("#daily-sheet").evaluate(
            "element => getComputedStyle(element).transform"
        ) == "none"
        assert reduced_errors == []
        reduced_context.close()
        browser.close()
