"""用临时 Git 仓库检查目录复用、并发占用和本机改动保留。"""

from concurrent.futures import ThreadPoolExecutor
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from workflow.gitops import acquire_workspace, changed_paths, cleanup_workspaces, commit, differences, git, manifest, prepare, working_paths, worktrees
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
                          commits={"前端": first_commit}, review_manifest={}, reviews=[{"result": {"findings": []}}])
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, lambda *args: None, store=self.store)
        self.addCleanup(flow.close)
        flow.state = self.state
        flow.finalize([], "", automatic=True)
        self.assertEqual(self.state["phase"], "complete")
        self.assertEqual(self.state["commits"]["后端"], "无代码修改")
        for name, content in originals.items():
            self.assertEqual((self.source / name).read_bytes(), content)
