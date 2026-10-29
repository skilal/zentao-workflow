"""在隔离服务中检查已完成会话的续聊、截图和历史交付展示。"""

from base64 import b64encode
from copy import deepcopy
from json import dumps
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import urlencode

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup import FollowupChecks, PNG
from workflow.web import LocalServer


def run():
    output = ROOT / ".local" / "followup-qa"
    output.mkdir(parents=True, exist_ok=True)
    fixture = FollowupChecks()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    app.default_session_id = state["id"]
    app.items = [state["item"]]
    other = deepcopy(state)
    other["id"] = app.sessions.store.new({"kind": "story", "id": 8, "title": "另一会话"})["id"]
    other["item"] = {"kind": "story", "id": 8, "title": "另一会话"}
    other["items"] = [other["item"]]
    app.sessions.add(other).engine.save()
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            context = browser.new_context(viewport={"width": 1500, "height": 1050})
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("if (!sessionStorage.getItem('workflow-page')) sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            send = page.get_by_role("button", name="发送并修复", exact=True)
            reply = page.get_by_placeholder("输入复现步骤、实际结果与预期结果，可粘贴测试截图…")
            expect(send).to_be_enabled()
            expect(reply).to_be_enabled()
            expect(page.locator("#review-1 .review-actions")).to_have_count(0)
            reply.fill("实际结果：保存失败；预期结果：保存成功。")

            # 失败图片须由用户明确移除；不能带着失败队列发送。
            page.locator("input[type=file]").set_input_files({"name": "错误.svg", "mimeType": "image/svg+xml", "buffer": b"<svg/>"})
            failed = page.locator(".attachment").filter(has_text="错误.svg")
            expect(failed.get_by_text("上传失败", exact=True)).to_be_visible()
            expect(send).to_be_disabled()
            failed.get_by_role("button", name="移除", exact=True).click()
            expect(send).to_be_enabled()

            # 正常上传、预览和移除走实际本地 HTTP 接口。
            page.locator("input[type=file]").set_input_files({"name": "上传截图.png", "mimeType": "image/png", "buffer": PNG})
            uploaded = page.locator(".attachment").filter(has_text="上传截图.png")
            expect(uploaded.locator("img")).to_be_visible()
            uploaded.get_by_role("button", name="移除", exact=True).click()
            expect(uploaded).to_have_count(0)

            # 粘贴图片同时保留剪贴板文字。
            reply.evaluate("""(input, data) => {
                const transfer = new DataTransfer();
                transfer.items.add(new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))], '粘贴截图.png', { type: 'image/png' }));
                transfer.setData('text/plain', '补充：点击保存后报错。');
                input.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
            }""", b64encode(PNG).decode())
            expect(reply).to_have_value("实际结果：保存失败；预期结果：保存成功。补充：点击保存后报错。")
            expect(page.get_by_alt_text("粘贴截图.png")).to_be_visible()
            expect(send).to_be_enabled()
            image_id = state["followup_images"][-1]["id"]
            page.reload()
            expect(reply).to_have_value("实际结果：保存失败；预期结果：保存成功。补充：点击保存后报错。")
            expect(page.get_by_alt_text("粘贴截图.png")).to_be_visible()

            # 图片不能从另一会话读取。
            response = page.request.get(server.origin + "/api/supplement-images?" + urlencode({
                "session_id": other["id"], "image_id": image_id, "purpose": "followup"}))
            assert response.status == 400

            page.screenshot(path=str(output / "完成后可继续发送.png"), full_page=True)
            send.click()
            expect(page.get_by_text("修复中", exact=True).first).to_be_visible()
            fixture.drain()
            expect(page.get_by_placeholder("输入回复、需要调整的计划内容或补充业务规则…")).to_be_disabled()
            expect(page.locator(".conversation-feed").get_by_alt_text("粘贴截图.png")).to_be_visible()
            expect(page.get_by_text("历史交付记录", exact=True)).to_be_visible()
            assert state["thread_id"] == "原需求线程"
            assert state["delivery_round"] == 2 and state["fix_round"] == 0
            fixture.finish()
            expect(send).to_be_enabled()
            expect(reply).to_have_value("")
            expect(page.locator(".conversation-feed").get_by_alt_text("粘贴截图.png")).to_be_visible()
            expect(page.get_by_role("button", name="移除", exact=True)).to_have_count(0)
            expect(page.locator("#review-1 .review-actions, #review-2 .review-actions")).to_have_count(0)

            # 纯图片反馈也可重复发起新一轮。
            page.locator("input[type=file]").set_input_files({"name": "第二次截图.png", "mimeType": "image/png", "buffer": PNG})
            expect(page.get_by_alt_text("第二次截图.png")).to_be_visible()
            expect(send).to_be_enabled()
            send.click()
            expect(page.get_by_text("修复中", exact=True).first).to_be_visible()
            fixture.drain()
            assert state["delivery_round"] == 3 and state["followup"]["text"] == ""
            fixture.finish()
            expect(send).to_be_enabled()
            page.reload()
            expect(page.locator(".conversation-feed img")).to_have_count(2)
            for width in (1500, 720, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"页面横向溢出：{width}"
            page.screenshot(path=str(output / "续聊历史窄屏.png"), full_page=True)
            assert not errors, errors
            (output / "result.json").write_text(dumps({"结果": "通过", "页面错误": errors,
                "覆盖": ["已完成会话发送", "失败图片移除", "上传预览移除", "文字与图片粘贴", "刷新草稿与截图", "跨会话图片隔离",
                         "原线程多轮续聊", "纯图片反馈", "历史 Review 只读", "历史提交保留", "发送中禁用", "宽窄屏布局"]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
