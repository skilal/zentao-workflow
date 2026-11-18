"""隔离会话和临时 Git 仓库检查文件清单，不读取业务仓库。"""

from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
from json import dumps, loads
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

from test_sessions import SessionFixture
from workflow.gitops import changed_paths, git, manifest
from workflow.mcp_server import Tools
from workflow.settings import WorkflowError
from workflow.shared_changes import file_version, initialize_session, record_write
from workflow.web import LocalServer, WebApplication


class ChangedFilesFixture(SessionFixture):
    def setUp(self):
        super().setUp()
        self.state = self.first.engine.state
        self.state.update(phase="implement", stage="已暂停", prepare_complete=True,
                          snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
        for role, name in (("frontend", "前端"), ("backend", "后端")):
            path = self.root / role
            path.mkdir()
            git(path, "init", "-b", "main")
            git(path, "config", "user.name", "文件清单检查")
            git(path, "config", "user.email", "files@example.invalid")
            for relative in ("业务.txt", "删除.txt", "本机.txt"):
                (path / relative).write_text("原始内容\n", encoding="utf-8")
            git(path, "add", ".")
            git(path, "commit", "-m", "初始化")
            self.state["repos"].append({"name": name, "role": role, "path": str(path), "source": str(path),
                "baseline": git(path, "rev-parse", "HEAD"), "branch": "main", "workspace_owned": False,
                "preserved_paths": ["本机.txt"]})
            (path / "本机.txt").write_text("本机保留内容不应返回\n", encoding="utf-8")
            git(path, "add", "本机.txt")
        self.first.engine.save()

    def change(self, index=0, relative="业务.txt"):
        path = Path(self.state["repos"][index]["path"]) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("任务文件正文不应返回\n", encoding="utf-8")
        return path

    def files(self):
        return self.application.changed_files(self.state["id"])


class ConversationChangesFixture(ChangedFilesFixture):
    def setUp(self):
        super().setUp()
        self.engine = self.first.engine
        self.state.update(stage="实施中", approved_plan="初次需求",
                          approved_hash=sha256("初次需求".encode()).hexdigest(),
                          max_fix_rounds=self.settings["review"]["max_fix_rounds"])
        initialize_session(self.engine.store, self.state)
        self.engine.save()
        self.tools = Tools(self.state["id"])

    def write(self, repository, relative, content):
        repo = next(repo for repo in self.state["repos"] if repo["name"] == repository)
        arguments = {"repository": repository, "path": relative,
                     "expected_version": file_version(Path(repo["path"]) / relative)}
        if content is None:
            return self.tools.call("delete_file", arguments)
        return self.tools.call("write_file", {**arguments, "content": content})

    def followup(self, text):
        self.state["stage"] = "已暂停"
        with patch.object(self.engine, "_prepare_followup"):
            self.engine.followup(text, [], uuid4().hex, review_enabled=False)
        self.state["stage"] = "续聊处理中"
        self.engine.save()

    def changes(self):
        return self.application.conversation_changes(self.state["id"])

    def diff_for(self, group, repository, relative):
        record = next(repo for repo in group["repositories"] if repo["name"] == repository)
        return self.application.conversation_diff(self.state["id"], str(group["message_index"]), record["id"], relative)["patch"]


class ConversationChangesChecks(ConversationChangesFixture):
    def test_followups_keep_separate_diffs_for_the_same_file_in_one_delivery(self):
        self.write("前端", "业务.txt", "第一版\n")
        self.write("后端", "删除.txt", None)
        self.engine.note("Codex", "第一版已完成")
        first = self.changes()["groups"][0]
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["message_index"], -1)
        self.assertEqual({repo["name"]: repo["files"] for repo in first["repositories"]}, {
            "前端": [{"path": "业务.txt", "kind": "修改"}], "后端": [{"path": "删除.txt", "kind": "删除"}]})
        self.followup("请继续修改同一文件")
        self.write("前端", "业务.txt", "第二版\n")
        self.write("后端", "新目录/新文件.txt", "")
        self.write("后端", "临时文件.txt", "临时修改\n")
        self.write("后端", "临时文件.txt", None)
        self.engine.note("Codex", "第二版已完成")
        self.followup("只问一个问题")
        first_saved, second = self.changes()["groups"]
        self.assertEqual(first_saved, first)
        self.assertEqual(self.state["delivery_round"], 1)
        self.assertEqual(second["message_index"], 1)
        self.assertEqual(second["total"], 2)
        self.assertIn("-原始内容\n+第一版", self.diff_for(first, "前端", "业务.txt").replace("\r\n", "\n"))
        self.assertIn("-第一版\n+第二版", self.diff_for(second, "前端", "业务.txt").replace("\r\n", "\n"))
        self.assertIn("+++ /dev/null", self.diff_for(first, "后端", "删除.txt"))
        self.assertIn("--- /dev/null", self.diff_for(second, "后端", "新目录/新文件.txt"))
        with self.assertRaises(WorkflowError):
            self.diff_for(second, "前端", "本机.txt")

        saved = self.changes()
        (Path(self.state["repos"][0]["path"]) / "业务.txt").write_bytes(b"manual after the reply\n")
        self.state.update(phase="complete", stage="已完成（未审查）")
        self.engine.save()
        self.followup("开始下一轮交付")
        self.assertEqual(self.state["delivery_round"], 2)
        self.assertFalse(self.state["repos"])
        restarted = WebApplication(self.settings)
        self.addCleanup(restarted.close)
        with patch("workflow.web.changed_paths", side_effect=AssertionError("已保存对话不扫描工作目录")):
            self.assertEqual(restarted.conversation_changes(self.state["id"]), saved)

    def test_shared_file_external_edits_and_legacy_records_are_not_mixed_into_diff(self):
        repo = self.state["repos"][0]
        relative = "业务.txt"
        path = Path(repo["path"]) / relative
        original = path.read_bytes()
        path.write_bytes(b"legacy\n")
        record_write(repo, relative, original, path.read_bytes())
        self.followup("两次写入同一文件")
        self.write("前端", relative, "first\n")
        path.write_bytes(b"first\nmanual\n")
        self.write("前端", relative, "second\nmanual\n")
        result = self.changes()
        self.assertEqual(result["legacy_repositories"], [{"name": "前端", "paths": [relative]}])
        self.assertEqual(len(result["groups"]), 1)
        diff = self.diff_for(result["groups"][0], "前端", relative)
        self.assertIn("-legacy\n+first", diff)
        self.assertIn("-first\n+second", diff)
        self.assertNotIn("+manual", diff)


