"""隔离检查 ZIP 内扫描 PDF 页图的预览和原文下载。"""

from pathlib import Path
from subprocess import CompletedProcess
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_previews import PreviewChecks, PNG
from test_zip_materials import zip_bytes
from workflow.web import LocalServer


def run():
    fixture = PreviewChecks()
    fixture.setUp()
    app = fixture.application
    app.connected = True
    client = app.sessions.zentao
    client.account, client.token = "测试账号", "模拟登录状态"
    data = zip_bytes([("扫描件.pdf", b"%PDF-reference"), ("元数据.xml", "有效字段")])
    task = {"id": 7, "name": "示例任务甲", "desc": "分析 PDF 和元数据", "story": 0,
            "assignedTo": {"account": "测试账号"}, "files": [{"id": 99, "extension": "zip", "title": "验收资料.zip"}]}
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    def render(command, **options):
        Path(command[-1] + "-1.png").write_bytes(PNG)
        Path(command[-1] + "-2.png").write_bytes(PNG)
        return CompletedProcess(command, 0)
    try:
        with patch.object(client, "_fetch", side_effect=[task, (data, "application/octet-stream")]), \
                patch("workflow.pdf_materials.which", return_value="pdftoppm.exe"), \
                patch("workflow.pdf_materials.run", side_effect=render), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1100})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.origin)
            page.locator(".item-row").filter(has_text="#7").click()
            expect(page.get_by_role("link", name="验收资料.zip", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="扫描件.pdf", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="元数据.xml", exact=True)).to_be_visible()
            for index in (1, 2):
                image = page.get_by_role("img", name=f"扫描件.pdf 第 {index} 页", exact=True)
                expect(image).to_be_visible()
                assert image.evaluate("image => image.complete && image.naturalWidth > 0")
                expect(page.get_by_text(f"第 {index} 页 · 已准备供 Codex 读取", exact=True)).to_be_visible()
            expect(page.get_by_text("有待补充的需求资料", exact=True)).to_have_count(0)
            with page.expect_download() as result:
                page.get_by_role("link", name="扫描件.pdf", exact=True).click()
            assert result.value.suggested_filename == "扫描件.pdf"
            assert Path(result.value.path()).read_bytes() == b"%PDF-reference"
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
