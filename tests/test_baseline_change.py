"""隔离仓库检查人工变更基线、会话保留及过期确认。"""

from copy import deepcopy
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.engine import Workflow
from workflow.gitops import cleanup_workspaces, git, workspace_claims
from workflow.store import session_repositories
from workflow.settings import ROOT, WorkflowError


class BaselineChangeChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        local = patch("workflow.store.LOCAL", self.root / "local")
        local.start()
        self.addCleanup(local.stop)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.engine = Workflow(settings, Mock())
        self.addCleanup(self.engine.close)
        self.state = self.engine.store.new({"kind": "bug", "id": 7, "title": "基线恢复"})
        self.engine.state = self.state
        self.state.update(stage="已停止", phase="followup", prepare_complete=True, thread_id="原线程",
                          plan="原计划", approved_plan="已确认计划", approved_hash="原授权",
                          followup={"text": "原消息"}, error="工作区 HEAD 已改变", max_fix_rounds=3)
        for role in ("frontend", "backend"):
            path = self.root / role
            path.mkdir()
            git(path, "init", "-b", "项目分支")
            git(path, "config", "user.name", "基线检查")
            git(path, "config", "user.email", "baseline@example.invalid")
            (path / "任务.txt").write_text("最初版本", encoding="utf-8")
            (path / "本机.txt").write_text("最初本机", encoding="utf-8")
            git(path, "add", ".")
            git(path, "commit", "-m", "初始化")
            baseline = git(path, "rev-parse", "HEAD")
            (path / "任务.txt").write_text("人工提交", encoding="utf-8")
            git(path, "add", ".")
            git(path, "commit", "-m", "更新")
            (path / "任务.txt").write_text("未提交任务", encoding="utf-8")
            (path / "本机.txt").write_text("保留本机", encoding="utf-8")
            git(path, "add", "本机.txt")
            self.state["repos"].append({"name": role, "role": role, "path": str(path),
                "source": str(path), "branch": "项目分支", "baseline": baseline,
                "workspace_owned": False, "preserved_paths": ["本机.txt"]})
        self.state["history"] = [{"role": "用户", "text": "原始需求及回答"}]
        self.state["reviews"] = [{"number": 1, "result": {"findings": []}}]
        self.state["decisions"] = [{"round": 1, "fix_ids": ["R01-01"]}]
        self.state["review_manifest"] = {"旧文件": "旧摘要"}
        self.engine.save()

    def preview(self):
        self.engine.change_baseline()
        return self.state["baseline_change_request"]["token"]

    def test_preview_confirmation_preserves_thread_history_code_index_and_backup(self):
        old = deepcopy(self.state)
        indexes = [git(repo["path"], "diff", "--cached", "--binary", binary=True) for repo in old["repos"]]
        token = self.preview()
        self.assertEqual(self.state["repos"], old["repos"])
        self.assertEqual(self.state["history"], old["history"])
        self.assertEqual(self.state["baseline_change_request"]["repositories"][0]["remaining_paths"], ["任务.txt"])
        self.engine.change_baseline(token)
        for key in ("thread_id", "plan", "approved_plan", "approved_hash", "followup", "reviews", "decisions"):
            self.assertEqual(self.state[key], old[key])
        self.assertEqual(self.state["history"][:len(old["history"])], old["history"])
        self.assertEqual(self.state["stage"], "已暂停")
        self.assertEqual(self.state["phase"], "followup")
        self.assertEqual(self.state["review_manifest"], {})
        self.assertEqual(self.engine.current_reviews(), [])
        for repo, index in zip(self.state["repos"], indexes):
            self.assertEqual(repo["baseline"], git(repo["path"], "rev-parse", "HEAD"))
            self.assertEqual(git(repo["path"], "diff", "--cached", "--binary", binary=True), index)
            self.assertEqual((Path(repo["path"]) / "任务.txt").read_text(encoding="utf-8"), "未提交任务")
            self.assertEqual(repo["preserved_paths"], ["本机.txt"])
        backup = loads(Path(self.state["baseline_changes"][-1]["backup"]).read_text(encoding="utf-8"))
        self.assertEqual(backup["repos"], old["repos"])
        self.assertEqual(backup["history"], old["history"])
        self.assertEqual(self.engine.store.load(self.state["id"])["thread_id"], "原线程")

    def test_changed_files_or_index_invalidate_preview_without_rebasing(self):
        old = deepcopy(self.state["repos"])
        token = self.preview()
        path = Path(old[1]["path"])
        (path / "任务.txt").write_text("预览后变化", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.engine.change_baseline(token)
        self.assertEqual(self.state["repos"], old)
        token = self.preview()
        git(path, "add", "任务.txt")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.engine.change_baseline(token)
        self.assertEqual(self.state["repos"], old)

    def test_changed_head_invalidates_confirmation(self):
        token = self.preview()
        git(self.state["repos"][0]["path"], "commit", "--allow-empty", "-m", "预览后提交")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.engine.change_baseline(token)

    def test_wrong_branch_and_detached_head_require_manual_resolution(self):
        path = self.state["repos"][0]["path"]
        git(path, "switch", "-c", "其他分支")
        with self.assertRaisesRegex(WorkflowError, "手工恢复原分支"):
            self.preview()
        git(path, "switch", "--detach")
        with self.assertRaisesRegex(WorkflowError, "游离 HEAD"):
            self.preview()

    def test_partial_commit_pending_question_and_active_turn_are_rejected(self):
        for key, value in (("commits", {"frontend": "提交"}), ("pending", {"id": "待答"})):
            previous = self.state[key]
            self.state[key] = value
            with self.assertRaises(WorkflowError):
                self.preview()
            self.state[key] = previous
        self.engine.turn_running = True
        with self.assertRaises(WorkflowError):
            self.preview()

    def test_review_restarts_checks_and_cannot_use_old_findings(self):
        self.state.update(phase="compile_fix", compile_checks=[{"delivery_round": 1, "status": "complete"}])
        self.engine.change_baseline(self.preview())
        self.assertEqual(self.state["phase"], "review")
        self.assertEqual(self.state["compile_checks"][0]["status"], "invalidated")
        self.assertEqual(self.engine.current_reviews(), [])
        self.engine._before_review = Mock()
        self.engine.resume()
        self.engine._before_review.assert_called_once()

    def test_plan_keeps_confirmation_and_does_not_auto_authorize(self):
        self.state.update(phase="plan", stage="等待计划确认", plan_round={"turns": 1, "asked": False})
        self.engine.change_baseline(self.preview())
        self.assertEqual(self.state["stage"], "等待计划确认")
        self.assertTrue(self.state["plan_round"]["asked"])
        self.assertEqual(self.state["approved_plan"], "已确认计划")

    def test_cancel_does_not_change_original_state_or_error(self):
        old = deepcopy(self.state)
        self.preview()
        self.engine.cancel_baseline_change()
        for key in ("repos", "error", "history", "thread_id", "stage"):
            self.assertEqual(self.state[key], old[key])
        self.assertIsNone(self.state["baseline_change_request"])

    def test_preview_can_be_confirmed_after_reloading_saved_session(self):
        token = self.preview()
        self.engine.restore(self.state["id"])
        self.engine.change_baseline(token)
        self.assertEqual(self.engine.state["thread_id"], "原线程")
        self.assertEqual(len(self.engine.state["baseline_changes"]), 1)

    def test_rollback_is_explicitly_displayed_and_only_confirmed_head_is_used(self):
        repo = self.state["repos"][0]
        head = git(repo["path"], "rev-parse", "HEAD")
        git(repo["path"], "reset", "--soft", repo["baseline"])
        repo["baseline"] = head
        token = self.preview()
        self.assertEqual(self.state["baseline_change_request"]["repositories"][0]["relationship"], "回退")
        self.engine.change_baseline(token)
        self.assertEqual(repo["baseline"], git(repo["path"], "rev-parse", "HEAD"))

    def test_backup_failure_leaves_original_session_intact(self):
        token = self.preview()
        old = deepcopy(self.state)
        with patch("pathlib.Path.write_text", side_effect=OSError("备份失败")):
            with self.assertRaises(OSError):
                self.engine.change_baseline(token)
        self.assertEqual(self.state, old)

    def relocation_targets(self):
        targets = {}
        for repo in self.state["repos"]:
            path = self.root / (repo["role"] + "-2")
            git(self.root, "clone", "--local", repo["path"], str(path))
            (path / "本机.txt").write_text("新目录本机改动", encoding="utf-8")
            git(path, "add", "本机.txt")
            targets[repo["name"]] = str(path)
        return targets

    def test_relocation_accepts_original_detached_head_and_preserves_both_directories(self):
        targets = self.relocation_targets()
        old = deepcopy(self.state["repos"])
        self.state["selections"] = [{"name": repo["name"], "path": repo["path"], "create": False}
                                    for repo in old]
        git(old[0]["path"], "switch", "--detach")
        self.engine.change_baseline(paths=targets)
        preview = self.state["baseline_change_request"]
        self.assertTrue(all(repo["directory_changed"] for repo in preview["repositories"]))
        self.assertEqual(self.state["repos"], old)
        self.assertEqual(preview["repositories"][0]["previous_remaining_paths"], ["任务.txt"])
        self.engine.change_baseline(preview["token"])
        for previous, repo, selection in zip(old, self.state["repos"], self.state["selections"]):
            self.assertEqual(repo["path"], targets[repo["name"]])
            self.assertEqual(repo["source"], targets[repo["name"]])
            self.assertEqual(selection["path"], repo["path"])
            self.assertEqual(repo["preserved_paths"], ["本机.txt"])
            self.assertFalse(repo["workspace_owned"])
            self.assertEqual((Path(previous["path"]) / "任务.txt").read_text(encoding="utf-8"), "未提交任务")
            self.assertTrue(git(repo["path"], "diff", "--cached", "--name-only"))
        self.assertEqual(git(old[0]["path"], "branch", "--show-current"), "")
        self.assertEqual(self.state["thread_id"], "原线程")
        self.assertEqual(self.state["retired_repos"], old)
        self.assertTrue(all(repo in session_repositories(self.state) for repo in old))
        claims = workspace_claims([self.state], "其他会话")
        self.assertIn(targets[old[0]["name"]].casefold(), claims)
        self.assertNotIn(old[0]["path"].casefold(), claims)

    def test_new_directory_does_not_need_to_contain_old_baseline(self):
        path = self.root / "新独立仓库"
        path.mkdir()
        git(path, "init", "-b", "项目分支")
        git(path, "-c", "user.name=检查", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "新基线")
        self.engine.change_baseline(paths={"frontend": str(path)})
        selected = self.state["baseline_change_request"]["repositories"][0]
        self.assertEqual(selected["relationship"], "更换目录，重新建立基线")
        self.engine.change_baseline(self.state["baseline_change_request"]["token"])
        self.assertEqual(self.state["repos"][0]["path"], str(path))

    def test_relocation_rejects_claimed_target_and_checks_again_at_confirmation(self):
        targets = self.relocation_targets()
        self.engine.change_baseline(paths=targets)
        token = self.state["baseline_change_request"]["token"]
        other = self.engine.store.new(None)
        other["repos"] = [{"path": targets["frontend"]}]
        self.engine.store.save(other)
        old = deepcopy(self.state["repos"])
        with self.assertRaisesRegex(WorkflowError, "未完成会话"):
            self.engine.change_baseline(token)
        self.assertEqual(self.state["repos"], old)

    def test_relocation_rechecks_new_directory_files_before_confirmation(self):
        targets = self.relocation_targets()
        self.engine.change_baseline(paths=targets)
        token = self.state["baseline_change_request"]["token"]
        (Path(targets["backend"]) / "新文件.txt").write_text("变化", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "重新预览"):
            self.engine.change_baseline(token)

    def test_relocation_preserves_staged_only_changes_even_when_worktree_matches_head(self):
        targets = self.relocation_targets()
        path = Path(targets["frontend"])
        original = (path / "任务.txt").read_bytes()
        (path / "任务.txt").write_text("只在暂存区保留", encoding="utf-8")
        git(path, "add", "任务.txt")
        (path / "任务.txt").write_bytes(original)
        staged = git(path, "diff", "--cached", "--binary", binary=True)
        self.engine.change_baseline(paths=targets)
        self.engine.change_baseline(self.state["baseline_change_request"]["token"])
        self.assertIn("任务.txt", self.state["repos"][0]["preserved_paths"])
        self.assertEqual(git(path, "diff", "--cached", "--binary", binary=True), staged)
        self.assertEqual((path / "任务.txt").read_bytes(), original)

    def test_retired_owned_workspace_is_reserved_and_cleaned_without_deleting_new_directory(self):
        targets = self.relocation_targets()
        repo = self.state["repos"][0]
        workspace = self.engine.store.directory(self.state["id"]) / "workspace"
        workspace.mkdir()
        old_path = workspace / "repo-1"
        git(repo["path"], "worktree", "add", "--detach", str(old_path), "HEAD")
        repo.update(path=str(old_path), workspace_owned=True)
        self.engine.change_baseline(paths=targets)
        self.engine.change_baseline(self.state["baseline_change_request"]["token"])
        self.assertIn(str(old_path).casefold(), workspace_claims([self.state], "其他会话"))
        cleanup_workspaces(self.engine.store, self.state, [])
        self.assertFalse(old_path.exists())
        self.assertTrue(Path(targets["frontend"]).is_dir())

    def test_relocation_validates_paths_before_changing_session(self):
        for paths in ([], {"未知仓库": str(self.root)}, {"frontend": "相对路径"},
                      {"frontend": str(self.root / "不存在")},
                      {"frontend": self.state["repos"][1]["path"], "backend": self.state["repos"][1]["path"]}):
            with self.subTest(paths=paths), self.assertRaises(WorkflowError):
                self.engine.change_baseline(paths=paths)
        self.assertNotIn("retired_repos", self.state)

    def test_migrated_next_round_keeps_new_directories_and_original_thread(self):
        from test_followup import FollowupChecks

        fixture = FollowupChecks()
        fixture.setUp()
        try:
            fixture.send("仅了解当前代码，不修改", review_enabled=False)
            fixture.pause_current()
            targets = {}
            for repo in fixture.state["repos"]:
                path = fixture.root / (repo["role"] + "-relocated")
                git(fixture.root, "clone", "--local", repo["path"], str(path))
                targets[repo["name"]] = str(path)
            fixture.engine.change_baseline(paths=targets)
            fixture.engine.change_baseline(fixture.state["baseline_change_request"]["token"])
            fixture.engine.resume()
            fixture.complete("当前代码已说明")
            self.assertEqual(fixture.state["phase"], "complete")
            fixture.send("继续说明，不修改", review_enabled=False)
            self.assertEqual(fixture.state["thread_id"], "原需求线程")
            self.assertTrue(all(repo["path"] == targets[repo["name"]] for repo in fixture.state["repos"]))
        finally:
            fixture.doCleanups()
