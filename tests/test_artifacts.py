"""隔离验证成果生成、会话归属和本地访问，不接触业务仓库或真实模型。"""

from hashlib import sha256
from http.client import HTTPConnection
from json import loads
from pathlib import Path
import sys
from subprocess import CREATE_NO_WINDOW, run
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlencode

from test_supplement_images import PNG, SupplementImageFixture
from workflow.approvals import is_controlled_write
from workflow.artifacts import artifact_file, dependency, preview_artifact, preview_image
from workflow.gitops import git
from workflow.mcp_server import TOOLS, Tools
from workflow.rpc import thread_parameters
from workflow.settings import WorkflowError
from workflow.user_input import register_input
from workflow.web import COOKIE_NAME, LocalServer, WebApplication, public_state


BLOCKS = {"blocks": [{"type": "heading", "text": "中文验收说明", "level": 1},
                     {"type": "paragraph", "text": "保留原始顺序与中文内容。"},
                     {"type": "list", "items": ["第一项", "第二项"], "ordered": True},
                     {"type": "table", "rows": [["名称", "说明"], ["档案", "验收通过"]]},
                     {"type": "paragraph", "text": "表格之后的内容"}]}
SHEETS = {"sheets": [{"name": "明细", "column_widths": [24, 18],
                      "rows": [[{"value": "名称", "bold": True}, "数值"], ["档案", 10],
                               ["合计", {"formula": "=SUM(B2:B2)", "number_format": "0.00"}], ["普通文字", "=1+2"]]},
                     {"name": "其他", "rows": [["第二工作表", True, None]]}]}


class ArtifactFixture(SupplementImageFixture):
    def setUp(self):
        super().setUp()
        self.engine = self.runtime.engine
        self.store = self.engine.store
        self.state.update(stage="计划中", phase="plan", thread_id="成果线程", turn_id="成果回合", prepare_complete=True)
        self.state["repos"] = [{"name": "前端", "role": "frontend", "path": str(self.root / "repo"),
                                "branch": "main", "baseline": "0" * 40}]
        (self.root / "repo").mkdir()
        self.engine.start_progress(self.state["turn_id"])
        self.engine.save()
        mocked = patch("workflow.mcp_server.load_settings", return_value=self.settings)
        mocked.start()
        self.addCleanup(mocked.stop)

    def create(self, name="中文成果.sql", format_name="text", content="SELECT '中文';", **extra):
        return Tools(self.state["id"]).call("create_artifact", {"name": name, "format": format_name, "content": content, **extra})

    def publish(self, relative, name=None):
        return Tools(self.state["id"]).call("publish_artifact", {"repository": "前端", "path": relative, "name": name or Path(relative).name})

    def preview(self, entry, **query):
        return preview_artifact(self.store, self.state, entry["id"], {key: [str(value)] for key, value in query.items()}, self.settings)

    def http_server(self):
        server = LocalServer(("127.0.0.1", 0), self.application)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(close)
        return server


