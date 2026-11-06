"""使用隔离服务验证外部工作区预览、确认、刷新恢复和窄屏交互。"""

from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_workspace_reuse_confirmation import WorkspaceReuseConfirmationChecks
from workflow.gitops import git
from workflow.web import LocalServer


def run():
    fixture = WorkspaceReuseConfirmationChecks()
    fixture.setUp()
    app, state = fixture.application, fixture.state
    app.default_session_id = state["id"]
    output = ROOT / ".local/workspace-reuse-implementation"
    output.mkdir(parents=True, exist_ok=True)
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            for stage in ("准备工作区失败", "已停止", "已暂停"):
                state["stage"] = stage
                fixture.engine.save()
                page.goto(server.origin)
                expect(page.get_by_role("button", name="预览复用已有工作区（前端）", exact=True)).to_be_enabled()
            preview = page.get_by_role("button", name="预览复用已有工作区（前端）", exact=True)
            confirm = page.get_by_role("button", name="确认复用并继续", exact=True)
            cancel = page.get_by_role("button", name="取消复用", exact=True)
            preview.click()
            expect(confirm).to_be_enabled()
            expect(page.get_by_text("初始基线", exact=True)).to_be_visible()
            expect(page.get_by_text("未被其他未完成会话占用", exact=True)).to_be_visible()
            assert not state["repos"]
            cancel.click()
            expect(confirm).to_have_count(0)
            assert not state["repos"]
            preview.click()
            expect(confirm).to_be_enabled()
            token = state["workspace_reuse_request"]["token"]
            page.reload()
            expect(confirm).to_be_enabled()
            assert state["workspace_reuse_request"]["token"] == token
            assert not state["repos"]
            page.screenshot(path=str(output / "外部工作区确认-宽屏.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(confirm).to_be_enabled()
            confirm.scroll_into_view_if_needed()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            page.screenshot(path=str(output / "外部工作区确认-窄屏.png"), full_page=True)
            # 预览后目录变化须重新预览；失败确认不会接管或启动模型。
            (fixture.external / "本机.txt").write_text("保留本机资料", encoding="utf-8")
            confirm.click()
            expect(page.get_by_text("工作区、HEAD 或文件在预览后发生变化，请重新预览并明确确认。", exact=True).first).to_be_visible()
            assert not state["repos"]
            fixture.engine._start_turn.assert_not_called()
            page.get_by_role("button", name="重新预览", exact=True).click()
            expect(page.get_by_text("本机.txt", exact=True)).to_be_visible()
            # 同一入口在续聊准备失败时继续原消息及原线程。
            cancel.click()
            expect(confirm).to_have_count(0)
            state.update(stage="已停止", phase="followup", thread_id="原线程",
                         followup={"text": "继续原消息", "plan_mode": False})
            fixture.engine.save()
            page.reload()
            preview.click()
            expect(confirm).to_be_enabled()
            confirm.click()
            expect(confirm).to_have_count(0)
            fixture.engine.executor.submit(lambda: None).result(timeout=20)
            assert state["prepare_complete"]
            assert state["thread_id"] == "原线程"
            assert state["followup"]["text"] == "继续原消息"
            assert state["repos"][0]["path"] == str(fixture.external)
            assert state["repos"][0]["preserved_paths"] == ["本机.txt"]
            assert state["repos"][0]["baseline"] == git(fixture.external, "rev-parse", "HEAD")
            fixture.engine._start_turn.assert_called_once()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        fixture.doCleanups()


if __name__ == "__main__":
    run()
    print("外部工作区复用页面检查通过。")
