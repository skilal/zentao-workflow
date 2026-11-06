"""隔离检查自动命名的实时显示、搜索、人工改名与刷新保留。"""

from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_session_titles import GeneratedSessionTitleChecks
from workflow.web import LocalServer


def run():
    fixture = GeneratedSessionTitleChecks()
    fixture.setUp()
    app = fixture.application
    app.codex_catalog = {"default_model": "模拟模型", "models": [
        {"model": "模拟模型", "display_name": "模拟模型", "reasoning_efforts": ["high"], "is_default": True}]}
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.origin)
            search = page.get_by_placeholder("搜索会话或项目名称、编号")
            search.fill("原任务文本")
            title = page.locator(".session-entry-custom-title")
            expect(title).to_have_text("待命名会话")
            fixture.register()
            fixture.title_event()
            expect(title).to_have_text("全宗号范围检索")
            expect(page.locator(".session-entry-title")).to_contain_text("任务 #7 · 原任务文本")
            search.fill("全宗号范围检索")
            expect(title).to_be_visible()
            page.reload()
            search.fill("全宗号范围检索")
            expect(title).to_have_text("全宗号范围检索")
            page.get_by_role("button", name="修改会话标题", exact=True).click()
            page.get_by_role("textbox", name="会话标题", exact=True).fill("人工指定会话名")
            page.locator(".ant-modal-confirm-btns .ant-btn-primary").click()
            search.fill("人工指定会话名")
            expect(title).to_have_text("人工指定会话名")
            fixture.title_event()
            expect(title).to_have_text("人工指定会话名")
            page.reload()
            search.fill("人工指定会话名")
            expect(title).to_have_text("人工指定会话名")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
