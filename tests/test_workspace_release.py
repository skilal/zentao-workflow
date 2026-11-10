"""隔离 Git 仓库检查释放、目录接管、审查范围与入口约束。"""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from test_workspace_handoff import HandoffFixture
from workflow.gitops import acquire_workspace, checkout_snapshot, cleanup_workspaces, commit, differences, ensure_baseline, git, manifest, workspace_claims
from workflow.mcp_server import Tools
from workflow.sessions import SessionManager
from workflow.settings import WorkflowError
from workflow.store import followup_available, write_authorized
from workflow.workspace_release import settle_workspace_releases


class WorkspaceReleaseChecks(HandoffFixture):
    def release(self, name="前端"):
        self.engine.preview_workspace_release(name)
        token = self.state["workspace_release_request"]["token"]
        self.engine.confirm_workspace_release(token)
        return token

    def adoption(self, name="前端", target=None):
        self.engine.preview_workspace_adoption(name, str(target or self.target))
        return self.state["workspace_release_request"]["token"]

    def test_release_keeps_head_files_and_ignored_files_and_allows_idea_checkout(self):
        (self.source / "cache").mkdir()
        ignored = self.source / "cache/临时数据.txt"
        ignored.write_text("保留", encoding="utf-8")
        before = deepcopy(self.state)
        snapshot = checkout_snapshot(self.source)
        head = git(self.source, "rev-parse", "HEAD")
        token = self.release()
        self.assertEqual(git(self.source, "branch", "--show-current"), "")
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), head)
        self.assertEqual(checkout_snapshot(self.source), snapshot)
        self.assertEqual(ignored.read_text(encoding="utf-8"), "保留")
        for key in ("phase", "stage", "commits", "thread_id", "plan", "pending"):
            self.assertEqual(self.state[key], before[key])
        self.assertEqual(self.state["repos"][0]["baseline"], before["repos"][0]["baseline"])
        self.assertEqual(self.state["repos"][1], before["repos"][1])
        self.engine.confirm_workspace_release(token)
        self.assertEqual(len(self.state["workspace_release_history"]), 1)
        git(self.target, "switch", "需求分支")
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), head)

    def test_all_stages_allow_release_including_pending_and_partial_commits(self):
        for phase, stage, pending in (("plan", "等待计划确认", None), ("implement", "已暂停", None),
                                      ("review", "等待修复选择", None), ("fix", "部分提交", None),
                                      ("plan", "等待人工回答", {"id": "问题", "method": "item/tool/requestUserInput"})):
            with self.subTest(stage=stage):
                self.state.update(phase=phase, stage=stage, pending=pending)
                self.release()
                self.assertEqual(self.state["pending"], pending)
                git(self.target, "switch", "需求分支")
                self.engine.confirm_workspace_adoption(self.adoption())
                self.assertEqual(self.state["stage"], stage)
                self.assertEqual(self.state["pending"], pending)
                self.source = self.target

    def test_staged_unstaged_and_untracked_modifications_block_release(self):
        path = self.source / "任务.txt"
        for staged in (False, True):
            path.write_text("未提交", encoding="utf-8")
            if staged:
                git(self.source, "add", "任务.txt")
            with self.assertRaisesRegex(WorkflowError, "未提交.*任务.txt"):
                self.engine.preview_workspace_release("前端")
            git(self.source, "restore", "--staged", "--worktree", "任务.txt")
        (self.source / "新文件.txt").write_text("未跟踪", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "未提交.*新文件.txt"):
            self.engine.preview_workspace_release("前端")
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")

    def test_running_operations_and_hooks_block_release(self):
        self.engine.turn_running = True
        with self.assertRaisesRegex(WorkflowError, "手动暂停"):
            self.engine.preview_workspace_release("前端")
        self.engine.turn_running = False
        self.state["stage"] = "实施中"
        with self.assertRaisesRegex(WorkflowError, "手动暂停"):
            self.engine.preview_workspace_release("前端")
        self.state["stage"] = "已暂停"
        hook = self.target / ".git/hooks/post-checkout"
        hook.write_text("阻止自动绕过", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "钩子"):
            self.engine.preview_workspace_release("前端")

    def test_preview_cancel_does_not_detach_and_stale_preview_does_not_mutate(self):
        with self.assertRaisesRegex(WorkflowError, "先预览"):
            self.engine.confirm_workspace_release(None)
        self.engine.preview_workspace_release("前端")
        token = self.state["workspace_release_request"]["token"]
        self.engine.cancel_workspace_release(token)
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")
        self.engine.preview_workspace_release("前端")
        token = self.state["workspace_release_request"]["token"]
        self.state["commits"]["前端"] = "已变化"
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.engine.confirm_workspace_release(token)
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")

    def test_interrupted_detach_requires_explicit_reconciliation_without_retry(self):
        original_save = self.engine.note
        with patch.object(self.engine, "note", side_effect=WorkflowError("模拟持久化中断")):
            with self.assertRaises(WorkflowError):
                self.release()
        repo = self.state["repos"][0]
        self.assertEqual(git(self.source, "branch", "--show-current"), "")
        self.assertEqual(repo["workspace_release"]["status"], "interrupted")
        # 模拟应用重启前只保存了进行中的状态；重启只标记中断，不执行 Git 切换。
        repo["workspace_release"]["status"] = "detaching"
        self.assertTrue(settle_workspace_releases(self.state))
        self.assertEqual(repo["workspace_release"]["status"], "interrupted")
        self.state["workspace_release_history"] = []
        self.engine.note = original_save
        with patch("workflow.workspace_release.git", wraps=git) as commands:
            self.release()
        self.assertFalse(any(call.args[1] == "switch" for call in commands.call_args_list))
        self.assertEqual(repo["workspace_release"]["status"], "released")

    def test_complete_released_workspace_stays_reserved_until_adopted(self):
        self.release()
        claims = workspace_claims(self.store.sessions(), "其他会话")
        self.assertEqual(claims[str(self.source.resolve()).casefold()], self.state["id"])
        other = self.store.new(None)
        self.store.save(other)
        selection = {**self.settings["repositories"][0], "branch": "另一分支", "base": "refs/heads/原分支", "create": True}
        record = acquire_workspace(selection, self.store.directory(other["id"]) / "workspace/repo-1", self.store, other)
        self.assertNotEqual(Path(record["path"]), self.source)

    def test_adoption_preserves_baseline_plan_pending_commits_and_local_changes(self):
        self.state.update(phase="plan", stage="等待人工回答", pending={"id": "问题", "method": "item/tool/requestUserInput"}, plan="原计划")
        before = deepcopy(self.state)
        self.release()
        git(self.target, "switch", "需求分支")
        (self.target / "本机配置.txt").write_text("本机配置", encoding="utf-8")
        git(self.target, "add", "本机配置.txt")
        (self.target / "新本机.txt").write_text("保留", encoding="utf-8")
        snapshot = checkout_snapshot(self.target)
        token = self.adoption()
        with patch.object(self.engine, "_start_turn") as start:
            self.engine.confirm_workspace_adoption(token)
        start.assert_not_called()
        repo = self.state["repos"][0]
        self.assertEqual(repo["baseline"], before["repos"][0]["baseline"])
        for key in ("plan", "pending", "commits", "thread_id", "phase", "stage"):
            self.assertEqual(self.state[key], before[key])
        self.assertEqual(checkout_snapshot(self.target), snapshot)
        self.assertEqual(repo["preserved_paths"], ["新本机.txt", "本机配置.txt"])
        self.assertFalse(repo["workspace_owned"])
        self.assertNotIn("workspace_release", repo)
        self.assertEqual(Path(repo["path"]), self.target)
        self.assertEqual(Path(self.state["retired_repos"][0]["path"]), self.source)
        self.assertTrue(self.state["retired_repos"][0]["workspace_owned"])
        self.assertTrue(Path(self.state["workspace_release_history"][-1]["backup"]).is_file())
        self.engine.confirm_workspace_adoption(token)

    def test_adoption_requires_original_branch_registered_same_git_and_fresh_snapshot(self):
        self.release()
        with self.assertRaisesRegex(WorkflowError, "切换到目标分支"):
            self.adoption()
        with self.assertRaisesRegex(WorkflowError, "未登记"):
            self.adoption(target=self.settings["repositories"][1]["path"])
        git(self.target, "switch", "需求分支")
        token = self.adoption()
        (self.target / "本机配置.txt").write_text("预览后变化", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.engine.confirm_workspace_adoption(token)
        self.assertIn("workspace_release", self.state["repos"][0])

    def test_changed_head_needs_separate_baseline_confirmation(self):
        self.state.update(phase="review", stage="等待修复选择", commits={}, plan="原计划",
                          reviews=[{"number": 1, "history_index": 0, "result": {"findings": []}}])
        baseline = self.state["repos"][0]["baseline"]
        self.release()
        git(self.target, "switch", "需求分支")
        git(self.target, "commit", "--allow-empty", "-m", "IDEA 人工更新")
        token = self.adoption()
        with self.assertRaisesRegex(WorkflowError, "单独预览"):
            self.engine.confirm_workspace_adoption(token)
        self.assertEqual(self.state["repos"][0]["baseline"], baseline)
        self.engine.confirm_workspace_adoption(token, change_baseline=True)
        self.assertEqual(self.state["repos"][0]["baseline"], git(self.target, "rev-parse", "HEAD"))
        self.assertEqual(self.state["review_start"], 1)
        self.assertEqual(self.state["stage"], "已暂停")
        self.assertTrue(self.state["baseline_review_pending"])
        self.assertEqual(self.state["plan"], "原计划")

    def test_adopted_committed_task_remains_reviewable_without_duplicate_commit(self):
        self.state.update(phase="review", stage="等待修复选择", commits={})
        self.release()
        git(self.target, "switch", "需求分支")
        self.engine.confirm_workspace_adoption(self.adoption())
        repo = self.state["repos"][0]
        ensure_baseline(repo)
        self.assertIn("已提交的需求代码", differences(repo)["patch"])
        expected = manifest([repo])
        self.assertIn("前端/任务.txt", expected)
        head = git(self.target, "rev-parse", "HEAD")
        self.assertEqual(commit(repo, "fix(测试): 保留原提交", expected), head)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), head)
        (self.target / "索引.txt").write_text("新的任务修改", encoding="utf-8")
        result = commit(repo, "fix(测试): 继续修改", manifest([repo]))
        self.assertNotEqual(result, head)
        ensure_baseline(repo)
        self.assertEqual(repo["handoff_head"], result)
        self.assertEqual(git(self.target, "show", "--format=", "--name-only", "HEAD"), "索引.txt")

    def test_changed_head_does_not_clear_frozen_partial_commits(self):
        self.state.update(phase="fix", stage="部分提交", commit_batch={"messages": "冻结标题"})
        commits, batch = deepcopy(self.state["commits"]), deepcopy(self.state["commit_batch"])
        self.release()
        git(self.target, "switch", "需求分支")
        git(self.target, "commit", "--allow-empty", "-m", "人工更新")
        token = self.adoption()
        with self.assertRaisesRegex(WorkflowError, "部分提交"):
            self.engine.confirm_workspace_adoption(token, change_baseline=True)
        self.assertEqual(self.state["commits"], commits)
        self.assertEqual(self.state["commit_batch"], batch)

    def test_all_repositories_must_be_adopted_before_continuing(self):
        self.release()
        self.release("后端")
        git(self.target, "switch", "需求分支")
        self.engine.confirm_workspace_adoption(self.adoption())
        self.assertFalse(followup_available(self.state))
        backend = Path(self.settings["repositories"][1]["path"])
        git(backend, "switch", "需求分支")
        self.engine.confirm_workspace_adoption(self.adoption("后端", backend))
        self.assertTrue(followup_available(self.state))

    def test_service_and_model_write_entrypoints_block_released_session(self):
        self.release()
        for action in ("approve_plan", "answer", "resume", "fix", "rereview", "compile_now", "compile_decision", "finalize", "followup", "preview_baseline"):
            with self.subTest(action=action), self.assertRaisesRegex(WorkflowError, "已释放"):
                self.application.dispatch(self.request(action))
        with self.assertRaisesRegex(WorkflowError, "已释放"):
            self.engine.followup("继续修改", [], "请求", False)
        with self.assertRaisesRegex(WorkflowError, "已释放"):
            self.engine._start_turn("继续修改")
        with self.assertRaisesRegex(WorkflowError, "已释放"):
            self.engine.finalize([], "")
        self.assertFalse(write_authorized(self.state))
        for tool in ("write_file", "replace_text", "delete_file", "request_review", "ask_user"):
            with self.subTest(tool=tool), self.assertRaisesRegex(WorkflowError, "已释放"):
                Tools(self.state["id"]).call(tool, {})
        self.assertTrue(self.application.session(self.state["id"])["state"]["workspaces_released"])
        self.assertIsInstance(self.application.diff(self.state["id"]), list)
        self.state["commits"].pop("前端")
        self.assertIn("已提交的需求代码", self.application.diff(self.state["id"])[0]["patch"])

    def test_revision_guard_and_busy_guard_apply_to_release_actions(self):
        with self.assertRaisesRegex(WorkflowError, "版本已过期"):
            self.application.dispatch(self.request("preview_workspace_release", {"repository": "前端"}, revision=-1))
        self.runtime.busy = "实施中"
        with self.assertRaisesRegex(WorkflowError, "上一个操作"):
            self.application.dispatch(self.request("preview_workspace_release", {"repository": "前端"}))
        self.runtime.busy = None

    def test_delete_only_removes_owned_workspace_after_adoption(self):
        self.release()
        git(self.target, "switch", "需求分支")
        self.engine.confirm_workspace_adoption(self.adoption())
        cleanup_workspaces(self.store, self.state, self.settings["repositories"])
        self.assertFalse(self.source.exists())
        self.assertTrue(self.target.exists())
        self.assertEqual(git(self.target, "branch", "--show-current"), "需求分支")

    def test_restart_keeps_released_status_without_reacquiring_branch(self):
        self.release()
        manager = SessionManager(self.settings, lambda *args: None)
        self.addCleanup(manager.close)
        restored = manager.get(self.state["id"]).engine.state
        self.assertEqual(restored["repos"][0]["workspace_release"]["status"], "released")
        self.assertEqual(git(self.source, "branch", "--show-current"), "")
