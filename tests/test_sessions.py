"""用模拟会话和临时 Git 仓库检查并行、事件隔离及删除。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from json import dumps, loads
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.gitops import cleanup_workspaces, git, prepare, worktrees
from workflow.settings import ROOT, WorkflowError
from workflow.web import LocalServer, WebApplication


class SessionFixture(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            patched = patch(target, self.root)
            patched.start()
            self.addCleanup(patched.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.application = WebApplication(self.settings)
        self.addCleanup(self.application.close)
        self.first = self.add_session(7)
        self.second = self.add_session(8)

    def add_session(self, number):
        state = self.application.sessions.store.new({"kind": "task", "id": number, "title": f"脱敏任务{number}"})
        state["codex"] = {"model": "示例模型", "reasoning_effort": "high"}
        runtime = self.application.sessions.add(state)
        self.application.emit(state["id"], "state", state)
        return runtime

    def request(self, runtime, action, data=None, **change):
        return {"action": action, "data": data or {}, "session_id": runtime.engine.state["id"],
                "request_id": str(uuid4()), "revision": runtime.revision, **change}

    def drain(self, runtime):
        runtime.engine.executor.submit(lambda: None).result(timeout=5)
        runtime.engine.control_executor.submit(lambda: None).result(timeout=5)
        self.application.management_executor.submit(lambda: None).result(timeout=5)


class SessionChecks(SessionFixture):
    def test_two_sessions_execute_concurrently_and_viewing_does_not_stop_them(self):
        entered = [Event(), Event()]
        release = Event()
        self.addCleanup(release.set)
        def running(index):
            entered[index].set()
            release.wait(5)
        futures = [runtime.engine.submit(running, index) for index, runtime in enumerate((self.first, self.second))]
        try:
            self.assertTrue(all(event.wait(3) for event in entered))
            for runtime in (self.first, self.second):
                runtime.engine._close_rpc = Mock()
                self.application.session(runtime.engine.state["id"])
                self.application.dispatch(self.request(runtime, "restore", {"id": runtime.engine.state["id"]}))
                runtime.engine._close_rpc.assert_not_called()
        finally:
            release.set()
            for future in futures:
                future.result(timeout=5)

    def test_state_stream_questions_and_errors_are_scoped(self):
        first_id = self.first.engine.state["id"]
        second_id = self.second.engine.state["id"]
        revision = self.second.revision
        self.application.emit(first_id, "delta", "甲会话输出")
        self.application.emit(second_id, "delta", "乙会话输出")
        self.application.emit(first_id, "questions", [{"id": "q"}])
        self.application.emit(first_id, "error", "甲会话错误")
        self.first.engine.save()
        self.assertEqual(self.second.revision, revision)
        self.assertEqual(self.application.session(first_id)["stream"], "甲会话输出")
        self.assertEqual(self.application.session(second_id)["stream"], "乙会话输出")
        packets = [packet.decode() for _, packet in self.application.events]
        for packet in packets:
            if "event: questions" in packet or "event: operation_error" in packet:
                self.assertIn(first_id, packet)
                self.assertNotIn(second_id, packet)

    def test_internal_turn_completion_publishes_idle_activity_after_the_job_finishes(self):
        self.first.engine.submit(lambda: self.first.engine.save()).result(timeout=5)
        self.drain(self.first)
        packets = [loads(packet.decode().split("data: ", 1)[1].strip()) for _, packet in self.application.events
                   if b"event: busy" in packet and self.first.engine.state["id"].encode() in packet]
        self.assertTrue(packets)
        self.assertFalse(packets[-1]["working"])

    def test_answer_is_allowed_while_its_turn_waits_and_pause_only_targets_one_session(self):
        self.first.engine.turn_running = True
        self.first.engine.state["stage"] = "等待人工回答"
        self.first.engine.answer = Mock()
        self.application.dispatch(self.request(self.first, "answer", {"text": "明确答复"}))
        self.drain(self.first)
        self.first.engine.answer.assert_called_once_with("明确答复", None)
        self.second.engine.pause = Mock()
        self.first.engine.pause = Mock()
        self.application.dispatch(self.request(self.first, "pause"))
        self.drain(self.first)
        self.first.engine.pause.assert_called_once()
        self.second.engine.pause.assert_not_called()
        self.first.engine.turn_running = False

    def test_versions_and_duplicate_requests_belong_to_the_target_session(self):
        self.first.engine.start_plan = Mock()
        self.second.engine.start_plan = Mock()
        self.first.engine.save()
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request(self.first, "plan", revision=-1))
        request = self.request(self.second, "plan")
        self.application.dispatch(request)
        self.drain(self.second)
        self.assertTrue(self.application.dispatch(request)["duplicate"])
        self.second.engine.start_plan.assert_called_once()
        other = self.request(self.first, "plan", request_id=request["request_id"])
        self.application.dispatch(other)
        self.drain(self.first)
        self.first.engine.start_plan.assert_called_once()

    def test_collection_updates_one_draft_but_cannot_change_a_started_session(self):
        items = [{"kind": "task", "id": 7, "title": "甲"}, {"kind": "task", "id": 8, "title": "乙"}]
        self.application.items = items
        snapshot = {"sources": [], "materials": [], "project": {"id": 41, "name": "示例项目"}}
        count = len(self.application.sessions.runtimes)
        with patch.object(self.application.sessions.zentao, "collect_many", return_value=snapshot):
            self.application.dispatch(self.request(self.first, "collect", {"items": items}))
            self.drain(self.first)
        self.assertEqual(len(self.application.sessions.runtimes), count)
        self.assertEqual(self.first.engine.state["items"], items)
        self.first.engine.state["branch_resolution"] = {"repositories": []}
        with self.assertRaisesRegex(WorkflowError, "开始计划"):
            self.application.dispatch(self.request(self.first, "collect", {"items": items}))

    def test_new_collection_returns_stable_id_for_repeated_request(self):
        item = {"kind": "task", "id": 9, "title": "查看资料"}
        self.application.items = [item]
        request = self.request(self.first, "collect", {"items": [item]}, session_id=None)
        snapshot = {"sources": [], "materials": [], "project": {"id": 41, "name": "示例项目"}}
        with patch.object(self.application.sessions.zentao, "collect_many", return_value=snapshot):
            result = self.application.dispatch(request)
            self.drain(self.application.sessions.get_preview(result["preview_id"]))
        duplicate = self.application.dispatch(request)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(result["preview_id"], duplicate["preview_id"])
        self.assertNotIn("session_id", result)
        self.assertEqual(len(self.application.sessions.runtimes), 2)

    def test_restarting_pauses_all_interrupted_sessions_and_clears_pending_requests(self):
        for runtime in (self.first, self.second):
            runtime.engine.state.update(stage="实施中", rpc_connected=True, pending={"id": "旧请求"})
            runtime.engine.save()
        restarted = WebApplication(deepcopy(self.settings))
        self.addCleanup(restarted.close)
        self.assertEqual(len(restarted.snapshot()["history"]), 2)
        for payload in restarted.snapshot()["sessions"].values():
            self.assertEqual(payload["state"]["stage"], "已暂停")
            self.assertFalse(payload["state"]["rpc_connected"])
            self.assertIsNone(payload["state"]["pending"])
            self.assertIsNone(restarted.sessions.get(payload["session_id"]).engine.rpc)

    def test_shutdown_closes_other_sessions_when_one_channel_fails(self):
        with patch.object(self.first.engine, "close", side_effect=WorkflowError("模拟通道退出失败")) as first_close, \
                patch.object(self.second.engine, "close") as second_close:
            with self.assertRaisesRegex(WorkflowError, "部分会话关闭未完成"):
                self.application.sessions.close()
            first_close.assert_called_once()
            second_close.assert_called_once()

    def test_delete_empty_session_is_idempotent_and_clears_legacy_pointer(self):
        session_id = self.first.engine.state["id"]
        pointer = self.root / "current-session.json"
        pointer.write_text(dumps({"id": session_id}), encoding="utf-8")
        request = self.request(self.first, "delete_session")
        self.application.dispatch(request)
        self.drain(self.second)
        self.assertNotIn(session_id, self.application.sessions.runtimes)
        self.assertFalse(self.application.sessions.store.directory(session_id).exists())
        self.assertFalse(pointer.exists())
        self.assertTrue(self.application.dispatch(request)["duplicate"])
        with self.assertRaises(WorkflowError):
            self.application.session(session_id)

    def test_record_removal_failure_can_be_retried_after_the_engine_was_closed(self):
        session_id = self.first.engine.state["id"]
        with patch.object(self.application.sessions.store, "remove", side_effect=OSError("模拟资料删除失败")):
            self.application.dispatch(self.request(self.first, "delete_session"))
            self.drain(self.second)
        self.assertTrue(self.first.engine.closed)
        self.assertTrue(self.application.session(session_id)["state"]["cleanup"]["finalizing"])
        with self.assertRaisesRegex(WorkflowError, "只能继续删除"):
            self.application.dispatch(self.request(self.first, "plan"))
        with self.assertRaisesRegex(WorkflowError, "不能修改补充图片"):
            self.application.upload_supplement_image(session_id, self.first.revision, "图片.png", "不应写入".encode("utf-8"))
        self.application.dispatch(self.request(self.first, "delete_session"))
        self.drain(self.second)
        self.assertNotIn(session_id, self.application.sessions.runtimes)

    def test_delete_rejects_an_active_stage_or_queued_operation(self):
        self.first.engine.state["stage"] = "实施中"
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request(self.first, "delete_session"))
        self.first.engine.state["stage"] = "已暂停"
        entered, release = Event(), Event()
        def operation():
            entered.set()
            release.wait(5)
        future = self.first.engine.submit(operation)
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request(self.first, "delete_session"))
        finally:
            release.set()
            future.result(timeout=5)

    def test_deletion_locks_only_its_target_session(self):
        entered, release = Event(), Event()
        def cleanup(*_):
            entered.set()
            release.wait(5)
        with patch("workflow.web.cleanup_workspaces", side_effect=cleanup):
            self.application.dispatch(self.request(self.first, "delete_session"))
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(WorkflowError, "正在删除"):
                    self.application.dispatch(self.request(self.first, "pause"))
                self.second.engine.start_plan = Mock()
                self.application.dispatch(self.request(self.second, "plan"))
                self.second.engine.executor.submit(lambda: None).result(timeout=5)
                self.second.engine.start_plan.assert_called_once()
            finally:
                release.set()
                self.drain(self.second)

    def test_http_session_endpoint_and_diff_require_explicit_session_id(self):
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
        headers = {"Cookie": cookie}
        client.request("GET", "/api/bootstrap", headers=headers)
        bootstrap = loads(client.getresponse().read())
        self.assertEqual(len(bootstrap["history"]), 2)
        self.assertEqual(set(bootstrap["sessions"]), set(self.application.sessions.runtimes))
        client.request("GET", "/api/session?id=" + self.second.engine.state["id"], headers=headers)
        response = client.getresponse()
        payload = loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["state"]["item"]["id"], 8)
        client.request("GET", "/api/diff", headers=headers)
        response = client.getresponse()
        self.assertEqual(response.status, 400)
        response.read()

    def test_store_rejects_an_absolute_path_and_another_session_path(self):
        for value in (str(self.root), "../其他会话", "..", ""):
            with self.subTest(value=value), self.assertRaises(WorkflowError):
                self.application.sessions.store.directory(value)

    def test_common_branch_choices_are_merged_under_the_shared_settings_lock(self):
        flows = [self.first.engine, self.second.engine]
        for index, flow in enumerate(flows):
            flow.state.update(stage="等待选择分支", project={"id": index + 41, "name": f"项目{index}"},
                              branch_resolution={"repositories": [{"role": "frontend", "status": "needs_user",
                                "catalog": [{"name": f"分支{index}", "local": True}], "source": ""}]})
            flow._advance_branches = Mock()
        snapshots = []
        def save(settings):
            snapshots.append(deepcopy(settings["project_branches"]))
        with patch("workflow.engine.save_settings", side_effect=save), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(flow.choose_branches, {"frontend": f"分支{index}"}) for index, flow in enumerate(flows)]
            for future in futures:
                future.result(timeout=5)
        saved = snapshots[-1][self.settings["zentao"]["base_url"].rstrip("/")]
        self.assertEqual(set(saved), {"41", "42"})


class WorkspaceDeletionChecks(SessionFixture):
    def setUp(self):
        super().setUp()
        self.sources = []
        self.first.engine.state["stage"] = "已暂停"
        directory = self.application.sessions.store.directory(self.first.engine.state["id"])
        for index, role in enumerate(("frontend", "backend")):
            source = self.root / f"source-{index}"
            source.mkdir()
            git(source, "init", "-b", "原目录分支")
            git(source, "config", "user.name", "会话检查")
            git(source, "config", "user.email", "session@example.invalid")
            (source / "说明.txt").write_text("初始文本", encoding="utf-8")
            git(source, "add", "说明.txt")
            git(source, "commit", "-m", "初始化脱敏资料")
            git(source, "branch", "项目分支")
            record = prepare(source, directory / "workspace" / f"repo-{index + 1}", "项目分支", "refs/heads/项目分支", False)
            record.update(name=role, role=role)
            self.first.engine.state["repos"].append(record)
            self.sources.append({"name": role, "path": str(source), "remote": "origin", "role": role})
        self.application.settings["repositories"] = self.sources
        self.first.engine.save()

    def test_clean_workspaces_are_removed_and_commits_and_source_directories_remain(self):
        branches = [git(source["path"], "rev-parse", "项目分支") for source in self.sources]
        session_id = self.first.engine.state["id"]
        self.application.dispatch(self.request(self.first, "delete_session"))
        self.drain(self.second)
        self.assertNotIn(session_id, self.application.sessions.runtimes)
        for index, source in enumerate(self.sources):
            self.assertEqual(len(worktrees(source["path"])), 1)
            self.assertEqual(git(source["path"], "rev-parse", "项目分支"), branches[index])
            self.assertEqual(git(source["path"], "branch", "--show-current"), "原目录分支")

    def test_all_workspaces_are_checked_before_any_removal(self):
        records = self.first.engine.state["repos"]
        (Path(records[1]["path"]) / "未跟踪.txt").write_text("保留", encoding="utf-8")
        self.application.dispatch(self.request(self.first, "delete_session"))
        self.drain(self.second)
        self.assertIn("未提交或未跟踪", self.first.engine.state["error"])
        self.assertTrue(all(Path(record["path"]).exists() for record in records))

    def test_staged_and_unstaged_changes_both_prevent_deletion(self):
        record = self.first.engine.state["repos"][0]
        path = Path(record["path"]) / "说明.txt"
        path.write_text("用户修改", encoding="utf-8")
        for staged in (False, True):
            if staged:
                git(record["path"], "add", "说明.txt")
            with self.subTest(staged=staged), self.assertRaisesRegex(WorkflowError, "未提交或未跟踪"):
                cleanup_workspaces(self.application.sessions.store, self.first.engine.state, self.sources)
        self.assertEqual(path.read_text(encoding="utf-8"), "用户修改")

    def test_partial_failure_keeps_record_and_retry_only_removes_remaining_workspace(self):
        removed = []
        original_git = git
        def fail_second(source, *args, **kwargs):
            if args[:2] == ("worktree", "remove"):
                if removed:
                    raise WorkflowError("模拟第二个工作区移除失败")
                removed.append(args[-1])
            return original_git(source, *args, **kwargs)
        with patch("workflow.gitops.git", side_effect=fail_second):
            self.application.dispatch(self.request(self.first, "delete_session"))
            self.drain(self.second)
        session_id = self.first.engine.state["id"]
        self.assertIn(session_id, self.application.sessions.runtimes)
        self.assertEqual(self.first.engine.state["cleanup"]["removed"], removed)
        self.assertIn("已清理", self.first.engine.state["error"])
        with self.assertRaisesRegex(WorkflowError, "只能继续删除"):
            self.application.dispatch(self.request(self.first, "resume"))
        self.application.dispatch(self.request(self.first, "delete_session"))
        self.drain(self.second)
        self.assertNotIn(session_id, self.application.sessions.runtimes)

    def test_registered_workspace_is_found_when_preparation_did_not_save_repo_record(self):
        self.first.engine.state["repos"] = []
        cleanup_workspaces(self.application.sessions.store, self.first.engine.state, self.sources)
        self.assertTrue(all(len(worktrees(source["path"])) == 1 for source in self.sources))

    def test_workspace_outside_the_session_cannot_be_removed(self):
        self.first.engine.state["repos"][0]["path"] = self.sources[0]["path"]
        with self.assertRaisesRegex(WorkflowError, "不在目标会话"):
            cleanup_workspaces(self.application.sessions.store, self.first.engine.state, self.sources)
        self.assertTrue(Path(self.sources[0]["path"]).exists())

    def test_branch_occupation_reports_session_id_and_path(self):
        session_id = self.first.engine.state["id"]
        with self.assertRaises(WorkflowError) as captured:
            prepare(self.sources[0]["path"], self.root / "新工作区", "项目分支", "refs/heads/项目分支", False)
        self.assertIn(session_id, str(captured.exception))
        self.assertIn(self.first.engine.state["repos"][0]["path"], str(captured.exception))

    def test_parallel_preparation_of_one_branch_only_creates_one_workspace(self):
        source = self.sources[0]["path"]
        git(source, "branch", "并发分支")
        def create(index):
            try:
                return prepare(source, self.root / f"并发工作区{index}", "并发分支", "refs/heads/并发分支", False)
            except WorkflowError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, (1, 2)))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(sum(isinstance(value, str) and "已被占用" in value for value in results), 1)
