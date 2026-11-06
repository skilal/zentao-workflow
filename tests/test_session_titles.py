"""会话标题与工作项信息独立保存，兼容旧记录及执行中的状态保存。"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.approvals import is_controlled_write
from workflow.mcp_server import TOOLS, Tools
from workflow.rpc import thread_parameters
from workflow.settings import ROOT, WorkflowError
from workflow.session_titles import title_generation_context
from workflow.store import session_title
from workflow.web import LocalServer, WebApplication


class SessionTitleFixture(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            patched = patch(target, self.root)
            patched.start()
            self.addCleanup(patched.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.application = WebApplication(self.settings)
        self.addCleanup(self.application.close)
        self.state = self.application.sessions.store.new({"kind": "task", "id": 7, "title": "原任务文本"})
        self.runtime = self.application.sessions.add(self.state)
        self.runtime.engine.save()

    def rename(self, title, **changes):
        return self.application.rename_session({"session_id": self.state["id"], "title": title,
            "request_id": str(uuid4()), **changes})


class SessionTitleChecks(SessionTitleFixture):
    def test_default_uses_existing_single_and_joint_item_labels_for_old_records(self):
        legacy = deepcopy(self.state)
        legacy.pop("custom_title")
        legacy.pop("auto_title")
        legacy.pop("title_pending")
        self.assertEqual(session_title(legacy), "任务 #7 · 原任务文本")
        legacy["items"].append({"kind": "story", "id": 8, "title": "原需求文本"})
        self.assertEqual(session_title(legacy), "任务 #7 · 原任务文本；需求 #8 · 原需求文本")
        legacy.pop("items")
        self.assertEqual(session_title(legacy), "任务 #7 · 原任务文本")

    def test_custom_title_survives_restarting_and_keeps_item_information(self):
        original_items = deepcopy(self.state["items"])
        payload = self.rename("  自定义交付标题  ")
        self.assertEqual(payload["state"]["title"], "自定义交付标题")
        self.assertEqual(payload["state"]["items"], original_items)
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(self.state["id"])["state"]["title"], "自定义交付标题")
        self.assertEqual(restored.snapshot()["history"][0]["title"], "自定义交付标题")
        self.assertTrue(any(b"event: state" in packet and "自定义交付标题".encode() in packet
                            for _, packet in self.application.events))

    def test_renaming_running_session_preserves_live_state_object_and_later_saves(self):
        self.state.update(stage="实施中", phase="implement")
        self.runtime.engine.turn_running = True
        self.runtime.busy = "plan"
        self.rename("执行中的自定义标题")
        self.assertIs(self.runtime.engine.state, self.state)
        self.assertEqual(self.state["stage"], "实施中")
        self.assertTrue(self.runtime.engine.turn_running)
        self.assertEqual(self.runtime.busy, "plan")
        self.runtime.engine.note("Codex", "后台继续保存进度")
        self.assertEqual(self.runtime.engine.store.load(self.state["id"])["custom_title"], "执行中的自定义标题")

    def test_duplicate_request_and_other_sessions_are_isolated(self):
        other = self.application.sessions.add(self.application.sessions.store.new(
            {"kind": "bug", "id": 9, "title": "另一个工作项"}))
        request_id = str(uuid4())
        self.rename("第一次标题", request_id=request_id)
        self.rename("第二次标题")
        duplicate = self.rename("第一次标题", request_id=request_id)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.state["custom_title"], "第二次标题")
        self.assertEqual(session_title(other.engine.state), "待命名会话")

    def test_invalid_deleted_and_cleaning_sessions_cannot_be_renamed(self):
        for title in (None, 42, "", " \n "):
            with self.subTest(title=title), self.assertRaises(WorkflowError):
                self.rename(title)
        with self.assertRaises(WorkflowError):
            self.rename("标题", session_id="已删除的会话")
        self.state["cleanup"] = {"finalizing": True}
        with self.assertRaisesRegex(WorkflowError, "清理阶段"):
            self.rename("标题")
        self.assertEqual(self.state["custom_title"], "")

    def test_save_failure_keeps_title_and_revision_unchanged(self):
        revision = self.runtime.revision
        with patch.object(self.runtime.engine.store, "save", side_effect=OSError("无法写入")):
            with self.assertRaises(OSError):
                self.rename("尚未保存的标题")
        self.assertEqual(self.state["custom_title"], "")
        self.assertEqual(self.runtime.revision, revision)

    def test_http_requires_cookie_and_csrf_and_returns_the_updated_title(self):
        server = LocalServer(("127.0.0.1", 0), self.application)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("GET", "/")
        response = client.getresponse()
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        response.read()
        client.close()
        payload = {"session_id": self.state["id"], "title": "接口设置的标题", "request_id": str(uuid4())}
        for headers, expected in (({}, 400), ({"Cookie": cookie}, 400),
                ({"Cookie": cookie, "X-Workflow-Token": self.application.csrf_token}, 200)):
            with closing(HTTPConnection("127.0.0.1", server.server_port, timeout=5)) as client:
                client.request("POST", "/api/sessions/title", dumps(payload),
                    {"Content-Type": "application/json", **headers})
                response = client.getresponse()
                value = loads(response.read())
                self.assertEqual(response.status, expected)
                if expected == 200:
                    self.assertEqual(value["state"]["title"], "接口设置的标题")


class GeneratedSessionTitleChecks(SessionTitleFixture):
    def setUp(self):
        super().setUp()
        self.engine = self.runtime.engine
        self.store = self.engine.store
        self.state.update(stage="计划中", phase="plan", thread_id="命名线程", turn_id="命名回合",
                          codex={"model": "模拟模型", "reasoning_effort": "high"}, supplement="支持数字范围检索",
                          snapshot={"sources": [{"title": "原任务文本", "content": "档案管理增加全宗号范围检索"},
                                                {"title": "关联需求", "content": "保留原有条件组合"}],
                                    "materials": [], "blockers": [], "feishu_links": []})
        self.engine.save()

    def register(self, title="全宗号范围检索"):
        return Tools(self.state["id"]).call("set_session_title", {"title": title})

    def title_event(self):
        self.engine._event({"method": "item/completed", "params": {
            "threadId": self.state["thread_id"], "turnId": self.state["turn_id"],
            "item": {"type": "mcpToolCall", "server": "workflow", "tool": "set_session_title"}}},
            self.engine.connection_epoch)

    def test_all_work_item_kinds_register_without_writing_state_then_publish_and_persist(self):
        for kind in ("task", "bug", "story"):
            with self.subTest(kind=kind):
                state = self.store.new({"kind": kind, "id": 11, "title": "冗长的原始工作项标题"})
                state.update(stage="计划中", phase="plan", thread_id="线程", turn_id="回合")
                runtime = self.application.sessions.add(state)
                runtime.engine.save()
                before = self.store.load(state["id"])
                self.assertEqual(session_title(state), "待命名会话")
                result = Tools(state["id"]).call("set_session_title", {"title": "档案日期同步修复"})
                self.assertEqual(result["title"], "档案日期同步修复")
                self.assertEqual(self.store.load(state["id"]), before)
                state["plan"] = "命名登记期间的后台计划"
                runtime.engine.save()
                self.assertEqual(session_title(state), "档案日期同步修复")
                self.assertFalse(state["title_pending"])
                self.assertEqual(state["plan"], "命名登记期间的后台计划")
                self.assertEqual(state["items"], before["items"])
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        for entry in restored.snapshot()["history"]:
            if entry["id"] != self.state["id"]:
                self.assertEqual(entry["title"], "档案日期同步修复")

    def test_tool_completed_updates_existing_live_state_and_emits_title(self):
        self.register()
        revision = self.runtime.revision
        self.title_event()
        self.assertIs(self.engine.state, self.state)
        self.assertGreater(self.runtime.revision, revision)
        self.assertEqual(self.application.session(self.state["id"])["state"]["title"], "全宗号范围检索")
        self.assertEqual(self.application.snapshot()["history"][0]["title"], "全宗号范围检索")
        self.assertTrue(any(b"event: state" in packet and "全宗号范围检索".encode() in packet
                            for _, packet in self.application.events))

    def test_joint_task_suffix_and_length_are_controlled_and_duplicate_calls_keep_first_title(self):
        self.state["items"].extend({"kind": "task", "id": number, "title": f"其他任务{number}"} for number in (8, 9))
        self.engine.save()
        context = title_generation_context(self.state)
        self.assertEqual(context["primary_item"], self.state["items"][0])
        self.assertEqual(context["suffix"], "等 3 项")
        with self.assertRaises(WorkflowError):
            self.register("名" * (context["max_length"] + 1))
        result = self.register("名" * context["max_length"])
        self.assertEqual(len(result["title"]), 20)
        self.assertTrue(result["title"].endswith("等 3 项"))
        self.assertEqual(self.register("不同概括"), result)
        self.title_event()
        self.assertEqual(self.register("再次概括")["title"], result["title"])

    def test_empty_multiline_and_overlong_names_do_not_create_request(self):
        for title in (None, 7, "", "  ", "一\n二", "一\r二", "一\t二", "一\u2028二", "名" * 21):
            with self.subTest(title=title), self.assertRaises(WorkflowError):
                self.register(title)
        self.assertFalse((self.store.directory(self.state["id"]) / "session-title-request.json").exists())
        self.assertEqual(self.register("  " + "名" * 20 + "  ")["title"], "名" * 20)

    def test_manual_name_wins_before_registration_during_delivery_and_after_generation(self):
        self.rename("人工指定名称")
        self.assertEqual(self.register()["status"], "unchanged")
        self.assertIsNone(title_generation_context(self.state))
        self.assertFalse((self.store.directory(self.state["id"]) / "session-title-request.json").exists())
        # 用新会话模拟模型已写出结果、控制器尚未收到事件时人工改名。
        other = self.store.new({"kind": "bug", "id": 12, "title": "另一工作项"})
        other.update(stage="计划中", thread_id="另一线程", turn_id="另一回合")
        runtime = self.application.sessions.add(other)
        runtime.engine.save()
        Tools(other["id"]).call("set_session_title", {"title": "模型生成名称"})
        self.rename("人工抢先命名", session_id=other["id"])
        runtime.engine.save()
        self.assertEqual(session_title(other), "人工抢先命名")
        self.assertEqual(other["auto_title"], "")
        third = self.store.new({"kind": "story", "id": 13, "title": "第三工作项"})
        third.update(stage="计划中", thread_id="第三线程", turn_id="第三回合")
        third_runtime = self.application.sessions.add(third)
        third_runtime.engine.save()
        Tools(third["id"]).call("set_session_title", {"title": "初始概括名称"})
        third_runtime.engine.save()
        self.rename("人工最终名称", session_id=third["id"])
        third_runtime.engine.note("Codex", "后台继续保存")
        self.assertEqual(self.store.load(third["id"])["custom_title"], "人工最终名称")

    def test_restart_collects_unconsumed_title_and_keeps_untitled_sessions_pending(self):
        self.register()
        other = self.store.new({"kind": "task", "id": 8, "title": "尚未命名的工作项"})
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        state = restored.sessions.get(self.state["id"]).engine.state
        self.assertEqual(session_title(state), "全宗号范围检索")
        self.assertFalse(state["title_pending"])
        pending = restored.sessions.get(other["id"]).engine.state
        self.assertTrue(pending["title_pending"])
        self.assertEqual(session_title(pending), "待命名会话")

    def test_parallel_registration_and_manual_rename_preserve_progress_and_manual_title(self):
        self.state["plan"] = "保留后台计划"
        self.engine.save()
        with ThreadPoolExecutor(max_workers=3) as executor:
            jobs = [executor.submit(self.register), executor.submit(self.rename, "人工并发名称"),
                    executor.submit(self.engine.save)]
            for job in jobs:
                job.result(timeout=5)
        self.title_event()
        saved = self.store.load(self.state["id"])
        self.assertEqual(saved["custom_title"], "人工并发名称")
        self.assertEqual(saved["plan"], "保留后台计划")
        self.assertEqual(session_title(saved), "人工并发名称")

    def test_legacy_and_regular_conversations_never_request_generation(self):
        legacy = deepcopy(self.state)
        legacy.pop("auto_title")
        legacy.pop("title_pending")
        self.store.save(legacy)
        self.assertIsNone(title_generation_context(legacy))
        with self.assertRaises(WorkflowError):
            self.register()
        conversation = self.store.new(None)
        conversation["conversation_title"] = "普通会话原有名称"
        self.store.save(conversation)
        self.assertIsNone(title_generation_context(conversation))
        self.assertEqual(session_title(conversation), "普通会话原有名称")
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        state = restored.sessions.get(legacy["id"]).engine.state
        self.assertNotIn("title_pending", state)
        self.assertEqual(session_title(state), "任务 #7 · 原任务文本")

    def test_only_plan_naming_tool_is_auto_authorized_and_business_writes_remain_forbidden(self):
        description = next(description for name, description, _ in TOOLS if name == "set_session_title")
        params = {"serverName": "workflow", "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call",
                  "tool_description": description, "tool_params": {"title": "范围检索改进"}}}
        self.assertTrue(is_controlled_write(self.state, params))
        with self.assertRaises(WorkflowError):
            Tools(self.state["id"]).call("write_file", {"repository": "前端", "path": "业务.txt", "content": "不能写入"})
        for changes in ({"phase": "branch", "stage": "识别分支中"}, {"phase": "implement", "stage": "实施中"},
                        {"phase": "plan", "stage": "已暂停"}, {"phase": "plan", "stage": "等待人工回答"},
                        {"phase": "plan", "stage": "计划中", "followup": {"text": "续聊"}}):
            self.state.update(changes)
            self.engine.save()
            self.assertFalse(is_controlled_write(self.state, params))
            with self.assertRaises(WorkflowError):
                self.register()

    def test_plan_prompt_uses_full_materials_and_current_model_without_an_extra_turn(self):
        self.engine.ensure_rpc = Mock()
        self.engine.rpc = Mock()
        self.engine.rpc.request.return_value = {"turn": {"id": "计划回合"}}
        self.engine._start_turn("按当前资料制定计划")
        self.engine.rpc.request.assert_called_once()
        method, params = self.engine.rpc.request.call_args.args
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["collaborationMode"]["settings"]["model"], "模拟模型")
        prompt = params["input"][0]["text"]
        self.assertIn("workflow.task_context(include_materials=true)", prompt)
        self.assertIn("workflow.set_session_title(title)", prompt)
        self.assertIn("6～12", prompt)
        context = Tools(self.state["id"]).call("task_context", {"include_materials": True})
        self.assertEqual(context["snapshot"]["sources"],
                         [{**source, "material_ids": []} for source in self.state["snapshot"]["sources"]])
        self.assertEqual(context["supplement"], "支持数字范围检索")
        self.assertEqual(context["session_title_generation"]["primary_item"], self.state["item"])
        self.register()
        self.title_event()
        self.engine.rpc.request.reset_mock()
        self.engine._start_turn("继续计划")
        self.engine.rpc.request.assert_called_once()
        self.assertNotIn("workflow.set_session_title(title)", self.engine.rpc.request.call_args.args[1]["input"][0]["text"])
        tools = thread_parameters(self.state, "模拟模型", {})["config"]["mcp_servers"]["workflow"]["enabled_tools"]
        self.assertIn("set_session_title", tools)

    def test_interruption_without_title_prompts_on_resume_but_followup_never_renames(self):
        self.engine.ensure_rpc = Mock()
        self.engine.rpc = Mock()
        self.engine.rpc.request.return_value = {"turn": {"id": "恢复回合"}}
        self.state.update(stage="已暂停", prepare_complete=True)
        self.engine.resume()
        prompt = self.engine.rpc.request.call_args.args[1]["input"][0]["text"]
        self.assertIn("workflow.set_session_title(title)", prompt)
        self.state.update(followup={"text": "新的补充需求"}, phase="plan", delivery_round=2)
        self.engine._start_turn("处理补充计划")
        self.assertNotIn("workflow.set_session_title(title)", self.engine.rpc.request.call_args.args[1]["input"][0]["text"])
