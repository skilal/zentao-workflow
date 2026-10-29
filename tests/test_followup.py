"""用隔离会话、模拟 Codex 和临时仓库检查已完成会话的多轮修复。"""

from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from itertools import count
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.approvals import is_controlled_write
from workflow.engine import Workflow
from workflow.gitops import cleanup_workspaces, commit, git, prepare, worktrees
from workflow.mcp_server import TOOLS, Tools
from workflow.settings import ROOT, WorkflowError
from workflow.store import write_authorized
from workflow.web import WebApplication, public_state


PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class FollowupChecks(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            mocked = patch(target, self.root / "local")
            mocked.start()
            self.addCleanup(mocked.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.application = WebApplication(self.settings)
        self.addCleanup(self.application.close)
        self.state = self.application.sessions.store.new({"kind": "story", "id": 7, "title": "测试反馈续聊"})
        self.state.update(stage="已完成（静态审查）", phase="complete", thread_id="原需求线程",
                          codex={"model": "模拟模型", "reasoning_effort": "high"},
                          snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                          plan="原需求计划", approved_plan="原需求计划", prepare_complete=True,
                          plan_hash=sha256("原需求计划".encode()).hexdigest(),
                          approved_hash=sha256("原需求计划".encode()).hexdigest(),
                          approval_source="manual", max_fix_rounds=3, fix_round=3,
                          acceptance={"ids": ["R01-01"], "reason": "原有问题已接受"})
        for index, role in enumerate(("frontend", "backend")):
            path = self.root / role
            path.mkdir()
            git(path, "init", "-b", "项目分支")
            git(path, "config", "user.name", "续聊检查")
            git(path, "config", "user.email", "followup@example.invalid")
            (path / "业务.txt").write_text("初始内容\n", encoding="utf-8")
            (path / "本机.txt").write_text("本机初始内容\n", encoding="utf-8")
            git(path, "add", ".")
            git(path, "commit", "-m", "初始化")
            baseline = git(path, "rev-parse", "HEAD")
            (path / "业务.txt").write_text("首轮交付\n", encoding="utf-8")
            git(path, "add", ".")
            git(path, "commit", "-m", "首轮交付")
            name = "前端" if index == 0 else "后端"
            self.state["repos"].append({"source": str(path), "path": str(path), "name": name, "role": role,
                "branch": "项目分支", "base": "refs/heads/项目分支", "baseline": baseline,
                "workspace_owned": False, "preserved_paths": []})
            self.state["commits"][name] = git(path, "rev-parse", "HEAD")
        self.state["reviews"] = [{"number": 1, "fix_round": 3, "history_index": 0, "result": {
            "summary": "原有问题已接受", "findings": [{"id": "R01-01", "title": "历史问题", "category": "业务链路问题",
                "origin": "原有问题", "severity": "P2", "previous_id": "", "repository": "前端", "path": "业务.txt", "line": 1,
                "evidence": "原版本中已经存在", "suggestion": "后续专项处理", "impact": "不影响当前验收"}], "resolved_previous": []}}]
        self.state["decisions"] = [{"round": 1, "fix_ids": ["R01-01"], "note": "历史选择"}]
        self.state["history"] = [{"time": "2026-01-01T00:00:00+00:00", "role": "Codex", "text": "历史审查", "phase": "review"}]
        self.runtime = self.application.sessions.add(self.state)
        self.engine = self.runtime.engine
        self.engine.save()
        self.turns = count(1)
        self.calls = []
        self.engine.ensure_rpc = Mock(side_effect=self.connect_mock)

    def connect_mock(self):
        if self.engine.rpc is None:
            self.engine.rpc = Mock()
            self.engine.rpc.model = "模拟模型"
            self.engine.rpc.request.side_effect = self.rpc_request

    def rpc_request(self, method, params):
        self.calls.append((method, params))
        return {"turn": {"id": f"回合-{next(self.turns)}"}}

    def send(self, text="修复测试反馈", image_ids=None, request_id=None):
        images = self.engine.validate_followup(text, image_ids or [])
        self.engine.followup(text, images, request_id or uuid4().hex)

    def complete(self, text):
        self.engine.last_message = text
        self.engine.turn_running = False
        self.engine._completed({"status": "completed", "id": self.state["turn_id"]},
                               self.state["id"], self.engine.connection_epoch)

    def finish(self):
        self.complete("已修复并完成静态检查")
        self.complete(dumps({"summary": "本轮无问题", "findings": [], "resolved_previous": []}))

    def request(self, **changes):
        return {"action": "followup", "data": {"text": "测试反馈", "image_ids": []},
                "session_id": self.state["id"], "revision": self.runtime.revision,
                "request_id": uuid4().hex, **changes}

    def drain(self):
        self.engine.executor.submit(lambda: None).result(timeout=10)

    def upload(self):
        self.application.upload_supplement_image(self.state["id"], self.runtime.revision, "测试截图.png", PNG, purpose="followup")
        return self.state["followup_images"][-1]

    def test_repeated_deliveries_keep_thread_and_only_commit_current_changes(self):
        original = deepcopy(self.state["commits"])
        for number in (2, 3):
            self.send(f"修复第 {number} 轮问题")
            self.assertEqual(self.state["thread_id"], "原需求线程")
            self.assertEqual(self.calls[-1][1]["threadId"], "原需求线程")
            self.assertEqual(self.state["delivery_round"], number)
            self.assertEqual(self.state["fix_round"], 0)
            self.assertEqual(self.state["commits"], {})
            self.assertEqual(self.engine.current_reviews(), [])
            context = Tools(self.state["id"]).call("task_context", {})
            self.assertEqual(context["decisions"], [])
            path = Path(self.state["repos"][0]["path"])
            Tools(self.state["id"]).call("write_file", {"repository": "前端", "path": "业务.txt", "content": f"第 {number} 轮修复\n"})
            self.complete("已修复")
            self.assertIn("上轮审查：null", self.calls[-1][1]["input"][0]["text"])
            self.complete(dumps({"summary": "无遗留问题", "findings": [], "resolved_previous": []}))
            self.assertEqual(self.state["phase"], "complete")
            self.assertTrue(git(path, "log", "-1", "--format=%s").startswith("fix(zentao-story-7):"))
            self.assertEqual(self.state["commits"]["后端"], "无代码修改")
            self.assertEqual(self.state["reviews"][0]["result"]["findings"][0]["id"], "R01-01")
        self.assertEqual(self.state["deliveries"][0]["commits"], original)
        self.assertEqual(len(self.state["deliveries"]), 2)
        self.assertEqual([review.get("delivery_round", 1) for review in self.state["reviews"]], [1, 2, 3])

    def test_image_only_feedback_is_persistent_attached_and_readonly(self):
        image = self.upload()
        self.send("", [image["id"]])
        self.assertTrue(write_authorized(self.state))
        message = self.state["history"][self.state["followup"]["history_index"]]
        self.assertEqual(message["images"][0]["id"], image["id"])
        self.assertIn({"type": "localImage", "path": image["path"]}, self.calls[-1][1]["input"])
        self.finish()
        with self.assertRaises(WorkflowError):
            self.application.remove_supplement_image(self.state["id"], self.runtime.revision, image["id"], purpose="followup")
        with self.assertRaises(WorkflowError):
            self.engine.validate_followup("再次修复", [image["id"]])
        self.assertTrue(Path(image["path"]).is_file())
        self.assertEqual(self.engine.store.load(self.state["id"])["history"][1]["images"][0]["id"], image["id"])

    def test_draft_images_restore_and_can_be_removed(self):
        image = self.upload()
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(self.state["id"])["state"]["followup_images"][0]["id"], image["id"])
        self.application.remove_supplement_image(self.state["id"], self.runtime.revision, image["id"], purpose="followup")
        self.assertFalse(Path(image["path"]).exists())
        with self.assertRaises(WorkflowError):
            self.application.upload_supplement_image(self.state["id"], self.runtime.revision, "截图.png", PNG, purpose=["followup"])

    def test_validation_rejects_empty_invalid_and_foreign_images(self):
        for text, ids in ((" ", []), (None, []), ("问题", "图片"), ("问题", [{}]), ("问题", ["外部图片"]), ("问题", ["a", "a"])):
            with self.subTest(text=text, ids=ids), self.assertRaises(WorkflowError):
                self.engine.validate_followup(text, ids)
        self.state["thread_id"] = None
        with self.assertRaises(WorkflowError):
            self.engine.validate_followup("问题", [])

    def test_stale_revision_and_busy_requests_do_not_create_deliveries(self):
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request(revision=-1))
        self.runtime.busy = "正在操作"
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request())
        self.runtime.busy = None
        self.assertEqual(self.state["deliveries"], [])

    def test_duplicate_request_survives_restart_and_old_delivery(self):
        request = self.request()
        self.application.dispatch(request)
        self.drain()
        self.assertTrue(self.application.dispatch(request)["duplicate"])
        self.finish()
        self.send("第二条反馈")
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        result = restored.dispatch(request)
        self.assertTrue(result["duplicate"])
        self.assertEqual(len(restored.sessions.get(self.state["id"]).engine.state["deliveries"]), 2)

    def test_occupied_branch_stops_and_resume_keeps_one_feedback(self):
        other = self.engine.store.new({"kind": "task", "id": 9, "title": "占用会话"})
        other.update(phase="implement", repos=[deepcopy(self.state["repos"][0])])
        self.engine.store.save(other)
        self.application.dispatch(self.request())
        self.drain()
        self.assertEqual(self.state["stage"], "已停止")
        self.assertFalse(self.state["prepare_complete"])
        self.assertIn("占用", self.state["error"])
        with self.assertRaises(WorkflowError):
            self.engine.rereview()
        other["phase"] = "complete"
        self.engine.store.save(other)
        self.engine.resume()
        self.assertEqual(self.state["stage"], "修复中")
        self.assertEqual(len([entry for entry in self.state["history"] if entry["role"] == "用户"]), 1)
        self.assertEqual(len(self.state["deliveries"]), 1)

    def test_switched_directory_and_local_changes_are_preserved(self):
        repository = self.state["repos"][0]
        path = Path(repository["path"])
        git(path, "switch", "-c", "其他分支")
        (path / "本机.txt").write_text("本机需要保留\n", encoding="utf-8")
        git(path, "add", "本机.txt")
        index = git(path, "diff", "--cached", binary=True)
        self.send()
        self.assertEqual(git(path, "branch", "--show-current"), "项目分支")
        self.assertIn("本机.txt", self.state["repos"][0]["preserved_paths"])
        with self.assertRaises(WorkflowError):
            Tools(self.state["id"]).call("write_file", {"repository": "前端", "path": "本机.txt", "content": "不能覆盖"})
        self.finish()
        self.assertEqual(git(path, "diff", "--cached", binary=True), index)
        self.assertEqual((path / "本机.txt").read_text(encoding="utf-8"), "本机需要保留\n")

    def test_partial_commit_only_continues_unfinished_repository(self):
        self.send()
        for repo in self.state["repos"]:
            (Path(repo["path"]) / "业务.txt").write_text("本轮修复\n", encoding="utf-8")
        self.complete("已修复")
        committed = []
        def partial(repo, message, expected):
            if repo["name"] == "后端":
                raise WorkflowError("模拟后端提交失败")
            committed.append(repo["name"])
            return commit(repo, message, expected)
        with patch("workflow.engine.commit", side_effect=partial), self.assertRaises(WorkflowError):
            self.complete(dumps({"summary": "无问题", "findings": [], "resolved_previous": []}))
        self.assertEqual(self.state["stage"], "部分提交")
        first = self.state["commits"]["前端"]
        self.engine.finalize([], "")
        self.assertEqual(self.state["phase"], "complete")
        self.assertEqual(self.state["commits"]["前端"], first)
        self.assertEqual(committed, ["前端"])

    def test_resume_uses_original_thread_and_failure_does_not_start_another(self):
        self.send()
        self.engine._close_rpc()
        del self.engine.ensure_rpc
        rpc = Mock()
        rpc.process.poll.return_value = None
        rpc.model = "模拟模型"
        rpc.disabled_mcp = {}
        with patch("workflow.engine.AppServer", return_value=rpc):
            self.engine.ensure_rpc()
        self.assertEqual(rpc.request.call_args.args[0], "thread/resume")
        self.assertEqual(rpc.request.call_args.args[1]["threadId"], "原需求线程")
        self.engine._close_rpc()
        rpc.request.side_effect = WorkflowError("原线程不可恢复")
        rpc.request.reset_mock()
        with patch("workflow.engine.AppServer", return_value=rpc), self.assertRaises(WorkflowError):
            self.engine.ensure_rpc()
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list], ["thread/resume"])
        self.assertIsNone(self.engine.rpc)

    def test_feedback_authorization_stops_when_feedback_changes_or_replanning(self):
        self.send()
        description = next(description for name, description, _ in TOOLS if name == "write_file")
        approval = {"serverName": "workflow", "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call",
            "tool_description": description, "tool_params": {"repository": "前端", "path": "业务.txt", "content": "修复"}}}
        self.assertTrue(write_authorized(self.state))
        self.assertTrue(is_controlled_write(self.state, approval))
        self.state["followup"]["text"] = "未授权的新反馈"
        self.assertFalse(write_authorized(self.state))
        self.assertFalse(is_controlled_write(self.state, approval))
        self.engine._new_plan_round()
        self.assertFalse(write_authorized(self.state))

    def test_legacy_sessions_expose_defaults_without_rewriting_saved_records(self):
        for key in ("delivery_round", "deliveries", "followup_images"):
            self.state.pop(key)
        self.engine.save()
        result = public_state(self.state)
        self.assertEqual(result["delivery_round"], 1)
        self.assertEqual(result["followup_images"], [])
        self.assertNotIn("delivery_round", self.engine.store.load(self.state["id"]))
        self.send()
        self.assertEqual(self.state["delivery_round"], 2)

    def test_new_worktree_is_allocated_when_source_is_busy_on_another_branch(self):
        source = Path(self.state["repos"][0]["source"])
        git(source, "switch", "-c", "其他项目")
        other = self.engine.store.new({"kind": "task", "id": 9})
        other.update(phase="implement", repos=[{**deepcopy(self.state["repos"][0]), "branch": "其他项目"}])
        self.engine.store.save(other)
        self.send()
        current = self.state["repos"][0]
        self.assertTrue(current["workspace_owned"])
        self.assertNotEqual(current["path"], str(source))
        self.assertEqual(git(current["path"], "branch", "--show-current"), "项目分支")
        self.assertEqual(git(source, "branch", "--show-current"), "其他项目")
        self.finish()

    def test_restart_resumes_partial_workspace_preparation_without_duplication(self):
        other = self.engine.store.new({"kind": "task", "id": 9})
        other.update(phase="implement", repos=[deepcopy(self.state["repos"][1])])
        self.engine.store.save(other)
        self.application.dispatch(self.request())
        self.drain()
        self.assertEqual(self.state["stage"], "已停止")
        self.assertEqual(len(self.state["repos"]), 1)
        first = deepcopy(self.state["repos"][0])
        self.application.close()
        other["phase"] = "complete"
        self.engine.store.save(other)
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        engine = restored.sessions.get(self.state["id"]).engine
        with patch.object(engine, "_start_turn") as start:
            engine.resume()
        start.assert_called_once()
        self.assertEqual(engine.state["repos"][0], first)
        self.assertEqual(len(engine.state["repos"]), 2)
        self.assertEqual(len(engine.state["deliveries"]), 1)
        self.assertEqual(len([entry for entry in engine.state["history"] if entry["role"] == "用户"]), 1)

    def test_simultaneous_followups_do_not_claim_the_same_directory(self):
        other = deepcopy(self.state)
        other["id"] = self.engine.store.new({"kind": "story", "id": 9})["id"]
        runtime = self.application.sessions.add(other)
        runtime.engine.save()
        def start(engine, *_):
            engine.state["stage"] = "修复中"
            engine.save()
        requests = [self.request(), self.request(session_id=other["id"], revision=runtime.revision)]
        with patch.object(Workflow, "_start_turn", start), ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(self.application.dispatch, requests))
            self.drain()
            runtime.engine.executor.submit(lambda: None).result(timeout=10)
        self.assertEqual(sorted([self.state["stage"], other["stage"]]), sorted(["修复中", "已停止"]))
        self.assertEqual(len(self.state["repos"]) + len(other["repos"]), 2)

    def test_delete_checks_archived_owned_workspaces_and_other_claims(self):
        repo = self.state["repos"][0]
        source = Path(repo["source"])
        workspace = self.engine.store.directory(self.state["id"]) / "workspace"
        old = prepare(source, workspace / "repo-1", "历史分支", "HEAD", True)
        old.update(name="历史前端", role="frontend", workspace_owned=True)
        current = prepare(source, workspace / "repo-1-delivery-2", "本轮分支", "HEAD", True)
        current.update(name="前端", role="frontend", workspace_owned=True)
        self.state.update(repos=[current], deliveries=[{"number": 1, "repos": [old]}])
        self.engine.save()
        other = self.engine.store.new({"kind": "task", "id": 9})
        other.update(phase="implement", repos=[deepcopy(old)])
        self.engine.store.save(other)
        with self.assertRaises(WorkflowError):
            cleanup_workspaces(self.engine.store, self.state, [])
        self.assertTrue(Path(current["path"]).exists())
        other["phase"] = "complete"
        self.engine.store.save(other)
        cleanup_workspaces(self.engine.store, self.state, [])
        self.assertFalse(Path(old["path"]).exists())
        self.assertFalse(Path(current["path"]).exists())
        self.assertEqual(len(worktrees(source)), 1)
