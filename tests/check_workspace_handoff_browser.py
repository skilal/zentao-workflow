"""隔离浏览器检查逐仓库交接、取消、窄屏和异常恢复。"""

from pathlib import Path
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_workspace_handoff import HandoffFixture
from workflow.gitops import checkout_snapshot, git
from workflow.settings import WorkflowError
from workflow.web import LocalServer


def run():
    fixture = HandoffFixture()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    state["custom_title"] = "交接检查会话"
    (fixture.target / "本机配置.txt").write_text("IDEA 本机配置", encoding="utf-8")
    (fixture.target / ".run").mkdir()
    (fixture.target / ".run/启动.xml").write_text("启动配置", encoding="utf-8")
    engine.save()
    app.default_session_id = state["id"]
    app.items = state["items"]
    app.codex_catalog = {"default_model": "模拟模型", "models": [
        {"model": "模拟模型", "display_name": "模拟模型", "reasoning_efforts": ["high"], "is_default": True}]}
    original = checkout_snapshot(fixture.target)
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = ROOT / ".local" / "workspace-handoff-qa"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            frontend = page.locator(".repository-details").filter(has_text="前端分支与工作区")
            frontend.locator(":scope > .ant-collapse-item > .ant-collapse-header").click()
            frontend.get_by_role("button", name="交接到本地开发目录", exact=True).click()
            dialog = page.get_by_role("dialog", name="交接到本地开发目录 · 前端", exact=True)
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role("textbox", name="本地开发目录")).to_have_value(str(fixture.target))
            dialog.get_by_role("button", name="预览交接", exact=True).click()
            expect(dialog.get_by_text("源工作目录", exact=True)).to_be_visible()
            expect(dialog.get_by_role("checkbox", name="本机配置.txt", exact=True)).not_to_be_checked()
            self_source = git(fixture.source, "branch", "--show-current")
            assert self_source == "需求分支"
            assert git(fixture.target, "branch", "--show-current") == "原分支"
            # 取消预览不切换分支；再次预览后在手机宽度中核对文件。
            dialog.locator(".ant-modal-footer button").first.click()
            expect(dialog).not_to_be_visible()
            frontend.get_by_role("button", name="交接到本地开发目录", exact=True).click()
            dialog.get_by_role("textbox", name="本地开发目录").fill(fixture.target.as_posix() + "/.")
            dialog.get_by_role("button", name="预览交接", exact=True).click()
            expect(dialog.get_by_role("checkbox", name="本机配置.txt", exact=True)).to_be_visible()
            expect(dialog.get_by_role("textbox", name="本地开发目录")).to_have_value(str(fixture.target))
            for width, height in ((1500, 1050), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                bounds = dialog.bounding_box()
                assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1, bounds
                assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= height + 1, bounds
                page.screenshot(path=str(output / f"交接预览-{width}.png"), animations="disabled")
            dialog.get_by_role("checkbox", name="本机配置.txt", exact=True).check()
            dialog.get_by_role("checkbox", name=".run/启动.xml", exact=True).check()
            dialog.get_by_role("button", name="确认交接", exact=True).click()
            expect(dialog).not_to_be_visible(timeout=30000)
            assert git(fixture.target, "branch", "--show-current") == "需求分支"
            assert git(fixture.source, "branch", "--show-current") == ""
            assert checkout_snapshot(fixture.target) == original
            assert state["repos"][0]["path"] == str(fixture.target)
            assert git(state["repos"][1]["path"], "branch", "--show-current") == "需求分支"
            page.reload()
            frontend.locator(":scope > .ant-collapse-item > .ant-collapse-header").click()
            expect(frontend.get_by_text(str(fixture.target), exact=True)).to_be_visible()
            frontend.get_by_role("button", name="交接到本地开发目录", exact=True).click()
            dialog.get_by_role("button", name="预览交接", exact=True).click()
            expect(dialog.get_by_text("已在该开发目录，无需交接。", exact=True)).to_be_visible()
            expect(dialog.get_by_role("button", name="确认交接", exact=True)).to_be_disabled()
            dialog.locator(".ant-modal-footer button").first.click()
            expect(dialog).not_to_be_visible()
            # 后端独立交接失败时保留恢复页面，刷新后也不自动执行 Git。
            page.set_viewport_size({"width": 1500, "height": 1050})
            backend = page.locator(".repository-details").filter(has_text="后端分支与工作区")
            backend.locator(":scope > .ant-collapse-item > .ant-collapse-header").click()
            backend.get_by_role("button", name="交接到本地开发目录", exact=True).click()
            recovery = page.get_by_role("dialog", name="交接到本地开发目录 · 后端", exact=True)
            recovery.get_by_role("button", name="预览交接", exact=True).click()
            expect(recovery.get_by_role("button", name="确认交接", exact=True)).to_be_enabled()
            backend_target = Path(fixture.settings["repositories"][1]["path"])
            def fail_switch(path, *args, **kwargs):
                if Path(path) == backend_target and args[0] == "switch":
                    raise WorkflowError("模拟后端交接中断")
                return git(path, *args, **kwargs)
            with patch("workflow.workspace_handoff.git", side_effect=fail_switch):
                recovery.get_by_role("button", name="确认交接", exact=True).click()
                expect(recovery.get_by_text("模拟后端交接中断", exact=True)).to_be_visible(timeout=30000)
                expect(recovery.get_by_text("源目录已释放目标分支", exact=False)).to_be_visible()
                expect(recovery.get_by_text("停止时开发目录状态", exact=True)).to_be_visible()
            page.reload()
            expect(recovery).to_be_visible()
            expect(recovery.get_by_text("备份位置", exact=True)).to_be_visible()
            assert git(backend_target, "branch", "--show-current") == "原分支"
            recovery.get_by_role("button", name="核对并继续交接", exact=True).click()
            expect(recovery).not_to_be_visible(timeout=30000)
            assert git(backend_target, "branch", "--show-current") == "需求分支"
            assert state["repos"][0]["path"] == str(fixture.target)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
