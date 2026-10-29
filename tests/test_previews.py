"""查看资料与正式会话的边界；外部服务全部使用脱敏模拟。"""

from base64 import b64decode
from copy import deepcopy
from http.client import HTTPConnection
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from workflow.settings import ROOT, WorkflowError
from workflow.web import COOKIE_NAME, LocalServer, WebApplication


PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class PreviewChecks(TestCase):
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
        self.application.codex_catalog = {"default_model": "示例模型", "models": [
            {"model": "示例模型", "display_name": "示例模型", "reasoning_efforts": ["high"], "is_default": True}]}
        self.addCleanup(self.application.close)
        self.items = [{"kind": "task", "id": 7, "title": "示例任务甲"},
                      {"kind": "task", "id": 8, "title": "示例任务乙"}]
        self.application.items = deepcopy(self.items)

    def request(self, action, runtime=None, data=None, **changes):
        return {"action": action, "request_id": str(uuid4()), "data": data or {},
                "preview_id": runtime.engine.state["id"] if runtime else None,
                "revision": runtime.revision if runtime else None, **changes}

    def drain(self, runtime):
        runtime.engine.executor.submit(lambda: None).result(timeout=5)

    def collect(self, runtime=None, items=None, error=None):
        def fetch(selected, directory):
            if error:
                raise WorkflowError(error)
            path = directory / "attachments" / "42.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)
            return {"items": deepcopy(selected), "sources": [], "materials": [{"file_id": 42,
                    "title": "示例图片", "path": str(path), "extension": "png", "image": True}],
                    "project": {"id": 41, "name": "示例项目"}, "blockers": [], "feishu_links": []}
        request = self.request("collect", runtime, {"items": items or [self.items[0]]})
        with patch.object(self.application.sessions.zentao, "collect_many", side_effect=fetch):
            result = self.application.dispatch(request)
            runtime = self.application.sessions.get_preview(result["preview_id"])
            self.drain(runtime)
        return runtime

    def plan_request(self, runtime, **changes):
        return {**self.request("plan", runtime, {"model": "示例模型", "reasoning_effort": "high", "supplement": "补充验收要求", "items": [
            {"kind": item["kind"], "id": item["id"]} for item in runtime.engine.state["items"]]}), **changes}

    def assert_no_sessions(self):
        self.assertFalse(self.application.sessions.runtimes)
        self.assertFalse(self.application.snapshot()["history"])
        self.assertFalse(self.application.snapshot()["sessions"])
        self.assertFalse(list(self.root.rglob("state.json")))
        self.assertFalse(self.application.pointer.exists())

    def test_viewing_and_changing_selection_never_create_a_session(self):
        with patch.object(self.application.sessions.store, "new", side_effect=AssertionError("查看不得创建会话")):
            runtime = self.collect()
            self.collect(runtime, self.items)
        self.assertEqual(runtime.engine.state["items"], self.items)
        self.assertNotIn("created_at", runtime.engine.state)
        self.assertFalse(list(runtime.engine.store.root.rglob("state.json")))
        self.assert_no_sessions()

    def test_read_failure_has_page_error_and_can_be_retried_explicitly(self):
        runtime = self.collect(error="模拟资料读取失败")
        self.assertEqual(runtime.engine.state["stage"], "资料读取失败")
        self.assertEqual(self.application.preview(runtime.engine.state["id"])["state"]["error"], "模拟资料读取失败")
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.plan_request(runtime))
        self.assert_no_sessions()
        self.collect(runtime)
        self.assertEqual(runtime.engine.state["stage"], "资料已读取")
        self.assertFalse(runtime.engine.state["error"])
        self.assert_no_sessions()

    def test_upload_and_remove_images_do_not_persist_a_session(self):
        runtime = self.collect()
        payload = self.application.upload_supplement_image(None, runtime.revision, "截图.png", PNG,
                                                         runtime.engine.state["id"])
        image = payload["state"]["supplement_images"][0]
        self.assertEqual(Path(image["path"]).read_bytes(), PNG)
        self.application.remove_supplement_image(None, runtime.revision, image["id"], runtime.engine.state["id"])
        self.assertFalse(Path(image["path"]).exists())
        self.assert_no_sessions()

    def test_start_plan_transfers_all_inputs_and_deduplicates_different_requests(self):
        runtime = self.collect(items=self.items)
        preview_id = runtime.engine.state["id"]
        self.application.upload_supplement_image(None, runtime.revision, "截图.png", PNG, preview_id)
        request = self.plan_request(runtime)
        preview_root = runtime.engine.store.root
        with patch("workflow.engine.Workflow.start_plan") as start:
            result = self.application.dispatch(request)
            created = self.application.sessions.get(result["session_id"])
            self.drain(created)
            duplicate = self.application.dispatch(request)
            repeated = self.application.dispatch({**request, "request_id": str(uuid4())})
        self.assertEqual(result["session_id"], duplicate["session_id"])
        self.assertEqual(result["session_id"], repeated["session_id"])
        start.assert_called_once_with("补充验收要求", False)
        self.assertEqual(len(self.application.sessions.runtimes), 1)
        state = created.engine.state
        self.assertEqual(state["items"], self.items)
        self.assertEqual(state["supplement"], "补充验收要求")
        self.assertIn("created_at", state)
        directory = self.application.sessions.store.directory(state["id"])
        for entry in [*state["snapshot"]["materials"], *state["supplement_images"]]:
            self.assertTrue(Path(entry["path"]).is_relative_to(directory))
            self.assertEqual(Path(entry["path"]).read_bytes(), PNG)
        self.application.management_executor.submit(lambda: None).result(timeout=5)
        self.assertFalse(preview_root.exists())
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(state["id"])["state"]["supplement_images"], state["supplement_images"])

    def test_stale_version_changed_items_and_uploading_block_creation(self):
        runtime = self.collect()
        cases = [self.plan_request(runtime, revision=-1),
                 self.plan_request(runtime, data={"supplement": "", "items": [{"kind": "task", "id": 8}]})]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(WorkflowError):
                self.application.dispatch(request)
        runtime.uploads = 1
        with self.assertRaisesRegex(WorkflowError, "上传完成"):
            self.application.dispatch(self.plan_request(runtime))
        runtime.uploads = 0
        self.assert_no_sessions()

    def test_collecting_blocks_plan_creation(self):
        entered, release = Event(), Event()
        def fetch(*_):
            entered.set()
            release.wait(5)
            raise WorkflowError("模拟中断")
        with patch.object(self.application.sessions.zentao, "collect_many", side_effect=fetch):
            result = self.application.dispatch(self.request("collect", data={"items": [self.items[0]]}))
            runtime = self.application.sessions.get_preview(result["preview_id"])
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(WorkflowError):
                    self.application.dispatch(self.plan_request(runtime))
                self.assert_no_sessions()
            finally:
                release.set()
                self.drain(runtime)

    def test_plan_failure_after_creation_keeps_one_recoverable_session(self):
        runtime = self.collect()
        with patch("workflow.engine.Workflow.start_plan", side_effect=WorkflowError("模拟分支识别失败")):
            result = self.application.dispatch(self.plan_request(runtime))
            created = self.application.sessions.get(result["session_id"])
            self.drain(created)
        self.assertEqual(created.engine.state["error"], "模拟分支识别失败")
        self.assertEqual(len(self.application.snapshot()["history"]), 1)

    def test_attachment_transfer_failure_keeps_preview_without_a_session(self):
        runtime = self.collect()
        with patch("workflow.web.copytree", side_effect=OSError("模拟文件转存失败")):
            with self.assertRaises(OSError):
                self.application.dispatch(self.plan_request(runtime))
        self.assertEqual(runtime.engine.state["stage"], "资料已读取")
        self.assert_no_sessions()

    def test_discard_restart_and_legacy_records_are_independent(self):
        legacy = self.application.sessions.store.new(self.items[0])
        legacy["stage"] = "资料读取失败"
        self.application.sessions.store.save(legacy)
        self.application.sessions.add(legacy)
        runtime = self.collect()
        other = self.collect(items=[self.items[1]])
        directory = runtime.engine.store.root
        self.application.discard_preview(runtime.engine.state["id"]).result(timeout=5)
        self.assertFalse(directory.exists())
        self.assertIn(other.engine.state["id"], self.application.sessions.previews)
        restarted = WebApplication(self.settings)
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.sessions.previews)
        self.assertEqual(restarted.snapshot()["history"][0]["stage"], "资料读取失败")
        self.assertEqual(restarted.session(legacy["id"])["state"], self.application.session(legacy["id"])["state"])
        other_directory = other.engine.store.root
        self.application.close()
        self.assertFalse(other_directory.exists())

    def test_http_preview_images_are_isolated_from_formal_sessions(self):
        runtime = self.collect()
        preview_id = runtime.engine.state["id"]
        server = LocalServer(("127.0.0.1", 0), self.application)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, url, body=None):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(method, url, body, {"Cookie": f"{COOKIE_NAME}={self.application.cookie_token}",
                    "X-Workflow-Token": self.application.csrf_token, "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        url = "/api/supplement-images?" + urlencode({"preview_id": preview_id, "revision": runtime.revision, "name": "截图.png"})
        status, body = request("POST", url, PNG)
        self.assertEqual(status, 200)
        image_id = loads(body)["state"]["supplement_images"][0]["id"]
        url = "/api/supplement-images?" + urlencode({"preview_id": preview_id, "image_id": image_id})
        self.assertEqual(request("GET", url), (200, PNG))
        other = self.collect(items=[self.items[1]])
        self.assertEqual(request("GET", url.replace(preview_id, other.engine.state["id"]))[0], 400)
        self.assertEqual(request("GET", f"/api/material?preview_id={preview_id}&file_id=42"), (200, PNG))
        self.assertEqual(request("GET", f"/api/session?id={preview_id}")[0], 400)
        self.assertEqual(request("GET", f"/api/preview?id={preview_id}")[0], 200)
        self.assert_no_sessions()
        self.assertEqual(request("POST", "/api/previews/discard", dumps({"preview_id": preview_id}))[0], 200)
