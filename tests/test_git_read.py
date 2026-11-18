"""只读 Git 工具的版本准确性、分页、权限和工作区隔离。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from workflow.gitops import git
from workflow.git_read import GIT_READ_TOOLS
from workflow.mcp_server import BRANCH_COMPARE_TOOL, TOOLS, Tools
from workflow.rpc import thread_parameters
from workflow.settings import WorkflowError
from workflow.store import SessionStore


class GitReadChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        local = patch("workflow.store.LOCAL", self.root / "local")
        local.start()
        self.addCleanup(local.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "历史检查")
        git(self.repo, "config", "user.email", "history@example.invalid")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "src").mkdir()
        self.path = self.repo / "src" / "业务.txt"
        self.path.write_text("\n".join(f"旧内容 {i}" for i in range(1, 604)), encoding="utf-8")
        (self.repo / "已删除.txt").write_text("仅历史存在", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "初始化历史", "-m", "保留完整提交说明")
        self.old = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "feature/历史")
        git(self.repo, "update-ref", "refs/remotes/origin/历史", self.old)
        git(self.repo, "tag", "v1")
        self.path.write_text("当前已提交内容", encoding="utf-8")
        (self.repo / "已删除.txt").unlink()
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "依赖.txt").write_text("排除内容", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "更新当前版本")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.path.write_text("本机暂存内容", encoding="utf-8")
        git(self.repo, "add", ".")
        self.path.write_text("本机未提交内容", encoding="utf-8")
        self.store = SessionStore()
        self.state = self.store.new({"kind": "task", "id": 1, "title": "历史查询"})
        self.state["repos"] = [{"name": "前端", "path": str(self.repo), "baseline": self.head}]
        self.store.save(self.state)
        self.tools = Tools(self.state["id"])

    def call(self, name, **arguments):
        return self.tools.call(name, {"repository": "前端", **arguments})

    def test_file_reads_branch_remote_tag_and_commit_without_current_version(self):
        for ref in ("feature/历史", "origin/历史", "v1", self.old):
            with self.subTest(ref=ref):
                result = self.call("git_file", ref=ref, path="src/业务.txt", start_line=500, line_count=2)
                self.assertEqual(result["commit"], self.old)
                self.assertEqual(result["text"], "500: 旧内容 500\n501: 旧内容 501")
                self.assertEqual(result["next_line"], 502)
                self.assertNotIn("version", result)
        self.assertEqual(self.call("git_file", ref=self.old, path="已删除.txt")["text"], "1: 仅历史存在")
        with self.assertRaisesRegex(WorkflowError, "不存在"):
            self.call("git_file", ref="HEAD", path="已删除.txt")
        with self.assertRaisesRegex(WorkflowError, "普通"):
            self.call("git_file", ref=self.old, path="src")

    def test_history_preserves_messages_and_paginates_at_fixed_commit(self):
        result = self.call("git_log", limit=1)
        self.assertEqual([item["commit"] for item in result["commits"]], [self.head])
        self.assertEqual(result["next_offset"], 1)
        last = self.call("git_log", ref=result["commit"], offset=1, limit=1)
        self.assertIsNone(last["next_offset"])
        self.assertEqual(last["commits"][0]["message"], "初始化历史\n\n保留完整提交说明")
        self.assertEqual(last["commits"][0]["parents"], [])
        self.assertEqual(len(self.call("git_log", path="src/业务.txt")["commits"]), 2)
        self.assertEqual(self.call("git_log", ref=self.old, path="不存在.txt")["commits"], [])

    def test_tree_lists_historical_paths_and_excludes_protected_directories(self):
        first = self.call("git_files", ref=self.old, limit=1)
        last = self.call("git_files", ref=first["commit"], offset=first["next_offset"], limit=1)
        self.assertIsNone(last["next_offset"])
        self.assertEqual({item["path"] for item in first["files"] + last["files"]}, {"src/业务.txt", "已删除.txt"})
        current = self.call("git_files", ref="HEAD")
        self.assertEqual([item["path"] for item in current["files"]], ["src/业务.txt"])
        self.assertEqual(self.call("git_files", ref=self.old, path="src")["total_files"], 1)

    def test_diff_is_between_commits_and_does_not_execute_external_drivers(self):
        git(self.repo, "config", "diff.external", "不存在的外部差异程序")
        result = self.call("git_diff", base_ref=self.old, ref="HEAD", path="src/业务.txt", line_count=500)
        self.assertEqual(result["base_commit"], self.old)
        self.assertIn("-旧内容 1", result["text"])
        self.assertIsNotNone(result["next_line"])
        last = self.call("git_diff", base_ref=self.old, ref=result["commit"], path="src/业务.txt",
                         start_line=result["next_line"], line_count=500)
        self.assertIn("+当前已提交内容", last["text"])
        self.assertNotIn("本机", result["text"] + last["text"])
        whole = self.call("git_diff", base_ref=self.old, ref="HEAD")
        self.assertNotIn("node_modules/依赖.txt", whole["files"])
        self.assertNotIn("排除内容", whole["text"])
        self.assertEqual(self.call("git_diff", base_ref="HEAD", ref="HEAD")["text"], "")

    def test_all_queries_preserve_worktree_index_branch_and_session(self):
        before = {"head": git(self.repo, "symbolic-ref", "HEAD"), "status": git(self.repo, "status", "--porcelain"),
                  "index": (self.repo / ".git" / "index").read_bytes(), "file": self.path.read_bytes(),
                  "state": self.store.load(self.state["id"])}
        self.call("git_log")
        self.call("git_files", ref=self.old)
        self.call("git_file", ref=self.old, path="src/业务.txt")
        self.call("git_diff", base_ref=self.old, ref="HEAD")
        self.assertEqual(git(self.repo, "symbolic-ref", "HEAD"), before["head"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), before["status"])
        self.assertEqual((self.repo / ".git" / "index").read_bytes(), before["index"])
        self.assertEqual(self.path.read_bytes(), before["file"])
        self.assertEqual(self.store.load(self.state["id"]), before["state"])

    def test_rejects_unbound_repository_bad_ref_path_and_pagination(self):
        with self.assertRaisesRegex(WorkflowError, "不属于"):
            self.tools.call("git_log", {"repository": "其他仓库"})
        for ref in ("不存在的分支", "--all", "", "HEAD:src/业务.txt"):
            with self.subTest(ref=ref), self.assertRaises(WorkflowError):
                self.call("git_log", ref=ref)
        for path in ("../外部.txt", "C:/外部.txt", "/外部.txt", ".git/config", "node_modules/依赖.txt"):
            for tool in GIT_READ_TOOLS:
                with self.subTest(path=path, tool=tool), self.assertRaises(WorkflowError):
                    self.call(tool, ref="HEAD", base_ref=self.old, path=path)
        for arguments in ({"limit": 101}, {"offset": -1}, {"limit": True}):
            with self.subTest(arguments=arguments), self.assertRaises(WorkflowError):
                self.call("git_log", **arguments)
        for arguments in ({"start_line": 0}, {"line_count": 501}, {"line_count": True}):
            with self.subTest(arguments=arguments), self.assertRaises(WorkflowError):
                self.call("git_file", ref="HEAD", path="src/业务.txt", **arguments)

    def test_paths_are_literal_and_binary_or_symlink_files_are_rejected(self):
        self.assertEqual(self.call("git_log", path="src/*.txt")["commits"], [])
        (self.repo / "二进制.dat").write_bytes(b"a\0b")
        git(self.repo, "add", ".")
        blob = git(self.repo, "hash-object", "-w", "--stdin", input_data="src/业务.txt".encode("utf-8"))
        git(self.repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},链接.txt")
        git(self.repo, "commit", "-m", "加入不可读文件")
        for path in ("二进制.dat", "链接.txt"):
            with self.subTest(path=path), self.assertRaises(WorkflowError):
                self.call("git_file", ref="HEAD", path=path)

    def test_read_tools_are_registered_and_exposed_to_normal_threads(self):
        state = {**self.state, "phase": "plan", "codex": {"reasoning_effort": "high"}}
        enabled = thread_parameters(state, "模拟模型", {})["config"]["mcp_servers"]["workflow"]["enabled_tools"]
        self.assertTrue(GIT_READ_TOOLS.issubset(enabled))
        definitions = {name: definition for name, _, definition in TOOLS}
        self.assertTrue(GIT_READ_TOOLS.issubset(definitions))
        self.assertEqual(set(enabled), set(definitions) - {BRANCH_COMPARE_TOOL})
