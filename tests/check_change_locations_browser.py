"""位置面板的历史折叠、实时完成、刷新、会话隔离及窄屏检查。"""

from copy import deepcopy
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
    engine = fixture.first.engine
    state = engine.state
    for runtime, title in ((fixture.first, "位置主会话"), (fixture.second, "位置其他会话")):
        runtime.engine.state.update(custom_title=title, phase="implement", stage="已暂停", prepare_complete=True,
                                    thread_id=f"模拟线程{title}",
                                    followup={"text": "查看位置说明"},
                                    snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
        runtime.engine.save()
    item = {"status": "located", "location_path": ["系统管理", "用户管理", "编辑弹窗", "保存按钮"],
            "route": "/system/users", "change": "新增必填校验", "steps": ["打开用户管理", "编辑用户后点击保存"], "reason": "",
            "files": [{"repository": "前端", "path": "src/views/system/users/页面.js"}],
            "evidence": [{"repository": "前端", "path": "src/router/routes.js", "line": 12, "description": "页面入口"}]}
    old = {"turn_id": "历史回合", "delivery_round": 1, "phase": "implement", "completed_at": state["updated_at"], "items": [deepcopy(item)]}
    old["items"][0]["change"] = "历史位置说明"
    state["change_locations"] = [old]
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
            expect(panel.get_by_text("历史位置说明", exact=True)).to_be_visible()
            newest = {"turn_id": "新回合", "delivery_round": 2, "phase": "followup", "completed_at": state["updated_at"], "items": [item]}
            state["change_locations"].append(newest)
            engine.save()
            expect(panel.get_by_text("新增必填校验", exact=True)).to_be_visible()
            expect(panel.get_by_text("历史位置说明", exact=True)).not_to_be_visible()
            expect(panel.get_by_text("系统管理 → 用户管理 → 编辑弹窗 → 保存按钮", exact=True).first).to_be_visible()
            expect(panel.get_by_text("前端 / src/router/routes.js:12 · 页面入口", exact=True).first).to_be_visible()
            panel.get_by_text("第 1 轮交付", exact=False).click()
            expect(panel.get_by_text("历史位置说明", exact=True)).to_be_visible()
            panel.get_by_text("第 1 轮交付", exact=False).click()
            page.reload()
            expect(panel.get_by_text("新增必填校验", exact=True)).to_be_visible()
            expect(panel.get_by_text("历史位置说明", exact=True)).not_to_be_visible()

            unknown = deepcopy(item)
            unknown.update(status="unresolved", location_path=[], steps=[], evidence=[], reason="菜单由数据库配置，尚未取得资料", change="更新接口返回值")
            no_ui = deepcopy(unknown)
            no_ui.update(status="no_ui", reason="仅定时任务使用", change="修复后台任务")
            state["change_locations"].append({**newest, "turn_id": "后端回合", "phase": "compile_fix", "items": [unknown, no_ui]})
            engine.save()
            expect(panel.get_by_text("菜单由数据库配置，尚未取得资料", exact=True)).to_be_visible()
            expect(panel.get_by_text("仅定时任务使用", exact=True)).to_be_visible()
            for width in (1440, 820, 600):
                page.set_viewport_size({"width": width, "height": 1050})
                assert panel.evaluate("element => element.scrollWidth <= element.clientWidth"), width
            page.set_viewport_size({"width": 1440, "height": 1050})
            page.locator(".session-entry-custom-title").get_by_text("位置其他会话", exact=True).click()
            expect(panel.get_by_text("暂无位置记录", exact=True)).to_be_visible()
            expect(panel.get_by_text("更新接口返回值", exact=True)).not_to_be_visible()
            page.locator(".session-entry-custom-title").get_by_text("位置主会话", exact=True).click()
            expect(panel.get_by_text("更新接口返回值", exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
