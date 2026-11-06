"""编译双入口及自动分析、修复、重查的隔离页面检查，不执行真实编译。"""

from copy import deepcopy
from pathlib import Path
from re import compile
import sys
from threading import Thread
from unittest.mock import Mock, patch
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_compile_automation import CompileAutomationChecks
from test_compile_checks import PASSED
from test_codex_models import CATALOG
from workflow.compile_checks import CompileRunner
from workflow.engine import PHASE_STAGE
from workflow.mcp_server import Tools
from workflow.web import LocalServer, WebApplication


def run():
    fixture = CompileAutomationChecks()
    fixture.setUp()
    app = server = None
    try:
        failure = fixture.caller_failure()
        fixture.state.update(phase="complete", stage="已完成（未审查）", compile_check={"enabled": False})
        fixture.flow.save()
        app = WebApplication(fixture.fixture.settings)
        app.codex_catalog = CATALOG
        app.default_session_id = fixture.state["id"]
        engine = app.sessions.get(fixture.state["id"]).engine
        stages = []
        def model_turn(text, output_schema=None):
            state = engine.state
            stages.append(state["phase"])
            state.update(stage=PHASE_STAGE[state["phase"]], turn_id=uuid4().hex)
            engine.start_progress(state["turn_id"])
            engine.save()
            if state["phase"] == "compile_analysis":
                assert output_schema is not None
                engine.last_message = fixture.analysis()
            elif state["phase"] == "compile_fix":
                tools = Tools(state["id"])
                tools.store = engine.store
                tools.call("replace_text", {"repository": "后端", "path": "技术组件/调用方.java", "old": "旧方法()", "new": "新方法()"})
                engine.last_message = "已修复任务相关调用。"
            else:
                raise AssertionError("独立编译不应进入其他模型阶段：" + state["phase"])
            engine._completed({"status": "completed", "id": state["turn_id"]}, state["id"], engine.connection_epoch)
        engine._start_turn = Mock(side_effect=model_turn)
        server = LocalServer(("127.0.0.1", 0), app)
        server.handle_error = lambda *_: None
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        with patch.object(CompileRunner, "check", side_effect=[failure, deepcopy(PASSED)]) as runner, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            button = page.get_by_role("button", name="立即编译检查", exact=True)
            expect(button).to_be_enabled()
            toggle = page.get_by_role("switch", name="本轮后端编译检查", exact=True)
            expect(toggle).not_to_be_checked()
            button.click()
            header = page.locator(".compile-checks > .ant-collapse-item > .ant-collapse-header")
            expect(header.get_by_text("通过", exact=True)).to_be_visible(timeout=30000)
            assert stages == ["compile_analysis", "compile_fix"], stages
            assert runner.call_count == 2
            assert engine.state["phase"] == "complete"
            assert engine.state["compile_check"]["enabled"] is False
            expect(toggle).not_to_be_checked()
            expect(button).to_be_enabled()
            header.click()
            expect(header).to_have_attribute("aria-expanded", "true")
            expect(page.get_by_text("独立检查", exact=True)).to_be_visible()
            expect(page.get_by_text("自动修复 1/3", exact=False)).to_be_visible()
            page.screenshot(path=str(ROOT / ".local" / "compile-automation-qa.png"), full_page=True)
            engine.followup = Mock()
            toggle.click()
            page.reload()
            expect(toggle).to_be_checked()
            page.get_by_placeholder("输入问题、需要获取的信息或修改要求，可粘贴图片和文件…").fill("继续调整代码")
            page.get_by_role("button", name=compile(r"^发\s*送$")).click()
            expect(page.get_by_placeholder("输入问题、需要获取的信息或修改要求，可粘贴图片和文件…")).to_have_value("")
            assert engine.followup.call_args.args[-1] is True
            engine.state.update(stage="等待人工回答", pending={"id": "待回答", "method": "workflow/ask_user",
                                "params": {"questions": [{"id": "规则", "header": "规则", "question": "需要补充业务规则"}]}})
            engine.save()
            expect(button).to_be_disabled()
            assert not errors, errors
            browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(5)
        if app:
            app.close()
        fixture.doCleanups()


if __name__ == "__main__":
    run()
