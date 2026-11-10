"""隔离检查提示词只读页，不连接禅道、模型或业务仓库。"""

from copy import deepcopy
from pathlib import Path
from re import compile as regex
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_previews import PreviewChecks
from workflow.web import LocalServer


def run():
    fixture = PreviewChecks()
    fixture.setUp()
    app = fixture.application
    state = app.sessions.store.new({"kind": "task", "id": 7, "title": "后台会话"})
    state.update(phase="implement", stage="实施中", thread_id="模拟线程", error="会话独有错误",
                 codex={"model": "示例模型", "reasoning_effort": "high"},
                 snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
    runtime = app.sessions.add(state)
    app.default_session_id = state["id"]
    original = deepcopy(state)
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = ROOT / ".local/prompt-catalog-change"
    output.mkdir(parents=True, exist_ok=True)
    actions, errors, reads = [], [], []
    try:
        with patch.object(app.sessions.zentao, "_fetch", side_effect=AssertionError("不得连接禅道")), \
                patch.object(runtime.engine, "ensure_rpc", side_effect=AssertionError("不得调用模型")), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            context = browser.new_context(viewport={"width": 1500, "height": 1050}, permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: actions.append(request.url) if request.method == "POST" else None)
            page.on("request", lambda request: reads.append(request.url) if request.url.endswith("/api/prompts") else None)
            page.goto(server.origin)
            menu = page.locator("#main-navigation")
            expect(menu.get_by_role("menuitem")).to_have_count(4)
            expect(menu.get_by_role("menuitem").nth(2)).to_contain_text("内置提示词")
            menu.get_by_role("menuitem", name="内置提示词").click()
            expect(page.get_by_text("只读展示", exact=True)).to_be_visible()
            expect(page.locator(".prompt-stages > .ant-tabs-nav .ant-tabs-tab-active")).to_have_text("需求分析与计划")
            expect(page.locator(".session-panel")).to_have_count(0)
            expect(page.locator(".workflow-strip")).to_have_count(0)
            expect(page.locator(".session-sidebar")).to_have_count(0)
            expect(page.get_by_text("会话独有错误", exact=True)).to_have_count(0)
            expect(page.locator("textarea, [contenteditable='true']")).to_have_count(0)
            expect(page.get_by_role("button", name="保存设置")).to_have_count(0)
            expect(page.locator(".prompt-text").first).to_contain_text("<proposed_plan>")
            expect(page.locator("proposed_plan")).to_have_count(0)
            text = page.locator(".prompt-text").first.inner_text()
            page.locator(".prompt-copy .ant-typography-copy").first.click()
            page.wait_for_function("async expected => (await navigator.clipboard.readText()).replace(/\\r\\n/g, '\\n') === expected", arg=text)
            page.get_by_role("tab", name="组成片段", exact=True).click()
            page.get_by_text("需求计划指令", exact=True).click()
            expect(page.get_by_text("workflow/prompts.py · PLAN", exact=True)).to_be_visible()
            page.get_by_role("tab", name="Review 审查", exact=True).click()
            expect(page.locator(".prompt-text:visible")).to_contain_text("上轮审查")
            page.get_by_role("tab", name="资料读取", exact=True).click()
            expect(page.get_by_text("由控制器执行，无独立模型提示词", exact=True)).to_be_visible()
            page.get_by_role("tab", name="暂停恢复", exact=True).click()
            page.locator(".prompt-variant:visible .ant-select-selector").click()
            page.get_by_title("恢复Review", exact=True).click()
            expect(page.locator(".prompt-text:visible")).to_contain_text("本轮不修复编号")
            page.get_by_role("tab", name="通用规则", exact=True).click()
            expect(page.locator(".prompt-text:visible")).to_contain_text("通用规则")
            page.screenshot(path=str(output / "prompts-desktop.png"), full_page=True)
            menu.get_by_role("menuitem", name="工作流设置").click()
            expect(page.get_by_label("固定 Review 提示词")).to_be_visible()
            expect(page.get_by_role("button", name="保存设置")).to_be_visible()
            menu.get_by_role("menuitem", name="内置提示词").click()
            page.reload()
            expect(page.get_by_text("只读展示", exact=True)).to_be_visible()
            expect(page.locator(".prompt-stages > .ant-tabs-nav .ant-tabs-tab-active")).to_have_text("需求分析与计划")
            page.set_viewport_size({"width": 740, "height": 980})
            expect(page.locator(".prompt-stages")).to_have_class(regex(r"ant-tabs-top"))
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(output / "prompts-narrow.png"), full_page=True)
            attempts = []

            def reject(route):
                attempts.append(route.request.url)
                route.fulfill(status=400, content_type="application/json", body='{"error":"示例读取失败"}')

            page.route("**/api/prompts", reject)
            page.get_by_role("button", name=regex(r"^刷\s*新$")).click()
            expect(page.get_by_text("提示词读取失败", exact=True)).to_be_visible()
            page.wait_for_timeout(500)
            assert len(attempts) == 1
            page.get_by_role("button", name="重新读取", exact=True).click()
            expect(page.get_by_text("示例读取失败", exact=True)).to_be_visible()
            assert len(attempts) == 2
            page.unroute("**/api/prompts", reject)
            page.get_by_role("button", name="重新读取", exact=True).click()
            expect(page.locator(".prompt-stages")).to_be_visible()
            assert len(reads) >= 5
            assert state == original
            assert actions == []
            assert errors == []
            browser.close()
        return {"页面检查": "通过", "只读": True, "窄屏": True, "复制": True, "人工重试": True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        fixture.doCleanups()


if __name__ == "__main__":
    print(run())
