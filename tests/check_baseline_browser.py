"""隔离检查基线预览、取消、刷新恢复及确认，不连接业务服务。"""

from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_baseline_change import BaselineChangeChecks
from workflow.web import LocalServer, WebApplication
from workflow.gitops import git


def run():
    fixture = BaselineChangeChecks()
    fixture.setUp()
    app = WebApplication(fixture.engine.settings)
    state = fixture.state
    state.update(phase="implement", followup=None,
                 codex={"model": "隔离模型", "reasoning_effort": "high"},
                 snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
    targets = fixture.relocation_targets()
    runtime = app.sessions.add(state)
    runtime.engine.save()
    app.default_session_id = state["id"]
    server = LocalServer(("127.0.0.1", 0), app)
    Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1600, "height": 1100})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            preview = page.get_by_role("button", name="变更目录与审查基线", exact=True)
            expect(preview).to_be_enabled()
            baseline = state["repos"][0]["baseline"]
            preview.click()
            page.get_by_role("button", name="预览目录与基线", exact=True).click()
            confirm = page.get_by_role("button", name="确认变更并保留会话", exact=True)
            expect(confirm).to_be_enabled()
            expect(page.get_by_text("旧基线", exact=True)).to_have_count(2)
            assert state["repos"][0]["baseline"] == baseline
            page.get_by_role("button", name="取消变更", exact=True).click()
            expect(confirm).to_have_count(0)
            assert state["repos"][0]["baseline"] == baseline
            preview.click()
            page.get_by_role("button", name="预览目录与基线", exact=True).click()
            expect(confirm).to_be_enabled()
            page.reload()
            expect(confirm).to_be_enabled()
            confirm.click()
            expect(confirm).to_have_count(0)
            expect(page.get_by_role("button", name="从暂停点继续", exact=True)).to_be_enabled()
            assert state["repos"][0]["baseline"] != baseline
            assert state["thread_id"] == "原线程"
            assert state["history"][0]["text"] == "原始需求及回答"
            assert state["stage"] == "已暂停"
            assert Path(state["baseline_changes"][-1]["backup"]).is_file()
            # 原目录游离 HEAD 时，仍可填写新目录并迁移原会话。
            git(state["repos"][0]["path"], "switch", "--detach")
            preview.click()
            for name, path in targets.items():
                page.get_by_role("textbox", name=f"{name}仓库目录", exact=True).fill(path)
            page.get_by_role("button", name="预览目录与基线", exact=True).click()
            expect(confirm).to_be_enabled()
            expect(page.get_by_text("原工作目录", exact=True)).to_have_count(2)
            confirm.click()
            expect(confirm).to_have_count(0)
            assert all(repo["path"] == targets[repo["name"]] for repo in state["repos"])
            assert state["thread_id"] == "原线程"
            assert state["repos"][0]["preserved_paths"] == ["本机.txt"]
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        fixture.doCleanups()


if __name__ == "__main__":
    run()
    print("基线变更页面检查通过。")
