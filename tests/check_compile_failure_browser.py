"""隔离检查失败原因、只读分析、依赖补齐及代码修复入口，不执行真实编译。"""

from copy import deepcopy
from pathlib import Path
import sys
from threading import Thread
from unittest.mock import Mock, patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_compile_checks import BLOCKED, CompileChecks, MISSING, PASSED
from test_codex_models import CATALOG
from workflow.compile_checks import CompileRunner
from workflow.engine import PHASE_STAGE
from workflow.web import LocalServer, WebApplication


def check():
    fixture = CompileChecks()
    fixture.setUp()
    server = None
    try:
        fixture.start([BLOCKED])
        app = WebApplication(fixture.settings)
        fixture.addCleanup(app.close)
        app.connected = True
        app.codex_catalog = CATALOG
        variants = [("历史失败", BLOCKED), ("缺少插件", {**MISSING, "failure_reasons": [
            "Cannot resolve plugin org.example:compiler:1 in offline mode"]}),
            ("代码错误", fixture.code_error())]
        for number, (title, result) in enumerate(variants):
            state = deepcopy(fixture.state)
            state.update(id=f"compile-browser-{number}", custom_title=title, plan_started_at=state["updated_at"])
            entry = state["compile_checks"][-1]["repositories"][0]
            entry.pop("failure_reasons", None)
            entry.update(result)
            runtime = app.sessions.add(state)

            def start_turn(*_, engine=runtime.engine):
                engine.state["stage"] = PHASE_STAGE[engine.state["phase"]]
                engine.save()
                if engine.state["phase"] == "compile_analysis":
                    engine.last_message = "历史记录没有保存具体诊断，请重新检查以取得准确失败原因。"
                    engine._completed({"status": "completed"}, engine.state["id"], engine.connection_epoch)

            runtime.engine._start_turn = Mock(side_effect=start_turn)
            runtime.engine.save()
            if number == 0:
                app.default_session_id = state["id"]
        server = LocalServer(("127.0.0.1", 0), app)
        server.handle_error = lambda *_: None
        Thread(target=server.serve_forever, daemon=True).start()
        errors = []
        with patch.object(CompileRunner, "check", return_value=deepcopy(PASSED)) as runner, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            expect(page.get_by_text("该历史记录未保存具体失败原因，请重新检查以取得诊断。", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="修复代码并重新检查", exact=True)).to_have_count(0)
            page.get_by_role("button", name="分析失败原因", exact=True).click()
            expect(page.get_by_text("失败分析", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="分析失败原因", exact=True)).to_be_enabled()
            runner.assert_not_called()
            page.reload()
            expect(page.get_by_text("失败分析", exact=True)).to_be_visible()

            page.get_by_text("缺少插件", exact=False).first.click()
            expect(page.get_by_text("Cannot resolve plugin org.example:compiler:1 in offline mode", exact=True)).to_be_visible()
            download = page.get_by_role("button", name="补齐依赖并重新检查（允许下载）", exact=True)
            expect(download).to_be_visible()
            page.reload()
            expect(download).to_be_visible()
            download.click()
            compile_header = page.locator(".compile-checks > .ant-collapse-item > .ant-collapse-header")
            expect(compile_header.get_by_text("通过", exact=True)).to_be_visible()
            expect(compile_header).to_have_attribute("aria-expanded", "false")
            compile_header.click()
            expect(page.get_by_text("编译检查通过。", exact=True)).to_be_visible()
            assert runner.call_count == 1
            assert runner.call_args.args[2] is True

            page.get_by_text("代码错误", exact=False).first.click()
            fix = page.get_by_role("button", name="修复代码并重新检查", exact=True)
            expect(fix).to_be_visible()
            output = ROOT / ".local" / "compile-failure-qa.png"
            page.screenshot(path=str(output), full_page=True)
            fix.click()
            expect(page.get_by_role("button", name="修复代码并重新检查", exact=True)).to_have_count(0)
            runtime = app.sessions.get("compile-browser-2")
            assert runtime.engine.state["phase"] == "compile_fix"
            assert runtime.engine.state["compile_checks"][-1]["repair_paths"] == ["档案服务/业务.java"]
            assert not errors, errors
            browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == "__main__":
    check()
