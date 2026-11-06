"""续聊文档与图片的隔离读取、传递、访问边界及持久化。"""

from base64 import b64decode
from copy import deepcopy
from http.client import HTTPConnection
from io import StringIO
from json import dumps, loads
from pathlib import Path
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlencode

import test_followup_attachments as fixtures
from test_supplement_images import PNG
from workflow.followup_documents import attachment_image, current_attachment_images, preview_document
from workflow.mcp_server import Tools, serve
from workflow.settings import WorkflowError
from workflow.web import COOKIE_NAME, LocalServer, WebApplication


def docx_bytes(*, external=False, unsupported=False):
    relation = ('TargetMode="External" Target="https://example.invalid/image.png"' if external else
                'Target="media/image1.svg"' if unsupported else 'Target="media/image1.png"')
    body = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
      <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>中文附件标题</w:t></w:r></w:p>
      <w:p><w:r><w:t>正文内容</w:t><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>表头一</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>表头二</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>数据</w:t><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>说明</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      <w:p><w:r><w:t>表格之后</w:t></w:r></w:p></w:body></w:document>'''
    rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="image" ' + relation + '/></Relationships>'
    return fixtures.zip_bytes([("word/document.xml", body), ("word/_rels/document.xml.rels", rels),
                               ("word/media/image1.svg" if unsupported else "word/media/image1.png", b"<svg/>" if unsupported else PNG)])


class FollowupDocumentChecks(TestCase):
    def setUp(self):
        self.fixture = fixtures.FollowupAttachmentChecks()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app, self.state, self.engine = self.fixture.application, self.fixture.state, self.fixture.engine
        self.runtime = self.fixture.runtime

    def upload(self, name="参考.docx", data=None):
        return self.fixture.upload(docx_bytes() if data is None else data, name)

    def tools(self):
        return Tools(self.state["id"])

    def fake_pdf(self, material, renderer):
        parent = Path(material["path"]).parent / "pages"
        parent.mkdir()
        material["pdf_pages"] = []
        for index in range(1, 3):
            path = parent / f"page-{index}.png"
            path.write_bytes(PNG)
            material["pdf_pages"].append({"page": index, "path": str(path), "title": f"第 {index} 页", "image": True})

    def test_docx_order_tables_and_images_are_readable_and_bound_to_message(self):
        entry = self.upload()
        self.assertTrue(entry["readable"])
        self.assertEqual(len(entry["images"]), 1)
        with self.assertRaises(WorkflowError):
            self.tools().call("read_followup_attachment", {"attachment_id": entry["id"]})
        self.fixture.send_zip(entry)
        result = self.tools().call("read_followup_attachment", {"attachment_id": entry["id"]})
        self.assertLess(result["text"].index("正文内容"), result["text"].index("表头一 | 表头二"))
        self.assertLess(result["text"].index("数据"), result["text"].index("表格之后"))
        self.assertIn("图片 image-1", result["text"])
        params = self.fixture.fixture.calls[-1][1]
        self.assertEqual(len([value for value in params["input"] if value["type"] == "localImage"]), 1)
        self.assertNotIn("正文内容", params["input"][0]["text"])
        message = self.state["history"][entry["history_index"]]
        self.assertEqual(message["followup_attachments"][0]["kind"], "docx")
        image = self.tools().call("read_followup_attachment", {"attachment_id": entry["id"], "image_id": "image-1"})
        self.assertEqual(image["content"][1]["type"], "image")
        self.assertEqual(b64decode(image["content"][1]["data"]), PNG)

    def test_docx_unsupported_and_external_images_are_explicit_partial_reads(self):
        for data in (docx_bytes(external=True), docx_bytes(unsupported=True)):
            entry = self.upload(data=data)
            self.assertTrue(entry["readable"])
            self.assertTrue(entry["issue"])
            self.assertEqual(entry["images"], [])
            self.assertIn("未读取", Path(entry["path"]).with_name("text.txt").read_text(encoding="utf-8"))

    def test_text_pagination_and_original_markup(self):
        entry = self.upload("元数据.xml", ('<记录 属性="原文">\n' + '\n'.join(str(i) for i in range(205)) + '\n</记录>').encode())
        self.fixture.send_zip(entry)
        result = self.tools().call("read_followup_attachment", {"attachment_id": entry["id"], "line_count": 100})
        self.assertIn('<记录 属性="原文">', result["text"])
        self.assertEqual(result["next_line"], 101)
        for args in ({"line_count": 0}, {"start_line": False}, {"member_id": "0"}, {"image_id": "not-an-image"}):
            with self.assertRaises(WorkflowError):
                self.tools().call("read_followup_attachment", {"attachment_id": entry["id"], **args})

    def test_pdf_pages_are_sent_and_tool_returns_actual_image(self):
        with patch("workflow.followup_documents.read_pdf", side_effect=self.fake_pdf):
            entry = self.upload("扫描件.pdf", b"%PDF-test")
        self.assertEqual(entry["page_count"], 2)
        self.fixture.send_zip(entry)
        inputs = self.fixture.fixture.calls[-1][1]["input"]
        # 相同内容只附加一次，各页仍可按页码独立读取。
        self.assertEqual(len([value for value in inputs if value["type"] == "localImage"]), 1)
        metadata = self.tools().call("read_followup_attachment", {"attachment_id": entry["id"]})
        self.assertEqual(metadata["reading_mode"], "page_images")
        image = self.tools().call("read_followup_attachment", {"attachment_id": entry["id"], "page": 2})
        self.assertEqual(b64decode(image["content"][1]["data"]), PNG)
        for page in (0, 3, True):
            with self.assertRaises(WorkflowError):
                self.tools().call("read_followup_attachment", {"attachment_id": entry["id"], "page": page})

    def test_parsing_failures_keep_original_without_false_readability(self):
        for name, data in (("损坏.docx", b"not-docx"), ("编码.txt", b"\xff\xff")):
            entry = self.upload(name, data)
            self.assertFalse(entry["readable"])
            self.assertEqual(Path(entry["path"]).read_bytes(), data)
            self.assertEqual(preview_document(self.engine.store, self.state, entry, {})["kind"], "unsupported")
        with patch("workflow.followup_documents.read_pdf", side_effect=ValueError("渲染工具缺失或 PDF 已加密")):
            entry = self.upload("受限.pdf", b"%PDF-test")
        self.assertFalse(entry["readable"])
        self.assertIn("渲染工具缺失", entry["issue"])
        self.fixture.send_zip(entry)
        result = self.tools().call("read_followup_attachment", {"attachment_id": entry["id"]})
        self.assertFalse(result["readable"])

    def test_delete_draft_removes_document_derived_files_but_sent_files_are_retained(self):
        entry = self.upload()
        directory = Path(entry["path"]).parent
        self.app.remove_followup_attachment(self.state["id"], self.runtime.revision, entry["id"])
        self.assertFalse(directory.exists())
        entry = self.upload()
        self.fixture.send_zip(entry)
        with self.assertRaises(WorkflowError):
            self.app.remove_followup_attachment(self.state["id"], self.runtime.revision, entry["id"])
        self.assertTrue(Path(entry["path"]).exists())

    def test_restart_restores_document_images_and_historical_references(self):
        entry = self.upload()
        self.fixture.send_zip(entry)
        self.fixture.fixture.complete("已读取资料")
        restored = WebApplication(self.app.settings)
        self.addCleanup(restored.close)
        state = restored.session(self.state["id"])["state"]
        self.assertEqual(state["history"][entry["history_index"]]["followup_attachments"][0]["images"][0]["id"], "image-1")
        image, _ = attachment_image(restored.sessions.store, state, state["followup_attachments"][0], {"image_id": "image-1"})
        self.assertEqual(image.read_bytes(), PNG)

    def test_pdf_preparation_does_not_hold_global_lock_or_allow_sending_early(self):
        entered, release, acquired = Event(), Event(), Event()
        def convert(material, renderer):
            entered.set()
            if not release.wait(5):
                raise ValueError("测试未释放准备任务")
            self.fake_pdf(material, renderer)
        errors = []
        def upload():
            try:
                self.upload("资料.pdf", b"%PDF-test")
            except Exception as error:
                errors.append(error)
        with patch("workflow.followup_documents.read_pdf", side_effect=convert):
            worker = Thread(target=upload)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.app.lock.acquire(timeout=1))
                self.app.lock.release()
                with self.assertRaisesRegex(WorkflowError, "上传附件"):
                    self.app.dispatch(self.fixture.fixture.request())
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(errors)
        self.assertEqual(self.runtime.uploads, 0)

    def test_mcp_protocol_returns_image_blocks_and_never_serializes_them_as_text(self):
        entry = self.upload()
        self.fixture.send_zip(entry)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "read_followup_attachment", "arguments": {"attachment_id": entry["id"], "image_id": "image-1"}}}
        output = StringIO()
        with patch("workflow.mcp_server.stdin", StringIO(dumps(request) + "\n")), patch("workflow.mcp_server.stdout", output):
            serve(self.state["id"])
        self.assertEqual(loads(output.getvalue())["result"]["content"][1]["type"], "image")

    def test_http_document_preview_download_images_and_session_isolation(self):
        entry = self.upload()
        server = LocalServer(("127.0.0.1", 0), self.app)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown(); server.server_close(); worker.join(3)
        self.addCleanup(close)
        def request(route="", session_id=None, **query):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                values = {"session_id": session_id or self.state["id"], "attachment_id": entry["id"], **query}
                connection.request("GET", "/api/followup-attachments" + route + "?" + urlencode(values),
                                   headers={"Cookie": f"{COOKIE_NAME}={self.app.cookie_token}", "Connection": "close"})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        self.assertEqual(request()[1], Path(entry["path"]).read_bytes())
        self.assertEqual(loads(request("/preview")[1])["kind"], "docx")
        self.assertEqual(request("/image", image_id="image-1")[1], PNG)
        other = self.engine.store.new(None)
        self.app.sessions.add(other)
        self.assertEqual(request("/preview", session_id=other["id"])[0], 400)
        self.assertEqual(request("/image", image_id="../outside")[0], 400)
        original = entry["images"][0]["path"]
        entry["images"][0]["path"] = str(Path(original).parents[2] / "outside.png")
        self.assertEqual(request("/image", image_id="image-1")[0], 400)

    def test_docx_limits_and_old_office_rejection(self):
        for extension in ("doc", "xls", "xlsx", "ppt", "pptx"):
            with self.assertRaisesRegex(WorkflowError, "请选择 PDF"):
                self.upload("不支持." + extension, b"sample")
        self.app.settings["zentao"]["max_attachment_mb"] = 1
        with self.assertRaises(WorkflowError):
            self.upload("过大.txt", b"x" * (1024 * 1024 + 1))
        data = fixtures.zip_bytes([("word/document.xml", b"x" * (16 * 1024 * 1024 + 1))])
        entry = self.upload(data=data)
        self.assertFalse(entry["readable"])
        self.assertIn("16 MB", entry["issue"])
