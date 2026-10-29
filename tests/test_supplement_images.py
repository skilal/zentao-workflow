"""补充图片的会话隔离、接口约束、持久化与分析输入。"""

from base64 import b64decode
from email.message import Message
from hashlib import sha256
from http.client import HTTPConnection
from io import BytesIO
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from workflow.mcp_server import Tools
from workflow.settings import ROOT, WorkflowError
from workflow.web import COOKIE_NAME, LocalServer, WebApplication, WebHandler, public_state


PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class SupplementImageFixture(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            patched = patch(target, self.root)
            patched.start()
            self.addCleanup(patched.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.application = WebApplication(self.settings)
        self.addCleanup(self.application.close)
        self.state = self.application.sessions.store.new({"kind": "task", "id": 7, "title": "图片补充需求"})
        self.state["codex"] = {"model": "模拟模型", "reasoning_effort": "high"}
        self.state["snapshot"] = {"sources": [], "materials": [], "blockers": [], "feishu_links": []}
        self.runtime = self.application.sessions.add(self.state)
        self.application.emit(self.state["id"], "state", self.state)

    def upload(self, name="截图.png", data=PNG, **changes):
        arguments = {"session_id": self.state["id"], "revision": self.runtime.revision, "name": name, "data": data, **changes}
        return self.application.upload_supplement_image(**arguments)


class SupplementImageChecks(SupplementImageFixture):
    def test_multiple_same_name_images_persist_and_restore(self):
        self.upload()
        payload = self.upload()
        images = payload["state"]["supplement_images"]
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0]["id"], images[1]["id"])
        self.assertNotEqual(images[0]["path"], images[1]["path"])
        self.assertEqual(Path(images[0]["path"]).read_bytes(), PNG)
        self.assertEqual(images[0]["size"], len(PNG))
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(self.state["id"])["state"]["supplement_images"], images)
        self.assertEqual(Tools(self.state["id"]).call("task_context", {})["supplement_images"], images)

    def test_old_sessions_expose_an_empty_list(self):
        self.state.pop("supplement_images")
        self.runtime.engine.save()
        self.assertEqual(public_state(self.state)["supplement_images"], [])
        self.assertEqual(Tools(self.state["id"]).call("task_context", {})["supplement_images"], [])
        self.upload()
        self.assertEqual(len(self.state["supplement_images"]), 1)

    def test_invalid_format_empty_and_oversized_images_do_not_change_state(self):
        self.settings["zentao"]["max_attachment_mb"] = 1
        for name, data in (("截图.svg", b"<svg/>"), ("截图.png", b"not an image"),
                           ("截图.jpg", PNG), ("截图.png", b""), ("", PNG),
                           ("截图.png", PNG + bytes(1024 * 1024))):
            with self.subTest(name=name, size=len(data)), self.assertRaises(WorkflowError):
                self.upload(name, data)
        self.assertEqual(self.state["supplement_images"], [])
        self.assertFalse(list(self.root.rglob("*.png")))

    def test_supported_signatures_match_the_extension(self):
        for extension, signature in (("PNG", PNG), ("jpg", b"\xff\xd8\xff"), ("jpeg", b"\xff\xd8\xff"),
                                     ("gif", b"GIF89a"), ("webp", b"RIFF\x00\x00\x00\x00WEBP"), ("bmp", b"BM")):
            with self.subTest(extension=extension):
                self.upload("图片." + extension, signature)
        self.assertEqual(len(self.state["supplement_images"]), 6)

    def test_revision_busy_and_stage_are_enforced(self):
        with self.assertRaises(WorkflowError):
            self.upload(revision=-1)
        with self.assertRaises(WorkflowError):
            self.upload(session_id="不存在的会话")
        self.runtime.busy = "plan"
        with self.assertRaises(WorkflowError):
            self.upload()
        self.runtime.busy = None
        self.runtime.engine.turn_running = True
        with self.assertRaises(WorkflowError):
            self.upload()
        self.runtime.engine.turn_running = False
        for stage in ("等待选择分支", "计划中", "等待计划确认", "实施中", "已暂停"):
            self.state["stage"] = stage
            with self.subTest(stage=stage), self.assertRaises(WorkflowError):
                self.upload()
        self.state["stage"] = "准备工作区失败"
        self.upload()

    def test_remove_requires_current_session_and_revision(self):
        self.upload()
        image = self.state["supplement_images"][0]
        other = self.application.sessions.store.new({"kind": "task", "id": 8})
        self.application.sessions.add(other)
        for session_id, revision in ((self.state["id"], -1), (other["id"], 0)):
            with self.assertRaises(WorkflowError):
                self.application.remove_supplement_image(session_id, revision, image["id"])
        self.assertTrue(Path(image["path"]).is_file())
        self.application.remove_supplement_image(self.state["id"], self.runtime.revision, image["id"])
        self.assertFalse(Path(image["path"]).exists())
        self.assertEqual(self.state["supplement_images"], [])
        self.assertEqual(self.application.sessions.store.load(self.state["id"])["supplement_images"], [])

    def test_images_reach_analysis_and_image_only_supplement_allows_confirmation(self):
        self.upload()
        engine = self.runtime.engine
        engine.ensure_rpc = Mock()
        engine.rpc = Mock(model="模拟模型")
        engine.rpc.request.return_value = {"turn": {"id": "模拟回合"}}
        self.state.update(thread_id="模拟线程", phase="plan", plan="根据截图实现",
                          snapshot={"materials": [], "blockers": ["请补充图片"], "feishu_links": []})
        engine._start_turn("请分析补充图片，缺失规则必须提问。")
        inputs = engine.rpc.request.call_args.args[1]["input"]
        self.assertEqual([entry["path"] for entry in inputs if entry["type"] == "localImage"],
                         [self.state["supplement_images"][0]["path"]])
        self.assertIn('"supplement_images"', inputs[0]["text"])
        self.state["stage"] = "等待计划确认"
        self.state["plan_hash"] = sha256(self.state["plan"].encode()).hexdigest()
        engine._start_turn = Mock()
        engine.approve_plan()
        self.assertEqual(self.state["phase"], "implement")
        engine._start_turn.assert_called_once()

    def test_images_are_stored_under_the_session_attachment_directory(self):
        self.upload()
        directory = self.application.sessions.store.directory(self.state["id"]) / "attachments" / "supplement"
        image = self.state["supplement_images"][0]
        self.assertTrue(Path(image["path"]).is_relative_to(directory))

    def test_session_deletion_removes_uploaded_files(self):
        self.upload()
        directory = self.application.sessions.store.directory(self.state["id"])
        self.application.delete_session(self.state["id"], self.runtime)
        self.assertFalse(directory.exists())


