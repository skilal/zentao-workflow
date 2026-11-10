"""隔离浏览器验证共享会话及片段预览，不连接业务服务。"""

from json import dumps, loads
from pathlib import Path
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_shared_workspace import SharedWorkspaceChecks
from workflow.settings import ROOT as PROJECT_ROOT
from workflow.shared_changes import candidates
from workflow.web import LocalServer, WebApplication


def run():
    fixture = SharedWorkspaceChecks()
    fixture.setUp()
    application = server = None
    try:
        fixture.replace(fixture.first, "共享.txt", "原始第3行", "甲需求内容")
        fixture.replace(fixture.second, "共享.txt", "原始第12行", "乙需求内容")
        for state, title in ((fixture.first, "共享甲会话"), (fixture.second, "共享乙会话")):
            state.update(custom_title=title, prepare_complete=True, thread_id="隔离线程", max_fix_rounds=3,
                         codex={"model": "模拟模型", "reasoning_effort": "high"},
                         snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
            fixture.store.save(state)
        state = fixture.first
        fingerprint = fixture.review(state)
        selected = state["repos"][0]["commit_scope"]
        result = {"summary": "已核对本轮片段", "findings": [], "resolved_previous": [], "commit_messages": [],
                  "commit_scope": [{"repository": "前端", "include": selected, "exclude": []}]}
        state.update(phase="review", stage="等待修复选择", review_manifest=fingerprint,
                     reviews=[{"number": 1, "fix_round": 0, "delivery_round": 1, "protocol_version": 3,
                               "history_index": 0, "result": result, "manifest": fingerprint}],
                     history=[{"role": "Codex", "phase": "review", "time": state["updated_at"],
                               "text": dumps(result), "review_protocol": 3, "delivery_round": 1}])
        fixture.store.save(state)
        with patch("workflow.web.LOCAL", fixture.root / "state"):
            application = WebApplication(loads((PROJECT_ROOT / "settings.example.json").read_text(encoding="utf-8")))
        application.codex_catalog = {"default_model": "模拟模型", "models": [
            {"model": "模拟模型", "display_name": "模拟模型", "reasoning_efforts": ["high"], "is_default": True}]}
        application.default_session_id = state["id"]
        server = LocalServer(("127.0.0.1", 0), application)
        server.handle_error = lambda *_: None
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1440, "height": 1080})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            details = page.locator(".repository-details").first
            expect(details).to_be_visible()
            header = details.locator(".ant-collapse-header").first
            if header.get_attribute("aria-expanded") != "true":
                header.click()
            expect(page.get_by_text("共享开发目录", exact=True)).to_be_visible()
            expect(page.get_by_text("共同使用", exact=True)).to_be_visible()
            expect(page.get_by_text("共享乙会话", exact=True).last).to_be_visible()
            expect(page.get_by_text("前端：纳入 1 个片段，排除 0 个片段", exact=True)).to_be_visible()
            page.get_by_role("button", name="查看累计代码差异").click()
            drawer = page.locator(".ant-drawer-content")
            expect(drawer.get_by_text("本轮代码差异与归属", exact=True)).to_be_visible()
            expect(drawer.get_by_text("本轮", exact=True)).to_be_visible()
            expect(drawer.get_by_text("其他会话", exact=True)).to_be_visible()
            expect(drawer.locator(".diff-text").last).to_contain_text("甲需求内容")
            expect(drawer.locator(".diff-text").last).not_to_contain_text("乙需求内容")
            assert not errors, errors
            output = PROJECT_ROOT / "work" / "shared-workspace-qa.png"
            output.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(output), full_page=True)
            browser.close()
        print("共享会话及分片预览检查通过")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if application:
            application.close()
        fixture.doCleanups()


if __name__ == "__main__":
    run()
