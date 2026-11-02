"""隔离检查逐题文件上传、粘贴、失败处理、刷新及提交，不连接真实 Codex。"""

from base64 import b64encode
from pathlib import Path
from re import compile
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_answer_attachments import AnswerAttachmentFixture, PNG
from workflow.web import LocalServer


def run():
    fixture = AnswerAttachmentFixture()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    state["repos"] = [{"name": "前端", "role": "frontend", "path": str(fixture.root), "branch": "main", "baseline": "0" * 40}]
    state["plan_round"] = {"number": 1, "turns": 1, "asked": True}
    engine.save()
    app.default_session_id = state["id"]
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with patch("workflow.engine.ensure_baseline"), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            first = page.locator(".question").filter(has_text="请提供参考模板")
            second = page.locator(".question").filter(has_text="请说明验收规则")
            submit = page.get_by_role("button", name="提交回答并继续", exact=True)
            try:
                expect(submit).to_be_disabled()
            except AssertionError as error:
                raise AssertionError(f"页面错误：{errors}；页面内容：{page.locator('body').inner_text()[:1800]}") from error

            # 上传失败保留在对应题目，所有问题完成后也不能带失败任务提交。
            second.locator("textarea").fill("按现有规则验收")
            page.route("**/api/answer-attachments?**", lambda route: route.fulfill(status=400, json={"error": "模拟上传失败"}), times=1)
            first.locator("input[type=file]").set_input_files({"name": "模板.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-reference"})
            failed = first.locator(".attachment").filter(has_text="模板.pdf")
            expect(failed.get_by_text("上传失败", exact=True)).to_be_visible()
            expect(submit).to_be_disabled()
            failed.get_by_role("button", name=compile(r"重\s*试")).click()
            expect(failed.get_by_text("已上传", exact=True)).to_be_visible()
            expect(submit).to_be_enabled()
            assert state["stage"] == "等待人工回答"
            assert len(state["answer_attachments"]) == 1
            assert not any(entry["role"] == "用户" for entry in state["history"])

            # 分批多文件、移除及逐题区分。
            second.locator("input[type=file]").set_input_files([
                {"name": "验收.txt", "mimeType": "text/plain", "buffer": "验收内容".encode()},
                {"name": "待移除.bin", "mimeType": "application/octet-stream", "buffer": b"data"}])
            removed = second.locator(".attachment").filter(has_text="待移除.bin")
            expect(removed.get_by_text("已上传", exact=True)).to_be_visible()
            removed.get_by_role("button", name="移除", exact=True).click()
            expect(removed).to_have_count(0)
            expect(first.get_by_text("验收.txt", exact=True)).to_have_count(0)
            expect(second.get_by_text("验收.txt", exact=True)).to_be_visible()

            # 粘贴图片时保留同次剪贴板文字，并关联到该题。
            second.locator("textarea").evaluate("""(input, data) => {
                const transfer = new DataTransfer();
                transfer.items.add(new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))], '规则截图.png', { type: 'image/png' }));
                transfer.setData('text/plain', '，参考截图');
                input.selectionStart = input.selectionEnd = input.value.length;
                input.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
            }""", b64encode(PNG).decode())
            expect(second.get_by_alt_text("规则截图.png")).to_be_visible()
            expect(second.locator("textarea")).to_have_value("按现有规则验收，参考截图")

            page.reload()
            expect(first.get_by_text("模板.pdf", exact=True)).to_be_visible()
            expect(second.get_by_alt_text("规则截图.png")).to_be_visible()
            expect(second.locator("textarea")).to_have_value("按现有规则验收，参考截图")
            expect(submit).to_be_enabled()

            # 提交失败不清空草稿和附件，成功后沿用原线程且进入历史记录。
            page.route("**/api/actions", lambda route: route.fulfill(status=400, json={"error": "模拟回答失败"}), times=1)
            submit.click()
            expect(page.get_by_text("模拟回答失败", exact=True)).to_be_visible()
            expect(submit).to_be_enabled()
            expect(first.get_by_text("模板.pdf", exact=True)).to_be_visible()
            submit.click()
            try:
                expect(page.get_by_text("计划中", exact=True).first).to_be_visible()
            except AssertionError as error:
                raise AssertionError(f"阶段：{state['stage']}；错误：{state.get('error')}；页面：{page.locator('body').inner_text()[-2200:]}") from error
            engine.executor.submit(lambda: None).result(timeout=10)
            feed = page.locator(".conversation-feed")
            expect(feed.get_by_text("模板.pdf", exact=True)).to_be_visible()
            expect(feed.get_by_alt_text("规则截图.png")).to_be_visible()
            assert state["thread_id"] == "原线程"
            assert all("history_index" in entry for entry in state["answer_attachments"])
            assert any(entry["type"] == "localImage" for entry in engine.rpc.request.call_args.args[1]["input"])

            # 提问后的计划仍须人工确认。
            engine.last_message = "<proposed_plan>按参考模板完成修改</proposed_plan>"
            engine.turn_running = False
            engine._completed({"status": "completed", "id": state["turn_id"]}, state["id"], engine.connection_epoch)
            expect(page.get_by_role("button", name="确认当前计划并实施", exact=True)).to_be_visible()
            assert state["stage"] == "等待计划确认"
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
