"""隔离检查新建会话的输入、项目、图片及刷新恢复，不访问真实禅道或 Codex。"""

from base64 import b64encode
from pathlib import Path
import sys
from threading import Thread
from unittest.mock import Mock, patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_conversations import ConversationChecks, PNG
from workflow.engine import Workflow
from workflow.web import LocalServer


def run():
    fixture = ConversationChecks()
    fixture.setUp()
    fixture.prepare_repositories()
    app = fixture.application
    app.sessions.zentao.list_projects = Mock(return_value=[{"id": 41, "name": "项目甲"}, {"id": 42, "name": "项目乙"}])
    def connect(engine):
        engine.state["thread_id"] = "模拟线程"
        engine.state["rpc_connected"] = True
        engine.rpc = Mock(model="示例模型")
        engine.rpc.request.return_value = {"turn": {"id": "模拟回合"}}
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with patch.object(Workflow, "ensure_rpc", connect), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.origin)
            page.get_by_role("button", name="新建会话", exact=True).click()
            message = page.get_by_placeholder("直接输入需求、问题或修改要求，可粘贴截图或图片，无需关联工作项…")
            send = page.get_by_role("button", name="发送并开始计划", exact=True)
            expect(message).to_be_enabled()
            expect(send).to_be_disabled()
            assert not app.snapshot()["history"]
            message.fill("调整页面交互")
            expect(send).to_be_enabled()

            # 失败附件须明确移除，不能带着失败队列开始计划。
            page.locator("input[type=file]").set_input_files({"name": "错误.svg", "mimeType": "image/svg+xml", "buffer": b"<svg/>"})
            failed = page.locator(".attachment").filter(has_text="错误.svg")
            expect(failed.get_by_text("上传失败", exact=True)).to_be_visible()
            expect(send).to_be_disabled()
            failed.get_by_role("button", name="移除", exact=True).click()
            expect(send).to_be_enabled()
            message.evaluate("""(input, data) => {
                const transfer = new DataTransfer();
                transfer.items.add(new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))], '需求截图.png', { type: 'image/png' }));
                transfer.setData('text/plain', '，参考截图');
                input.selectionStart = input.selectionEnd = input.value.length;
                input.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
            }""", b64encode(PNG).decode())
            expect(message).to_have_value("调整页面交互，参考截图")
            expect(page.get_by_alt_text("需求截图.png")).to_be_visible()

            # 项目可在发送前调整；保持无项目发送时，等待人工确认。
            project = page.get_by_role("combobox", name="查看阶段所属项目", exact=True)
            project.locator("xpath=ancestor::div[contains(@class, 'ant-select-selector')]").click()
            page.locator(".ant-select-dropdown:visible").get_by_text("项目甲 #41", exact=True).click()
            expect(project).to_have_attribute("aria-expanded", "false")
            expect(send).to_be_enabled()
            project.locator("xpath=ancestor::div[contains(@class, 'ant-select-selector')]").click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(has_text="无项目对话").click()
            expect(send).to_be_enabled()
            send.click()
            expect(page.get_by_text("等待确认项目", exact=True).first).to_be_visible()
            assert len(app.snapshot()["history"]) == 1
            runtime = next(iter(app.sessions.runtimes.values()))
            fixture.drain(runtime)
            assert runtime.engine.state["items"] == []
            page.get_by_role("combobox", name="所属项目", exact=True).locator("xpath=ancestor::div[contains(@class, 'ant-select-selector')]").click()
            page.locator(".ant-select-dropdown:visible").get_by_text("项目甲 #41", exact=True).click()
            page.get_by_role("button", name="确认项目并继续", exact=True).click()
            expect(page.get_by_text("计划中", exact=True).first).to_be_visible()
            fixture.drain(runtime)
            user_message = page.locator(".chat-entry.user").filter(has_text="调整页面交互，参考截图")
            expect(user_message).to_be_visible()
            expect(user_message.get_by_alt_text("需求截图.png")).to_be_visible()
            page.wait_for_function("() => [...document.querySelectorAll('.chat-entry.user img')].every(img => img.complete && img.naturalWidth > 0)")
            page.reload()
            expect(user_message).to_be_visible()
            expect(user_message.get_by_alt_text("需求截图.png")).to_be_visible()

            # 当前项目在下一次新建中继承，纯图片也可以发送。
            page.get_by_role("button", name="新建会话", exact=True).click()
            expect(message).to_be_enabled()
            expect(page.locator(".project-info-name")).to_have_text("项目甲")
            expect(send).to_be_disabled()
            page.locator("input[type=file]").set_input_files({"name": "纯图片.png", "mimeType": "image/png", "buffer": PNG})
            expect(page.get_by_alt_text("纯图片.png")).to_be_visible()
            expect(send).to_be_enabled()
            for width in (1500, 720, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"页面横向溢出：{width}"
            output = ROOT / ".local" / "conversations-qa"
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / "新建无关联会话.png"), full_page=True)
            send.click()
            expect(page.get_by_text("计划中", exact=True).first).to_be_visible()
            assert len(app.snapshot()["history"]) == 2
            assert app.snapshot()["history"][0]["title"] == "图片需求"
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