class ChangedFilesChecks(ChangedFilesFixture):
    def test_names_only_cover_added_modified_deleted_and_preserve_index_and_state(self):
        self.change()
        self.change(relative="目录/中文新增.txt")
        (Path(self.state["repos"][0]["path"]) / "删除.txt").unlink()
        self.change(1)
        before = deepcopy(self.state)
        revision = self.first.revision
        indexes = [git(repo["path"], "diff", "--cached", "--binary", binary=True) for repo in self.state["repos"]]
        result = self.files()
        self.assertEqual(result, {"session_id": self.state["id"], "total": 4, "repositories": [
            {"name": "前端", "paths": sorted(["业务.txt", "删除.txt", "目录/中文新增.txt"])},
            {"name": "后端", "paths": ["业务.txt"]}]})
        self.assertNotIn("正文", dumps(result, ensure_ascii=False))
        self.assertNotIn("保留内容", dumps(result, ensure_ascii=False))
        self.assertEqual(self.state, before)
        self.assertEqual(self.first.revision, revision)
        self.assertEqual(indexes, [git(repo["path"], "diff", "--cached", "--binary", binary=True)
                                   for repo in self.state["repos"]])

    def test_reverted_uncommitted_files_disappear_even_after_a_review_snapshot(self):
        path = self.change()
        added = self.change(relative="新增.txt")
        self.state["review_manifest"] = manifest(self.state["repos"])
        self.assertEqual(self.files()["total"], 2)
        path.write_text("原始内容\n", encoding="utf-8")
        added.unlink()
        self.assertEqual(self.files()["total"], 0)

    def test_committed_and_completed_files_use_snapshots_after_restart(self):
        self.change()
        self.change(1, "新增.txt")
        self.state["review_manifest"] = manifest(self.state["repos"])
        for repo in self.state["repos"]:
            paths = [key[len(repo["name"]) + 1:] for key in self.state["review_manifest"]
                     if key.startswith(repo["name"] + "/")]
            git(repo["path"], "add", "--", *paths)
            git(repo["path"], "commit", "--only", "-m", "本次交付", "--", *paths)
            self.state["commits"][repo["name"]] = git(repo["path"], "rev-parse", "HEAD")
        self.state.update(phase="complete", stage="已完成（静态审查）")
        self.first.engine.save()
        with patch("workflow.web.changed_paths", side_effect=AssertionError("已完成交付不能扫描工作目录")):
            result = self.files()
            restarted = WebApplication(self.settings)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.changed_files(self.state["id"]), result)
        self.assertEqual(result["total"], 2)

    def test_history_accumulates_without_old_directories_and_deduplicates_per_repository(self):
        self.change()
        archived = {"repos": deepcopy(self.state["repos"]), "review_manifest": {
            "前端/业务.txt": "历史摘要", "前端/本机.txt": "不可纳入", "后端/业务.txt": "历史摘要"},
            "uncommitted_manifest": {"前端/历史新增.txt": "历史摘要"}}
        for repo in archived["repos"]:
            repo["path"] = str(self.root / "已不存在的历史工作区" / repo["role"])
        self.state["deliveries"] = [archived, deepcopy(archived)]
        self.assertEqual(self.files()["repositories"], [
            {"name": "前端", "paths": ["业务.txt", "历史新增.txt"]}, {"name": "后端", "paths": ["业务.txt"]}])
        self.state["repos"] = []
        with patch("workflow.web.changed_paths", side_effect=AssertionError("不扫描历史工作区")):
            self.assertEqual(self.files()["total"], 3)

    def test_partial_commit_uses_snapshot_for_committed_repository_only(self):
        self.change()
        backend = self.change(1)
        self.state["review_manifest"] = manifest(self.state["repos"])
        self.state["commits"] = {"前端": "已提交"}
        backend.write_text("原始内容\n", encoding="utf-8")
        with patch("workflow.web.changed_paths", wraps=changed_paths) as scan:
            self.assertEqual(self.files()["repositories"], [{"name": "前端", "paths": ["业务.txt"]}])
            self.assertEqual([call.args[0]["name"] for call in scan.call_args_list], ["后端"])

    def test_unreviewed_completion_is_frozen_and_sessions_are_isolated(self):
        self.change(relative="未审查.txt")
        self.state["uncommitted_manifest"] = manifest(self.state["repos"])
        self.state.update(phase="complete", stage="已完成（未审查）")
        with patch("workflow.web.changed_paths", side_effect=AssertionError("完成后仅查看快照")):
            self.assertEqual(self.files()["total"], 1)
            self.assertEqual(self.application.changed_files(self.second.engine.state["id"])["total"], 0)

    def test_invalid_baseline_is_an_error_instead_of_an_empty_list(self):
        self.state["repos"][0]["baseline"] = "错误基线"
        with self.assertRaises(WorkflowError):
            self.files()

    def test_http_requires_a_session_and_returns_only_the_selected_file_names(self):
        self.change()
        server = LocalServer(("127.0.0.1", 0), self.application)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(client.close)
        client.request("GET", "/")
        response = client.getresponse()
        headers = {"Cookie": response.getheader("Set-Cookie").split(";", 1)[0]}
        response.read()
        for session_id, status in (("", 400), ("不存在", 400), (self.state["id"], 200)):
            client.request("GET", "/api/changed-files?session_id=" + quote(session_id), headers=headers)
            response = client.getresponse()
            result = loads(response.read())
            self.assertEqual(response.status, status)
            if status == 200:
                self.assertEqual(result, self.files())
