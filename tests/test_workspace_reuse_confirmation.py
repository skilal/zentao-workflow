"""隔离验证外部工作区人工确认、过期校验、续聊和并发占用。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.gitops import acquire_workspace, changed_paths, cleanup_workspaces, git, prepare
from workflow.settings import ROOT, WorkflowError
from workflow.sessions import SessionManager
from workflow.web import WebApplication


class WorkspaceReuseConfirmationChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            patched = patch(target, self.root / "local")
            patched.start()
            self.addCleanup(patched.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.settings["repositories"] = []
        self.selections = []
        for name, role in (("前端", "frontend"), ("后端", "backend")):
            source = self.root / role
            source.mkdir()
            git(source, "init", "-b", "原分支")
            git(source, "config", "user.name", "工作区复用检查")
            git(source, "config", "user.email", "reuse@example.invalid")
            (source / "业务.txt").write_text("初始内容\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "初始基线")
            git(source, "branch", "项目分支")
            self.settings["repositories"].append({"name": name, "role": role, "path": str(source), "remote": "origin"})
            self.selections.append({"name": name, "role": role, "path": str(source), "branch": "项目分支",
                                    "base": "refs/heads/项目分支", "create": False})
        self.source = Path(self.selections[0]["path"])
        self.external = self.root / "外部工作区"
        prepare(self.source, self.external, "项目分支", "refs/heads/项目分支", False)
        self.application = WebApplication(self.settings)
        self.addCleanup(self.application.close)
        self.runtime = self.new_runtime(1)
        self.engine = self.runtime.engine
        self.state = self.engine.state

    def new_runtime(self, number):
        state = self.application.sessions.store.new({"kind": "task", "id": number, "title": "外部工作区恢复"})
        state.update(stage="已停止", phase="plan", prepare_complete=False,
                     selections=deepcopy(self.selections), supplement="保留补充说明",
                     codex={"model": "隔离模型", "reasoning_effort": "high"},
                     snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                     history=[{"role": "用户", "text": "原始需求", "phase": "plan"}],
                     error="历史版本的分支占用提示", max_fix_rounds=3)
        runtime = self.application.sessions.add(state)
        runtime.engine._start_turn = Mock()
        runtime.engine.save()
        return runtime

    def preview(self, engine=None):
        engine = engine or self.engine
        engine.preview_workspace_reuse("前端")
        return engine.state["workspace_reuse_request"]["token"]

    def request(self, action, data=None, **change):
        return {"action": action, "data": data or {}, "session_id": self.state["id"],
                "request_id": uuid4().hex, "revision": self.runtime.revision, **change}

    def dispatch(self, action, data=None):
        self.application.dispatch(self.request(action, data))
        self.engine.executor.submit(lambda: None).result(timeout=10)

    def test_external_workspace_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(WorkflowError, "预览复用已有工作区"):
            acquire_workspace(self.state["selections"][0], self.root / "unused", self.engine.store, self.state)
        self.assertFalse(self.state["repos"])
        self.assertNotIn("workspace_reuse_request", self.state)

    def test_old_failed_session_can_preview_cancel_and_confirm_without_code_or_index_changes(self):
        (self.external / "业务.txt").write_text("已暂存内容\n", encoding="utf-8")
        git(self.external, "add", ".")
        (self.external / "业务.txt").write_text("未暂存内容\n", encoding="utf-8")
        (self.external / "本机.txt").write_text("未跟踪资料\n", encoding="utf-8")
        before = {name: (self.external / name).read_bytes() for name in ("业务.txt", "本机.txt")}
        index = git(self.external, "diff", "--cached", "--binary", binary=True)
        token = self.preview()
        preview = self.state["workspace_reuse_request"]
        self.assertEqual(preview["subject"], "初始基线")
        self.assertEqual(preview["snapshot"]["paths"], ["业务.txt", "本机.txt"])
        self.assertFalse(self.state["repos"])
        self.engine.cancel_workspace_reuse()
        self.assertIsNone(self.state["workspace_reuse_request"])
        with self.assertRaises(WorkflowError):
            self.engine.confirm_workspace_reuse(token)
        self.engine.confirm_workspace_reuse(self.preview())
        repo = self.state["repos"][0]
        self.assertEqual(repo["path"], str(self.external))
        self.assertEqual(repo["source"], str(self.source))
        self.assertFalse(repo["workspace_owned"])
        self.assertEqual(repo["baseline"], git(self.external, "rev-parse", "HEAD"))
        self.assertEqual(changed_paths(repo), [])
        self.assertTrue(self.state["prepare_complete"])
        self.assertEqual(len(self.state["repos"]), 2)
        self.assertEqual(len(self.state["workspace_reuses"]), 1)
        self.assertEqual(self.state["history"][0]["text"], "原始需求")
        self.engine._start_turn.assert_called_once()
        self.assertEqual(git(self.external, "diff", "--cached", "--binary", binary=True), index)
        for name, content in before.items():
            self.assertEqual((self.external / name).read_bytes(), content)
        cleanup_workspaces(self.engine.store, self.state, self.settings["repositories"])
        self.engine.store.remove(self.state["id"])
        self.assertTrue(self.external.is_dir())

    def test_preview_survives_restart_and_resume_cannot_bypass_confirmation(self):
        token = self.preview()
        restarted = SessionManager(self.settings, Mock())
        self.addCleanup(restarted.close)
        saved = restarted.get(self.state["id"]).engine.state
        self.assertEqual(saved["workspace_reuse_request"]["token"], token)
        self.assertFalse(saved["repos"])
        self.engine.state = saved
        with self.assertRaisesRegex(WorkflowError, "确认或取消"):
            self.engine.resume()
        with self.assertRaisesRegex(WorkflowError, "确认或取消"):
            saved["stage"] = "准备工作区失败"
            self.engine.prepare_and_plan(saved["selections"], saved["supplement"])
        for manual in (False, True):
            with self.assertRaisesRegex(WorkflowError, "确认或取消"):
                self.engine.start_plan(saved["supplement"], manual=manual)
            self.assertEqual(saved["phase"], "plan")
        saved.update(stage="已停止", phase="followup", followup={"text": "继续原消息"}, thread_id="原线程")
        with self.assertRaisesRegex(WorkflowError, "确认或取消"):
            self.engine._prepare_followup()
        saved.update(phase="plan", followup=None, thread_id=None)
        self.engine.confirm_workspace_reuse(token)
        self.assertTrue(saved["prepare_complete"])

    def test_followup_and_plan_followup_keep_thread_messages_and_round(self):
        for plan_mode in (False, True):
            runtime = self.new_runtime(10 + int(plan_mode))
            engine, state = runtime.engine, runtime.engine.state
            state.update(phase="plan" if plan_mode else "followup", thread_id="原线程", delivery_round=2,
                         followup={"text": "原续聊消息", "plan_mode": plan_mode},
                         plan_round={"number": 2, "turns": 0, "asked": False})
            engine.save()
            token = self.preview(engine)
            engine.confirm_workspace_reuse(token)
            self.assertEqual(state["thread_id"], "原线程")
            self.assertEqual(state["delivery_round"], 2)
            self.assertEqual(state["followup"]["text"], "原续聊消息")
            self.assertEqual(state["plan_round"]["number"], 2)
            self.assertTrue(state["prepare_complete"])
            engine._start_turn.assert_called_once()
            state.update(phase="complete", stage="已完成（未审查）")
            engine.save()

    def test_partial_preparation_is_preserved_and_later_failure_does_not_undo_binding(self):
        selection = self.state["selections"][1]
        acquire_workspace(selection, self.root / "unused", self.engine.store, self.state)
        backend = deepcopy(self.state["repos"][0])
        token = self.preview()
        self.engine.confirm_workspace_reuse(token)
        self.assertEqual(self.state["repos"][0], backend)
        self.assertEqual(len(self.state["repos"]), 2)

    def test_later_repository_failure_keeps_confirmed_binding_and_resume_does_not_reconfirm(self):
        backend = self.state["selections"][1]
        other_path = self.root / "后端外部目录"
        prepare(backend["path"], other_path, "项目分支", "refs/heads/项目分支", False)
        with self.assertRaisesRegex(WorkflowError, "预览复用已有工作区"):
            self.engine.confirm_workspace_reuse(self.preview())
        self.assertEqual(len(self.state["repos"]), 1)
        self.assertIsNone(self.state["workspace_reuse_request"])
        self.assertEqual(len(self.state["workspace_reuses"]), 1)
        self.engine.preview_workspace_reuse("后端")
        self.engine.confirm_workspace_reuse(self.state["workspace_reuse_request"]["token"])
        self.assertTrue(self.state["prepare_complete"])
        self.assertEqual(len(self.state["workspace_reuses"]), 2)

    def test_head_index_worktree_untracked_and_round_changes_invalidate_preview(self):
        token = self.preview()
        changes = [lambda: (self.external / "业务.txt").write_text("变动\n", encoding="utf-8"),
                   lambda: git(self.external, "add", "."),
                   lambda: (self.external / "新增.txt").write_text("新增\n", encoding="utf-8"),
                   lambda: git(self.external, "commit", "-m", "更新已提交内容"),
                   lambda: self.state.update(delivery_round=2)]
        for change in changes:
            change()
            with self.assertRaisesRegex(WorkflowError, "重新预览"):
                self.engine.confirm_workspace_reuse(token)
            self.assertFalse(self.state["repos"])
            token = self.preview()
        self.engine.confirm_workspace_reuse(token)

    def test_wrong_token_and_changed_selection_are_rejected(self):
        self.preview()
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.engine.confirm_workspace_reuse("无效凭证")
        token = self.state["workspace_reuse_request"]["token"]
        self.state["selections"][0]["branch"] = "其他分支"
        with self.assertRaisesRegex(WorkflowError, "仓库选择已经变化"):
            self.engine.confirm_workspace_reuse(token)
        self.assertFalse(self.state["repos"])

    def test_other_unfinished_session_ownership_is_rechecked(self):
        token = self.preview()
        other = self.new_runtime(2)
        other.engine.state["repos"] = [{"path": str(self.external), "name": "前端"}]
        other.engine.save()
        with self.assertRaisesRegex(WorkflowError, other.engine.state["id"]):
            self.engine.confirm_workspace_reuse(token)
        self.assertFalse(self.state["repos"])

    def test_concurrent_confirmations_only_one_session_can_register(self):
        other = self.new_runtime(2)
        tokens = [self.preview(), self.preview(other.engine)]
        engines = [self.engine, other.engine]
        for engine in engines:
            engine.prepare_and_plan = Mock()
        def confirm(index):
            try:
                engines[index].confirm_workspace_reuse(tokens[index])
                return "成功"
            except WorkflowError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(confirm, range(2)))
        self.assertEqual(results.count("成功"), 1)
        self.assertEqual(sum("其他未完成会话" in result for result in results), 1)

    def test_locked_worktree_hook_and_removed_registration_block_confirmation(self):
        token = self.preview()
        git(self.source, "worktree", "lock", str(self.external))
        with self.assertRaisesRegex(WorkflowError, "锁定"):
            self.engine.confirm_workspace_reuse(token)
        git(self.source, "worktree", "unlock", str(self.external))
        hook = self.source / ".git/hooks/post-checkout"
        hook.write_text("拒绝绕过钩子\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "Git 钩子"):
            self.engine.confirm_workspace_reuse(token)
        hook.unlink()
        git(self.external, "switch", "--detach")
        with self.assertRaisesRegex(WorkflowError, "未找到"):
            self.engine.confirm_workspace_reuse(token)
        self.assertFalse(self.state["repos"])

    def test_prunable_worktree_and_session_managed_directory_are_not_adopted(self):
        token = self.preview()
        with patch("workflow.workspace_reuse.worktrees", return_value=[{
                "worktree": str(self.external), "branch": "refs/heads/项目分支", "prunable": "登记失效"}]):
            with self.assertRaisesRegex(WorkflowError, "登记失效"):
                self.engine.confirm_workspace_reuse(token)
        managed = self.engine.store.directory(self.state["id"]) / "workspace/repo-1"
        with patch("workflow.workspace_reuse.worktrees", return_value=[{
                "worktree": str(managed), "branch": "refs/heads/项目分支"}]):
            with self.assertRaisesRegex(WorkflowError, "会话管理工作区"):
                self.engine.confirm_workspace_reuse(token)

    def test_api_validation_revision_and_three_actions(self):
        for action, data in (("preview_workspace_reuse", {}), ("preview_workspace_reuse", {"repository": 1}),
                             ("confirm_workspace_reuse", {}), ("confirm_workspace_reuse", {"token": []})):
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request(action, data))
        with self.assertRaisesRegex(WorkflowError, "过期"):
            self.application.dispatch(self.request("preview_workspace_reuse", {"repository": "前端"}, revision=-1))
        self.dispatch("preview_workspace_reuse", {"repository": "前端"})
        self.assertIsNotNone(self.state["workspace_reuse_request"])
        self.dispatch("cancel_workspace_reuse")
        self.assertIsNone(self.state["workspace_reuse_request"])
        self.dispatch("preview_workspace_reuse", {"repository": "前端"})
        self.dispatch("confirm_workspace_reuse", {"token": self.state["workspace_reuse_request"]["token"]})
        self.assertTrue(self.state["prepare_complete"])

    def test_prepared_completed_pending_and_partial_commit_sessions_cannot_preview(self):
        for change in ({"prepare_complete": True}, {"phase": "complete"}, {"pending": {"id": "待答问题"}},
                       {"commits": {"后端": "部分提交"}}, {"stage": "计划中"}):
            original = deepcopy(self.state)
            self.state.update(change)
            with self.assertRaises(WorkflowError):
                self.preview()
            self.state.clear()
            self.state.update(original)

    def test_missing_or_prepared_repository_cannot_be_selected(self):
        with self.assertRaisesRegex(WorkflowError, "尚未准备成功"):
            self.engine.preview_workspace_reuse("不存在")
        backend = self.state["selections"][1]
        acquire_workspace(backend, self.root / "unused", self.engine.store, self.state)
        with self.assertRaisesRegex(WorkflowError, "尚未准备成功"):
            self.engine.preview_workspace_reuse("后端")

    def test_first_preparation_with_existing_thread_still_prepares_remaining_repositories(self):
        self.state["thread_id"] = "原线程"
        self.engine.confirm_workspace_reuse(self.preview())
        self.assertTrue(self.state["prepare_complete"])
        self.assertEqual(len(self.state["repos"]), 2)
        self.assertEqual(self.state["thread_id"], "原线程")
