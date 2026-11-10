"""整体页面说明仅在完成后展示，覆盖刷新、替换、会话隔离和窄屏。"""

from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_sessions import SessionFixture
from workflow.web import LocalServer


def run():
    fixture = SessionFixture()
    fixture.setUp()
    app = fixture.application
    engine, state = fixture.first.engine, fixture.first.engine.state
    for runtime, title in ((fixture.first, "汇总主会话"), (fixture.second, "汇总其他会话")):
        runtime.engine.state.update(custom_title=title, phase="followup", stage="续聊处理中", prepare_complete=True,
                                    followup={"text": "修改页面"},
                                    snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
        runtime.engine.save()
    state["change_summary"] = [{"page": "系统管理 → 用户管理", "description": "新增保存时的必填校验"}]
    # 历史逐回合记录保留在旧会话数据中，界面不再读取。
    state["change_locations"] = [{"turn_id": "旧回合", "items": [{"files": ["旧文件.js"]}]}]
    engine.save()
    app.default_session_id = state["id"]
    app.codex_catalog = {"default_model": "示例模型", "models": [
        {"model": "示例模型", "display_name": "示例模型", "reasoning_efforts": ["high"], "is_default": True}]}
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            panel = page.locator(".change-locations")
            expect(page.get_by_text("工作流会话", exact=True)).to_be_visible()
            expect(panel).to_have_count(0)
            state.update(phase="complete", stage="已完成（未审查）")
            engine.save()
            expect(panel.get_by_text("整体改动说明", exact=True)).to_be_visible()
            expect(panel.get_by_text("系统管理 → 用户管理", exact=True)).to_be_visible()
            expect(panel.get_by_text("新增保存时的必填校验", exact=True)).to_be_visible()
            expect(panel).not_to_contain_text("旧文件.js")
            expect(panel).not_to_contain_text("回合")
            page.reload()
            expect(panel.get_by_text("新增保存时的必填校验", exact=True)).to_be_visible()
            state["change_summary"] = [{"page": "档案管理 → 检索页面", "description": "调整查询条件"}]
            engine.save()
            expect(panel.get_by_text("调整查询条件", exact=True)).to_be_visible()
            expect(panel).not_to_contain_text("新增保存时的必填校验")
            for width in (1440, 820, 600):
                page.set_viewport_size({"width": width, "height": 1050})
                assert panel.evaluate("element => element.scrollWidth <= element.clientWidth"), width
            page.set_viewport_size({"width": 1440, "height": 1050})
            fixture.second.engine.state.update(phase="complete", stage="已完成（未审查）")
            fixture.second.engine.save()
            page.locator(".session-entry-custom-title").get_by_text("汇总其他会话", exact=True).click()
            expect(panel.get_by_text("暂无整体改动说明", exact=True)).to_be_visible()
            page.locator(".session-entry-custom-title").get_by_text("汇总主会话", exact=True).click()
            expect(panel.get_by_text("调整查询条件", exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
