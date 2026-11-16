"""隔离服务检查目录选择、刷新恢复、基线迁移和窄屏交互。"""

from pathlib import Path
import sys
from threading import Thread
from unittest.mock import Mock, patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_repository_selection import RepositorySelectionChecks
from workflow.gitops import git
from workflow.web import LocalServer, WebApplication


def select_directory(page, name, path, branch):
    page.get_by_role("combobox", name=f"{name}候选目录", exact=True).locator("xpath=ancestor::div[contains(@class,'ant-select-selector')]").click()
    page.locator(".ant-select-dropdown:visible").get_by_text(f"{path}（{branch}）", exact=True).click()


def run():
    fixture = RepositorySelectionChecks()
    fixture.setUp()
    local = patch("workflow.web.LOCAL", fixture.root / "local")
    local.start()
    git(fixture.second(), "switch", "-c", "目标分支")
    git(fixture.second(), "commit", "--amend", "--no-edit", environment_override={
        "GIT_COMMITTER_DATE": "2026-01-01T10:00:00+08:00"})
    app = WebApplication(fixture.settings)
    state = fixture.state
    runtime = app.sessions.add(state)
    engine = runtime.engine
    app.default_session_id = state["id"]

    def plan_ready(*args):
        state["stage"] = "等待计划确认"
        engine.save()

    engine._start_turn = Mock(side_effect=plan_ready)
    engine.detect_repositories()
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    Thread(target=server.serve_forever, daemon=True).start()
    output = ROOT / ".local/repository-selection"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            confirm = page.get_by_role("button", name="确认目录并继续", exact=True)
            expect(confirm).to_be_disabled()
            expect(page.get_by_text("匹配目录的 HEAD 提交者时间相同，请明确选择目录。", exact=True)).to_be_visible()
            page.reload()
            expect(confirm).to_be_disabled()
            assert not state["repos"]
            select_directory(page, "前端", str(fixture.second()), "目标分支")
            expect(confirm).to_be_enabled()
            page.screenshot(path=str(output / "目录选择-宽屏.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            confirm.scroll_into_view_if_needed()
            page.screenshot(path=str(output / "目录选择-窄屏.png"), full_page=True, animations="disabled")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            custom = fixture.make_repo("custom-browser", "目标分支", "2026-02-01T10:00:00+08:00")
            page.get_by_role("textbox", name="前端其他仓库目录", exact=True).fill(str(custom))
            page.get_by_role("button", name="重新检测目录", exact=True).click()
            expect(confirm).to_be_enabled()
            expect(page.get_by_role("combobox", name="前端候选目录", exact=True)
                   .locator("xpath=ancestor::div[contains(@class,'ant-select-selector')]")).to_contain_text(str(custom))
            confirm.click()
            expect(confirm).to_have_count(0)
            expect(page.get_by_role("button", name="变更目录与审查基线", exact=True)).to_be_enabled()
            expect(page.locator(".project-info-panel .project-info-path").first).to_have_attribute("title", str(custom))
            assert state["repos"][0]["path"] == str(custom)
            page.set_viewport_size({"width": 1500, "height": 1050})
            git(fixture.second(), "switch", "-c", "迁移目标分支")
            (fixture.second() / "本机.txt").write_text("保留暂存", encoding="utf-8")
            git(fixture.second(), "add", "本机.txt")
            page.get_by_role("button", name="变更目录与审查基线", exact=True).click()
            modal = page.get_by_role("dialog", name="变更仓库目录与审查基线", exact=True)
            expect(modal.get_by_text("迁移目标分支", exact=False).first).to_be_visible()
            select_directory(page, "前端", str(fixture.second()), "迁移目标分支")
            assert state["repos"][0]["path"] == str(custom)
            page.screenshot(path=str(output / "目录切换-宽屏.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(modal).to_be_visible()
            page.screenshot(path=str(output / "目录切换-窄屏.png"), full_page=True, animations="disabled")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            page.get_by_role("button", name="预览目录与基线", exact=True).click()
            apply = page.get_by_role("button", name="确认变更并保留会话", exact=True)
            expect(apply).to_be_enabled()
            assert state["repos"][0]["path"] == str(custom)
            page.reload()
            expect(apply).to_be_enabled()
            apply.click()
            expect(apply).to_have_count(0)
            expect(page.locator(".project-info-panel .project-info-path").first).to_have_attribute("title", str(fixture.second()))
            assert state["repos"][0]["path"] == str(fixture.second())
            assert state["repos"][0]["branch"] == "迁移目标分支"
            assert state["repos"][0]["preserved_paths"] == ["本机.txt"]
            assert git(fixture.second(), "diff", "--cached", "--name-only") == "本机.txt"
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        local.stop()
        fixture.doCleanups()


if __name__ == "__main__":
    run()
    print("目录检测与切换页面检查通过。")
