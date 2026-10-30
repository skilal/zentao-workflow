"""无关联会话使用隔离存储、模拟协议及临时仓库，不访问真实业务系统。"""

from base64 import b64decode
from copy import deepcopy
from itertools import count
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.engine import Workflow
from workflow.gitops import git
from workflow.mcp_server import Tools
from workflow.projects import project_locked
from workflow.settings import ROOT, WorkflowError
from workflow.web import WebApplication


PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class ConversationChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            mocked = patch(target, self.root / "local")
            mocked.start()
            self.addCleanup(mocked.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.application = WebApplication(self.settings)
        self.addCleanup(self.application.close)
        self.application.codex_catalog = {"default_model": "示例模型", "models": [
            {"model": "示例模型", "display_name": "示例模型", "reasoning_efforts": ["high"], "is_default": True}]}
        self.application.items = [{"kind": "task", "id": 7, "title": "示例任务", "project_id": 41, "project_name": "项目甲"},
                                  {"kind": "bug", "id": 8, "title": "示例问题", "project_id": 42, "project_name": "项目乙"}]

    def request(self, action, runtime=None, data=None, **changes):
        return {"action": action, "request_id": uuid4().hex, "data": data or {},
                "preview_id": runtime.engine.state["id"] if runtime else None,
                "revision": runtime.revision if runtime else None, **changes}

    def drain(self, runtime):
        runtime.engine.executor.submit(lambda: None).result(timeout=10)
        self.application.management_executor.submit(lambda: None).result(timeout=10)

    def conversation(self, project_id=0):
        result = self.application.dispatch(self.request("new_conversation", data={"project_id": project_id}))
        runtime = self.application.sessions.get_preview(result["preview_id"])
        self.drain(runtime)
        return runtime

    def plan_request(self, runtime, text="调整页面交互", **changes):
        return {**self.request("plan", runtime, {"items": [], "supplement": text,
                "model": "示例模型", "reasoning_effort": "high", "compile_check_enabled": False}), **changes}

    def promote(self, runtime, text="调整页面交互"):
        with patch.object(Workflow, "start_plan") as start:
            result = self.application.dispatch(self.plan_request(runtime, text))
            created = self.application.sessions.get(result["session_id"])
            self.drain(created)
        start.assert_called_once_with(text, False)
        return created

    def test_new_conversation_is_temporary_and_duplicate_creation_is_idempotent(self):
        request = self.request("new_conversation", data={"project_id": 41})
        with patch.object(self.application.sessions.zentao, "collect_many") as collect:
            result = self.application.dispatch(request)
            runtime = self.application.sessions.get_preview(result["preview_id"])
            self.drain(runtime)
            duplicate = self.application.dispatch(request)
        collect.assert_not_called()
        self.assertEqual(duplicate["preview_id"], result["preview_id"])
        self.assertEqual(len(self.application.sessions.previews), 1)
        self.assertIsNone(runtime.engine.state["item"])
        self.assertEqual(runtime.engine.state["items"], [])
        self.assertEqual(runtime.engine.state["project"]["id"], 41)
        self.assertEqual(runtime.engine.state["snapshot"]["sources"], [])
        self.assertFalse(self.application.snapshot()["history"])
        self.assertFalse(list(self.root.rglob("state.json")))
        preview_root = runtime.engine.store.root
        self.application.discard_preview(result["preview_id"]).result(timeout=10)
        self.assertFalse(preview_root.exists())

    def test_invalid_new_conversation_does_not_create_a_preview(self):
        for project_id in (-1, True, "41", None, 99):
            with self.subTest(project_id=project_id), self.assertRaises(WorkflowError):
                self.application.dispatch(self.request("new_conversation", data={"project_id": project_id}))
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request("new_conversation", session_id="其他会话"))
        self.assertFalse(self.application.sessions.previews)

    def test_text_message_persists_once_with_title_and_locked_configuration(self):
        preview = self.conversation(41)
        text = "  调整页面交互\n" + "需求描述" * 20
        request = self.plan_request(preview, text)
        with patch.object(Workflow, "start_plan") as start:
            result = self.application.dispatch(request)
            runtime = self.application.sessions.get(result["session_id"])
            self.drain(runtime)
            same = self.application.dispatch(request)
            repeated = self.application.dispatch({**request, "request_id": uuid4().hex})
        start.assert_called_once_with(text, False)
        self.assertEqual(result["session_id"], same["session_id"])
        self.assertEqual(result["session_id"], repeated["session_id"])
        state = runtime.engine.state
        self.assertEqual(self.application.session(state["id"])["state"]["title"], " ".join(text.split())[:50])
        self.assertEqual(state["supplement"], text.strip())
        self.assertEqual(state["history"][0]["text"], text.strip())
        self.assertEqual(state["history"][0]["role"], "用户")
        self.assertEqual(len(state["history"]), 1)
        self.assertTrue(project_locked(state))
        with self.assertRaises(WorkflowError):
            self.application.move_project({"session_id": state["id"], "project_id": 42,
                                          "revision": runtime.revision, "request_id": uuid4().hex})
        for data in ({"model": "另一模型"}, {"compile_check_enabled": True}):
            with self.assertRaises(WorkflowError):
                self.application.dispatch(self.request("plan", data=data, session_id=state["id"], revision=runtime.revision))
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(state["id"])["state"]["history"], state["history"])
        context = Tools(state["id"]).call("task_context", {"include_materials": True})
        self.assertIsNone(context["item"])
        self.assertEqual(context["items"], [])
        self.assertEqual(context["supplement"], text.strip())
        self.assertEqual(context["snapshot"]["sources"], [])

    def test_images_only_and_mixed_messages_keep_image_history_and_paths(self):
        for text in ("", "按照图片调整"):
            with self.subTest(text=text):
                preview = self.conversation()
                self.application.upload_supplement_image(None, preview.revision, "需求截图.png", PNG, preview.engine.state["id"])
                runtime = self.promote(preview, text)
                state = runtime.engine.state
                history = state["history"][0]
                self.assertEqual(history["image_purpose"], "supplement")
                self.assertEqual(history["images"], state["supplement_images"])
                path = Path(history["images"][0]["path"])
                self.assertTrue(path.is_relative_to(self.application.sessions.store.directory(state["id"])))
                self.assertEqual(path.read_bytes(), PNG)
                self.assertEqual(self.application.session(state["id"])["state"]["title"], text or "图片需求")

    def test_empty_message_stale_selection_and_uploading_cannot_promote(self):
        preview = self.conversation()
        cases = [self.plan_request(preview, " \n "), self.plan_request(preview, revision=-1),
                 self.plan_request(preview, data={"items": [{"kind": "task", "id": 7}], "supplement": "消息"})]
        for request in cases:
            with self.assertRaises(WorkflowError):
                self.application.dispatch(request)
        preview.uploads = 1
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.plan_request(preview))
        preview.uploads = 0
        self.assertFalse(self.application.sessions.runtimes)
        self.assertFalse(list(self.root.rglob("state.json")))

    def test_project_can_change_before_send_and_missing_project_waits(self):
        preview = self.conversation(41)
        self.application.move_project({"preview_id": preview.engine.state["id"], "project_id": 42,
                                      "revision": preview.revision, "request_id": uuid4().hex})
        self.assertEqual(preview.engine.state["snapshot"]["project"]["id"], 42)
        runtime = self.promote(preview)
        with patch.object(runtime.engine, "_resolve_branches") as resolve:
            runtime.engine.start_plan("调整页面交互")
        resolve.assert_called_once()
        self.assertEqual(runtime.engine.state["project"]["id"], 42)
        preview = self.conversation()
        with patch.object(Workflow, "ensure_rpc", side_effect=AssertionError("确认项目之前不启动模型")):
            result = self.application.dispatch(self.plan_request(preview))
            runtime = self.application.sessions.get(result["session_id"])
            self.drain(runtime)
        self.assertEqual(runtime.engine.state["stage"], "等待确认项目")
        with patch.object(runtime.engine, "_resolve_branches") as resolve:
            runtime.engine.confirm_project(41, "项目甲")
        resolve.assert_called_once()
        self.assertEqual(runtime.engine.state["project"]["id"], 41)

    def test_optional_collection_can_use_the_same_preview(self):
        preview = self.conversation()
        item = deepcopy(self.application.items[0])
        snapshot = {"items": [item], "sources": [], "materials": [], "blockers": [], "feishu_links": [],
                    "project": {"id": 41, "name": "项目甲", "candidates": []}}
        with patch.object(self.application.sessions.zentao, "collect_many", return_value=snapshot):
            result = self.application.dispatch(self.request("collect", preview, {"items": [{"kind": "task", "id": 7}]}))
            self.drain(preview)
        self.assertEqual(result["preview_id"], preview.engine.state["id"])
        self.assertEqual(preview.engine.state["item"], item)
        self.assertEqual(preview.engine.state["items"], [item])

    def prepare_repositories(self):
        self.settings["repositories"] = []
        for role, name in (("frontend", "前端"), ("backend", "后端")):
            path = self.root / role
            path.mkdir()
            git(path, "init", "-b", "项目分支")
            git(path, "config", "user.name", "会话检查")
            git(path, "config", "user.email", "conversation@example.invalid")
            (path / "页面.txt").write_text("初始内容\n", encoding="utf-8")
            git(path, "add", "页面.txt")
            git(path, "commit", "-m", "初始化")
            self.settings["repositories"].append({"role": role, "name": name, "path": str(path), "remote": "origin"})
        self.settings["project_branches"] = {self.settings["zentao"]["base_url"].rstrip("/"): {
            "41": {"name": "项目甲", "branches": {"frontend": "项目分支", "backend": "项目分支"}}}}

    def test_plan_implementation_review_commit_and_followup_without_a_work_item(self):
        self.prepare_repositories()
        turns, calls = count(1), []
        def connect(engine):
            if engine.rpc is None:
                engine.state["thread_id"] = engine.state.get("thread_id") or "模拟线程"
                engine.state["rpc_connected"] = True
                engine.rpc = Mock(model="示例模型")
                def request(method, params):
                    calls.append((method, params))
                    return {"turn": {"id": f"模拟回合-{next(turns)}"}}
                engine.rpc.request.side_effect = request
        def complete(engine, text):
            engine.last_message = text
            engine.turn_running = False
            engine._completed({"id": engine.state["turn_id"], "status": "completed"},
                              engine.state["id"], engine.connection_epoch)
        with patch.object(Workflow, "ensure_rpc", connect), patch("workflow.engine.refresh_branches", side_effect=AssertionError("不访问远程")):
            preview = self.conversation(41)
            result = self.application.dispatch(self.plan_request(preview))
            runtime = self.application.sessions.get(result["session_id"])
            self.drain(runtime)
            engine, state = runtime.engine, runtime.engine.state
            self.assertEqual(state["stage"], "计划中")
            self.assertTrue(all(entry["source"] == "common" for entry in state["branch_resolution"]["repositories"]))
            self.assertIn("未关联禅道工作项", calls[-1][1]["input"][0]["text"])
            complete(engine, "<proposed_plan>调整页面交互</proposed_plan>")
            self.assertEqual(state["stage"], "实施中")
            self.assertEqual(state["approval_source"], "auto")
            Tools(state["id"]).call("write_file", {"repository": "前端", "path": "页面.txt", "content": "更新交互\n"})
            complete(engine, "已完成修改")
            self.assertEqual(state["stage"], "审查中")
            complete(engine, dumps({"summary": "无遗留问题", "findings": [], "resolved_previous": []}))
            self.assertEqual(state["stage"], "已完成（静态审查）")
            frontend = next(repo for repo in state["repos"] if repo["role"] == "frontend")
            self.assertEqual(git(frontend["path"], "log", "-1", "--format=%s"), "feat: 调整页面交互")
            images = engine.validate_followup("解释修改内容", [])
            engine.followup("解释修改内容", images, uuid4().hex, False)
            self.assertEqual(state["stage"], "续聊处理中")
            complete(engine, "已说明修改内容")
            self.assertEqual(state["stage"], "已完成（未审查）")
            self.assertIsNone(state["item"])
            self.assertEqual(self.application.session(state["id"])["state"]["title"], "调整页面交互")
