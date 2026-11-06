"""用临时 Git 仓库验证交接、保留文件、恢复与会话生命周期。"""

from copy import deepcopy
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.gitops import checkout_snapshot, cleanup_workspaces, git, workspace_claims
from workflow.settings import ROOT, WorkflowError
from workflow.store import followup_available
from workflow.web import WebApplication


class HandoffFixture(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            mocked = patch(target, self.root / "local")
            mocked.start()
            self.addCleanup(mocked.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.settings["repositories"] = []
        for name, role in (("前端", "frontend"), ("后端", "backend")):
            path = self.root / role
            path.mkdir()
            git(path, "init", "-b", "原分支")
            git(path, "config", "user.name", "交接检查")
            git(path, "config", "user.email", "handoff@example.invalid")
            git(path, "config", "core.autocrlf", "false")
            for filename in ("任务.txt", "本机配置.txt", "索引.txt", "待删.txt"):
                (path / filename).write_text("初始内容\n", encoding="utf-8")
            (path / ".gitignore").write_text(".idea/\ncache/\n", encoding="utf-8")
            git(path, "add", ".")
            git(path, "commit", "-m", "初始基线")
            git(path, "branch", "需求分支")
            self.settings["repositories"].append({"name": name, "role": role, "path": str(path), "remote": "origin"})
        self.application = WebApplication(self.settings)
        self.addCleanup(self.application.close)
        self.store = self.application.sessions.store
        self.state = self.store.new({"kind": "task", "id": 7, "title": "已完成需求"})
        self.state.update(phase="complete", stage="已完成（静态审查）", prepare_complete=True, thread_id="原线程",
                          codex={"model": "模拟模型", "reasoning_effort": "high"}, max_fix_rounds=3,
                          snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
        for index, repository in enumerate(self.settings["repositories"]):
            source = repository["path"]
            path = self.store.directory(self.state["id"]) / "workspace" / f"repo-{index + 1}"
            baseline = git(source, "rev-parse", "HEAD")
            path.parent.mkdir(parents=True, exist_ok=True)
            git(source, "worktree", "add", str(path), "需求分支")
            (path / "任务.txt").write_text("已提交的需求代码\n", encoding="utf-8")
            git(path, "add", "任务.txt")
            git(path, "commit", "-m", "完成需求")
            self.state["repos"].append({**repository, "source": source, "path": str(path), "branch": "需求分支",
                                        "base": "refs/heads/需求分支", "baseline": baseline, "workspace_owned": True,
                                        "workspace_mode": "created", "create": False, "preserved_paths": []})
            self.state["commits"][repository["name"]] = git(path, "rev-parse", "HEAD")
        self.state["selections"] = [{**repo, "path": repo["source"]} for repo in self.state["repos"]]
        self.runtime = self.application.sessions.add(self.state)
        self.engine = self.runtime.engine
        self.engine.save()
        self.source = Path(self.state["repos"][0]["path"])
        self.target = Path(self.settings["repositories"][0]["path"])

    def preview(self, name="前端", path=None):
        self.engine.preview_workspace_handoff(name, str(path or self.target))
        return self.state["workspace_handoff_request"]["token"]

    def confirm(self, token=None, paths=None):
        self.engine.confirm_workspace_handoff(token or self.state["workspace_handoff_request"]["token"], paths)

    def fail_switch(self, path, *args, **kwargs):
        if Path(path) == self.target and args[0] == "switch":
            raise WorkflowError("模拟目标目录切换失败")
        return git(path, *args, **kwargs)

    def request(self, action, data=None, **change):
        return {"action": action, "data": data or {}, "session_id": self.state["id"],
                "revision": self.runtime.revision, "request_id": uuid4().hex, **change}


class WorkspaceHandoffChecks(HandoffFixture):
    def test_handoff_preserves_all_local_states_and_only_backs_up_selected_files(self):
        config = self.target / "本机配置.txt"
        config.write_text("暂存配置\n", encoding="utf-8")
        git(self.target, "add", "本机配置.txt")
        config.write_text("未暂存配置\n", encoding="utf-8")
        (self.target / "索引.txt").write_text("只在暂存区\n", encoding="utf-8")
        git(self.target, "add", "索引.txt")
        (self.target / "索引.txt").write_text("初始内容\n", encoding="utf-8")
        (self.target / "待删.txt").unlink()
        (self.target / ".run").mkdir()
        (self.target / ".run/启动.xml").write_bytes(b"local run configuration")
        (self.target / "未勾选.txt").write_text("原地保留", encoding="utf-8")
        (self.target / ".idea").mkdir()
        (self.target / ".idea/workspace.xml").write_bytes(b"ignored IDE settings")
        before = checkout_snapshot(self.target)
        repo_before, other_before = deepcopy(self.state["repos"][0]), deepcopy(self.state["repos"][1])
        commits = deepcopy(self.state["commits"])
        token = self.preview()
        self.confirm(token, ["本机配置.txt", ".run/启动.xml", "待删.txt", "索引.txt"])
        repo = self.state["repos"][0]
        self.assertEqual(git(self.source, "branch", "--show-current"), "")
        self.assertEqual(git(self.target, "branch", "--show-current"), "需求分支")
        self.assertEqual((self.target / "任务.txt").read_text(encoding="utf-8"), "已提交的需求代码\n")
        self.assertEqual(checkout_snapshot(self.target), before)
        self.assertEqual((self.target / ".idea/workspace.xml").read_bytes(), b"ignored IDE settings")
        self.assertEqual(repo["preserved_paths"], before["paths"])
        self.assertEqual(repo["baseline"], repo_before["baseline"])
        self.assertEqual(self.state["commits"], commits)
        self.assertEqual(self.state["repos"][1], other_before)
        self.assertEqual(self.state["retired_repos"], [repo_before])
        self.assertFalse(repo["workspace_owned"])
        self.assertEqual((repo["path"], repo["source"]), (str(self.target), str(self.target)))
        manifest = loads((Path(repo["checkout_backup"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({entry["path"] for entry in manifest["files"]}, {"本机配置.txt", ".run/启动.xml", "待删.txt", "索引.txt"})
        self.assertTrue(next(entry for entry in manifest["files"] if entry["path"] == "待删.txt")["deleted"])
        self.assertIn("只在暂存区", (Path(repo["checkout_backup"]) / "index.patch").read_text(encoding="utf-8"))
        self.assertTrue(followup_available(self.state))
        self.assertEqual(self.state["thread_id"], "原线程")

    def test_source_preserved_changes_stay_in_old_directory(self):
        (self.source / "源本机.txt").write_text("源目录保留", encoding="utf-8")
        self.state["repos"][0]["preserved_paths"] = ["源本机.txt"]
        self.engine.save()
        before = checkout_snapshot(self.source)
        self.preview()
        self.confirm(paths=[])
        self.assertEqual(checkout_snapshot(self.source), before)
        self.assertFalse((self.target / "源本机.txt").exists())

    def test_uncommitted_task_and_staged_only_task_changes_are_rejected(self):
        (self.source / "任务.txt").write_text("尚未提交", encoding="utf-8")
        git(self.source, "add", "任务.txt")
        (self.source / "任务.txt").write_text("已提交的需求代码\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "未提交"):
            self.preview()
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")

    def test_target_task_overlap_and_branch_switch_overlap_are_rejected_even_if_unselected(self):
        (self.target / "任务.txt").write_text("本机也修改需求文件", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "同一文件"):
            self.preview()
        (self.target / "任务.txt").write_text("初始内容\n", encoding="utf-8")
        (self.target / "本机配置.txt").write_text("目标原分支提交", encoding="utf-8")
        git(self.target, "add", "本机配置.txt")
        git(self.target, "commit", "-m", "原分支独有配置")
        (self.target / "本机配置.txt").write_text("本机未提交配置", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "同一文件"):
            self.preview()

    def test_ignored_files_that_would_be_overwritten_are_rejected(self):
        (self.source / ".idea").mkdir()
        (self.source / ".idea/workspace.xml").write_text("需求分支跟踪的文件", encoding="utf-8")
        git(self.source, "add", "-f", ".idea/workspace.xml")
        git(self.source, "commit", "-m", "包含 IDE 文件")
        (self.target / ".idea").mkdir()
        (self.target / ".idea/workspace.xml").write_text("本机被忽略的文件", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "忽略"):
            self.preview()
        self.assertEqual((self.target / ".idea/workspace.xml").read_text(encoding="utf-8"), "本机被忽略的文件")

    def test_ignored_file_created_after_preview_invalidates_confirmation(self):
        (self.source / ".idea").mkdir()
        (self.source / ".idea/workspace.xml").write_text("需求文件", encoding="utf-8")
        git(self.source, "add", "-f", ".idea/workspace.xml")
        git(self.source, "commit", "-m", "添加需检查的忽略路径")
        token = self.preview()
        (self.target / ".idea").mkdir()
        (self.target / ".idea/workspace.xml").write_text("预览后新增的本机配置", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "忽略"):
            self.confirm(token, [])
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")

    def test_preview_token_rechecks_files_index_and_head(self):
        token = self.preview()
        (self.target / "本机配置.txt").write_text("预览后修改", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.confirm(token, [])
        token = self.preview()
        git(self.target, "add", "本机配置.txt")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.confirm(token, [])
        token = self.preview()
        git(self.source, "commit", "--allow-empty", "-m", "预览后 HEAD 更新")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.confirm(token, [])
        self.assertEqual(git(self.target, "branch", "--show-current"), "原分支")

    def test_running_pending_and_incomplete_sessions_are_rejected(self):
        for change in ({"phase": "implement", "stage": "已暂停"}, {"phase": "complete", "stage": "已完成（未审查）", "pending": {"id": "问题"}}):
            self.state.update(change)
            with self.assertRaises(WorkflowError):
                self.preview()
        self.state.update(pending=None)
        self.engine.turn_running = True
        with self.assertRaises(WorkflowError):
            self.preview()

    def test_hooks_locks_foreign_repositories_and_managed_targets_are_rejected(self):
        hook = self.target / ".git/hooks/post-checkout"
        hook.write_text("exit 1\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "钩子"):
            self.preview()
        hook.unlink()
        git(self.target, "worktree", "lock", str(self.source))
        with self.assertRaisesRegex(WorkflowError, "锁定"):
            self.preview()
        git(self.target, "worktree", "unlock", str(self.source))
        with self.assertRaises(WorkflowError):
            self.preview(path=Path(self.settings["repositories"][1]["path"]))
        with self.assertRaisesRegex(WorkflowError, "会话管理目录"):
            self.preview(path=self.source)

    def test_other_active_session_occupancy_invalidates_preview(self):
        token = self.preview()
        other = self.store.new({"kind": "bug", "id": 8, "title": "占用目标目录"})
        other.update(phase="plan", stage="已暂停", repos=[{**self.state["repos"][0], "path": str(self.target)}])
        self.application.sessions.add(other).engine.save()
        with self.assertRaisesRegex(WorkflowError, "其他未完成"):
            self.confirm(token, [])
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")

    def test_failed_switch_reserves_paths_and_explicit_resume_is_idempotent(self):
        token = self.preview()
        with patch("workflow.workspace_handoff.git", side_effect=self.fail_switch), self.assertRaisesRegex(WorkflowError, "模拟"):
            self.confirm(token, [])
        request = self.state["workspace_handoff_request"]
        self.assertEqual((request["status"], request["step"]), ("failed", "switch"))
        self.assertEqual(request["actual"]["source"]["branch"], "HEAD")
        self.assertEqual(request["actual"]["target"]["branch"], "原分支")
        self.assertEqual(request["completed_steps"], ["本机文件备份已保存", "源目录已释放目标分支"])
        self.assertEqual(git(self.source, "branch", "--show-current"), "")
        self.assertEqual(git(self.target, "branch", "--show-current"), "原分支")
        self.assertFalse(followup_available(self.state))
        claims = workspace_claims(self.store.sessions(), "其他会话")
        self.assertEqual(claims[str(self.source).casefold()], self.state["id"])
        self.assertEqual(claims[str(self.target).casefold()], self.state["id"])
        self.confirm(token)
        with patch("workflow.workspace_handoff.git") as command:
            self.confirm(token)
        command.assert_not_called()
        self.assertEqual(len(self.state["workspace_handoffs"]), 1)

    def test_restart_recognizes_command_finished_before_progress_was_saved(self):
        token = self.preview()
        def interrupt(path, *args, **kwargs):
            value = git(path, *args, **kwargs)
            if Path(path) == self.source and args[:2] == ("switch", "--detach"):
                raise KeyboardInterrupt("模拟进程中断")
            return value
        with patch("workflow.workspace_handoff.git", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.confirm(token, [])
        self.application.close()
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        engine = restored.sessions.get(self.state["id"]).engine
        self.assertEqual(engine.state["workspace_handoff_request"]["status"], "interrupted")
        self.assertEqual(git(self.target, "branch", "--show-current"), "原分支")
        engine.confirm_workspace_handoff(token)
        self.assertEqual(engine.state["repos"][0]["path"], str(self.target))

    def test_restore_original_branches_before_cancelling_started_handoff(self):
        token = self.preview()
        with patch("workflow.workspace_handoff.git", side_effect=self.fail_switch), self.assertRaises(WorkflowError):
            self.confirm(token, [])
        backup = Path(self.state["workspace_handoff_request"]["backup"])
        with self.assertRaisesRegex(WorkflowError, "恢复原分支"):
            self.engine.cancel_workspace_handoff(token)
        git(self.source, "switch", "需求分支")
        self.engine.cancel_workspace_handoff(token)
        self.assertIsNone(self.state["workspace_handoff_request"])
        self.assertTrue((backup / "manifest.json").exists())
        self.assertEqual(self.state["workspace_handoffs"][-1]["status"], "cancelled")

    def test_metadata_save_failure_leaves_recoverable_request_and_old_binding(self):
        token = self.preview()
        original = self.store.save
        def fail_once(state):
            if state["repos"][0].get("workspace_mode") == "handed_off":
                raise OSError("模拟目录绑定保存失败")
            return original(state)
        with patch.object(self.store, "save", side_effect=fail_once), self.assertRaises(OSError):
            self.confirm(token, [])
        self.assertEqual(self.state["repos"][0]["path"], str(self.source))
        self.assertEqual(self.state["workspace_handoff_request"]["status"], "failed")
        self.confirm(token)
        self.assertEqual(self.state["repos"][0]["path"], str(self.target))

    def test_backup_failure_changes_no_git_state_and_can_be_cancelled(self):
        token = self.preview()
        with patch.object(self.engine, "_backup_handoff", side_effect=OSError("模拟备份失败")), self.assertRaises(OSError):
            self.confirm(token, [])
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")
        self.assertEqual(git(self.target, "branch", "--show-current"), "原分支")
        (self.target / "本机配置.txt").write_text("失败后人工继续修改的配置", encoding="utf-8")
        self.engine.cancel_workspace_handoff(token)
        self.assertEqual((self.target / "本机配置.txt").read_text(encoding="utf-8"), "失败后人工继续修改的配置")

    def test_file_changes_during_backup_stop_before_detaching_and_allow_safe_cancel(self):
        token = self.preview()
        original = self.engine._backup_handoff
        def changed(request):
            original(request)
            (self.target / "本机配置.txt").write_text("备份期间发生变化", encoding="utf-8")
        with patch.object(self.engine, "_backup_handoff", side_effect=changed), self.assertRaisesRegex(WorkflowError, "文件或暂存状态"):
            self.confirm(token, [])
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")
        self.engine.cancel_workspace_handoff(token)
        self.assertEqual((self.target / "本机配置.txt").read_text(encoding="utf-8"), "备份期间发生变化")

    def test_target_branch_changes_after_detaching_stop_before_target_checkout(self):
        token = self.preview()
        head = self.state["workspace_handoff_request"]["source_head"]
        tree = git(self.source, "rev-parse", "HEAD^{tree}")
        next_head = git(self.source, "commit-tree", tree, "-p", head, input_data=b"external commit\n")
        def advance(path, *args, **kwargs):
            result = git(path, *args, **kwargs)
            if Path(path) == self.source and args[:2] == ("switch", "--detach"):
                git(self.source, "update-ref", "refs/heads/需求分支", next_head, head)
            return result
        with patch("workflow.workspace_handoff.git", side_effect=advance), self.assertRaisesRegex(WorkflowError, "目标分支提交已变化"):
            self.confirm(token, [])
        self.assertEqual(git(self.target, "branch", "--show-current"), "原分支")
        git(self.source, "update-ref", "refs/heads/需求分支", head, next_head)
        self.confirm(token)

    def test_restart_after_target_checkout_finishes_without_repeating_git_switch(self):
        token = self.preview()
        def interrupt(path, *args, **kwargs):
            result = git(path, *args, **kwargs)
            if Path(path) == self.target and args[:2] == ("switch", "需求分支"):
                raise KeyboardInterrupt("模拟切换目标后中断")
            return result
        with patch("workflow.workspace_handoff.git", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.confirm(token, [])
        original = git
        commands = []
        def record(path, *args, **kwargs):
            commands.append(args)
            return original(path, *args, **kwargs)
        with patch("workflow.workspace_handoff.git", side_effect=record):
            self.confirm(token)
        self.assertFalse(any(args[0] == "switch" for args in commands))
        self.assertEqual(self.state["repos"][0]["path"], str(self.target))

    def test_pending_handoff_blocks_other_operations_at_http_and_controller_entries(self):
        self.preview()
        for action in ("followup", "compile_now", "preview_baseline", "delete_session", "resume"):
            with self.subTest(action=action), self.assertRaisesRegex(WorkflowError, "交接"):
                self.application.dispatch(self.request(action))
        for operation in (lambda: self.engine.compile_now("请求"), lambda: self.engine.change_baseline(),
                          self.engine.resume, lambda: self.engine.followup("续聊", [], "请求", False),
                          lambda: cleanup_workspaces(self.store, self.state, self.settings["repositories"])):
            with self.assertRaisesRegex(WorkflowError, "交接"):
                operation()

    def test_preview_cancel_noop_and_file_selection_validation(self):
        token = self.preview()
        with self.assertRaisesRegex(WorkflowError, "选择要备份"):
            self.confirm(token, ["不属于预览的文件.txt"])
        self.engine.cancel_workspace_handoff(token)
        self.assertIsNone(self.state["workspace_handoff_request"])
        self.assertEqual(git(self.source, "branch", "--show-current"), "需求分支")
        self.confirm(self.preview(), [])
        token = self.preview()
        self.assertTrue(self.state["workspace_handoff_request"]["already_here"])
        with self.assertRaisesRegex(WorkflowError, "无需交接"):
            self.confirm(token, [])
        self.engine.cancel_workspace_handoff(token)

    def test_manual_commits_and_independent_compile_use_confirmed_handoff_head(self):
        backend = self.state["repos"][1]
        git(backend["path"], "commit", "--allow-empty", "-m", "人工追加提交")
        head = git(backend["path"], "rev-parse", "HEAD")
        self.confirm(self.preview("后端", Path(backend["source"])), [])
        with patch.object(self.engine, "_begin_compile") as begin:
            self.engine.compile_now("仅核对入口，不实际编译")
        begin.assert_called_once()
        self.assertEqual(self.state["manual_compile"]["heads"]["后端"], head)

    def test_followup_reuses_new_directory_and_preserved_paths(self):
        (self.target / "本机配置.txt").write_text("本机配置保留", encoding="utf-8")
        self.confirm(self.preview(), ["本机配置.txt"])
        self.engine._start_turn = Mock()
        self.engine.followup("继续原需求", [], "后续请求", False)
        self.assertEqual(self.state["repos"][0]["path"], str(self.target))
        self.assertIn("本机配置.txt", self.state["repos"][0]["preserved_paths"])
        self.assertEqual(self.state["thread_id"], "原线程")
        self.engine._start_turn.assert_called_once()

    def test_session_cleanup_removes_old_owned_workspaces_but_keeps_idea_directory(self):
        (self.target / "本机配置.txt").write_text("本机配置保留", encoding="utf-8")
        self.confirm(self.preview(), ["本机配置.txt"])
        cleanup_workspaces(self.store, self.state, self.settings["repositories"])
        self.assertFalse(self.source.exists())
        self.assertTrue((self.target / "任务.txt").exists())
        self.assertEqual((self.target / "本机配置.txt").read_text(encoding="utf-8"), "本机配置保留")

    def test_dispatch_revision_and_request_deduplication(self):
        data = {"repository": "前端", "path": str(self.target)}
        with self.assertRaisesRegex(WorkflowError, "版本"):
            self.application.dispatch(self.request("preview_workspace_handoff", data, revision=-1))
        request = self.request("preview_workspace_handoff", data)
        self.application.dispatch(request)
        self.engine.executor.submit(lambda: None).result(timeout=20)
        self.assertTrue(self.application.dispatch(request)["duplicate"])
        token = self.state["workspace_handoff_request"]["token"]
        request = self.request("confirm_workspace_handoff", {"token": token, "paths": []})
        self.application.dispatch(request)
        self.engine.executor.submit(lambda: None).result(timeout=20)
        self.assertTrue(self.application.dispatch(request)["duplicate"])
        self.assertEqual(len(self.state["workspace_handoffs"]), 1)
