"""隔离页面检查自动分配目录与冲突回退提示，使用现有页面组件。"""

from pathlib import Path
import sys
from threading import Thread
from unittest.mock import Mock

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_workspace_allocation import WorkspaceAllocationChecks
from workflow.gitops import checkout_snapshot, git
from workflow.web import LocalServer, WebApplication


def run():
    fixture = WorkspaceAllocationChecks()
    fixture.setUp()
    app = WebApplication(fixture.settings)
    state = fixture.state
    runtime = app.sessions.get(state["id"])
    engine = runtime.engine
    def start(*_):
        state.update(stage="等待计划补充")
        engine.save()
    engine._start_turn = Mock(side_effect=start)
    state.update(stage="等待选择目录", phase="branch", branch_resolution={"repositories": [
        {**repo, "branch": "项目/develop", "status": "resolved", "source": "common", "reason": "人工常用分支", "question": "",
         "candidates": [], "catalog": [], "refreshed": False} for repo in fixture.settings["repositories"]]})
    engine.state = state
    engine._detect_repositories("prepare", state["branch_resolution"]["repositories"], {})
    app.default_session_id = state["id"]
    output = ROOT / ".local/workspace-allocation-browser"
    output.mkdir(parents=True, exist_ok=True)
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1440, "height": 1024})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            button = page.get_by_role("button", name="确认目录并继续", exact=True)
            expect(button).to_be_enabled()
            expect(page.get_by_text("将按顺序切换空闲目录；没有空闲目录时创建独立工作区。", exact=True).first).to_be_visible()
            page.screenshot(path=str(output / "目录分配-宽屏.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(button).to_be_enabled()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            button.click()
            expect(page.get_by_text("等待计划补充", exact=True).first).to_be_visible(timeout=30000)
            assert len(state["repos"]) == 2
            assert all(git(repo["path"], "branch", "--show-current") == "项目/develop" for repo in state["repos"])
            selection = state["selections"][0]
            state.update(stage="准备工作区失败", checkout_request={"status": "rolled_back", "selection": selection,
                "from_branch": "其他项目/develop", "path": selection["path"], "head": state["repos"][0]["baseline"],
                "baseline": state["repos"][0]["baseline"], "snapshot": checkout_snapshot(selection["path"]), "automatic": True})
            engine.save()
            page.reload()
            expect(page.get_by_text("智能切换发生冲突，已恢复原分支、文件及暂存状态。请人工处理目标分支切换后确认继续。", exact=True)).to_be_visible()
            page.screenshot(path=str(output / "冲突回退-窄屏.png"), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        fixture.doCleanups()


if __name__ == "__main__":
    run()
