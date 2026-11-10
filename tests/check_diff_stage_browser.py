"""核对改动阶段的提示、暂停状态与真实模型回合展示。"""

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
    app, engine = fixture.application, fixture.first.engine
    state = engine.state
    state.update(phase="implement", stage="核对改动中", followup={"text": "核对改动"},
                 snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                 progress_turns=[{"id": "模型已结束", "phase": "implement", "history_index": 0,
                                  "status": "completed", "items": []}])
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
            message = page.get_by_text("正在核对代码差异与修改归属…", exact=False)
            expect(message).to_be_visible()
            expect(page.get_by_text("模型正在处理中…", exact=True)).to_have_count(0)
            page.reload()
            expect(message).to_be_visible()
            state["stage"] = "已暂停"
            engine.save()
            expect(message).to_have_count(0)
            state["stage"] = "实施中"
            state["progress_turns"][-1].update(id="模型新回合", status="running")
            engine.save()
            expect(page.get_by_text("模型正在处理中", exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
