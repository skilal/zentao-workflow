"""隔离检查续聊 ZIP 上传、重试、历史目录、下载和图片兼容。"""

from json import dumps
from pathlib import Path
from re import compile
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup import FollowupChecks, PNG
from test_followup_attachments import SAMPLE
from workflow.mcp_server import Tools
from workflow.web import LocalServer


def run():
    output = ROOT / ".local" / "followup-zip-qa"
    output.mkdir(parents=True, exist_ok=True)
    fixture = FollowupChecks()
    fixture.setUp()
    app, state = fixture.application, fixture.state
    app.default_session_id = state["id"]
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            send = page.get_by_role("button", name=compile(r"^发\s*送$"))
            zip_input = page.get_by_label("上传续聊文件", exact=True)
            expect(page.get_by_role("button", name="上传文件", exact=True)).to_be_visible()
            expect(page.get_by_role("switch", name="修改后自动 Review", exact=True)).not_to_be_checked()

            page.route("**/api/followup-attachments?**", lambda route: route.fulfill(status=400, json={"error": "模拟 ZIP 上传失败"}), times=1)
            zip_input.set_input_files({"name": "归档样例.zip", "mimeType": "application/zip", "buffer": SAMPLE})
            card = page.locator(".attachment").filter(has_text="归档样例.zip")
            expect(card.get_by_text("上传失败", exact=True)).to_be_visible()
            expect(send).to_be_disabled()
            card.get_by_role("button", name=compile(r"^重\s*试$")).click()
            expect(card.get_by_role("link", name="归档样例.zip", exact=True)).to_be_visible()
            expect(send).to_be_enabled()
            assert state["delivery_round"] == 1 and not state.get("followup")
            card.get_by_text("目录与读取状态", exact=True).click()
            expect(card.get_by_text("归档包/空目录/", exact=True)).to_be_visible()
            expect(card.get_by_text("归档包/元数据.xml", exact=True)).to_be_visible()
            expect(card.get_by_text("OFD · 3 字节 · 仅列出文件信息", exact=True)).to_be_visible()

            zip_input.set_input_files({"name": "待移除.zip", "mimeType": "application/zip", "buffer": SAMPLE})
            removed = page.locator(".attachment").filter(has_text="待移除.zip")
            expect(removed.get_by_role("link")).to_be_visible()
            removed.get_by_role("button", name="移除", exact=True).click()
            expect(removed).to_have_count(0)
            page.reload()
            expect(card.get_by_role("link", name="归档样例.zip", exact=True)).to_be_visible()

            page.route("**/api/actions", lambda route: route.fulfill(status=400, json={"error": "模拟发送失败"}), times=1)
            send.click()
            expect(page.get_by_text("模拟发送失败", exact=True)).to_be_visible()
            expect(card.get_by_role("button", name="移除", exact=True)).to_be_visible()
            send.click()
            expect(page.get_by_text("续聊处理中", exact=True).first).to_be_visible()
            fixture.drain()
            assert state["followup"]["text"] == "" and len(state["followup"]["attachments"]) == 1
            attachment_id = state["followup"]["attachments"][0]["id"]
            xml = Tools(state["id"]).call("read_followup_attachment", {"attachment_id": attachment_id, "member_id": "2"})
            assert '类型="电子"' in xml["text"]
            fixture.finish()
            expect(page.get_by_role("button", name="上传文件", exact=True)).to_be_enabled()
            history = page.locator(".conversation-feed .attachment").filter(has_text="归档样例.zip")
            expect(history.get_by_role("link", name="归档样例.zip", exact=True)).to_be_visible()
            expect(history.get_by_role("button", name="移除", exact=True)).to_have_count(0)
            with page.expect_download() as download:
                history.get_by_role("link", name="归档样例.zip", exact=True).click()
            assert Path(download.value.path()).read_bytes() == SAMPLE
            page.reload()
            expect(history).to_be_visible()
            history.get_by_text("目录与读取状态", exact=True).click()
            expect(history.get_by_text("归档包/元数据.xml", exact=True)).to_be_visible()

            # 新 ZIP 和图片共用上传队列，图片预览保持原有行为。
            zip_input.set_input_files({"name": "再次补充.zip", "mimeType": "application/zip", "buffer": SAMPLE})
            page.get_by_label("上传续聊图片", exact=True).set_input_files({"name": "补充截图.png", "mimeType": "image/png", "buffer": PNG})
            expect(page.get_by_alt_text("补充截图.png")).to_be_visible()
            expect(page.get_by_role("link", name="再次补充.zip", exact=True)).to_be_visible()
            expect(send).to_be_enabled()
            page.screenshot(path=str(output / "续聊ZIP目录与附件.png"), full_page=True)
            for width in (720, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"页面横向溢出：{width}"
            assert not errors, errors
            (output / "browser-result.json").write_text(dumps({"结果": "通过", "页面错误": errors,
                "覆盖": ["失败重试", "移除", "上传不自动发送", "仅ZIP发送", "发送失败保留", "目录与XML状态", "历史下载",
                         "刷新恢复", "历史只读", "图片兼容", "宽窄屏"]}, ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
