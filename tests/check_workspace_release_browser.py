"""隔离浏览器检查释放弹窗、逐仓库接管、禁用状态及基线确认。"""

from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_workspace_handoff import HandoffFixture
from workflow.gitops import git
from workflow.web import LocalServer


def run():
    fixture = HandoffFixture()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    state["custom_title"] = "释放分支占用检查"
    engine.save()
    app.default_session_id = state["id"]
    app.items = state["items"]
    app.codex_catalog = {"default_model": "模拟模型", "models": [
        {"model": "模拟模型", "display_name": "模拟模型", "reasoning_efforts": ["high"], "is_default": True}]}
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = ROOT / ".local/workspace-release-qa"
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

            def unfold_frontend():
                header = frontend.locator(":scope > .ant-collapse-item > .ant-collapse-header")
                if header.get_attribute("aria-expanded") != "true":
                    header.click()

            unfold_frontend()
            frontend.get_by_role("button", name="释放分支占用", exact=True).click()
            dialog = page.get_by_role("dialog", name="释放分支占用 · 前端", exact=True)
            expect(dialog).to_be_visible()
            expect(dialog.get_by_text("当前提交", exact=True)).to_be_visible()
            assert git(fixture.source, "branch", "--show-current") == "需求分支"
            dialog.locator(".ant-modal-footer button").first.click()
            expect(dialog).not_to_be_visible()
            assert git(fixture.source, "branch", "--show-current") == "需求分支"

            frontend.get_by_role("button", name="释放分支占用", exact=True).click()
            expect(dialog).to_be_visible()
            for width, height in ((1500, 1050), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                bounds = dialog.bounding_box()
                assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1, bounds
                assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= height + 1, bounds
                page.screenshot(path=str(output / f"释放预览-{width}.png"), animations="disabled")
            dialog.get_by_role("button", name="确认释放", exact=True).click()
            expect(dialog).not_to_be_visible(timeout=30000)
            expect(frontend.get_by_text("分支占用已释放，可在 IDEA 原项目切换", exact=True)).to_be_visible()
            assert git(fixture.source, "branch", "--show-current") == ""
            expect(page.get_by_role("button", name="立即编译检查", exact=True)).to_be_disabled()

            git(fixture.target, "switch", "需求分支")
            baseline = state["repos"][0]["baseline"]
            page.reload()
            unfold_frontend()
            frontend.get_by_role("button", name="确认改用本地开发目录", exact=True).click()
            adoption = page.get_by_role("dialog", name="确认改用本地开发目录 · 前端", exact=True)
            expect(adoption.get_by_role("textbox", name="本地开发目录")).to_have_value(str(fixture.target))
            adoption.get_by_role("button", name="预览接管", exact=True).click()
            expect(adoption.get_by_text("原任务基线", exact=True)).to_be_visible()
            page.screenshot(path=str(output / "接管预览-390.png"), animations="disabled")
            adoption.get_by_role("button", name="确认改用本地开发目录", exact=True).click()
            expect(adoption).not_to_be_visible(timeout=30000)
            assert state["repos"][0]["path"] == str(fixture.target)
            assert state["repos"][0]["baseline"] == baseline
            expect(frontend.get_by_role("button", name="释放分支占用", exact=True)).to_be_enabled()

            # 当前工作区就是 IDEA 原目录，也可再次释放并按实际 HEAD 预览接管。
            frontend.get_by_role("button", name="释放分支占用", exact=True).click()
            dialog.get_by_role("button", name="确认释放", exact=True).click()
            expect(dialog).not_to_be_visible(timeout=30000)
            git(fixture.target, "switch", "需求分支")
            git(fixture.target, "commit", "--allow-empty", "-m", "IDEA 人工更新")
            frontend.get_by_role("button", name="确认改用本地开发目录", exact=True).click()
            adoption.get_by_role("button", name="预览接管", exact=True).click()
            expect(adoption.get_by_text("IDEA 当前提交已变化，不能直接接管", exact=True)).to_be_visible()
            adoption.get_by_role("button", name="预览基线变化", exact=True).click()
            baseline_dialog = page.get_by_role("dialog", name="确认变更审查基线并接管", exact=True)
            expect(baseline_dialog).to_be_visible()
            page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
            bounds = baseline_dialog.bounding_box()
            assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 845, bounds
            page.screenshot(path=str(output / "基线变化确认-390.png"), animations="disabled")
            baseline_dialog.get_by_role("button", name="确认变更基线并接管", exact=True).click()
            expect(baseline_dialog).not_to_be_visible(timeout=30000)
            expect(adoption).not_to_be_visible()
            assert state["repos"][0]["baseline"] == git(fixture.target, "rev-parse", "HEAD")

            # 未提交修改显示具体文件，保持分支绑定；后台执行时按钮不可用。
            page.set_viewport_size({"width": 1500, "height": 1050})
            (fixture.target / "任务.txt").write_text("未提交修改", encoding="utf-8")
            frontend.get_by_role("button", name="释放分支占用", exact=True).click()
            expect(page.locator("#root").get_by_text("工作区存在未提交修改，请先处理后释放：任务.txt", exact=True)).to_be_visible()
            assert git(fixture.target, "branch", "--show-current") == "需求分支"
            state["stage"] = "实施中"
            engine.turn_running = True
            engine.save()
            expect(frontend.get_by_role("button", name="释放分支占用", exact=True)).to_be_disabled()
            engine.turn_running = False
            state["stage"] = "已完成（静态审查）"
            engine.save()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
