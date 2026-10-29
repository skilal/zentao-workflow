"""会话标题与工作项信息独立保存，兼容旧记录及执行中的状态保存。"""

from copy import deepcopy
from contextlib import closing
from http.client import HTTPConnection
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from workflow.settings import ROOT, WorkflowError
from workflow.store import session_title
from workflow.web import LocalServer, WebApplication


class SessionTitleChecks(TestCase):
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
        self.state = self.application.sessions.store.new({"kind": "task", "id": 7, "title": "原任务文本"})
        self.runtime = self.application.sessions.add(self.state)
        self.runtime.engine.save()

    def rename(self, title, **changes):
        return self.application.rename_session({"session_id": self.state["id"], "title": title,
            "request_id": str(uuid4()), **changes})

    def test_default_uses_existing_single_and_joint_item_labels_for_old_records(self):
        legacy = deepcopy(self.state)
        legacy.pop("custom_title")
        self.assertEqual(session_title(legacy), "任务 #7 · 原任务文本")
        legacy["items"].append({"kind": "story", "id": 8, "title": "原需求文本"})
        self.assertEqual(session_title(legacy), "任务 #7 · 原任务文本；需求 #8 · 原需求文本")
        legacy.pop("items")
        self.assertEqual(session_title(legacy), "任务 #7 · 原任务文本")

    def test_custom_title_survives_restarting_and_keeps_item_information(self):
        original_items = deepcopy(self.state["items"])
        payload = self.rename("  自定义交付标题  ")
        self.assertEqual(payload["state"]["title"], "自定义交付标题")
        self.assertEqual(payload["state"]["items"], original_items)
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(self.state["id"])["state"]["title"], "自定义交付标题")
        self.assertEqual(restored.snapshot()["history"][0]["title"], "自定义交付标题")
        self.assertTrue(any(b"event: state" in packet and "自定义交付标题".encode() in packet
                            for _, packet in self.application.events))

    def test_renaming_running_session_preserves_live_state_object_and_later_saves(self):
        self.state.update(stage="实施中", phase="implement")
        self.runtime.engine.turn_running = True
        self.runtime.busy = "plan"
        self.rename("执行中的自定义标题")
        self.assertIs(self.runtime.engine.state, self.state)
        self.assertEqual(self.state["stage"], "实施中")
        self.assertTrue(self.runtime.engine.turn_running)
        self.assertEqual(self.runtime.busy, "plan")
        self.runtime.engine.note("Codex", "后台继续保存进度")
        self.assertEqual(self.runtime.engine.store.load(self.state["id"])["custom_title"], "执行中的自定义标题")

    def test_duplicate_request_and_other_sessions_are_isolated(self):
        other = self.application.sessions.add(self.application.sessions.store.new(
            {"kind": "bug", "id": 9, "title": "另一个工作项"}))
        request_id = str(uuid4())
        self.rename("第一次标题", request_id=request_id)
        self.rename("第二次标题")
        duplicate = self.rename("第一次标题", request_id=request_id)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.state["custom_title"], "第二次标题")
        self.assertEqual(session_title(other.engine.state), "Bug #9 · 另一个工作项")

    def test_invalid_deleted_and_cleaning_sessions_cannot_be_renamed(self):
        for title in (None, 42, "", " \n "):
            with self.subTest(title=title), self.assertRaises(WorkflowError):
                self.rename(title)
        with self.assertRaises(WorkflowError):
            self.rename("标题", session_id="已删除的会话")
        self.state["cleanup"] = {"finalizing": True}
        with self.assertRaisesRegex(WorkflowError, "清理阶段"):
            self.rename("标题")
        self.assertEqual(self.state["custom_title"], "")

    def test_save_failure_keeps_title_and_revision_unchanged(self):
        revision = self.runtime.revision
        with patch.object(self.runtime.engine.store, "save", side_effect=OSError("无法写入")):
            with self.assertRaises(OSError):
                self.rename("尚未保存的标题")
        self.assertEqual(self.state["custom_title"], "")
        self.assertEqual(self.runtime.revision, revision)

    def test_http_requires_cookie_and_csrf_and_returns_the_updated_title(self):
        server = LocalServer(("127.0.0.1", 0), self.application)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("GET", "/")
        response = client.getresponse()
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response.read()
        client.close()
        payload = {"session_id": self.state["id"], "title": "接口设置的标题", "request_id": str(uuid4())}
        for headers, expected in (({}, 400), ({"Cookie": cookie}, 400),
                ({"Cookie": cookie, "X-Workflow-Token": self.application.csrf_token}, 200)):
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                client.request("POST", "/api/sessions/title", dumps(payload),
                    {"Content-Type": "application/json", **headers})
                response = client.getresponse()
                value = loads(response.read())
                self.assertEqual(response.status, expected)
                if expected == 200:
                    self.assertEqual(value["state"]["title"], "接口设置的标题")
