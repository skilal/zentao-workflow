"""检查新增本地操作的会话、版本和重复请求约束。"""

from concurrent.futures import Future
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

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
        self.state = self.application.engine.store.new({"kind": "task", "id": 7, "title": "脱敏工作项"})
        self.application.engine.state = self.state
        self.application.emit("state", self.state)
        self.application.engine.submit = Mock(side_effect=self.submit)

    def submit(self, operation, *args, **kwargs):
        operation(*args)
        future = Future()
        future.set_result(None)
        return future

    def request(self, action, data=None, **kwargs):
        return {"action": action, "data": data or {}, "session_id": self.state["id"],
                "revision": self.application.revision, "request_id": str(uuid4()), **kwargs}

    def test_plan_uses_only_supplement_and_server_side_repository_resolution(self):
        operation = self.application.engine.start_plan = Mock()
        self.application.dispatch(self.request("plan", {"supplement": "业务规则"}))
        operation.assert_called_once_with("业务规则", False)
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request("plan", {"selections": [], "supplement": ""}))

    def test_manual_edit_and_confirmation_are_explicit_operations(self):
        operation = self.application.engine.start_plan = Mock()
        self.application.dispatch(self.request("edit_branches", {"supplement": "业务规则"}))
        operation.assert_called_once_with("业务规则", True)
        choose = self.application.engine.choose_branches = Mock()
        choices = {"frontend": "示例分支"}
        self.application.dispatch(self.request("choose_branches", {"choices": choices}))
        choose.assert_called_once_with(choices)

    def test_branch_and_project_decisions_reject_stale_revision_or_wrong_session(self):
        for action in ("plan", "edit_branches", "choose_branches", "confirm_project"):
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request(action, revision=-1))
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request(action, session_id="其他会话"))
        self.application.engine.submit.assert_not_called()

    def test_duplicate_branch_request_does_not_repeat_preparation(self):
        operation = self.application.engine.choose_branches = Mock()
        request = self.request("choose_branches", {"choices": {"backend": "示例分支"}})
        self.application.dispatch(request)
        result = self.application.dispatch(request)
        self.assertTrue(result["duplicate"])
        operation.assert_called_once()

    def test_project_confirmation_requires_a_positive_id_and_nonempty_name(self):
        for project_id, name in ((True, "示例"), (0, "示例"), (41, " ")):
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request("confirm_project", {"project_id": project_id, "name": name}))
        operation = self.application.engine.confirm_project = Mock()
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