class ArtifactChecks(ArtifactFixture):
    def test_text_versions_are_independent_and_do_not_write_session_or_repository(self):
        before = self.store.load(self.state["id"])
        first = self.create()
        second = self.create(content="SELECT 2;")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.load(self.state["id"]), before)
        self.assertEqual(list((self.root / "repo").iterdir()), [])
        entry, path = artifact_file(self.store, self.state, first["id"])
        self.assertEqual(path.read_text(encoding="utf-8"), "SELECT '中文';")
        self.assertEqual(entry["sha256"], sha256(path.read_bytes()).hexdigest())
        self.assertIn("artifact_id=", first["download_url"])
        self.assertEqual(len(Tools(self.state["id"]).call("task_context", {})["artifacts"]), 2)

    def test_reserved_internal_names_are_safe_display_names(self):
        entry = self.create("manifest.json", content='{"有效":true}')
        self.assertEqual(self.preview(entry)["items"], ['{"有效":true}'])

    def test_text_pagination_markdown_and_invalid_pages(self):
        entry = self.create("说明.md", content="\n".join(f"第{i}行" for i in range(205)))
        first, second, third = self.preview(entry), self.preview(entry, page=2), self.preview(entry, page=3)
        self.assertEqual(first["kind"], "markdown")
        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(second["items"][0], "第100行")
        self.assertFalse(third["has_next"])
        for page in (0, -1, "bad", 4):
            with self.assertRaises(WorkflowError):
                self.preview(entry, page=page)

    def test_publish_keeps_a_snapshot_and_supports_images_and_unknown_files(self):
        source = self.root / "repo" / "原始.png"
        source.write_bytes(PNG)
        entry = self.publish("原始.png")
        source.write_bytes(b"changed")
        metadata, path = artifact_file(self.store, self.state, entry["id"])
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(self.preview(entry)["kind"], "image")
        image, content_type = preview_image(self.store, self.state, entry["id"], {})
        self.assertEqual((image.read_bytes(), content_type), (PNG, "image/png"))
        (self.root / "repo" / "示例.bin").write_bytes(b"sample")
        self.assertEqual(self.preview(self.publish("示例.bin"))["kind"], "unsupported")
        output = self.root / "repo" / "outputs"
        output.mkdir()
        (output / "成果.txt").write_text("已有输出内容", encoding="utf-8")
        self.assertEqual(self.preview(self.publish("outputs/成果.txt"))["items"], ["已有输出内容"])

    def test_paths_filename_limits_and_missing_sources(self):
        for name in ("../其他.sql", "CON.sql", "a:b.sql", "x\\a.sql", "说明.", ""):
            with self.assertRaises(WorkflowError):
                self.create(name)
        for relative in ("../外部.sql", "C:/外部.sql", ".git/config", ".local/文件.txt", "missing.txt"):
            with self.assertRaises(WorkflowError):
                self.publish(relative)
        with self.assertRaises(WorkflowError):
            self.create("伪装.docx")
        with self.assertRaises(WorkflowError):
            self.create("错误.txt", "docx", BLOCKS)
        self.settings["zentao"]["max_attachment_mb"] = 0
        with self.assertRaisesRegex(WorkflowError, "上限"):
            self.create()
        (self.root / "repo" / "大文件.txt").write_bytes(b"too large")
        with self.assertRaisesRegex(WorkflowError, "上限"):
            self.publish("大文件.txt")

    def test_link_and_junction_paths_are_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        (target / "外部.txt").write_text("不能返回", encoding="utf-8")
        link = self.root / "repo" / "链接"
        result = run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        with self.assertRaises(WorkflowError):
            self.publish("链接/外部.txt")
        # 会话成果根目录也不能被 junction 替换。
        artifacts = self.store.directory(self.state["id"]) / "artifacts"
        result = run(["cmd", "/c", "mklink", "/J", str(artifacts), str(target)], capture_output=True, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        self.addCleanup(lambda: artifacts.rmdir() if artifacts.exists() else None)
        with self.assertRaises(WorkflowError):
            self.create()

    def test_stage_authorization_does_not_grant_business_write(self):
        description = next(description for name, description, _ in TOOLS if name == "create_artifact")
        params = {"serverName": "workflow", "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call",
                  "tool_description": description, "tool_params": {"name": "说明.txt", "format": "text", "content": "内容"}}}
        self.assertTrue(is_controlled_write(self.state, params))
        with self.assertRaisesRegex(WorkflowError, "禁止修改"):
            Tools(self.state["id"]).call("write_file", {"repository": "前端", "path": "业务.txt", "content": "不能写入"})
        for stage in ("审查中", "编译修复中", "等待人工回答", "已暂停"):
            self.state["stage"] = stage
            self.engine.save()
            self.assertFalse(is_controlled_write(self.state, params))
            with self.assertRaises(WorkflowError):
                self.create()
        self.state["stage"] = "计划中"
        self.engine.save()
        register_input(self.store, self.state, [{"id": "x", "header": "选择", "question": "需要什么？"}])
        with self.assertRaisesRegex(WorkflowError, "等待用户"):
            self.create()

    def test_missing_dependency_and_invalid_document_leave_no_manifest(self):
        with patch("workflow.artifacts.import_module", side_effect=ModuleNotFoundError("模拟依赖缺失", name="docx")):
            with self.assertRaisesRegex(WorkflowError, "requirements-artifacts"):
                self.create("说明.docx", "docx", BLOCKS)
        for content in ({}, {"blocks": [{"type": "table", "rows": [["一"], ["一", "二"]]}]},
                        {"blocks": [{"type": "heading", "text": "标题", "level": 9}]}):
            with self.assertRaises(WorkflowError):
                self.create("说明.docx", "docx", content)
        self.assertEqual(Tools(self.state["id"]).call("task_context", {})["artifacts"], [])

    def test_dependency_refreshes_user_directory_created_after_startup(self):
        directory = self.root / "new-user-site"
        directory.mkdir()
        (directory / "新成果依赖.py").write_text("验证值 = '用户目录新安装依赖'\n", encoding="utf-8")
        original = sys.path[:]
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original))
        self.addCleanup(lambda: sys.modules.pop("新成果依赖", None))
        with patch("workflow.artifacts.site.getusersitepackages", return_value=str(directory)), \
             patch("workflow.artifacts.site.ENABLE_USER_SITE", True):
            loaded = dependency("新成果依赖", "验证依赖")
        self.assertEqual(loaded.验证值, "用户目录新安装依赖")

    def test_broken_dependency_reports_import_cause_instead_of_missing_package(self):
        with patch("workflow.artifacts.import_module", side_effect=ImportError("DLL load failed: 模拟传递依赖损坏")):
            with self.assertRaisesRegex(WorkflowError, "加载失败.*DLL load failed"):
                dependency("docx", "python-docx")

    def test_document_content_order_and_multi_sheet_formulas(self):
        word = self.create("中文说明.docx", "docx", BLOCKS)
        blocks = self.preview(word)["items"]
        self.assertEqual(blocks[0]["text"], "中文验收说明")
        self.assertEqual(blocks[2], {"type": "list", "ordered": True, "items": ["第一项", "第二项"]})
        self.assertEqual(blocks[-2]["rows"][1], ["档案", "验收通过"])
        self.assertEqual(blocks[-1]["text"], "表格之后的内容")
        excel = self.create("中文清单.xlsx", "xlsx", SHEETS)
        view = self.preview(excel)
        self.assertEqual(view["sheets"], ["明细", "其他"])
        self.assertEqual(view["items"][2][1], "=SUM(B2:B2)")
        self.assertEqual(view["items"][3][1], "=1+2")
        openpyxl = dependency("openpyxl", "openpyxl")
        _, path = artifact_file(self.store, self.state, excel["id"])
        workbook = openpyxl.load_workbook(path)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["明细"]["B4"].data_type, "s")
        self.assertEqual(workbook["明细"]["B3"].data_type, "f")
        self.assertTrue(workbook["明细"]["A1"].font.bold)
        self.assertEqual(workbook["明细"].column_dimensions["A"].width, 24)
        self.assertEqual(self.preview(excel, sheet="其他")["items"][0][0], "第二工作表")
        with self.assertRaises(WorkflowError):
            self.preview(excel, sheet="不存在")

    def test_pdf_generation_and_page_image_access(self):
        entry = self.create("验收报告.pdf", "pdf", BLOCKS)
        _, path = artifact_file(self.store, self.state, entry["id"])
        self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        with patch("workflow.artifacts.read_pdf") as render:
            def pages(material, renderer):
                image = path.parent / "pages" / "page-1.png"
                image.parent.mkdir()
                image.write_bytes(PNG)
                material["pdf_pages"] = [{"page": 1, "path": str(image)}]
            render.side_effect = pages
            self.assertEqual(self.preview(entry)["total"], 1)
            self.assertEqual(self.preview(entry)["kind"], "pdf")
            self.assertEqual(render.call_count, 1)
        image, _ = preview_image(self.store, self.state, entry["id"], {})
        self.assertEqual(image.read_bytes(), PNG)
        with self.assertRaises(WorkflowError):
            self.preview(entry, page=2)
        other = self.create("另一个.pdf", "pdf", BLOCKS)
        with patch("workflow.artifacts.read_pdf", side_effect=ValueError("模拟渲染工具缺失")):
            with self.assertRaisesRegex(WorkflowError, "原文件仍可下载"):
                self.preview(other)
        self.assertTrue(artifact_file(self.store, self.state, other["id"])[1].is_file())

    def test_reconcile_events_reply_binding_interruption_and_restart(self):
        entry = self.create()
        self.engine._event({"method": "item/completed", "params": {"threadId": "成果线程", "turnId": "成果回合",
                           "item": {"id": "生成工具", "type": "mcpToolCall", "server": "workflow", "tool": "create_artifact"}}}, self.engine.connection_epoch)
        self.assertEqual(self.state["artifacts"][0]["turn_status"], "running")
        self.engine.note("Codex", "已生成文件", turn_id="成果回合")
        self.engine.sync_artifacts()
        self.assertEqual(self.state["artifacts"][0]["history_index"], len(self.state["history"]) - 1)
        self.engine.pause()
        self.assertEqual(self.state["artifacts"][0]["turn_status"], "interrupted")
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(self.state["id"])["state"]["artifacts"][0]["id"], entry["id"])
        self.assertEqual(artifact_file(restored.sessions.store, self.state, entry["id"])[1].read_bytes(), "SELECT '中文';".encode())
        self.assertEqual(public_state({"id": "old", "history": [], "repos": [], "reviews": []})["artifacts"], [])

    def test_restart_recovers_manifest_not_yet_collected(self):
        entry = self.create()
        self.assertEqual(self.state["artifacts"], [])
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        recovered = restored.session(self.state["id"])["state"]["artifacts"]
        self.assertEqual(recovered[0]["id"], entry["id"])
        self.assertEqual(recovered[0]["turn_status"], "interrupted")

    def test_tool_configuration_and_delete_with_session(self):
        entry = self.create()
        tools = thread_parameters(self.state, "模拟模型", {})["config"]["mcp_servers"]["workflow"]["enabled_tools"]
        self.assertIn("create_artifact", tools)
        self.assertIn("publish_artifact", tools)
        directory = self.store.directory(self.state["id"])
        self.store.remove(self.state["id"])
        self.assertFalse(directory.exists())
        # 从运行时移除，避免清理时重新保存测试会话。
        self.engine.state = None

    def test_completed_artifact_only_turns_skip_review_and_commit(self):
        repo = self.state["repos"][0]
        git(repo["path"], "init", "-b", "main")
        git(repo["path"], "config", "user.name", "成果隔离检查")
        git(repo["path"], "config", "user.email", "artifact@example.invalid")
        git(repo["path"], "commit", "--allow-empty", "-m", "初始化")
        repo["baseline"] = git(repo["path"], "rev-parse", "HEAD")
        for phase, stage in (("followup", "续聊处理中"), ("implement", "实施中")):
            self.state.update(phase=phase, stage=stage, review_enabled=True, turn_id=phase,
                              followup={"request_id": "仅生成文档"} if phase == "followup" else None)
            self.engine.start_progress(phase)
            self.engine.save()
            entry = self.create()
            self.engine.last_message = "已生成可下载文件。"
            with patch.object(self.engine, "_begin_review") as review, patch("workflow.engine.commit") as commit:
                self.engine._completed({"status": "completed", "id": self.state["turn_id"]}, self.state["id"], self.engine.connection_epoch)
            review.assert_not_called()
            commit.assert_not_called()
            self.assertEqual(self.state["phase"], "complete")
            self.assertEqual(self.state["uncommitted_manifest"], {})
            artifact = self.state["artifacts"][-1]
            self.assertEqual(artifact["id"], entry["id"])
            self.assertEqual(artifact["turn_status"], "completed")
            self.assertEqual(self.state["history"][artifact["history_index"]]["role"], "Codex")
            self.assertEqual(git(repo["path"], "status", "--porcelain"), "")

    def test_http_download_preview_authentication_isolation_and_integrity(self):
        entry = self.create("中文成果.sql", content="SELECT '中文';")
        server = self.http_server()
        query = urlencode({"session_id": self.state["id"], "artifact_id": entry["id"]})
        def request(route="download", query_string=query, **headers):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", f"/api/artifacts/{route}?{query_string}", headers={
                    "Cookie": f"{COOKIE_NAME}={self.application.cookie_token}", "Connection": "close", **headers})
                response = connection.getresponse()
                return response.status, response.read(), response.getheader("Content-Disposition")
            finally:
                connection.close()
        status, data, disposition = request()
        self.assertEqual((status, data), (200, "SELECT '中文';".encode()))
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertEqual(loads(request("preview")[1])["items"], ["SELECT '中文';"])
        for headers in ({"Cookie": ""}, {"Origin": "https://external.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(request(**headers)[0], 400)
        other = self.store.new({"kind": "task", "id": 8})
        self.application.sessions.add(other)
        self.assertEqual(request(query_string=query.replace(self.state["id"], other["id"]))[0], 400)
        self.assertEqual(request(query_string=query.replace(entry["id"], "../outside"))[0], 400)
        # 内容篡改不会继续返回旧清单对应的文件。
        _, path = artifact_file(self.store, self.state, entry["id"])
        path.write_bytes(b"changed")
        self.assertEqual(request()[0], 400)
