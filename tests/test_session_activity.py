"""隔离检查会话对话时间、历史兼容、排序与实时响应，不连接业务系统。"""

from copy import deepcopy
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from workflow.settings import ROOT
from workflow.web import WebApplication, history_summary, public_state, session_activity


class SessionActivityFixture(TestCase):
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

    def add_session(self, session_id, history, project_id=41):
        state = self.application.sessions.store.new({"kind": "task", "id": 7, "title": "时间排序检查"}, save=False)
        state.update(id=session_id, custom_title=session_id, history=deepcopy(history),
                     project={"id": project_id, "name": f"项目{project_id}", "candidates": []})
        runtime = self.application.sessions.add(state)
        runtime.engine.save()
        return runtime


class SessionActivityChecks(SessionActivityFixture):
    def test_history_uses_actual_instants_and_excludes_system_and_controller(self):
        state = {"history": [
            {"role": "Codex", "time": "2026-11-10T09:00:00+08:00"},
            {"role": "用户", "time": "2026-11-10T02:00:00+00:00"},
            {"role": "用户", "time": "2026-11-10T09:30:00+08:00"},
            {"role": "系统", "time": "2026-11-11T01:00:00+00:00"},
            {"role": "控制器", "time": "2026-11-12T01:00:00+00:00"}]}
        original = deepcopy(state)
        self.assertEqual(session_activity(state), {"last_dialogue_at": "2026-11-10T02:00:00+00:00",
                                                  "last_user_message_at": "2026-11-10T02:00:00+00:00"})
        self.assertEqual(state, original)

    def test_no_user_or_no_dialogue_has_explicit_empty_times(self):
        self.assertEqual(session_activity({"history": []}), {"last_dialogue_at": None, "last_user_message_at": None})
        self.assertEqual(session_activity({"history": [{"role": "系统", "time": "2026-11-10T00:00:00+00:00"}]}),
                         {"last_dialogue_at": None, "last_user_message_at": None})
        self.assertEqual(session_activity({"history": [{"role": "Codex", "time": "2026-11-10T00:00:00+00:00"}]}),
                         {"last_dialogue_at": "2026-11-10T00:00:00+00:00", "last_user_message_at": None})

    def test_summary_and_detail_derive_old_history_without_persisting_new_fields(self):
        runtime = self.add_session("legacy-session", [{"role": "用户", "text": "历史问题", "time": "2026-11-01T00:00:00+00:00"}])
        state = runtime.engine.state
        original = deepcopy(state)
        for response in (history_summary(state), public_state(state), self.application.session(state["id"])["state"]):
            self.assertEqual(response["last_dialogue_at"], "2026-11-01T00:00:00+00:00")
            self.assertEqual(response["last_user_message_at"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(state, original)
        saved = self.application.sessions.store.load(state["id"])
        self.assertNotIn("last_dialogue_at", saved)
        self.assertNotIn("last_user_message_at", saved)

    def test_snapshot_orders_by_dialogue_then_id_and_puts_empty_sessions_last(self):
        self.add_session("a-old", [{"role": "用户", "text": "旧消息", "time": "2026-11-10T09:00:00+08:00"}])
        self.add_session("b-new", [{"role": "Codex", "text": "新回复", "time": "2026-11-10T02:00:00+00:00"}], 42)
        self.add_session("c-tie", [{"role": "用户", "text": "同一时刻", "time": "2026-11-10T10:00:00+08:00"}])
        empty = self.add_session("z-empty", [])
        empty.engine.state["updated_at"] = "2099-01-01T00:00:00+00:00"
        self.assertEqual([entry["id"] for entry in self.application.snapshot()["history"]],
                         ["c-tie", "b-new", "a-old", "z-empty"])

    def test_view_rename_save_and_restart_preserve_dialogue_times(self):
        runtime = self.add_session("unchanged-session", [{"role": "用户", "text": "原消息", "time": "2026-11-01T00:00:00+00:00"}])
        before = session_activity(runtime.engine.state)
        self.application.session(runtime.engine.state["id"])
        self.application.rename_session({"session_id": runtime.engine.state["id"], "title": "新名称", "request_id": str(uuid4())})
        runtime.engine.state.update(stage="计划中", rpc_connected=True)
        runtime.engine.save()
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        response = restored.snapshot()["history"][0]
        self.assertEqual({key: response[key] for key in before}, before)
        self.assertEqual(response["stage"], "已暂停")

    def test_message_events_update_dialogue_and_user_time_independently(self):
        runtime = self.add_session("live-session", [{"role": "用户", "text": "旧问题", "time": "2026-11-01T00:00:00+00:00"}])
        with patch("workflow.store.timestamp", return_value="2026-11-10T00:00:00+00:00"):
            runtime.engine.note("Codex", "新回复")
        state = self.application.session("live-session")["state"]
        self.assertEqual(state["last_dialogue_at"], "2026-11-10T00:00:00+00:00")
        self.assertEqual(state["last_user_message_at"], "2026-11-01T00:00:00+00:00")
        with patch("workflow.store.timestamp", return_value="2026-11-10T01:00:00+00:00"):
            runtime.engine.note("用户", "继续对话")
        packet = self.application.events[-1][1].decode("utf-8")
        self.assertIn("event: conversation", packet)
        data = loads(next(line[6:] for line in packet.splitlines() if line.startswith("data: ")))
        self.assertEqual(data["state"]["last_dialogue_at"], "2026-11-10T01:00:00+00:00")
        self.assertEqual(data["state"]["last_user_message_at"], "2026-11-10T01:00:00+00:00")
