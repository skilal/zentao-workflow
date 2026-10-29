"""检查新增本地操作的会话、版本和重复请求约束。"""

from concurrent.futures import Future
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.mcp_server import Tools
from workflow.settings import ROOT, WorkflowError
from workflow.web import WebApplication, public_state


class BranchActionChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            patched = patch(target, Path(temporary.name))
            patched.start()
            self.addCleanup(patched.stop)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.application = WebApplication(settings)
        self.addCleanup(self.application.close)
        self.state = self.application.sessions.store.new({"kind": "task", "id": 7, "title": "脱敏工作项"})
        self.runtime = self.application.sessions.add(self.state)
        self.application.emit(self.state["id"], "state", self.state)
        self.runtime.engine.submit = Mock(side_effect=self.submit)

    def submit(self, operation, *args, **kwargs):
        operation(*args)
        future = Future()
        future.set_result(None)
        return future

    def request(self, action, data=None, **kwargs):
        return {"action": action, "data": data or {}, "session_id": self.state["id"],
                "revision": self.runtime.revision, "request_id": str(uuid4()), **kwargs}

    def test_plan_uses_only_supplement_and_server_side_repository_resolution(self):
        operation = self.runtime.engine.start_plan = Mock()
        self.application.dispatch(self.request("plan", {"supplement": "业务规则"}))
        operation.assert_called_once_with("业务规则", False)
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request("plan", {"selections": [], "supplement": ""}))

    def test_manual_edit_and_confirmation_are_explicit_operations(self):
        operation = self.runtime.engine.start_plan = Mock()
        self.application.dispatch(self.request("edit_branches", {"supplement": "业务规则"}))
        operation.assert_called_once_with("业务规则", True)
        choose = self.runtime.engine.choose_branches = Mock()
        choices = {"frontend": "示例分支"}
        self.application.dispatch(self.request("choose_branches", {"choices": choices}))
        choose.assert_called_once_with(choices)

    def test_branch_and_project_decisions_reject_stale_revision_or_wrong_session(self):
        for action in ("plan", "edit_branches", "choose_branches", "confirm_project"):
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request(action, revision=-1))
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request(action, session_id="其他会话"))
        self.runtime.engine.submit.assert_not_called()

    def test_duplicate_branch_request_does_not_repeat_preparation(self):
        operation = self.runtime.engine.choose_branches = Mock()
        request = self.request("choose_branches", {"choices": {"backend": "示例分支"}})
        self.application.dispatch(request)
        result = self.application.dispatch(request)
        self.assertTrue(result["duplicate"])
        operation.assert_called_once()

    def test_project_confirmation_requires_a_positive_id_and_nonempty_name(self):
        for project_id, name in ((True, "示例"), (0, "示例"), (41, " ")):
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request("confirm_project", {"project_id": project_id, "name": name}))
        operation = self.runtime.engine.confirm_project = Mock()
        self.application.dispatch(self.request("confirm_project", {"project_id": 41, "name": " 示例项目 "}))
        operation.assert_called_once_with(41, "示例项目")

    def test_public_state_retains_project_and_branch_choices_without_raw_sources(self):
        self.state.update(project={"id": 41, "name": "示例项目"},
                          branch_resolution={"repositories": [{"role": "frontend", "candidates": ["示例分支"]}]},
                          snapshot={"sources": [{"title": "示例", "raw": {"project": 41}}]})
        state = public_state(self.state)
        self.assertEqual(state["project"]["id"], 41)
        self.assertEqual(state["branch_resolution"]["repositories"][0]["candidates"], ["示例分支"])
        self.assertNotIn("raw", state["snapshot"]["sources"][0])

    def test_collect_passes_all_selected_tasks_using_server_side_metadata(self):
        items = [{"kind": "task", "id": 7, "title": "前端任务"},
                 {"kind": "task", "id": 8, "title": "后端任务"}]
        self.application.items = items
        operation = self.runtime.engine.collect = Mock()
        self.application.dispatch(self.request("collect", {"items": [
            {"kind": "task", "id": 7, "title": "客户端伪造标题"}, {"kind": "task", "id": 8}]}))
        operation.assert_called_once_with(items)

    def test_collect_rejects_empty_duplicate_unknown_and_mixed_selections(self):
        task = {"kind": "task", "id": 7}
        story = {"kind": "story", "id": 8}
        self.application.items = [task, story]
        operation = self.runtime.engine.collect = Mock()
        for choices in ([], [task, task], [task, story], [{"kind": "task", "id": 9}],
                        [{"kind": "task", "id": True}], [{"kind": [], "id": 7}], [None]):
            with self.subTest(choices=choices), self.assertRaises(WorkflowError):
                self.application.dispatch(self.request("collect", {"items": choices}))
        operation.assert_not_called()

    def test_joint_collection_survives_restore_history_and_mcp_context(self):
        items = [{"kind": "task", "id": 7, "title": "前端任务"},
                 {"kind": "task", "id": 8, "title": "后端任务"}]
        self.application.items = items
        snapshot = {"items": items, "project": {"id": 41, "name": "示例项目"}, "sources": [], "materials": []}
        with patch.object(self.runtime.engine.zentao, "collect_many", return_value=snapshot):
            self.application.dispatch(self.request("collect", {"items": items}))
        session_id = self.runtime.engine.state["id"]
        self.runtime.engine.restore(session_id)
        self.assertEqual(self.application.session(session_id)["state"]["items"], items)
        self.assertEqual(self.application.snapshot()["history"][0]["items"], items)
        self.assertEqual(Tools(session_id).call("task_context", {})["items"], items)

    def test_joint_delivery_commit_description_identifies_both_tasks(self):
        self.state.update(stage="等待修复选择", items=[self.state["item"], {"kind": "task", "id": 8, "title": "前端任务"}],
                          repos=[{"name": "前端", "path": "模拟前端工作区"}, {"name": "后端", "path": "模拟后端工作区"}],
                          reviews=[{"result": {"findings": []}}], review_manifest={})
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.commit", return_value="模拟提交") as commit:
            self.runtime.engine.finalize([], "")
        self.assertEqual(commit.call_count, 2)
        for call in commit.call_args_list:
            self.assertIn("zentao-tasks-7,8", call.args[1])
            self.assertIn("脱敏工作项；前端任务", call.args[1])
