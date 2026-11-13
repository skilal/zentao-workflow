"""临时 Git 仓库验证共享目录、编辑归属及分片提交，不访问业务仓库。"""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from subprocess import run
from sys import executable
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.gitops import acquire_workspace, cleanup_workspaces, commit, ensure_baseline, git, manifest, repository_lock
from workflow.delivery import previous_task_changes, validate_review_request
from workflow.mcp_server import Tools
from workflow.settings import WorkflowError
from workflow.shared_changes import candidates, initialize, load_record, select_scope, snapshot_review
from workflow.store import SessionStore
from workflow.engine import Workflow
from workflow.settings import ROOT


class SharedWorkspaceChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        mocked = patch("workflow.store.LOCAL", self.root / "state")
        mocked.start()
        self.addCleanup(mocked.stop)
        self.store = SessionStore()
        self.path = self.root / "repo"
        self.path.mkdir()
        git(self.path, "init", "-b", "项目分支")
        git(self.path, "config", "user.name", "共享检查")
        git(self.path, "config", "user.email", "shared@example.invalid")
        git(self.path, "config", "core.autocrlf", "false")
        self.original = "".join(f"原始第{number}行\n" for number in range(20))
        for name in ("共享.txt", "甲.txt", "乙.txt", "人工.txt"):
            (self.path / name).write_text(self.original, encoding="utf-8")
        git(self.path, "add", ".")
        git(self.path, "commit", "-m", "初始化")
        self.first = self.add_session(1)
        self.second = self.add_session(2)

    def add_session(self, number):
        state = self.store.new({"kind": "task", "id": number, "title": "共享开发"})
        selection = {"path": str(self.path), "branch": "项目分支", "base": "refs/heads/项目分支", "create": False,
                     "name": "前端", "role": "frontend"}
        destination = self.store.directory(state["id"]) / "workspace" / "repo-1"
        repo = acquire_workspace(selection, destination, self.store, state)
        plan = "实施共享需求"
        state.update(phase="implement", stage="实施中", approved_plan=plan, approved_hash=sha256(plan.encode()).hexdigest())
        self.store.save(state)
        return state

    def replace(self, state, name, old, new):
        tool = Tools(state["id"])
        current = tool.call("read_file", {"repository": "前端", "path": name})
        return tool.call("replace_text", {"repository": "前端", "path": name, "old": old, "new": new,
                                          "expected_version": current["version"]})

    def review(self, state, include=None):
        repo = state["repos"][0]
        repo.pop("commit_scope", None)
        snapshot_review(repo)
        current = candidates(repo)
        selected = [entry["id"] for entry in current if entry["source"] == "本轮"] if include is None else include
        excluded = [{"id": entry["id"], "reason": "不属于本轮需求"} for entry in current
                    if entry["source"] == "本轮" and entry["id"] not in selected]
        fingerprint = select_scope([repo], [{"repository": "前端", "include": selected, "exclude": excluded}])
        return fingerprint

    def test_same_branch_reuses_directory_and_different_files_commit_independently(self):
        self.assertEqual(self.first["repos"][0]["path"], self.second["repos"][0]["path"])
        self.replace(self.first, "甲.txt", "原始第3行", "甲需求")
        self.replace(self.second, "乙.txt", "原始第7行", "乙需求")
        first_manifest = self.review(self.first)
        second_manifest = self.review(self.second)
        first_commit = commit(self.first["repos"][0], "fix(共享): 甲需求", first_manifest)
        self.assertEqual(git(self.path, "show", "--format=", "--name-only", first_commit), "甲.txt")
        ensure_baseline(self.second["repos"][0])
        second_commit = commit(self.second["repos"][0], "fix(共享): 乙需求", second_manifest)
        self.assertEqual(git(self.path, "show", "--format=", "--name-only", second_commit), "乙.txt")

    def test_adjacent_lines_in_same_git_hunk_are_submitted_separately(self):
        self.replace(self.first, "共享.txt", "原始第3行", "甲需求")
        self.replace(self.second, "共享.txt", "原始第4行", "乙需求")
        original_worktree = (self.path / "共享.txt").read_bytes()
        first_manifest, second_manifest = self.review(self.first), self.review(self.second)
        first_commit = commit(self.first["repos"][0], "fix(共享): 甲片段", first_manifest)
        committed = git(self.path, "show", first_commit + ":共享.txt")
        self.assertIn("甲需求", committed)
        self.assertNotIn("乙需求", committed)
        self.assertEqual((self.path / "共享.txt").read_bytes(), original_worktree)
        self.assertEqual(manifest([self.second["repos"][0]]), second_manifest)
        second_commit = commit(self.second["repos"][0], "fix(共享): 乙片段", second_manifest)
        self.assertIn("乙需求", git(self.path, "show", second_commit + ":共享.txt"))
        self.assertEqual((self.path / "共享.txt").read_bytes(), original_worktree)

    def test_other_staged_and_unstaged_changes_survive_same_file_commit(self):
        manual = self.original.replace("原始第15行", "人工暂存")
        (self.path / "共享.txt").write_text(manual, encoding="utf-8")
        git(self.path, "add", "共享.txt")
        (self.path / "人工.txt").write_text("其他暂存\n", encoding="utf-8")
        git(self.path, "add", "人工.txt")
        self.replace(self.first, "共享.txt", "原始第3行", "本轮需求")
        self.replace(self.second, "共享.txt", "原始第10行", "其他需求")
        working = (self.path / "共享.txt").read_bytes()
        commit(self.first["repos"][0], "fix(共享): 保留暂存", self.review(self.first))
        head = git(self.path, "show", "HEAD:共享.txt")
        staged = git(self.path, "show", ":共享.txt")
        self.assertIn("本轮需求", head)
        self.assertNotIn("人工暂存", head)
        self.assertIn("人工暂存", staged)
        self.assertIn("本轮需求", staged)
        self.assertNotIn("其他需求", staged)
        self.assertEqual(git(self.path, "diff", "--cached", "--name-only").splitlines(), ["人工.txt", "共享.txt"])
        self.assertEqual((self.path / "共享.txt").read_bytes(), working)

    def test_adjacent_insertions_commit_independently_and_keep_source_order(self):
        (self.path / "共享.txt").write_text(self.original.replace("原始第15行", "人工暂存"), encoding="utf-8")
        git(self.path, "add", "共享.txt")
        self.replace(self.first, "共享.txt", "原始第3行\n", "原始第3行\n甲新增\n甲第二行\n")
        self.replace(self.second, "共享.txt", "甲第二行\n", "甲第二行\n乙新增\n")
        working = (self.path / "共享.txt").read_bytes()
        own = [entry["id"] for entry in candidates(self.first["repos"][0]) if entry["source"] == "本轮"]
        first = self.review(self.first, list(reversed(own)))
        second = self.review(self.second)
        commit(self.first["repos"][0], "feat(共享): 甲新增", first)
        text = git(self.path, "show", "HEAD:共享.txt")
        self.assertIn("甲新增\n甲第二行\n", text)
        self.assertNotIn("乙新增", text)
        self.assertEqual(manifest(self.second["repos"]), second)
        commit(self.second["repos"][0], "feat(共享): 乙新增", second)
        self.assertEqual((self.path / "共享.txt").read_bytes(), working)
        self.assertIn("甲第二行\n乙新增\n", git(self.path, "show", "HEAD:共享.txt"))
        self.assertIn("人工暂存", git(self.path, "show", ":共享.txt"))
        self.assertNotIn("人工暂存", git(self.path, "show", "HEAD:共享.txt"))

    def test_stale_version_never_overwrites_other_session(self):
        tool = Tools(self.first["id"])
        stale = tool.call("read_file", {"repository": "前端", "path": "共享.txt"})
        self.replace(self.second, "共享.txt", "原始第4行", "其他需求")
        with self.assertRaisesRegex(WorkflowError, "版本"):
            tool.call("write_file", {"repository": "前端", "path": "共享.txt", "content": self.original,
                                     "expected_version": stale["version"]})
        self.assertIn("其他需求", (self.path / "共享.txt").read_text(encoding="utf-8"))

    def test_existing_line_change_next_to_manual_insertions_is_isolated(self):
        for name, replacement in (("甲.txt", "原始第3行\n人工第一行\n人工第二行\n"),
                                  ("乙.txt", "人工第一行\n人工第二行\n原始第3行\n")):
            with self.subTest(name=name):
                path = self.path / name
                path.write_text(self.original.replace("原始第3行\n", replacement), encoding="utf-8")
                self.first.update(phase="implement", stage="实施中")
                self.first["repos"][0].pop("commit_scope", None)
                self.store.save(self.first)
                self.replace(self.first, path.name, "原始第3行", "本轮修改")
                working = path.read_bytes()
                commit(self.first["repos"][0], "fix(共享): 只修改原有行", self.review(self.first))
                self.assertEqual(git(self.path, "show", "HEAD:" + name), self.original.replace("原始第3行", "本轮修改").strip())
                self.assertEqual(path.read_bytes(), working)

    def test_unequal_replacement_with_repeated_removed_lines_has_unique_ids(self):
        path = self.path / "共享.txt"
        path.write_text("开头\n\n\n\n\n\n末尾\n", encoding="utf-8")
        git(self.path, "add", path.name)
        git(self.path, "commit", "-m", "重复空行基线")
        self.replace(self.first, path.name, "开头\n\n\n\n\n\n末尾\n", "本轮新内容\n")
        fragments = candidates(self.first["repos"][0])
        self.assertEqual(len(fragments), len({entry["id"] for entry in fragments}))
        commit(self.first["repos"][0], "refactor(共享): 替换重复行", self.review(self.first))
        self.assertEqual(git(self.path, "show", "HEAD:共享.txt"), "本轮新内容")

    def test_overlapping_edit_is_reviewed_and_committed_with_unrelated_changes_preserved(self):
        self.replace(self.first, "共享.txt", "原始第3行", "甲需求")
        self.replace(self.first, "共享.txt", "原始第12行", "甲独立需求")
        self.replace(self.second, "共享.txt", "甲需求", "乙需求")
        repo = self.second["repos"][0]
        fragments = candidates(repo)
        own = next(entry for entry in fragments if "乙需求" in entry["new"])
        self.assertEqual(own["source"], "本轮")
        self.assertIn("前端/共享.txt", manifest([repo]))
        working = (self.path / "共享.txt").read_bytes()
        committed = commit(repo, "fix(共享): 接续修改", self.review(self.second))
        text = git(self.path, "show", committed + ":共享.txt")
        self.assertIn("乙需求", text)
        self.assertNotIn("甲独立需求", text)
        self.assertEqual((self.path / "共享.txt").read_bytes(), working)

    def test_foreign_fragment_can_be_included_after_review(self):
        self.replace(self.first, "共享.txt", "原始第3行", "甲需求")
        repo = self.second["repos"][0]
        self.assertEqual(manifest([repo]), {})
        foreign = candidates(repo)[0]
        self.assertEqual(foreign["source"], "其他会话")
        fingerprint = self.review(self.second, [foreign["id"]])
        committed = commit(repo, "fix(共享): 纳入相关修改", fingerprint)
        self.assertIn("甲需求", git(self.path, "show", committed + ":共享.txt"))

    def test_overlap_sources_are_retained_and_require_scope_selection(self):
        self.replace(self.first, "共享.txt", "原始第3行", "甲需求")
        tool = Tools(self.second["id"])
        current = tool.call("read_file", {"repository": "前端", "path": "共享.txt"})
        tool.call("delete_file", {"repository": "前端", "path": "共享.txt", "expected_version": current["version"]})
        repo = self.second["repos"][0]
        fragment = candidates(repo)[0]
        self.assertEqual(fragment["source"], "本轮")
        self.assertEqual(set(fragment["owners"]), {self.first["id"], self.second["id"]})
        snapshot_review(repo)
        with self.assertRaisesRegex(WorkflowError, "本轮修改片段遗漏"):
            select_scope([repo], [{"repository": "前端", "include": [], "exclude": []}])
        committed = commit(repo, "fix(共享): 删除相关文件", self.review(self.second))
        self.assertEqual(git(self.path, "ls-tree", "--name-only", committed, "--", "共享.txt"), "")

    def test_committed_foreign_history_does_not_block_latest_version_write(self):
        self.replace(self.first, "共享.txt", "原始第3行", "甲需求")
        commit(self.first["repos"][0], "fix(共享): 完成甲需求", self.review(self.first))
        tool = Tools(self.second["id"])
        current = tool.call("read_file", {"repository": "前端", "path": "共享.txt"})
        tool.call("write_file", {"repository": "前端", "path": "共享.txt", "expected_version": current["version"],
                                 "content": self.original.replace("原始第3行", "乙需求")})
        committed = commit(self.second["repos"][0], "fix(共享): 接续已提交代码", self.review(self.second))
        self.assertIn("乙需求", git(self.path, "show", committed + ":共享.txt"))

    def test_historical_review_request_keeps_foreign_sources_available(self):
        name = "共同新增.txt"
        Tools(self.first["id"]).call("write_file", {"repository": "前端", "path": name,
            "content": "甲需求\n", "expected_version": "missing"})
        self.replace(self.second, name, "甲需求\n", "甲需求\n乙需求\n")
        self.second["delivery_round"] = 2
        repo = self.second["repos"][0]
        repo.pop("change_record")
        initialize(self.store, self.second, repo)
        followup_scope = dumps(["重新审查并提交", []], ensure_ascii=False)
        self.second.update(phase="followup", stage="续聊处理中", approval_source="followup",
                           followup={"history_index": 0, "request_id": "历史审查", "text": "重新审查并提交", "images": [],
                                     "authorization_hash": sha256(followup_scope.encode()).hexdigest()},
                           history=[{"role": "用户", "text": "重新审查并提交"}])
        self.assertEqual(candidates(repo)[0]["source"], "归属重叠")
        self.assertEqual(previous_task_changes(self.second), [
            {"repository": "前端", "path": name, "unchanged": True}])
        request = validate_review_request(self.second, {"instruction": "重新审查并提交", "commit_message": "",
            "include_paths": [{"repository": "前端", "path": name}]})
        repo["review_paths"] = [name]
        self.assertEqual(manifest([repo]), request["included_manifest"])
        snapshot_review(repo)
        with self.assertRaisesRegex(WorkflowError, "本轮修改片段遗漏"):
            select_scope([repo], [{"repository": "前端", "include": [], "exclude": []}])
        committed = commit(repo, "fix(共享): 纳入历史接续修改", self.review(self.second, [candidates(repo)[0]["id"]]))
        self.assertIn("乙需求", git(self.path, "show", committed + ":" + name))

    def test_manual_changes_can_be_selected_after_review(self):
        (self.path / "甲.txt").write_text(self.original.replace("原始第3行", "人工修复"), encoding="utf-8")
        candidate = candidates(self.first["repos"][0])[0]
        self.assertEqual(candidate["source"], "待判断")
        commit_id = commit(self.first["repos"][0], "fix(共享): 人工修复", self.review(self.first, [candidate["id"]]))
        self.assertIn("人工修复", git(self.path, "show", commit_id + ":甲.txt"))

    def test_changed_selected_fragment_blocks_commit_and_keeps_index(self):
        self.replace(self.first, "共享.txt", "原始第3行", "甲需求")
        fingerprint = self.review(self.first)
        before = git(self.path, "diff", "--cached", "--binary", binary=True)
        file = self.path / "共享.txt"
        file.write_text(file.read_text(encoding="utf-8").replace("甲需求", "人工改写"), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "差异已变化"):
            commit(self.first["repos"][0], "fix(共享): 不应提交", fingerprint)
        self.assertEqual(git(self.path, "diff", "--cached", "--binary", binary=True), before)

    def test_create_delete_and_reverted_changes(self):
        tool = Tools(self.first["id"])
        tool.call("write_file", {"repository": "前端", "path": "新增.txt", "content": "新需求\n", "expected_version": "missing"})
        version = tool.call("read_file", {"repository": "前端", "path": "甲.txt"})["version"]
        tool.call("delete_file", {"repository": "前端", "path": "甲.txt", "expected_version": version})
        self.replace(self.first, "共享.txt", "原始第3行", "临时修改")
        self.replace(self.first, "共享.txt", "临时修改", "原始第3行")
        self.assertEqual(set(manifest([self.first["repos"][0]])), {"前端/新增.txt", "前端/甲.txt"})
        commit(self.first["repos"][0], "fix(共享): 增删", self.review(self.first))
        self.assertEqual(git(self.path, "show", "HEAD:新增.txt"), "新需求")
        self.assertFalse((self.path / "甲.txt").exists())

    def test_review_file_presence_changes_invalidate_commit(self):
        self.replace(self.first, "甲.txt", "原始第3行", "本轮修复")
        context = self.path / "审查上下文.txt"
        for originally_exists in (False, True):
            with self.subTest(originally_exists=originally_exists):
                if originally_exists:
                    context.write_bytes(b"")
                elif context.exists():
                    context.unlink()
                fingerprint = self.review(self.first)
                self.first.update(phase="review", stage="审查中")
                self.store.save(self.first)
                Tools(self.first["id"]).call("read_file", {"repository": "前端", "path": context.name})
                if originally_exists:
                    context.unlink()
                else:
                    context.write_bytes(b"")
                head = git(self.path, "rev-parse", "HEAD")
                index = (self.path / ".git" / "index").read_bytes()
                with self.assertRaisesRegex(WorkflowError, "存在状态已变化"):
                    commit(self.first["repos"][0], "fix(共享): 不应提交", fingerprint)
                self.assertEqual(git(self.path, "rev-parse", "HEAD"), head)
                self.assertEqual((self.path / ".git" / "index").read_bytes(), index)

    def test_branch_switch_and_rewind_remain_blocked(self):
        repo = self.first["repos"][0]
        git(self.path, "switch", "-c", "其他分支")
        with self.assertRaisesRegex(WorkflowError, "分支已改变"):
            ensure_baseline(repo)
        git(self.path, "switch", "项目分支")
        (self.path / "新提交.txt").write_text("新提交", encoding="utf-8")
        git(self.path, "add", "新提交.txt")
        git(self.path, "commit", "-m", "新提交")
        repo["checkout_head"] = git(self.path, "rev-parse", "HEAD")
        git(self.path, "reset", "--hard", "HEAD~1")
        with self.assertRaisesRegex(WorkflowError, "HEAD 已改变"):
            ensure_baseline(repo)

    def test_records_survive_restart_and_new_round_uses_historical_origin(self):
        self.replace(self.first, "甲.txt", "原始第3行", "历史修复")
        restored = self.store.load(self.first["id"])
        self.assertEqual(candidates(restored["repos"][0])[0]["source"], "本轮")
        restored["delivery_round"] = 2
        restored["repos"][0].pop("change_record")
        initialize(self.store, restored, restored["repos"][0])
        self.assertEqual(candidates(restored["repos"][0])[0]["source"], "历史任务")
        self.assertEqual(manifest(restored["repos"]), {})

    def test_existing_review_pipeline_automatically_commits_only_confirmed_scope(self):
        self.replace(self.first, "共享.txt", "原始第3行", "甲需求")
        self.replace(self.second, "共享.txt", "原始第12行", "乙需求")
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, Mock(), store=self.store)
        self.addCleanup(flow.close)
        flow.state = self.first
        self.first.update(snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                          stage="实施中", turn_id="检查回合", max_fix_rounds=3)
        with patch.object(flow, "_start_turn"):
            flow._begin_review()
        self.first["stage"] = "审查中"
        own = [entry["id"] for entry in candidates(self.first["repos"][0]) if entry["source"] == "本轮"]
        flow.last_message = dumps({"summary": "无待处理问题", "findings": [], "resolved_previous": [],
            "commit_scope": [{"repository": "前端", "include": own, "exclude": []}],
            "commit_messages": [{"repository": "前端", "type": "fix", "scope": "共享", "summary": "修复甲需求"}]})
        flow._completed({"status": "completed"}, self.first["id"], flow.connection_epoch)
        self.assertEqual(self.first["phase"], "complete")
        self.assertEqual(self.first["reviews"][-1]["protocol_version"], 3)
        self.assertIn("甲需求", git(self.path, "show", "HEAD:共享.txt"))
        self.assertNotIn("乙需求", git(self.path, "show", "HEAD:共享.txt"))
        self.assertIn("乙需求", (self.path / "共享.txt").read_text(encoding="utf-8"))

    def test_claimed_fragment_can_be_reviewed_by_another_session_but_cannot_be_committed_twice(self):
        (self.path / "甲.txt").write_text("人工修复\n", encoding="utf-8")
        identifier = candidates(self.first["repos"][0])[0]["id"]
        snapshot_review(self.second["repos"][0])
        first = self.review(self.first, [identifier])
        second = select_scope(self.second["repos"], [{"repository": "前端", "include": [identifier], "exclude": []}])
        self.assertEqual(first, second)
        commit(self.first["repos"][0], "fix(共享): 纳入人工修复", first)
        head = git(self.path, "rev-parse", "HEAD")
        with self.assertRaisesRegex(WorkflowError, "已变化|已被提交"):
            commit(self.second["repos"][0], "fix(共享): 不重复提交", second)
        self.assertEqual(git(self.path, "rev-parse", "HEAD"), head)

    def test_staged_overlap_stops_before_commit_and_preserves_worktree(self):
        file = self.path / "共享.txt"
        file.write_text(self.original.replace("原始第3行", "人工暂存"), encoding="utf-8")
        git(self.path, "add", "共享.txt")
        self.replace(self.first, "共享.txt", "人工暂存", "本轮修改")
        fingerprint = self.review(self.first)
        head, index, working = git(self.path, "rev-parse", "HEAD"), git(self.path, "show", ":共享.txt"), file.read_bytes()
        with self.assertRaisesRegex(WorkflowError, "暂存片段重叠"):
            commit(self.first["repos"][0], "fix(共享): 重叠不能提交", fingerprint)
        self.assertEqual(git(self.path, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.path, "show", ":共享.txt"), index)
        self.assertEqual(file.read_bytes(), working)

    def test_interrupted_index_sync_can_be_explicitly_completed_without_duplicate_commit(self):
        self.replace(self.first, "甲.txt", "原始第3行", "本轮修复")
        fingerprint = self.review(self.first)
        with patch("workflow.scoped_commit.finish_transaction", side_effect=WorkflowError("模拟同步中断")):
            with self.assertRaisesRegex(WorkflowError, "模拟同步中断"):
                commit(self.first["repos"][0], "fix(共享): 中断后核对", fingerprint)
        committed = git(self.path, "rev-parse", "HEAD")
        result = commit(self.first["repos"][0], "fix(共享): 中断后核对", fingerprint)
        self.assertEqual(result, committed)
        self.assertEqual(git(self.path, "rev-list", "--count", "HEAD"), "2")
        self.assertFalse(Path(str(self.path / ".git" / "index") + ".lock").exists())

    def test_unrelated_binary_file_does_not_block_text_changes(self):
        (self.path / "本机.bin").write_bytes(b"\0\xff\x01")
        self.replace(self.first, "甲.txt", "原始第3行", "本轮修复")
        fingerprint = self.review(self.first)
        commit(self.first["repos"][0], "fix(共享): 保留二进制资料", fingerprint)
        self.assertEqual(git(self.path, "show", "--format=", "--name-only", "HEAD"), "甲.txt")
        self.assertEqual((self.path / "本机.bin").read_bytes(), b"\0\xff\x01")

    def test_identical_replacements_keep_ids_after_another_commit(self):
        file = self.path / "共享.txt"
        contents = self.original.replace("原始第3行", "重复原文").replace("原始第14行", "重复原文")
        file.write_text(contents, encoding="utf-8")
        git(self.path, "add", "共享.txt")
        git(self.path, "commit", "-m", "相同文本不同上下文")
        self.replace(self.first, "共享.txt", "重复原文\n原始第4行", "相同修改\n原始第4行")
        self.replace(self.second, "共享.txt", "重复原文\n原始第15行", "相同修改\n原始第15行")
        first, second = self.review(self.first), self.review(self.second)
        commit(self.first["repos"][0], "fix(共享): 第一个位置", first)
        self.assertEqual(manifest(self.second["repos"]), second)
        commit(self.second["repos"][0], "fix(共享): 第二个位置", second)
        self.assertEqual(git(self.path, "show", "HEAD:共享.txt").count("相同修改"), 2)

    def test_repository_lock_crosses_processes_and_check_lock_is_independent(self):
        code = ("import sys; from workflow.gitops import repository_lock, check_lock; "
                "lock = (check_lock if sys.argv[2]=='check' else repository_lock)(sys.argv[1]); "
                "acquired=lock.acquire(timeout=0.2); print(acquired); "
                "lock.release() if acquired else None")
        with repository_lock(self.path):
            blocked = run([executable, "-c", code, str(self.path), "repository"], capture_output=True, timeout=10)
            independent = run([executable, "-c", code, str(self.path), "check"], capture_output=True, timeout=10)
        available = run([executable, "-c", code, str(self.path), "repository"], capture_output=True, timeout=10)
        self.assertEqual((blocked.returncode, independent.returncode, available.returncode), (0, 0, 0))
        self.assertEqual(blocked.stdout.strip(), b"False")
        self.assertEqual(independent.stdout.strip(), b"True")
        self.assertEqual(available.stdout.strip(), b"True")

    def test_owned_directory_cannot_be_deleted_while_shared_by_another_session(self):
        git(self.path, "branch", "独立项目")
        owner = self.store.new({"kind": "task", "id": 3, "title": "目录所有者"})
        borrower = self.store.new({"kind": "task", "id": 4, "title": "共享使用者"})
        selection = {"path": str(self.path), "name": "前端", "role": "frontend", "branch": "独立项目",
                     "base": "refs/heads/独立项目", "create": False}
        owned = acquire_workspace(selection, self.store.directory(owner["id"]) / "workspace" / "repo-1", self.store, owner)
        shared = acquire_workspace(selection, self.store.directory(borrower["id"]) / "workspace" / "repo-1", self.store, borrower)
        self.assertTrue(owned["workspace_owned"])
        self.assertFalse(shared["workspace_owned"])
        with self.assertRaisesRegex(WorkflowError, borrower["id"]):
            cleanup_workspaces(self.store, owner, [selection])
        self.assertTrue(Path(owned["path"]).is_dir())

    def test_newly_adopted_backend_scope_is_checked_without_an_extra_review_turn(self):
        repo = self.first["repos"][0]
        repo["role"] = "backend"
        (self.path / "甲.txt").write_text("人工补充的需求修复\n", encoding="utf-8")
        self.first.update(snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                          compile_check={"enabled": True}, max_fix_rounds=3, turn_id="隔离审查",
                          compile_checks=[{"delivery_round": 1, "status": "complete", "manifest": {}, "repositories": []}])
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        flow = Workflow(settings, Mock(), store=self.store)
        self.addCleanup(flow.close)
        flow.state = self.first
        with patch.object(flow, "_start_turn") as model:
            flow._begin_review()
            self.first["stage"] = "审查中"
            selected = [entry["id"] for entry in candidates(repo)]
            flow.last_message = dumps({"summary": "已核对人工补充", "findings": [], "resolved_previous": [],
                "commit_scope": [{"repository": "前端", "include": selected, "exclude": []}],
                "commit_messages": [{"repository": "前端", "type": "fix", "scope": "共享", "summary": "纳入本轮修复"}]})
            with patch("workflow.compile_flow.backend_modules", return_value=[]), patch(
                    "workflow.compile_flow.CompileRunner.check", return_value={"status": "passed", "summary": "模拟结果", "diagnostics": []}) as check:
                flow._completed({"status": "completed"}, self.first["id"], flow.connection_epoch)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(check.call_count, 1)
        self.assertEqual(self.first["phase"], "complete")
        self.assertEqual(len(self.first["reviews"]), 1)
