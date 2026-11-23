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

from workflow.engine import PHASE_STAGE, Workflow
from workflow.gitops import git
from review_fixtures import VersionedTools as Tools, scoped_result
from workflow.projects import project_locked
from workflow.settings import ROOT, WorkflowError
from workflow.store import session_title
from workflow.user_input import INPUT_STAGES, register_input
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
        self.assertEqual(self.application.session(state["id"])["state"]["title"], "待命名会话")
        self.assertTrue(state["title_pending"])
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
        self.assertEqual(context["session_title_generation"],
                         {"max_length": 20, "suffix": "", "primary_item": None})

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
                self.assertEqual(self.application.session(state["id"])["state"]["title"], "待命名会话")
                state.update(stage="计划中", thread_id="命名线程", turn_id="命名回合")
                runtime.engine.save()
                Tools(state["id"]).call("set_session_title", {"title": "页面交互优化"})
                runtime.engine.save()
                self.assertEqual(session_title(state), "页面交互优化")
                self.assertFalse(state["title_pending"])

    def test_manual_title_overrides_uncollected_conversation_title_and_survives_restart(self):
        runtime = self.promote(self.conversation(41), "希望把档案列表里的查询条件改为支持范围检索")
        engine, state = runtime.engine, runtime.engine.state
        state.update(stage="计划中", thread_id="命名线程", turn_id="命名回合")
        engine.save()
        Tools(state["id"]).call("set_session_title", {"title": "档案范围检索"})
        self.application.rename_session({"session_id": state["id"], "title": "人工指定名称", "request_id": uuid4().hex})
        engine.save()
        self.assertEqual(session_title(state), "人工指定名称")
        self.assertEqual(state["auto_title"], "")
        self.assertEqual(Tools(state["id"]).call("set_session_title", {"title": "另一个名称"})["status"], "unchanged")
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(state["id"])["state"]["title"], "人工指定名称")

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

    def steering_session(self):
        runtime = self.promote(self.conversation(41))
        engine = runtime.engine
        engine.state.update(stage="计划中", phase="plan", thread_id="原线程", turn_id="原回合", rpc_connected=True)
        engine.rpc = Mock()
        engine.rpc.request.return_value = {"turnId": "原回合"}
        engine.turn_running = True
        engine.start_progress("原回合")
        return runtime

    def test_steer_saves_ordered_messages_in_the_current_turn_once(self):
        runtime = self.steering_session()
        engine, state = runtime.engine, runtime.engine.state
        runtime.busy = "plan"
        requests = [self.request("steer", data={"text": text, "turn_id": "原回合"},
                                 session_id=state["id"], revision=-1) for text in ("先核对输入", "再考虑分页")]
        for request in requests:
            self.assertEqual(self.application.dispatch(request)["turn_id"], "原回合")
        engine.turn_running = False
        self.assertTrue(self.application.dispatch(requests[0])["duplicate"])
        with self.assertRaisesRegex(WorkflowError, "请求标识已被使用"):
            self.application.dispatch({**requests[0], "data": {"text": "不同文字", "turn_id": "原回合"}})
        for call, request in zip(engine.rpc.request.call_args_list, requests):
            self.assertEqual(call.args, ("turn/steer", {"threadId": "原线程", "expectedTurnId": "原回合",
                "input": [{"type": "text", "text": request["data"]["text"]}], "clientUserMessageId": request["request_id"]}))
        self.assertEqual(engine.rpc.request.call_count, 2)
        history = engine.store.load(state["id"])["history"]
        self.assertEqual([entry["text"] for entry in history], ["调整页面交互", "先核对输入", "再考虑分页"])
        self.assertTrue(all((entry["role"], entry["turn_id"], entry["delivery_round"]) == ("用户", "原回合", 1)
                            for entry in history[1:]))
        self.assertEqual(len(state["progress_turns"]), 1)
        self.assertEqual(state["supplement"], "调整页面交互")

    def test_steer_cannot_target_another_turn_or_bypass_a_pending_question(self):
        runtime = self.steering_session()
        engine, state = runtime.engine, runtime.engine.state
        before = deepcopy(state["history"])
        request = self.request("steer", data={"text": "补充说明", "turn_id": "原回合"}, session_id=state["id"])
        for phase, stage in PHASE_STAGE.items():
            state.update(phase=phase, stage=stage)
            self.assertEqual(self.application.session(state["id"])["state"]["steer_turn_id"],
                             "原回合" if stage in INPUT_STAGES else None)
        state.update(phase="plan", stage="计划中")
        for changes in ({"turn_id": "旧回合"}, {"text": " "}, {"attachment_ids": ["附件"]}):
            with self.subTest(changes=changes), self.assertRaises(WorkflowError):
                self.application.dispatch({**request, "data": {**request["data"], **changes}})
        for key, value in (("pause_requested", True), ("rpc_connected", False), ("stage", "编译检查中"),
                           ("pending", {"id": 1, "params": {"questions": []}})):
            old = state.get(key)
            state[key] = value
            with self.subTest(key=key), self.assertRaises(WorkflowError):
                self.application.dispatch(request)
            state[key] = old
        register_input(engine.store, state, [{"id": "scope", "header": "范围", "question": "是否调整范围？"}])
        with self.assertRaises(WorkflowError):
            self.application.dispatch(request)
        engine.rpc.request.assert_not_called()
        self.assertEqual(state["history"], before)

    def test_steer_rejects_a_mismatched_response_without_stopping_the_turn(self):
        runtime = self.steering_session()
        engine, state = runtime.engine, runtime.engine.state
        request = self.request("steer", data={"text": "补充说明", "turn_id": "原回合"}, session_id=state["id"])
        for response in (None, {"turnId": "下一回合"}):
            engine.rpc.request.return_value = response
            with self.assertRaisesRegex(WorkflowError, "发送结果未确认"):
                self.application.dispatch(request)
        self.assertEqual(state["stage"], "计划中")
        self.assertTrue(engine.turn_running)
        self.assertEqual(len(state["history"]), 1)

    def test_steer_is_recorded_before_completion_even_if_captured_first(self):
        runtime = self.steering_session()
        engine, state = runtime.engine, runtime.engine.state
        engine.last_message = "已纳入补充说明"

        def request_steer(method, params):
            engine._event({"method": "turn/completed", "params": {"threadId": "原线程",
                           "turn": {"id": "原回合", "status": "completed"}}}, engine.connection_epoch)
            self.assertFalse(engine.turn_running)
            return {"turnId": "原回合"}

        engine.rpc.request.side_effect = request_steer
        self.application.dispatch(self.request("steer", data={"text": "补充说明", "turn_id": "原回合"}, session_id=state["id"]))
        self.drain(runtime)
        self.assertEqual([(entry["role"], entry["text"]) for entry in state["history"][-2:]],
                         [("用户", "补充说明"), ("Codex", "已纳入补充说明")])
        self.assertEqual(state["stage"], "等待计划补充")

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
                    if method == "turn/steer":
                        return {"turnId": params["expectedTurnId"]}
                    return {"turn": {"id": f"模拟回合-{next(turns)}"}}
                engine.rpc.request.side_effect = request
        def complete(engine, text):
            engine.last_message = scoped_result(engine.state, text)
            engine.turn_running = False
            engine._completed({"id": engine.state["turn_id"], "status": "completed"},
                              engine.state["id"], engine.connection_epoch)
        with patch.object(Workflow, "ensure_rpc", connect), patch("workflow.engine.refresh_branches", side_effect=AssertionError("不访问远程")):
            preview = self.conversation(41)
            result = self.application.dispatch(self.plan_request(preview))
            runtime = self.application.sessions.get(result["session_id"])
            self.drain(runtime)
            engine, state = runtime.engine, runtime.engine.state
            def steer(text):
                turn_id = state["turn_id"]
                result = self.application.dispatch(self.request("steer", data={"text": text, "turn_id": turn_id}, session_id=state["id"]))
                self.assertEqual(result["turn_id"], turn_id)
                self.assertEqual(state["turn_id"], turn_id)
            self.assertEqual(state["stage"], "计划中")
            self.assertTrue(all(entry["source"] == "common" for entry in state["branch_resolution"]["repositories"]))
            self.assertIn("未关联禅道工作项", calls[-1][1]["input"][0]["text"])
            self.assertIn("workflow.set_session_title(title)", calls[-1][1]["input"][0]["text"])
            self.assertIn("首条需求消息", calls[-1][1]["input"][0]["text"])
            Tools(state["id"]).call("set_session_title", {"title": "页面交互优化"})
            engine._event({"method": "item/completed", "params": {
                "threadId": state["thread_id"], "turnId": state["turn_id"],
                "item": {"type": "mcpToolCall", "server": "workflow", "tool": "set_session_title"}}},
                engine.connection_epoch)
            self.assertEqual(self.application.session(state["id"])["state"]["title"], "页面交互优化")
            steer("先核对交互入口")
            complete(engine, "<proposed_plan>调整页面交互</proposed_plan>")
            self.assertEqual(state["stage"], "实施中")
            self.assertEqual(state["approval_source"], "auto")
            Tools(state["id"]).call("write_file", {"repository": "前端", "path": "页面.txt", "content": "更新交互\n"})
            complete(engine, "已完成修改")
            self.assertEqual(state["stage"], "审查中")
            steer("重点看没有数据的场景")
            complete(engine, dumps({"summary": "无遗留问题", "findings": [], "resolved_previous": [], "commit_messages": [{"repository": "前端", "type": "feat", "scope": "页面", "summary": "调整页面交互"}]}))
            self.assertEqual(state["stage"], "已完成（静态审查）")
            frontend = next(repo for repo in state["repos"] if repo["role"] == "frontend")
            self.assertEqual(git(frontend["path"], "log", "-1", "--format=%s"), "feat(页面): 调整页面交互")
            images = engine.validate_followup("解释修改内容", [])
            engine.followup("解释修改内容", images, uuid4().hex, False)
            self.assertEqual(state["stage"], "续聊处理中")
            self.assertNotIn("workflow.set_session_title(title)", calls[-1][1]["input"][0]["text"])
            complete(engine, "已说明修改内容")
            self.assertEqual(state["stage"], "已完成（未审查）")
            self.assertIsNone(state["item"])
            self.assertEqual(self.application.session(state["id"])["state"]["title"], "页面交互优化")
            restored = WebApplication(self.settings)
            self.addCleanup(restored.close)
            self.assertEqual(restored.session(state["id"])["state"]["title"], "页面交互优化")
