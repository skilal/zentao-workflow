"""使用临时独立 Git 仓库验证目录匹配，不访问业务仓库。"""

from copy import deepcopy
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.engine import Workflow
from workflow.gitops import branch_catalog, checkout_snapshot, git
from workflow.repository_selection import candidate_paths, inspect_repository, match_repository, repository_catalog
from workflow.settings import ROOT, WorkflowError


class RepositorySelectionChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=ROOT / ".local")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        local = patch("workflow.store.LOCAL", self.root / "local")
        local.start()
        self.addCleanup(local.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.settings["workspace_root"] = str(self.root / "worktrees")
        self.settings["repositories"] = []
        for role, name in (("frontend", "前端"), ("backend", "后端")):
            first = self.make_repo(role, "目标分支", "2026-01-01T10:00:00+08:00")
            second = self.make_repo(role + "-2", "其他分支", "2026-01-02T10:00:00+08:00")
            self.settings["repositories"].append({"name": name, "role": role, "path": str(first),
                "candidate_paths": [str(second)], "remote": "origin"})
        self.flow = Workflow(self.settings, Mock())
        self.addCleanup(self.flow.close)
        self.state = self.flow.store.new({"kind": "task", "id": 7, "title": "目录匹配"})
        self.flow.state = self.state
        self.state.update(project={"id": 41, "name": "示例项目", "candidates": []},
                          codex={"model": "示例模型", "reasoning_effort": "high"},
                          snapshot={"sources": [{"title": "任务", "content": "规则", "raw": {"project": 41}}],
                                    "materials": [], "blockers": [], "feishu_links": []},
                          branch_resolution={"manual": False, "repositories": [
                              {**repo, "status": "resolved", "branch": "目标分支", "catalog": branch_catalog(repo),
                               "candidates": [], "source": "common", "reason": "使用项目常用分支。",
                               "preferred": "", "refreshed": False, "refresh_state": ""}
                              for repo in self.settings["repositories"]]},
                          stage="等待选择目录", phase="branch")
        self.flow._start_turn = Mock()
        self.flow.save()

    def make_repo(self, name, branch, date):
        path = self.root / name
        path.mkdir()
        git(path, "init", "-b", branch)
        git(path, "config", "user.name", "目录检查")
        git(path, "config", "user.email", "directory@example.invalid")
        (path / "文件.txt").write_text(name, encoding="utf-8")
        git(path, "add", ".")
        git(path, "commit", "-m", "初始化", environment_override={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return path

    def second(self, role="frontend"):
        return Path(next(repo for repo in self.settings["repositories"] if repo["role"] == role)["candidate_paths"][0])

    def detection(self, role="frontend"):
        return next(entry for entry in self.state["repository_detection"]["repositories"] if entry["role"] == role)

    def choices(self):
        return {entry["role"]: entry["path"] or entry["candidates"][0]["path"]
                for entry in self.state["repository_detection"]["repositories"] if entry["status"] != "bound"}

    def test_only_current_branch_matches_and_roles_are_independent(self):
        git(self.second(), "branch", "目标分支")
        git(self.second("backend"), "switch", "-c", "目标分支")
        self.flow.detect_repositories()
        self.assertEqual(self.detection()["path"], str(self.root / "frontend"))
        self.assertEqual(self.detection("backend")["path"], str(self.second("backend")))
        self.assertEqual(git(self.second(), "branch", "--show-current"), "其他分支")

    def test_newest_committer_time_wins_among_matching_current_branches(self):
        git(self.second(), "switch", "-c", "目标分支")
        self.flow.detect_repositories()
        self.assertEqual(self.detection()["path"], str(self.second()))
        self.assertIn("提交者时间最新", self.detection()["reason"])

    def test_equal_time_requires_explicit_selection_and_survives_restore(self):
        first = self.settings["repositories"][0]
        equal = self.make_repo("equal", "目标分支", "2026-01-01T10:00:00+08:00")
        first["candidate_paths"] = [str(equal)]
        self.flow.detect_repositories()
        self.assertEqual(self.detection()["status"], "needs_user")
        self.assertEqual(self.state["stage"], "等待选择目录")
        self.flow.restore(self.state["id"])
        self.state = self.flow.state
        self.assertEqual(self.state["stage"], "等待选择目录")
        self.assertFalse(self.flow._start_turn.called)
        self.flow.choose_repositories(self.choices())
        self.assertEqual(self.state["phase"], "plan")
        self.assertEqual(len(self.state["repos"]), 2)

    def test_no_match_plans_allocation_and_custom_match_still_takes_precedence(self):
        git(self.root / "frontend", "switch", "-c", "另一个分支")
        self.flow.detect_repositories()
        self.assertEqual(self.detection()["status"], "selected")
        self.assertEqual(self.detection()["action"], "allocate")
        self.assertEqual(self.state["repos"], [])
        custom = self.make_repo("custom", "目标分支", "2026-02-01T10:00:00+08:00")
        self.flow.detect_repositories(paths={"frontend": str(custom)})
        self.assertEqual(self.detection()["path"], str(custom))
        self.flow.choose_repositories(self.choices())
        self.assertEqual(self.state["repos"][0]["path"], str(custom))
        self.assertEqual(git(self.root / "frontend", "branch", "--show-current"), "另一个分支")
        self.assertEqual(len(git(custom, "worktree", "list").splitlines()), 1)

    def test_detection_error_blocks_automatic_selection_but_allows_manual_choice(self):
        self.settings["repositories"][0]["candidate_paths"].append(str(self.root / "missing"))
        self.flow.detect_repositories()
        self.assertEqual(self.detection()["status"], "needs_user")
        self.assertTrue(self.detection()["candidates"][-1]["error"])
        self.flow.choose_repositories(self.choices())
        self.assertEqual(len(self.state["repos"]), 2)

    def test_manual_choice_rejects_head_or_branch_changes_after_detection(self):
        for change in ("commit", "branch"):
            with self.subTest(change=change):
                self.flow.detect_repositories()
                if change == "commit":
                    git(self.root / "frontend", "commit", "--allow-empty", "-m", "外部变化")
                else:
                    git(self.root / "frontend", "switch", "-c", "不同分支")
                with self.assertRaisesRegex(WorkflowError, "重新检测"):
                    self.flow.choose_repositories(self.choices())
                self.assertEqual(self.state["repos"], [])

    def test_binding_rechecks_snapshot_and_does_not_choose_next_newest(self):
        git(self.second(), "switch", "-c", "目标分支")
        self.flow.detect_repositories()
        git(self.second(), "commit", "--allow-empty", "-m", "外部提交")
        with self.assertRaisesRegex(WorkflowError, "重新检测"):
            self.flow._prepare_matched_repositories()
        self.assertEqual(self.state["stage"], "准备工作区失败")
        self.assertEqual(self.state["repos"], [])
        self.assertEqual(self.state["selections"][0]["path"], str(self.second()))

    def test_selected_directory_hook_stops_without_falling_back(self):
        git(self.second(), "switch", "-c", "目标分支")
        self.flow.detect_repositories()
        (self.second() / ".git/hooks/pre-commit").write_text("禁止执行", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "Git 钩子"):
            self.flow._prepare_matched_repositories()
        self.assertEqual(self.state["repos"], [])
        self.assertEqual(self.state["selections"][0]["path"], str(self.second()))

    def test_directory_claimed_for_another_branch_is_not_taken_over(self):
        other = self.flow.store.new({"kind": "task", "id": 8, "title": "其他会话"})
        other.update(phase="plan", repos=[{"name": "前端", "path": str(self.root / "frontend"), "branch": "其他目标分支"}])
        self.flow.store.save(other)
        self.flow.detect_repositories()
        with self.assertRaisesRegex(WorkflowError, "不同目标分支占用"):
            self.flow.choose_repositories(self.choices())
        self.assertEqual(self.state["repos"], [])
        self.assertEqual(git(self.root / "frontend", "branch", "--show-current"), "目标分支")

    def test_pause_and_resume_do_not_answer_pending_directory_question(self):
        git(self.root / "frontend", "switch", "-c", "不同分支")
        self.flow.detect_repositories()
        self.flow.pause()
        self.flow.resume()
        self.assertEqual(self.state["stage"], "等待选择目录")
        self.assertFalse(self.flow._start_turn.called)

    def test_committer_time_is_used_instead_of_author_time(self):
        git(self.second(), "switch", "-c", "目标分支")
        git(self.second(), "commit", "--allow-empty", "-m", "作者时间较新", environment_override={
            "GIT_AUTHOR_DATE": "2026-12-01T10:00:00+08:00", "GIT_COMMITTER_DATE": "2025-12-01T10:00:00+08:00"})
        self.flow.detect_repositories()
        self.assertEqual(self.detection()["path"], str(self.root / "frontend"))

    def test_unique_match_starts_plan_from_exact_directory_and_preserves_staged_files(self):
        first = self.root / "frontend"
        (first / "本机.txt").write_text("本机保留", encoding="utf-8")
        git(first, "add", "本机.txt")
        before = checkout_snapshot(first)
        self.state.pop("branch_resolution")
        site = self.settings["zentao"]["base_url"]
        self.settings["project_branches"] = {site: {"41": {"branches": {"frontend": "目标分支", "backend": "目标分支"}}}}
        self.state["stage"] = "资料已读取"
        self.flow.start_plan("补充")
        self.assertEqual([repo["path"] for repo in self.state["repos"]], [str(first), str(self.root / "backend")])
        self.assertEqual(checkout_snapshot(first), before)
        self.assertIn("本机.txt", self.state["repos"][0]["preserved_paths"])
        self.assertTrue(self.state["repos"][0]["directory_matched"])
        self.flow._start_turn.assert_called_once()

    def test_staged_changes_are_preserved_when_worktree_already_matches_head(self):
        path = self.root / "frontend"
        (path / "文件.txt").write_text("已暂存内容", encoding="utf-8")
        git(path, "add", "文件.txt")
        (path / "文件.txt").write_text("frontend", encoding="utf-8")
        before = checkout_snapshot(path)
        self.flow.detect_repositories()
        self.flow.choose_repositories(self.choices())
        self.assertIn("文件.txt", self.state["repos"][0]["preserved_paths"])
        self.assertEqual(checkout_snapshot(path), before)

    def test_branch_catalog_keeps_separate_clone_metadata_and_secondary_only_branch(self):
        repo = self.settings["repositories"][0]
        catalog = repository_catalog(repo, branch_catalog)
        self.assertEqual({entry["name"] for entry in catalog}, {"目标分支", "其他分支"})
        git(self.second(), "branch", "目标分支")
        entry = next(entry for entry in repository_catalog(repo, branch_catalog) if entry["name"] == "目标分支")
        self.assertEqual(len(entry["sources"]), 2)
        self.assertNotEqual(entry["sources"][0]["commit"], entry["sources"][1]["commit"])

    def test_common_branch_existing_only_in_secondary_clone_uses_that_clone(self):
        git(self.second(), "switch", "-c", "仅第二目录分支")
        self.state.pop("branch_resolution")
        site = self.settings["zentao"]["base_url"]
        self.settings["project_branches"] = {site: {"41": {"branches": {
            "frontend": "仅第二目录分支", "backend": "目标分支"}}}}
        self.state["stage"] = "资料已读取"
        self.flow.start_plan("")
        self.assertEqual(self.state["repos"][0]["path"], str(self.second()))
        self.assertEqual(self.state["repos"][0]["baseline"], git(self.second(), "rev-parse", "HEAD"))
        self.assertFalse(git(self.root / "frontend", "rev-parse", "--verify", "refs/heads/仅第二目录分支", optional=True))

    def test_targeted_fetch_uses_catalog_source_clone(self):
        repo = self.settings["repositories"][0]
        remote_ref = "refs/remotes/origin/仅第二目录远程分支"
        git(self.second(), "update-ref", remote_ref, "HEAD")
        repo = {**repo, "catalog": repository_catalog(repo, branch_catalog)}
        metadata = next(source for entry in repo["catalog"] if entry["name"] == "仅第二目录远程分支"
                        for source in entry["sources"])
        with patch("workflow.engine.fetch_branch", return_value=metadata) as fetch:
            self.flow._fetch_branch(repo, "仅第二目录远程分支")
        self.assertEqual(fetch.call_args.args[0]["path"], str(self.second()))
        self.assertFalse(git(self.root / "frontend", "rev-parse", "--verify", remote_ref, optional=True))

    def test_candidate_paths_are_optional_and_case_insensitively_deduplicated(self):
        repo = {**self.settings["repositories"][0]}
        repo.pop("candidate_paths")
        self.assertEqual(candidate_paths(repo), [repo["path"]])
        repo["candidate_paths"] = [repo["path"].upper(), repo["path"]]
        self.assertEqual(candidate_paths(repo), [repo["path"]])
        for value in ("invalid", ["relative"], [False]):
            repo["candidate_paths"] = value
            with self.assertRaises(WorkflowError):
                candidate_paths(repo)

    def test_detached_head_is_visible_but_cannot_match(self):
        git(self.second(), "switch", "--detach")
        result = match_repository(self.settings["repositories"][0], "目标分支")
        self.assertEqual(result["status"], "needs_user")
        self.assertIn("游离 HEAD", result["candidates"][1]["error"])

    def test_baseline_detection_only_suggests_until_explicit_confirmation(self):
        from test_baseline_change import BaselineChangeChecks

        fixture = BaselineChangeChecks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        targets = fixture.relocation_targets()
        fixture.engine.settings["repositories"] = [{"name": repo["name"], "role": repo["role"], "path": repo["path"],
            "candidate_paths": [targets[repo["name"]]], "remote": "origin"} for repo in fixture.state["repos"]]
        original = deepcopy(fixture.state["repos"])
        fixture.engine.detect_repositories("baseline")
        self.assertEqual(fixture.state["repos"], original)
        fixture.engine.change_baseline(paths=targets)
        self.assertEqual(fixture.state["repos"], original)
        token = fixture.state["baseline_change_request"]["token"]
        fixture.engine.change_baseline(token)
        self.assertEqual(fixture.state["thread_id"], "原线程")
        for repo in fixture.state["repos"]:
            self.assertEqual(repo["path"], targets[repo["name"]])
            self.assertIn("本机.txt", repo["preserved_paths"])

    def test_prepare_cannot_override_existing_bound_session(self):
        self.state["prepare_complete"] = True
        with self.assertRaises(WorkflowError):
            self.flow.detect_repositories()
        self.assertNotIn("repository_detection", self.state)

    def test_baseline_confirmation_updates_binding_snapshot(self):
        self.flow.detect_repositories()
        self.flow.choose_repositories(self.choices())
        self.state["stage"] = "已暂停"
        git(self.root / "frontend", "commit", "--allow-empty", "-m", "人工提交")
        self.flow.change_baseline()
        self.flow.change_baseline(self.state["baseline_change_request"]["token"])
        self.assertEqual(self.state["selections"][0]["expected_head"], git(self.root / "frontend", "rev-parse", "HEAD"))