class SupplementImageHttpChecks(SupplementImageFixture):
    def setUp(self):
        super().setUp()
        self.server = LocalServer(("127.0.0.1", 0), self.application)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def stop_server():
            self.server.shutdown()
            self.server.server_close()
            thread.join(3)
        self.addCleanup(stop_server)

    def request(self, method, path, body=None, **headers):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers={"Cookie": f"{COOKIE_NAME}={self.application.cookie_token}",
                "X-Workflow-Token": self.application.csrf_token, "Connection": "close", **headers})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type")
        finally:
            connection.close()

    def upload_url(self):
        return "/api/supplement-images?" + urlencode({"session_id": self.state["id"], "revision": self.runtime.revision, "name": "截图.png"})

    def test_http_upload_preview_remove_and_cross_session_denial(self):
        status, data, _ = self.request("POST", self.upload_url(), PNG)
        self.assertEqual(status, 200)
        payload = loads(data)
        image = payload["state"]["supplement_images"][0]
        url = "/api/supplement-images?" + urlencode({"session_id": self.state["id"], "image_id": image["id"]})
        status, data, content_type = self.request("GET", url)
        self.assertEqual((status, data, content_type), (200, PNG, "image/png"))
        other = self.application.sessions.store.new({"kind": "task", "id": 8})
        self.application.sessions.add(other)
        other_url = "/api/supplement-images?" + urlencode({"session_id": other["id"], "image_id": image["id"]})
        self.assertEqual(self.request("GET", other_url)[0], 400)
        body = dumps({"session_id": self.state["id"], "revision": payload["revision"], "image_id": image["id"]})
        self.assertEqual(self.request("POST", "/api/supplement-images/remove", body, **{"Content-Type": "application/json"})[0], 200)
        self.assertEqual(self.request("GET", url)[0], 400)

    def test_http_requires_cookie_token_origin_and_current_revision(self):
        for headers in ({"Cookie": ""}, {"X-Workflow-Token": "invalid-token"}, {"Origin": "https://other.invalid"}):
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.request("POST", self.upload_url(), PNG, **headers)[0], 400)
        url = self.upload_url()
        self.assertEqual(self.request("POST", url, PNG)[0], 200)
        self.assertEqual(self.request("POST", url, PNG)[0], 400)

    def test_rejected_requests_consume_bounded_body_before_responding_without_upload(self):
        for path, body in ((self.upload_url(), PNG), ("/api/actions", b'{"action":"connect"}')):
            for invalid in ({"Cookie": ""}, {"X-Workflow-Token": "invalid-token"}, {"Origin": "https://other.invalid"}):
                with self.subTest(path=path, headers=list(invalid)):
                    handler = object.__new__(WebHandler)
                    handler.server, handler.path, handler.command = self.server, path, "POST"
                    handler.headers = Message()
                    for name, value in {"Host": f"127.0.0.1:{self.server.server_port}",
                                        "Cookie": f"{COOKIE_NAME}={self.application.cookie_token}",
                                        "X-Workflow-Token": self.application.csrf_token,
                                        "Content-Length": str(len(body)), **invalid}.items():
                        handler.headers[name] = value
                    handler.rfile = BytesIO(body)
                    handler._json = Mock(side_effect=lambda *_: self.assertEqual(handler.rfile.tell(), len(body)))
                    with patch.object(self.application, "upload_supplement_image") as upload, \
                         patch.object(self.application, "dispatch") as dispatch:
                        handler.do_POST()
                    self.assertEqual(handler._json.call_args.args[0], 400)
                    upload.assert_not_called()
                    dispatch.assert_not_called()

    def test_repeated_unauthorized_uploads_keep_error_responses_readable(self):
        for attempt in range(10):
            for headers in ({"Cookie": ""}, {"X-Workflow-Token": "invalid-token"}, {"Origin": "https://other.invalid"}):
                with self.subTest(attempt=attempt, headers=list(headers)):
                    self.assertEqual(self.request("POST", self.upload_url(), PNG, **headers)[0], 400)

    def test_http_enforces_size_before_reading_body(self):
        self.settings["zentao"]["max_attachment_mb"] = 1
        status, _, _ = self.request("POST", self.upload_url(), b"", **{"Content-Length": str(1024 * 1024 + 1)})
        self.assertEqual(status, 400)
