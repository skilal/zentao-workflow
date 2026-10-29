"""项目分区、查看归属、旧会话移动与启动锁定。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.projects import ProjectPartitions, project_locked
from workflow.settings import ROOT, WorkflowError
from workflow.web import LocalServer, WebApplication


class ProjectPartitionChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            patched = patch(target, self.root)
            patched.start()
            self.addCleanup(patched.stop)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.application = WebApplication(settings)
        self.addCleanup(self.application.close)
        self.application.items = [{"kind": "task", "id": 7, "title": "示例任务", "project_id": 41,
                                   "project_name": "同名项目"},
                                  {"kind": "task", "id": 8, "title": "另一任务", "project_id": 42,
                                   "project_name": "同名项目"}]

    def add_session(self, project_id=41):
        state = self.application.sessions.store.new(self.application.items[0])
        state["codex"] = {"model": "示例模型", "reasoning_effort": "high"}
        state["snapshot"] = {"project": {"id": project_id, "name": "同名项目", "candidates": []},
                             "sources": [{"raw": {"project": project_id}, "material_ids": []}],
                             "materials": [], "blockers": [], "feishu_links": []}
        runtime = self.application.sessions.add(state)
        runtime.engine.save()
        return runtime

    def move_request(self, runtime, project_id=42, **changes):
        return {"session_id": runtime.engine.state["id"], "project_id": project_id,
                "revision": runtime.revision, "request_id": str(uuid4()), **changes}

    def drain(self, runtime):
        runtime.engine.executor.submit(lambda: None).result(timeout=5)
        runtime.engine.control_executor.submit(lambda: None).result(timeout=5)
        self.application.management_executor.submit(lambda: None).result(timeout=5)

    def test_catalog_uses_ids_and_explicit_saved_projects_without_guessing_names(self):
        first = self.add_session()
        second = self.add_session(0)
        second.engine.state["item"]["project"] = "不能猜测的项目名"
        second.engine.save()
        snapshot = self.application.snapshot()
        self.assertEqual({value["id"] for value in snapshot["project_partitions"]}, {41, 42})
        summaries = {value["id"]: value for value in snapshot["history"]}
        self.assertEqual(summaries[first.engine.state["id"]]["project"]["id"], 41)
        self.assertEqual(summaries[second.engine.state["id"]]["project"]["id"], 0)
        self.assertTrue(summaries[first.engine.state["id"]]["can_move_project"])

    def test_manual_empty_partition_persists_deduplicates_and_is_site_scoped(self):
        request = {"project_id": 53, "request_id": str(uuid4())}
        with patch.object(self.application.sessions.zentao, "_fetch", return_value={"id": 53, "name": "真实项目"}) as fetch:
            result = self.application.add_project(request)
            duplicate = self.application.add_project(request)
            self.application.add_project({**request, "request_id": str(uuid4())})
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(result["project"], {"id": 53, "name": "真实项目"})
        self.assertEqual(len([value for value in self.application.projects() if value["id"] == 53]), 1)
        restored = ProjectPartitions(self.root / "project-partitions.json")
        site = self.application.settings["zentao"]["base_url"].rstrip("/")
        self.assertEqual(restored.list(site, [], []), [{"id": 53, "name": "真实项目", "manual": True}])
        self.assertEqual(restored.list("https://another.invalid", [], []), [])
        self.assertTrue(any(b"event: projects\n" in packet for _, packet in self.application.events))

    def test_invalid_or_inaccessible_project_is_not_saved_or_retried(self):
        for project_id in (True, 0, -1, "41", None):
            with self.subTest(project_id=project_id), self.assertRaises(WorkflowError):
                self.application.add_project({"project_id": project_id, "request_id": str(uuid4())})
        with patch.object(self.application.sessions.zentao, "_fetch", side_effect=WorkflowError("无项目权限")) as fetch:
            with self.assertRaisesRegex(WorkflowError, "无项目权限"):
                self.application.add_project({"project_id": 53, "request_id": str(uuid4())})
            fetch.assert_called_once()
        with patch.object(self.application.sessions.zentao, "_fetch", return_value={"id": 99, "name": "错误项目"}):
            with self.assertRaisesRegex(WorkflowError, "不符合预期"):
                self.application.add_project({"project_id": 53, "request_id": str(uuid4())})
        self.assertFalse((self.root / "project-partitions.json").exists())

    def test_move_updates_execution_snapshot_and_persists_without_modifying_raw_materials(self):
        runtime = self.add_session()
        raw = deepcopy(runtime.engine.state["snapshot"]["sources"])
        request = self.move_request(runtime)
        with patch.object(self.application.sessions.zentao, "_fetch") as fetch:
            moved = self.application.move_project(request)
            duplicate = self.application.move_project(request)
        fetch.assert_not_called()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(moved["state"]["project"]["id"], 42)
        self.assertEqual(moved["state"]["snapshot"]["project"]["id"], 42)
        saved = self.application.sessions.store.load(runtime.engine.state["id"])
        self.assertEqual(saved["project_override"]["id"], 42)
        self.assertEqual(saved["snapshot"]["sources"], raw)
        with self.assertRaisesRegex(WorkflowError, "版本已过期"):
            self.application.move_project({**request, "request_id": str(uuid4())})

    def test_failed_persistence_keeps_memory_and_client_version_unchanged(self):
        runtime = self.add_session()
        original = deepcopy(runtime.engine.state)
        revision = runtime.revision
        with patch.object(runtime.engine.store, "save", side_effect=OSError("无法写入")):
            with self.assertRaises(OSError):
                self.application.move_project(self.move_request(runtime))
        self.assertEqual(runtime.engine.state, original)
        self.assertEqual(runtime.revision, revision)

    def test_clearing_project_requires_confirmation_when_plan_starts(self):
        runtime = self.add_session()
        moved = self.application.move_project(self.move_request(runtime, None))
        self.assertEqual(moved["state"]["project"]["id"], 0)
        self.assertEqual(moved["state"]["project"]["candidates"], [])
        runtime.engine.start_plan("")
        self.assertEqual(runtime.engine.state["stage"], "等待确认项目")
        self.assertTrue(project_locked(runtime.engine.state))

    def test_explicit_project_survives_material_rereading_and_drives_branch_resolution(self):
        runtime = self.add_session()
        self.application.move_project(self.move_request(runtime))
        snapshot = deepcopy(runtime.engine.state["snapshot"])
        snapshot["project"] = {"id": 41, "name": "原项目"}
        with patch.object(runtime.engine.zentao, "collect_many", return_value=snapshot):
            runtime.engine.collect([self.application.items[0]])
        self.assertEqual(runtime.engine.state["project"]["id"], 42)
        self.assertEqual(runtime.engine.state["snapshot"]["project"]["id"], 42)
        with patch.object(runtime.engine, "_resolve_branches") as resolve:
            runtime.engine.start_plan("")
        resolve.assert_called_once()
        self.assertEqual(runtime.engine.state["project"]["id"], 42)

    def test_started_work_artifacts_and_busy_sessions_reject_moves(self):
        runtime = self.add_session()
        original = deepcopy(runtime.engine.state)
        for change in ({"stage": "已暂停"}, {"plan_started_at": "已启动"}, {"branch_resolution": {}},
                       {"repos": [{"path": "既有工作区"}]}, {"thread_id": "既有线程"},
                       {"plan": "已有计划"}, {"cleanup": {"finalizing": True}}):
            with self.subTest(change=change):
                runtime.engine.state = {**deepcopy(original), **change}
                with self.assertRaises(WorkflowError):
                    self.application.move_project(self.move_request(runtime))
                self.assertFalse(self.application.session_payload(runtime)["state"]["can_move_project"])
        runtime.engine.state = original
        runtime.busy = "collect"
        with self.assertRaisesRegex(WorkflowError, "尚未结束"):
            self.application.move_project(self.move_request(runtime))
        runtime.busy = None
        with self.assertRaisesRegex(WorkflowError, "不存在"):
            self.application.move_project(self.move_request(runtime, 999))

    def test_accepting_plan_locks_project_even_if_execution_fails(self):
        runtime = self.add_session()
        request = {"action": "plan", "data": {}, "request_id": str(uuid4()),
                   "session_id": runtime.engine.state["id"], "revision": runtime.revision}
        with patch.object(runtime.engine, "start_plan", side_effect=WorkflowError("模拟计划失败")):
            self.application.dispatch(request)
            self.drain(runtime)
        self.assertTrue(runtime.engine.state["plan_started_at"])
        runtime.engine.state["stage"] = "资料已读取"
        with self.assertRaisesRegex(WorkflowError, "已开始需求分析与计划"):
            self.application.move_project(self.move_request(runtime))
        self.assertTrue(self.application.sessions.store.load(runtime.engine.state["id"])["plan_started_at"])

    def test_move_and_start_plan_are_serialized_by_the_same_revision_and_lock(self):
        runtime = self.add_session()
        barrier = Barrier(2)
        move = self.move_request(runtime)
        plan = {"action": "plan", "session_id": runtime.engine.state["id"], "data": {},
                "revision": runtime.revision, "request_id": str(uuid4())}
        def invoke(action):
            barrier.wait(timeout=3)
            try:
                return action()
            except WorkflowError:
                return None
        with patch.object(runtime.engine, "start_plan", Mock()), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invoke, lambda: self.application.move_project(move)),
                       executor.submit(invoke, lambda: self.application.dispatch(plan))]
            results = [future.result(timeout=5) for future in futures]
            self.drain(runtime)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(runtime.engine.state.get("project", {"id": 41})["id"], 42 if results[0] else 41)

    def test_http_endpoints_enforce_csrf_and_return_live_project_summaries(self):
        runtime = self.add_session()
        server = LocalServer(("127.0.0.1", 0), self.application)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(client.close)
        client.request("GET", "/")
        response = client.getresponse()
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response.read()
        headers = {"Cookie": cookie, "Content-Type": "application/json"}
        client.request("POST", "/api/sessions/project", dumps(self.move_request(runtime)), headers)
        response = client.getresponse()
        response.read()
        self.assertEqual(response.status, 400)
        client.close()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(client.close)
        headers["X-Workflow-Token"] = self.application.csrf_token
        client.request("POST", "/api/sessions/project", dumps(self.move_request(runtime)), headers)
        response = client.getresponse()
        payload = loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["state"]["project"]["id"], 42)
        with patch.object(self.application.sessions.zentao, "_fetch", return_value={"id": 53, "name": "新增项目"}):
            client.request("POST", "/api/projects", dumps({"project_id": 53, "request_id": str(uuid4())}), headers)
            response = client.getresponse()
            payload = loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["project"]["id"], 53)
