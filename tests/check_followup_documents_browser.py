"""隔离页面检查文档混合粘贴、真实 PDF 页图、Word 图片及消息归属。"""

from base64 import b64encode
from io import BytesIO
from pathlib import Path
from re import compile
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup_documents import FollowupDocumentChecks, docx_bytes
from test_supplement_images import PNG
from workflow.settings import load_settings
from workflow.web import LocalServer


def pdf_bytes():
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(400, 500))
    canvas.drawString(35, 450, "Reference PDF page 1")
    canvas.showPage()
    canvas.drawImage(ImageReader(BytesIO(PNG)), 35, 200, width=200, height=200)
    canvas.save()
    return buffer.getvalue()


def run():
    fixture = FollowupDocumentChecks()
    fixture.setUp()
    app, state, engine = fixture.app, fixture.state, fixture.engine
    app.settings["pdf"] = load_settings()["pdf"]
    app.default_session_id = state["id"]
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(20000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page','plan')")
            page.goto(server.origin)
            file_input = page.get_by_label("上传续聊文件", exact=True)
            reply = page.get_by_placeholder("输入问题、需要获取的信息或修改要求，可粘贴图片和文件…")
            send = page.get_by_role("button", name=compile(r"^发\s*送$"))
            page.route("**/api/followup-attachments?**", lambda route: route.fulfill(status=400, json={"error": "模拟文档上传失败"}), times=1)
            file_input.set_input_files({"name": "重试.txt", "mimeType": "text/plain", "buffer": b"retry"})
            card = page.locator(".attachment").filter(has_text="重试.txt")
            expect(card.get_by_text("模拟文档上传失败", exact=True)).to_be_visible()
            expect(send).to_be_disabled()
            card.get_by_role("button", name=compile(r"^重\s*试$")).click()
            expect(card.get_by_role("link", name="重试.txt", exact=True)).to_be_visible()
            card.get_by_role("button", name=compile(r"^移\s*除$")).click()
            expect(card).to_have_count(0)
            word = docx_bytes()
            files = [{"name": "中文参考.docx", "type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": b64encode(word).decode()},
                     {"name": "图文资料.pdf", "type": "application/pdf", "data": b64encode(pdf_bytes()).decode()},
                     {"name": "元数据.xml", "type": "text/xml", "data": b64encode('<记录 属性="原文">中文</记录>'.encode()).decode()},
                     {"name": "截图.png", "type": "image/png", "data": b64encode(PNG).decode()}]
            reply.evaluate("""(input, files) => {
              const data = new DataTransfer(); data.setData('text/plain', '请根据这些文件分析');
              for (const file of files) data.items.add(new File([Uint8Array.from(atob(file.data), c => c.charCodeAt(0))], file.name, {type:file.type}));
              input.focus(); input.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
            }""", files)
            expect(reply).to_have_value("请根据这些文件分析")
            for name in ("中文参考.docx", "图文资料.pdf", "元数据.xml"):
                expect(page.get_by_role("link", name=name, exact=True)).to_be_visible(timeout=60000)
            expect(send).to_be_enabled()
            assert not state.get("followup")
            expect(page.get_by_text("已准备 2 页供 AI 读取", exact=False)).to_be_visible()
            def preview(name):
                card = page.locator("[data-followup-file]").filter(has_text=name)
                card.get_by_role("button", name=compile(r"^预\s*览$")).click()
                drawer = page.locator(".ant-drawer-content").filter(has_text=f"文件预览 · {name}")
                expect(drawer).to_be_visible()
                return drawer
            def close(drawer):
                drawer.get_by_role("button", name="Close").click()
                expect(drawer).not_to_be_visible()
            drawer = preview("中文参考.docx")
            expect(drawer.get_by_role("heading", name="中文附件标题")).to_be_visible()
            expect(drawer.get_by_role("cell", name="表头一", exact=True)).to_be_visible()
            image = drawer.get_by_alt_text("Word 图片 image-1").first
            expect(image).to_be_visible()
            page.wait_for_function("image => image.complete && image.naturalWidth > 0", arg=image.element_handle())
            page.mouse.move(20, 20)
            page.screenshot(path=str(ROOT / ".local" / "followup-word-preview.png"))
            close(drawer)
            drawer = preview("图文资料.pdf")
            expect(drawer.get_by_text("第 1 / 2 页", exact=True)).to_be_visible()
            drawer.get_by_role("button", name="下一页").click()
            expect(drawer.get_by_text("第 2 / 2 页", exact=True)).to_be_visible()
            close(drawer)
            drawer = preview("元数据.xml")
            expect(drawer.locator("pre")).to_have_text('<记录 属性="原文">中文</记录>')
            close(drawer)
            with page.expect_download() as download:
                page.get_by_role("link", name="中文参考.docx", exact=True).click()
            assert Path(download.value.path()).read_bytes() == word
            page.reload()
            expect(reply).to_have_value("请根据这些文件分析")
            expect(page.get_by_role("link", name="中文参考.docx", exact=True)).to_be_visible()
            send.click()
            expect(page.locator("textarea").first).to_have_value("")
            fixture.fixture.fixture.drain()
            expect(page.get_by_role("link", name="中文参考.docx", exact=True)).to_be_visible()
            params = fixture.fixture.fixture.calls[-1][1]
            assert len([item for item in params["input"] if item["type"] == "localImage"]) >= 2
            message = state["history"][state["followup"]["history_index"]]
            assert len(message["followup_attachments"]) == 3
            assert len(message["images"]) == 1
            fixture.fixture.fixture.complete("已接收并读取资料")
            page.reload()
            history_card = page.locator("[data-followup-file]").filter(has_text="中文参考.docx")
            expect(history_card.get_by_role("button", name=compile(r"^移\s*除$"))).to_have_count(0)
            file_input.set_input_files({"name": "只有附件.txt", "mimeType": "text/plain", "buffer": '仅附件消息'.encode()})
            expect(page.get_by_role("link", name="只有附件.txt", exact=True)).to_be_visible()
            expect(send).to_be_enabled()
            with page.expect_response(lambda response: response.url.endswith("/api/actions") and response.request.method == "POST"):
                send.click()
            fixture.fixture.fixture.drain()
            expect(page.get_by_role("link", name="只有附件.txt", exact=True)).to_be_visible()
            page.wait_for_function("() => document.querySelector('textarea')?.disabled === true")
            assert len(state["followup"]["attachments"]) == 1
            assert state["followup"]["text"] == ""
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown(); server.server_close(); worker.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
