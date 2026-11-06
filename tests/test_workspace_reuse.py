"""用临时 Git 仓库检查目录复用、并发占用和本机改动保留。"""

from concurrent.futures import ThreadPoolExecutor
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.gitops import acquire_workspace, carry_checkout, changed_paths, cleanup_workspaces, commit, confirm_checkout, differences, git, manifest, prepare, working_paths, worktrees
from workflow.engine import Workflow
from workflow.mcp_server import Tools
from workflow.settings import ROOT, WorkflowError
from workflow.store import SessionStore


class WorkspaceReuseChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patched = patch("workflow.store.LOCAL", self.root / "local")
        patched.start()
        self.addCleanup(patched.stop)
        self.store = SessionStore()
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "主分支")
        git(self.source, "config", "user.name", "目录复用检查")
        git(self.source, "config", "user.email", "workspace@example.invalid")
        for name in ("本机配置.txt", "已暂存配置.txt", "任务.txt"):
            (self.source / name).write_text("初始内容\n", encoding="utf-8")
        git(self.source, "add", ".")
        git(self.source, "commit", "-m", "初始化脱敏资料")
        self.state = self.new_session(1)

    def new_session(self, number):
        return self.store.new({"kind": "task", "id": number, "title": "脱敏任务"})

    def selection(self, branch="主分支", create=False, track=False):
        return {"name": "前端", "role": "frontend", "path": str(self.source), "branch": branch,
                "base": ("refs/remotes/origin/" if create and track else "refs/heads/") + branch,
                "create": create, "track": track}

    def acquire(self, branch="主分支", state=None, create=False, track=False):
        state = self.state if state is None else state
        return acquire_workspace(self.selection(branch, create, track),
            self.store.directory(state["id"]) / "workspace" / "repo-1", self.store, state)

    def local_changes(self):
        (self.source / "本机配置.txt").write_text("本机保留配置\n", encoding="utf-8")
        (self.source / "已暂存配置.txt").write_text("已暂存保留配置\n", encoding="utf-8")
        git(self.source, "add", "已暂存配置.txt")
        (self.source / "已暂存配置.txt").write_text("同时保留未暂存的本机配置\n", encoding="utf-8")
        (self.source / "本机未跟踪.txt").write_text("本机资料\n", encoding="utf-8")
        return {name: (self.source / name).read_bytes() for name in
                ("本机配置.txt", "已暂存配置.txt", "本机未跟踪.txt")}

    def test_matching_branch_reuses_original_directory_and_preserves_local_changes(self):
        originals = self.local_changes()
        index = git(self.source, "diff", "--cached", "--binary", binary=True)
        record = self.acquire()
        self.assertEqual(record["path"], str(self.source))
        self.assertFalse(record["workspace_owned"])
        self.assertEqual(record["workspace_mode"], "reused")
        self.assertEqual(set(record["preserved_paths"]), set(originals))
        self.assertEqual(len(worktrees(self.source)), 1)
        self.assertEqual(git(self.source, "diff", "--cached", "--binary", binary=True), index)
        self.assertEqual(self.store.load(self.state["id"])["repos"], [record])

    def test_idle_original_directory_switches_and_carries_local_changes(self):
        git(self.source, "branch", "项目分支")
        originals = self.local_changes()
        record = self.acquire("项目分支")
        self.assertEqual(record["workspace_mode"], "switched")
        self.assertEqual(git(self.source, "branch", "--show-current"), "项目分支")
        self.assertEqual(set(record["preserved_paths"]), set(originals))
        for name, content in originals.items():
            self.assertEqual((self.source / name).read_bytes(), content)
        self.assertEqual(len(worktrees(self.source)), 1)

    def test_remote_branch_is_created_with_tracking_in_idle_original_directory(self):
        git(self.source, "config", "remote.origin.url", str(self.root / "unused-remote.git"))
        git(self.source, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(self.source, "update-ref", "refs/remotes/origin/远程项目", git(self.source, "rev-parse", "HEAD"))
        record = self.acquire("远程项目", create=True, track=True)
        self.assertEqual(record["path"], str(self.source))
        self.assertEqual(git(self.source, "rev-parse", "--abbrev-ref", "@{upstream}"), "origin/远程项目")

    def test_new_worktree_is_created_when_no_directory_is_idle(self):
        self.acquire()
        git(self.source, "branch", "其他项目")
        other = self.new_session(2)
        record = self.acquire("其他项目", other)
        self.assertTrue(record["workspace_owned"])
        self.assertEqual(record["workspace_mode"], "created")
        self.assertEqual(git(self.source, "branch", "--show-current"), "主分支")
        self.assertEqual(git(record["path"], "branch", "--show-current"), "其他项目")

    def test_paused_session_retains_exclusive_ownership(self):
        self.acquire()
        self.state.update(stage="已暂停")
        self.store.save(self.state)
        other = self.new_session(2)
        with self.assertRaisesRegex(WorkflowError, self.state["id"]):
            self.acquire(state=other)
        self.assertFalse(other["repos"])

    def test_completed_session_releases_original_directory(self):
        self.acquire()
        self.state.update(phase="complete", stage="已完成（静态审查）")
        self.store.save(self.state)
        git(self.source, "branch", "新项目")
        record = self.acquire("新项目", self.new_session(2))
        self.assertEqual(record["path"], str(self.source))
        self.assertEqual(record["workspace_mode"], "switched")

    def test_completed_managed_directory_is_used_before_creating_another(self):
        self.acquire()
        git(self.source, "branch", "原项目")
        completed = self.new_session(2)
        old = self.acquire("原项目", completed)
        completed.update(phase="complete", stage="已完成（静态审查）")
        self.store.save(completed)
        git(self.source, "branch", "新项目")
        current = self.new_session(3)
        record = self.acquire("新项目", current)
        self.assertEqual(record["path"], old["path"])
        self.assertFalse(record["workspace_owned"])
        self.assertEqual(len(worktrees(self.source)), 2)
        with self.assertRaisesRegex(WorkflowError, current["id"]):
            cleanup_workspaces(self.store, completed, [self.selection()])
        cleanup_workspaces(self.store, current, [self.selection()])
        self.store.remove(current["id"])
        self.assertTrue(Path(old["path"]).exists())
        cleanup_workspaces(self.store, completed, [self.selection()])
        self.assertFalse(Path(old["path"]).exists())

    def test_worktree_belonging_to_an_external_tool_is_not_switched(self):
        git(self.source, "branch", "外部工具分支")
        external = prepare(self.source, self.root / "external", "外部工具分支", "refs/heads/外部工具分支", False)
        self.acquire()
        git(self.source, "branch", "新项目")
        record = self.acquire("新项目", self.new_session(2))
        self.assertEqual(record["workspace_mode"], "created")
        self.assertEqual(git(external["path"], "branch", "--show-current"), "外部工具分支")

    def test_switch_conflict_stops_without_discarding_or_stashing_changes(self):
        git(self.source, "switch", "-c", "冲突分支")
        (self.source / "本机配置.txt").write_text("目标分支配置\n", encoding="utf-8")
        git(self.source, "commit", "-am", "目标分支配置")
        git(self.source, "switch", "主分支")
        (self.source / "本机配置.txt").write_text("原目录保留配置\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.acquire("冲突分支")
        self.assertEqual(git(self.source, "branch", "--show-current"), "主分支")
        self.assertEqual((self.source / "本机配置.txt").read_text(encoding="utf-8"), "原目录保留配置\n")
        self.assertFalse(git(self.source, "stash", "list"))
        self.assertFalse(self.state["repos"])
        self.assertEqual(len(worktrees(self.source)), 1)

    def checkout_request(self, overlap=False):
        original = "".join(f"第{index}行\n" for index in range(12))
        file = self.source / "本机配置.txt"
        file.write_text(original, encoding="utf-8")
        git(self.source, "commit", "-am", "初始化多行配置")
        git(self.source, "switch", "-c", "项目分支")
        target = original.replace("第0行" if overlap else "第11行", "目标分支配置")
        file.write_text(target, encoding="utf-8")
        git(self.source, "commit", "-am", "目标项目配置")
        git(self.source, "switch", "主分支")
        local = original.replace("第0行", "本机保留配置")
        file.write_text(local, encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.acquire("项目分支")
        self.state.update(stage="准备工作区失败", selections=[self.selection("项目分支")], supplement="",
                          snapshot={"sources": [], "materials": []})
        self.store.save(self.state)
        return local, target

    def test_smart_checkout_merges_selected_file_and_continues_current_session(self):
        local, target = self.checkout_request()
        (self.source / "已暂存配置.txt").write_text("其他已暂存改动\n", encoding="utf-8")
        git(self.source, "add", "已暂存配置.txt")
        (self.source / "已暂存配置.txt").write_text("其他未暂存改动\n", encoding="utf-8")
        (self.source / "其他.txt").write_text("未选本机资料\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.acquire("项目分支")
        index = git(self.source, "diff", "--cached", "--binary", binary=True)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, lambda *args: None, self.store)
        self.addCleanup(flow.close)
        flow.state, flow._start_turn = self.state, Mock()
        flow.smart_checkout(["本机配置.txt"])
        flow._start_turn.assert_called_once()
        request = self.state["checkout_request"]
        self.assertEqual(request["status"], "restored")
        self.assertEqual(git(self.source, "branch", "--show-current"), "项目分支")
        self.assertEqual((self.source / "本机配置.txt").read_text(encoding="utf-8"),
                         target.replace("第0行", "本机保留配置"))
        self.assertEqual(git(self.source, "show", request["stash"] + ":本机配置.txt"), local.strip())
        self.assertEqual(git(self.source, "diff", "--cached", "--binary", binary=True), index)
        self.assertEqual((self.source / "已暂存配置.txt").read_text(encoding="utf-8"), "其他未暂存改动\n")
        self.assertEqual((self.source / "其他.txt").read_text(encoding="utf-8"), "未选本机资料\n")
        record = self.state["repos"][0]
        self.assertEqual(record["checkout_backup"], request["stash"])
        self.assertTrue({"本机配置.txt", "已暂存配置.txt", "其他.txt"}.issubset(record["preserved_paths"]))
        self.assertFalse(manifest([record]))
        (self.source / "任务.txt").write_text("需求修改\n", encoding="utf-8")
        commit(record, "fix: 当前需求", manifest([record]))
        self.assertEqual(git(self.source, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "任务.txt")
        self.assertEqual(git(self.source, "diff", "--cached", "--binary", binary=True), index)

    def test_old_checkout_failure_retries_saved_branches_without_reidentification(self):
        self.checkout_request()
        self.state.pop("checkout_request")
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, lambda *args: None, self.store)
        self.addCleanup(flow.close)
        flow.state, flow._resolve_branches = self.state, Mock()
        with self.assertRaises(WorkflowError):
            flow.start_plan("继续当前需求")
        flow._resolve_branches.assert_not_called()
        self.assertEqual(self.state["checkout_request"]["status"], "waiting")
        self.assertEqual(self.state["selections"], [self.selection("项目分支")])

    def test_smart_checkout_restores_selected_staged_and_unstaged_changes(self):
        local, target = self.checkout_request()
        git(self.source, "add", "本机配置.txt")
        (self.source / "本机配置.txt").write_text(local.replace("第1行", "本机未暂存配置"), encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.acquire("项目分支")
        carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        self.assertEqual(git(self.source, "show", ":本机配置.txt"),
                         target.replace("第0行", "本机保留配置").strip())
        self.assertEqual((self.source / "本机配置.txt").read_text(encoding="utf-8"),
                         target.replace("第0行", "本机保留配置").replace("第1行", "本机未暂存配置"))

    def test_smart_checkout_conflict_keeps_backup_and_waits_for_explicit_resolution(self):
        local, target = self.checkout_request(overlap=True)
        with self.assertRaisesRegex(WorkflowError, "备份保留"):
            carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        request = self.state["checkout_request"]
        self.assertEqual(request["status"], "restore_failed")
        self.assertEqual(git(self.source, "show", request["stash"] + ":本机配置.txt"), local.strip())
        self.assertTrue(git(self.source, "ls-files", "--unmerged"))
        self.assertFalse(self.state["repos"])
        with self.assertRaisesRegex(WorkflowError, "解决合并冲突"):
            confirm_checkout(self.source, self.store, self.state)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, lambda *args: None, self.store)
        self.addCleanup(flow.close)
        flow.state, flow._start_turn = self.store.load(self.state["id"]), Mock()
        with self.assertRaisesRegex(WorkflowError, "智能切换尚未完成"):
            flow.prepare_and_plan(flow.state["selections"], "")
        flow._start_turn.assert_not_called()
        (self.source / "本机配置.txt").write_text(local, encoding="utf-8")
        git(self.source, "add", "本机配置.txt")
        flow.checkout_resolved()
        flow._start_turn.assert_called_once()
        self.assertIn("本机配置.txt", flow.state["repos"][0]["preserved_paths"])
        self.assertFalse(manifest(flow.state["repos"]))

    def test_smart_checkout_rejects_unselected_unknown_duplicate_and_changed_files(self):
        self.checkout_request()
        for paths in ([], ["任务.txt"], ["本机配置.txt", "本机配置.txt"]):
            with self.subTest(paths=paths), self.assertRaisesRegex(WorkflowError, "明确选择"):
                carry_checkout(self.source, self.store, self.state, paths)
        (self.source / "本机配置.txt").write_text("确认后又修改了配置\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "本机改动已变化"):
            carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        self.assertFalse(git(self.source, "stash", "list"))
        self.assertEqual(git(self.source, "branch", "--show-current"), "主分支")

    def test_checkout_confirmation_distinguishes_branch_conflict_and_changed_baseline(self):
        local, _ = self.checkout_request(overlap=True)
        request = self.state["checkout_request"]
        request["status"] = "switch_failed"
        with self.assertRaisesRegex(WorkflowError, "当前分支为 主分支，目标分支为 项目分支"):
            confirm_checkout(self.source, self.store, self.state)
        request["status"] = "waiting"
        with self.assertRaisesRegex(WorkflowError, "备份保留"):
            carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        with self.assertRaisesRegex(WorkflowError, "仓库仍有未解决的合并冲突"):
            confirm_checkout(self.source, self.store, self.state)
        (self.source / "本机配置.txt").write_text(local, encoding="utf-8")
        git(self.source, "add", "本机配置.txt")
        (self.source / "任务.txt").write_text("目标分支新提交\n", encoding="utf-8")
        git(self.source, "add", "任务.txt")
        git(self.source, "commit", "--only", "-m", "目标分支更新", "--", "任务.txt")
        baseline, head = request["baseline"], git(self.source, "rev-parse", "HEAD")
        self.assertFalse(confirm_checkout(self.source, self.store, self.state))
        self.assertEqual(request["baseline_change"], {"previous": baseline, "current": head, "confirmed": False})
        self.assertEqual(request["baseline"], baseline)
        self.assertEqual(request["status"], "restore_failed")

    def test_smart_checkout_does_not_shelve_unselected_blocking_changes(self):
        self.checkout_request()
        git(self.source, "switch", "--discard-changes", "项目分支")
        (self.source / "任务.txt").write_text("项目任务配置\n", encoding="utf-8")
        git(self.source, "commit", "-am", "项目任务配置")
        git(self.source, "switch", "主分支")
        (self.source / "本机配置.txt").write_text("本机保留配置\n", encoding="utf-8")
        (self.source / "任务.txt").write_text("未勾选的本机任务改动\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.acquire("项目分支")
        with self.assertRaisesRegex(WorkflowError, "备份保留"):
            carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        request = self.state["checkout_request"]
        self.assertEqual(request["status"], "switch_failed")
        self.assertEqual(git(self.source, "branch", "--show-current"), "主分支")
        self.assertEqual((self.source / "任务.txt").read_text(encoding="utf-8"), "未勾选的本机任务改动\n")
        self.assertEqual(git(self.source, "show", request["stash"] + ":本机配置.txt"), "本机保留配置")
        with self.assertRaisesRegex(WorkflowError, "目标分支"):
            confirm_checkout(self.source, self.store, self.state)

    def updated_checkout(self):
        local, _ = self.checkout_request(overlap=True)
        with self.assertRaisesRegex(WorkflowError, "备份保留"):
            carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        (self.source / "本机配置.txt").write_text(local, encoding="utf-8")
        git(self.source, "add", "本机配置.txt")
        (self.source / "任务.txt").write_text("外部更新后的内容\n", encoding="utf-8")
        git(self.source, "add", "任务.txt")
        git(self.source, "commit", "--only", "-m", "外部更新", "--", "任务.txt")
        return git(self.source, "rev-parse", "HEAD")

    def test_preparation_previews_new_baseline_and_continues_only_after_confirmation(self):
        head = self.updated_checkout()
        self.state["plan_started_at"] = "准备工作区前已锁定项目"
        self.state["error"] = "原基准已变化"
        request = self.state["checkout_request"]
        previous, stash = request["baseline"], request["stash"]
        index = git(self.source, "diff", "--cached", "--binary", binary=True)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, lambda *args: None, self.store)
        self.addCleanup(flow.close)
        flow.state, flow._start_turn = self.state, Mock()
        flow.checkout_resolved()
        flow._start_turn.assert_not_called()
        self.assertEqual(request["baseline"], previous)
        self.assertEqual(request["status"], "restore_failed")
        self.assertEqual(flow.state["error"], "")
        saved = self.store.load(self.state["id"])
        self.assertEqual(saved["checkout_request"]["baseline_change"],
                         {"previous": previous, "current": head, "confirmed": False})
        flow.checkout_resolved(head)
        flow._start_turn.assert_called_once()
        self.assertEqual(request["baseline"], head)
        self.assertTrue(request["baseline_change"]["confirmed"])
        self.assertEqual(flow.state["repos"][0]["baseline"], head)
        self.assertIn("本机配置.txt", flow.state["repos"][0]["preserved_paths"])
        self.assertEqual(flow.state["repos"][0]["checkout_backup"], stash)
        self.assertFalse(manifest(flow.state["repos"]))
        self.assertEqual(git(self.source, "diff", "--cached", "--binary", binary=True), index)
        self.assertEqual(git(self.source, "rev-parse", "refs/stash"), stash)

    def test_new_baseline_confirmation_rechecks_preview_when_head_advances_again(self):
        first = self.updated_checkout()
        request = self.state["checkout_request"]
        previous = request["baseline"]
        with self.assertRaisesRegex(WorkflowError, "先核对页面"):
            confirm_checkout(self.source, self.store, self.state, first)
        self.assertFalse(confirm_checkout(self.source, self.store, self.state))
        (self.source / "任务.txt").write_text("再次更新后的内容\n", encoding="utf-8")
        git(self.source, "add", "任务.txt")
        git(self.source, "commit", "--only", "-m", "再次外部更新", "--", "任务.txt")
        second = git(self.source, "rev-parse", "HEAD")
        self.assertFalse(confirm_checkout(self.source, self.store, self.state, first))
        self.assertEqual(request["baseline"], previous)
        self.assertEqual(request["baseline_change"]["current"], second)
        self.assertFalse(request["baseline_change"]["confirmed"])
        with self.assertRaisesRegex(WorkflowError, "先核对页面"):
            confirm_checkout(self.source, self.store, self.state, first)
        self.assertTrue(confirm_checkout(self.source, self.store, self.state, second))
        self.assertEqual(request["baseline"], second)

    def test_new_baseline_is_rejected_after_actual_planning_implementation_or_review(self):
        self.updated_checkout()
        previous = self.state["checkout_request"]["baseline"]
        for key, value in (("stage", "已暂停"), ("phase", "implement"), ("phase", "review"),
                           ("prepare_complete", True), ("thread_id", "已启动计划线程"),
                           ("plan_round", {"number": 1}), ("plan", "已有计划"),
                           ("approved_plan", "已确认计划"), ("reviews", [{}]), ("commits", {"前端": "已提交"})):
            original = self.state.get(key)
            self.state[key] = value
            with self.subTest(key=key, value=value), self.assertRaisesRegex(WorkflowError, "仅尚未开始实际计划"):
                confirm_checkout(self.source, self.store, self.state)
            self.state[key] = original
        self.assertEqual(self.state["checkout_request"]["baseline"], previous)
        self.assertNotIn("baseline_change", self.state["checkout_request"])

    def test_new_baseline_rejects_rollback_and_diverged_history(self):
        self.updated_checkout()
        request = self.state["checkout_request"]
        previous = request["baseline"]
        git(self.source, "reset", "--soft", previous + "^")
        with self.assertRaisesRegex(WorkflowError, "不是旧基准的快进更新"):
            confirm_checkout(self.source, self.store, self.state)
        git(self.source, "commit", "-m", "分叉后的提交")
        with self.assertRaisesRegex(WorkflowError, "不是旧基准的快进更新"):
            confirm_checkout(self.source, self.store, self.state)
        self.assertEqual(request["baseline"], previous)
        self.assertNotIn("baseline_change", request)

    def test_returning_to_original_baseline_invalidates_pending_new_baseline(self):
        head = self.updated_checkout()
        request = self.state["checkout_request"]
        previous = request["baseline"]
        self.assertFalse(confirm_checkout(self.source, self.store, self.state))
        git(self.source, "reset", "--soft", previous)
        with self.assertRaisesRegex(WorkflowError, "页面展示的新基准已失效"):
            confirm_checkout(self.source, self.store, self.state, head)
        self.assertTrue(confirm_checkout(self.source, self.store, self.state))
        self.assertEqual(request["baseline"], previous)
        self.assertNotIn("baseline_change", request)

    def test_smart_checkout_preserves_untracked_backup_when_target_already_has_file(self):
        git(self.source, "switch", "-c", "项目分支")
        file = self.source / "本机[1].txt"
        file.write_text("目标分支文件\n", encoding="utf-8")
        git(self.source, "--literal-pathspecs", "add", "--", file.name)
        git(self.source, "commit", "-m", "目标分支新增文件")
        git(self.source, "switch", "主分支")
        file.write_text("本机未跟踪文件\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.acquire("项目分支")
        with self.assertRaisesRegex(WorkflowError, "备份保留"):
            carry_checkout(self.source, self.store, self.state, [file.name])
        request = self.state["checkout_request"]
        self.assertEqual(request["status"], "restore_failed")
        self.assertEqual(file.read_text(encoding="utf-8"), "目标分支文件\n")
        self.assertEqual(git(self.source, "show", request["stash"] + "^3:" + file.name), "本机未跟踪文件")

    def test_smart_checkout_keeps_preexisting_stash_and_stops_for_hooks(self):
        (self.source / "任务.txt").write_text("用户原有备份\n", encoding="utf-8")
        git(self.source, "stash", "push", "-m", "用户原有备份")
        original_stash = git(self.source, "rev-parse", "refs/stash")
        self.checkout_request()
        hook = self.source / ".git" / "hooks" / "post-checkout"
        hook.write_text("不应执行此文件", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "Git 钩子"):
            carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        self.assertEqual(git(self.source, "rev-parse", "refs/stash"), original_stash)
        hook.unlink()
        carry_checkout(self.source, self.store, self.state, ["本机配置.txt"])
        self.assertEqual(git(self.source, "rev-parse", "stash@{1}"), original_stash)

    def test_review_and_commit_include_only_task_files_and_keep_existing_index(self):
        originals = self.local_changes()
        index = git(self.source, "diff", "--cached", "--binary", binary=True)
        record = self.acquire()
        baseline = record["baseline"]
        (self.source / "任务.txt").write_text("本次任务修改\n", encoding="utf-8")
        (self.source / "任务新增.txt").write_text("任务新增内容\n", encoding="utf-8")
        self.assertEqual(changed_paths(record), ["任务.txt", "任务新增.txt"])
        diff = differences(record)
        self.assertNotIn("本机配置.txt", diff["patch"])
        self.assertNotIn("已暂存配置.txt", diff["patch"])
        self.assertEqual(diff["untracked"], ["任务新增.txt"])
        result = commit(record, "feat(workflow): 脱敏任务", manifest([record]))
        self.assertEqual(result, git(self.source, "rev-parse", "HEAD"))
        committed = git(self.source, "diff", "--name-only", "-z", baseline, "HEAD", binary=True)
        self.assertEqual({value.decode("utf-8") for value in committed.split(b"\0") if value}, {"任务.txt", "任务新增.txt"})
        self.assertEqual(git(self.source, "diff", "--cached", "--binary", binary=True), index)
        self.assertFalse(working_paths(record))
        for name, content in originals.items():
            self.assertEqual((self.source / name).read_bytes(), content)

    def test_local_changes_alone_do_not_create_a_task_commit_or_diff(self):
        self.local_changes()
        record = self.acquire()
        self.assertFalse(manifest([record]))
        self.assertEqual(differences(record)["patch"], "")
        self.assertEqual(differences(record)["untracked"], [])
        self.assertIsNone(commit(record, "不应提交本机改动", {}))
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), record["baseline"])

    def test_file_tools_refuse_to_modify_preserved_files_including_path_aliases(self):
        originals = self.local_changes()
        self.acquire()
        self.state.update(stage="实施中", approved_hash="已确认")
        self.store.save(self.state)
        tools = Tools(self.state["id"])
        for name, arguments in (
            ("write_file", {"content": "不能覆盖"}),
            ("replace_text", {"old": "本机", "new": "不能替换"}),
            ("delete_file", {}),
        ):
            with self.subTest(tool=name), self.assertRaisesRegex(WorkflowError, "本机保留改动"):
                tools.call(name, {"repository": "前端", "path": "不存在目录/../本机配置.txt", **arguments})
        self.assertEqual((self.source / "本机配置.txt").read_bytes(), originals["本机配置.txt"])

    def test_deleting_borrowing_session_keeps_original_directory_and_dirty_files(self):
        originals = self.local_changes()
        record = self.acquire()
        self.state["selections"] = [self.selection()]
        cleanup_workspaces(self.store, self.state, [self.selection()])
        self.store.remove(self.state["id"])
        self.assertTrue((self.source / ".git").is_dir())
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), record["baseline"])
        for name, content in originals.items():
            self.assertEqual((self.source / name).read_bytes(), content)

    def test_simultaneous_sessions_cannot_acquire_the_same_directory(self):
        states = [self.state, self.new_session(2)]
        def acquire(state):
            try:
                return self.acquire(state=state)
            except WorkflowError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(acquire, states))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, str) and "已被占用" in result for result in results), 1)
        self.assertEqual(sum(len(state["repos"]) for state in self.store.sessions()), 1)

    def test_renamed_local_files_are_both_excluded_from_task_changes(self):
        git(self.source, "mv", "本机配置.txt", "重命名配置.txt")
        record = self.acquire()
        self.assertTrue({"本机配置.txt", "重命名配置.txt"}.issubset(record["preserved_paths"]))
        self.assertFalse(changed_paths(record))

    def test_partial_commit_can_finish_without_committing_preserved_local_changes(self):
        originals = self.local_changes()
        record = self.acquire()
        (self.source / "任务.txt").write_text("任务代码\n", encoding="utf-8")
        first_commit = commit(record, "feat(workflow): 第一仓库", manifest([record]))
        other = self.root / "backend"
        git(self.root, "clone", "--local", str(self.source), str(other))
        second = {"name": "后端", "source": str(other), "path": str(other), "branch": "主分支",
                  "baseline": git(other, "rev-parse", "HEAD"), "base": "refs/heads/主分支"}
        self.state.update(phase="review", stage="部分提交", repos=[record, second],
                          commits={"前端": first_commit}, review_manifest={}, reviews=[{"number": 1, "result": {"findings": []}}],
                          commit_batch={"delivery_round": 1, "review_number": 1, "manifest": {},
                                        "messages": {"前端": "feat(业务): 完成任务"}})
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, lambda *args: None, store=self.store)
        self.addCleanup(flow.close)
        flow.state = self.state
        flow.finalize([], "", automatic=True)
        self.assertEqual(self.state["phase"], "complete")
        self.assertEqual(self.state["commits"]["后端"], "无代码修改")
        for name, content in originals.items():
            self.assertEqual((self.source / name).read_bytes(), content)
