"""隔离验证分支历史、自动分配、逐文件判定及冲突回退。"""

from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.branching import BRANCH_INSTRUCTIONS, branch_context
from workflow.engine import Workflow
from workflow.gitops import acquire_workspace, branch_history, checkout_snapshot, cleanup_workspaces, git
from workflow.settings import ROOT, WorkflowError
from workflow.workspace_idle import IdleAnalysisRequired, idle_evidence, redact_evidence
from workflow.workspace_paths import managed_workspace, workspace_directory


class WorkspaceAllocationChecks(TestCase):
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
        for role in ("frontend", "backend"):
            path = self.root / role
            path.mkdir()
            git(path, "init", "-b", "其他项目/develop")
            git(path, "config", "user.name", "工作区检查")
            git(path, "config", "user.email", "allocation@example.invalid")
            (path / "logback.xml").write_text("原始配置\n", encoding="utf-8")
            (path / "业务.txt").write_text("原始业务\n", encoding="utf-8")
            git(path, "add", ".")
            git(path, "commit", "-m", "项目初始提交")
            git(path, "branch", "项目/develop")
            self.settings["repositories"].append({"name": role, "role": role, "path": str(path), "remote": "origin"})
        self.engine = Workflow(self.settings, Mock())
        self.addCleanup(self.engine.close)
        self.state = self.engine.store.new({"kind": "task", "id": 7, "title": "项目任务"})
        self.state.update(project={"id": 41, "name": "项目（售后）"}, codex={"model": "测试", "reasoning_effort": "high"},
                          snapshot={"sources": [{"title": "任务", "content": "项目需求"}], "materials": [], "blockers": [], "feishu_links": []},
                          workspace_root=self.settings["workspace_root"])
        self.engine.state = self.state
        self.engine._start_turn = Mock()
        self.engine.save()

    def selection(self, role="frontend"):
        repository = next(repo for repo in self.settings["repositories"] if repo["role"] == role)
        return {**repository, "branch": "项目/develop", "base": "refs/heads/项目/develop", "create": False,
                "track": False, "allocate": True, "candidate_paths": [repository["path"]]}

    def acquire(self, selection=None):
        selection = selection or self.selection()
        return acquire_workspace(selection, workspace_directory(self.engine.store, self.state) / selection["role"], self.engine.store, self.state)

    def assess(self, path, impact="nonfunctional"):
        path = str(Path(path).resolve())
        snapshot = checkout_snapshot(path)
        self.state.setdefault("idle_assessments", {})[path.casefold()] = {
            "path": path, "head": git(path, "rev-parse", "HEAD"), "branch": git(path, "branch", "--show-current"),
            "snapshot": snapshot, "files": [{"path": name, "impact": impact, "reason": "已核对内容和用途"} for name in snapshot["paths"]]}

    def test_clean_directory_switches_without_ai(self):
        record = self.acquire()
        self.assertEqual(record["workspace_mode"], "switched")
        self.assertEqual(record["path"], str(self.root / "frontend"))
        self.engine._start_turn.assert_not_called()

    def test_nonfunctional_changes_preserve_index_unstaged_and_untracked(self):
        path = self.root / "frontend"
        (path / "logback.xml").write_text("本机已暂存\n", encoding="utf-8")
        git(path, "add", "logback.xml")
        (path / "logback.xml").write_text("本机未暂存\n", encoding="utf-8")
        (path / "字体.ttf").write_bytes(b"\0\1\0\0font")
        before = checkout_snapshot(path)
        self.assess(path)
        record = self.acquire()
        self.assertEqual(checkout_snapshot(path), before)
        self.assertEqual(set(record["preserved_paths"]), {"logback.xml", "字体.ttf"})
        self.assertEqual(self.state["checkout_request"]["status"], "restored")

    def test_functional_or_unknown_changes_create_in_configured_root(self):
        path = self.root / "frontend"
        for impact in ("functional", "unknown"):
            with self.subTest(impact=impact):
                branch = f"项目/{impact}"
                git(path, "branch", branch)
                (path / "logback.xml").write_text("修改会影响功能\n", encoding="utf-8")
                self.assess(path, impact)
                selection = {**self.selection(), "branch": branch}
                record = acquire_workspace(selection, workspace_directory(self.engine.store, self.state) / impact, self.engine.store, self.state)
                self.assertTrue(Path(record["path"]).is_relative_to(Path(self.settings["workspace_root"])))
                self.assertEqual(record["workspace_mode"], "created")
                self.assertEqual(git(path, "branch", "--show-current"), "其他项目/develop")

    def test_active_session_claim_prevents_switching_even_if_clean(self):
        path = self.root / "frontend"
        other = self.engine.store.new({"kind": "task", "id": 8, "title": "占用"})
        other["repos"] = [{"name": "前端", "path": str(path), "branch": "其他项目/develop"}]
        self.engine.store.save(other)
        record = self.acquire()
        self.assertEqual(record["workspace_mode"], "created")
        self.assertEqual(git(path, "branch", "--show-current"), "其他项目/develop")

    def test_matching_secondary_clone_wins_before_switching_first(self):
        source = self.root / "frontend"
        second = self.root / "frontend-2"
        git(self.root, "clone", str(source), str(second))
        git(second, "switch", "项目/develop")
        selection = self.selection()
        selection["candidate_paths"].append(str(second))
        record = self.acquire(selection)
        self.assertEqual(record["path"], str(second))
        self.assertEqual(git(source, "branch", "--show-current"), "其他项目/develop")

    def test_snapshot_change_invalidates_ai_decision(self):
        path = self.root / "frontend"
        (path / "logback.xml").write_text("本机配置\n", encoding="utf-8")
        self.assess(path)
        (path / "logback.xml").write_text("另一项修改\n", encoding="utf-8")
        with self.assertRaises(IdleAnalysisRequired):
            self.acquire()
        self.assertEqual(git(path, "branch", "--show-current"), "其他项目/develop")

    def test_existing_target_worktree_precedes_dirty_source_analysis(self):
        path = self.root / "frontend"
        target = workspace_directory(self.engine.store, self.state) / "existing"
        git(path, "worktree", "add", str(target), "项目/develop")
        (path / "业务.txt").write_text("源目录的业务修改\n", encoding="utf-8")
        record = self.acquire()
        self.assertEqual(Path(record["path"]), target)
        self.assertEqual(record["workspace_mode"], "reused")
        self.assertNotIn("idle_assessments", self.state)

    def test_secondary_registered_target_precedes_primary_idle_directory(self):
        path = self.root / "frontend"
        second = self.root / "frontend-2"
        git(self.root, "clone", str(path), str(second))
        target = workspace_directory(self.engine.store, self.state) / "secondary-existing"
        git(second, "worktree", "add", "-b", "项目/develop", str(target), "origin/项目/develop")
        other = self.engine.store.new({"kind": "task", "id": 8, "title": "第二个 clone 的原目录被占用"})
        other["repos"] = [{"name": "前端", "path": str(second), "branch": "其他项目/develop"}]
        self.engine.store.save(other)
        selection = self.selection()
        selection["candidate_paths"].append(str(second))
        record = self.acquire(selection)
        self.assertEqual(Path(record["path"]), target)
        self.assertEqual(git(path, "branch", "--show-current"), "其他项目/develop")

    def test_staged_only_change_is_included_in_idle_evidence(self):
        path = self.root / "frontend"
        (path / "logback.xml").write_text("已暂存\n", encoding="utf-8")
        git(path, "add", ".")
        (path / "logback.xml").write_text("原始配置\n", encoding="utf-8")
        snapshot = checkout_snapshot(path)
        self.assertIn("logback.xml", snapshot["paths"])
        self.assertIn("已暂存", idle_evidence(path, snapshot)[0]["staged_diff"])

    def test_conflict_rolls_back_original_branch_index_and_files(self):
        path = self.root / "frontend"
        git(path, "switch", "项目/develop")
        (path / "logback.xml").write_text("目标分支配置\n", encoding="utf-8")
        git(path, "add", ".")
        git(path, "commit", "-m", "目标配置")
        git(path, "switch", "其他项目/develop")
        (path / "logback.xml").write_text("本机已暂存配置\n", encoding="utf-8")
        git(path, "add", ".")
        (path / "logback.xml").write_text("本机未暂存配置\n", encoding="utf-8")
        (path / "字体.ttf").write_bytes(b"font")
        before = checkout_snapshot(path)
        self.assess(path)
        with self.assertRaisesRegex(WorkflowError, "已恢复原分支"):
            self.acquire()
        self.assertEqual(checkout_snapshot(path), before)
        self.assertEqual(git(path, "branch", "--show-current"), "其他项目/develop")
        self.assertEqual(self.state["checkout_request"]["status"], "rolled_back")
        self.assertFalse(self.state["repos"])

    def test_first_prepare_independently_switches_and_starts_plan(self):
        self.state.update(stage="资料已读取")
        self.settings["project_branches"] = {self.settings["zentao"]["base_url"]: {"41": {"branches": {
            "frontend": "项目/develop", "backend": "项目/develop"}}}}
        self.engine.start_plan("")
        self.assertEqual(len(self.state["repos"]), 2)
        self.assertEqual(self.state["phase"], "plan")
        self.assertTrue(all(repo["workspace_mode"] == "switched" for repo in self.state["repos"]))

    def test_ai_idle_round_resumes_preparation_and_keeps_decision(self):
        path = self.root / "frontend"
        (path / "logback.xml").write_text("本机日志目录\n", encoding="utf-8")
        self.state.update(stage="资料已读取")
        self.engine.prepare_and_plan([self.selection()], "")
        self.assertIn("workspace_analysis", self.state)
        self.assertEqual(self.engine._idle_context()["step"], "idle")
        self.engine._complete_idle(dumps({"files": [{"path": "logback.xml", "impact": "nonfunctional", "reason": "仅本机日志路径"}]}))
        self.assertNotIn("workspace_analysis", self.state)
        self.assertEqual(self.state["phase"], "plan")
        self.assertEqual(len(self.state["repos"]), 1)

    def test_created_workspace_is_managed_and_cleanup_keeps_source(self):
        path = self.root / "frontend"
        (path / "业务.txt").write_text("业务改动\n", encoding="utf-8")
        self.assess(path, "functional")
        record = self.acquire()
        self.assertEqual(managed_workspace(self.engine.store, record["path"], [self.state]), self.state["id"])
        cleanup_workspaces(self.engine.store, self.state, self.settings["repositories"])
        self.assertFalse(Path(record["path"]).exists())
        self.assertTrue(path.exists())

    def test_cleanup_finds_secondary_clone_workspace_before_record_was_saved(self):
        path = self.root / "frontend"
        second = self.root / "frontend-2"
        git(self.root, "clone", str(path), str(second))
        target = workspace_directory(self.engine.store, self.state) / "repo-1"
        git(second, "worktree", "add", "-b", "项目/develop", str(target), "origin/项目/develop")
        selection = self.selection()
        selection["candidate_paths"].append(str(second))
        self.state["selections"] = [selection]
        self.engine.save()
        cleanup_workspaces(self.engine.store, self.state, self.settings["repositories"])
        self.assertFalse(target.exists())
        self.assertTrue(second.exists())

    def test_histories_keep_local_and_remote_refs_separate(self):
        path = self.root / "frontend"
        original = git(path, "rev-parse", "HEAD")
        git(path, "update-ref", "refs/remotes/origin/项目/develop", original)
        git(path, "commit", "--allow-empty", "-m", "本地新提交")
        git(path, "branch", "-f", "项目/develop", "HEAD")
        history = branch_history(self.settings["repositories"][0], "项目/develop")
        self.assertEqual(len(history["refs"]), 2)
        self.assertNotEqual(history["refs"][0]["commit"], history["refs"][1]["commit"])
        self.assertTrue(all(len(ref["commits"]) <= 10 for ref in history["refs"]))

    def test_branch_shortlist_then_history_verification(self):
        self.state.update(stage="资料已读取")
        self.engine.start_plan("使用本项目长期开发分支")
        context = branch_context(self.state)
        self.assertEqual(context["repositories"][0]["step"], "shortlist")
        self.assertEqual(context["sources"][0]["content"], "项目需求")
        result = {"role": "frontend", "status": "selected", "branch": "项目/develop", "reason": "名称贴合项目", "candidates": [], "question": ""}
        self.engine._complete_branches(dumps({"repositories": [result]}))
        context = branch_context(self.state)
        self.assertEqual(context["repositories"][0]["step"], "verify")
        self.assertTrue(context["repositories"][0]["histories"])
        self.assertEqual(self.state["repos"], [])
        self.engine._complete_branches(dumps({"repositories": [result]}))
        self.assertEqual(self.state["branch_resolution"]["repositories"][0]["branch"], "项目/develop")
        self.assertIn("不能仅因更新晚", BRANCH_INSTRUCTIONS)

    def test_old_unprepared_ai_selection_is_reidentified(self):
        self.state.update(stage="等待选择目录", phase="branch", thread_id="分支线程",
            repository_detection={"purpose": "prepare"}, branch_resolution={"repositories": [
                {"role": "frontend", "source": "ai", "branch": "旧的错误分支"}]})
        self.engine.start_plan("重新判断")
        self.assertNotIn("repository_detection", self.state)
        self.assertEqual(self.state["branch_resolution"]["repositories"][0]["status"], "pending")
        self.assertEqual(self.state["thread_id"], "分支线程")

    def test_shortlist_pages_cover_all_branches_without_large_source_body(self):
        repository = {**self.settings["repositories"][0], "status": "pending", "refreshed": False,
                      "catalog": [{"name": f"项目-{number}/develop", "local": True, "remote": False}
                                  for number in range(401)],
                      "analysis": {"step": "shortlist", "offset": 200, "shortlist": []}}
        self.state["branch_resolution"] = {"repositories": [repository], "active_role": "frontend"}
        self.state["snapshot"]["sources"][0]["content"] = "大量正文" * 300000
        context = branch_context(self.state)
        self.assertFalse(context["sources_cached"])
        self.state["branch_resolution"]["sources_input"] = {"thread_id": self.state.get("thread_id"), "revision": context["sources_revision"]}
        context = branch_context(self.state)
        self.assertTrue(context["sources_cached"])
        self.assertEqual(len(context["repositories"][0]["branches"]["branches"]), 200)
        self.assertLess(len(dumps(context)), 100000)
        repository["analysis"]["offset"] = 400
        self.assertEqual(branch_context(self.state)["repositories"][0]["branches"]["branches"][0]["name"], "项目-400/develop")
        self.state["snapshot"]["sources"][0]["content"] = "正文已变更，归属另一个项目"
        self.assertFalse(branch_context(self.state)["sources_cached"])

    def test_idle_resume_rechecks_changed_files_instead_of_reusing_old_answer(self):
        path = self.root / "frontend"
        (path / "logback.xml").write_text("日志路径\n", encoding="utf-8")
        self.state.update(stage="资料已读取")
        self.engine.prepare_and_plan([self.selection()], "")
        first = self.state["workspace_analysis"]["snapshot"]
        self.engine.pause()
        (path / "logback.xml").write_text("新的日志配置\n", encoding="utf-8")
        self.engine.resume()
        self.assertNotEqual(self.state["workspace_analysis"]["snapshot"], first)
        self.assertFalse(self.state["repos"])

    def test_incomplete_ai_file_result_does_not_authorize_checkout(self):
        path = self.root / "frontend"
        (path / "logback.xml").write_text("本机日志路径\n", encoding="utf-8")
        self.state.update(stage="资料已读取")
        self.engine.prepare_and_plan([self.selection()], "")
        with self.assertRaisesRegex(WorkflowError, "逐文件"):
            self.engine._complete_idle(dumps({"files": []}))
        self.assertFalse(self.state["repos"])
        self.assertEqual(git(path, "branch", "--show-current"), "其他项目/develop")

    def test_failed_rollback_retains_stash_and_progress(self):
        path = self.root / "frontend"
        git(path, "switch", "项目/develop")
        (path / "logback.xml").write_text("目标配置\n", encoding="utf-8")
        git(path, "commit", "-am", "目标分支配置")
        git(path, "switch", "其他项目/develop")
        (path / "logback.xml").write_text("本机配置\n", encoding="utf-8")
        self.assess(path)
        def interrupted(repository, *args, **kwargs):
            if args == ("switch", "--no-overwrite-ignore", "其他项目/develop"):
                raise WorkflowError("模拟回退被外部锁阻止")
            return git(repository, *args, **kwargs)
        with patch("workflow.workspace_idle.git", side_effect=interrupted):
            with self.assertRaisesRegex(WorkflowError, "回退未完成"):
                self.acquire()
        request = self.state["checkout_request"]
        self.assertEqual(request["status"], "rollback_failed")
        self.assertTrue(git(path, "rev-parse", request["stash"]))
        self.assertFalse(self.state["repos"])

    def test_business_thread_cwd_uses_central_root(self):
        from workflow.rpc import thread_parameters
        self.state["phase"] = "plan"
        parameters = thread_parameters(self.state, "测试模型", {})
        self.assertEqual(Path(parameters["cwd"]), workspace_directory(self.engine.store, self.state))

    def test_ai_sees_real_competing_feature_history_before_dev_selection(self):
        path = self.root / "frontend"
        git(path, "switch", "-c", "feature/项目-20261117-专题")
        git(path, "commit", "--allow-empty", "-m", "专题最新修改")
        git(path, "switch", "其他项目/develop")
        self.state["stage"] = "资料已读取"
        self.engine.start_plan("")
        result = {"role": "frontend", "status": "selected", "branch": "项目/develop", "reason": "项目主线优先",
                  "candidates": ["feature/项目-20261117-专题"], "question": ""}
        self.engine._complete_branches(dumps({"repositories": [result]}))
        histories = branch_context(self.state)["repositories"][0]["histories"]
        self.assertEqual({entry["name"] for entry in histories}, {"项目/develop", "feature/项目-20261117-专题"})
        result["candidates"] = []
        self.engine._complete_branches(dumps({"repositories": [result]}))
        self.assertEqual(self.state["branch_resolution"]["repositories"][0]["branch"], "项目/develop")

    def test_idle_evidence_redacts_xml_and_assignment_credentials(self):
        text = '<password>example-hidden</password>\n<property name="token" value="example-token"/>\npassword=example-assignment'
        cleaned = redact_evidence(text)
        self.assertNotIn("example-hidden", cleaned)
        self.assertNotIn("example-token", cleaned)
        self.assertNotIn("example-assignment", cleaned)
