"""检查资料去重、局部搜索、分段批量阅读和人工等待的交付边界。"""

from copy import deepcopy
from json import loads
from os import scandir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.approvals import is_controlled_write
from workflow.engine import Workflow
from workflow.gitops import git
from workflow.mcp_server import TOOLS, Tools
from workflow.sessions import SessionManager
from workflow.settings import ROOT, WorkflowError
from workflow.user_input import input_request_path


class PerformanceFixture(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        mocked = patch("workflow.store.LOCAL", self.root / "local")
        mocked.start()
        self.addCleanup(mocked.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.flow = Workflow(self.settings, Mock())
        self.addCleanup(self.flow.close)
        self.state = self.flow.store.new({"kind": "task", "id": 7, "title": "性能检查"})
        self.flow.state = self.state
        self.state.update(snapshot={"sources": [{"title": "需求", "content": "验收正文", "version": 2,
                                               "material_ids": [9], "raw": {"desc": "重复的接口原文"}}],
                                    "materials": [], "blockers": [], "feishu_links": []},
                          codex={"model": "模拟模型", "reasoning_effort": "high"},
                          thread_id="原线程", prepare_complete=True, max_fix_rounds=3)
        self.rpc = Mock(model="模拟模型")
        self.rpc.request.return_value = {"turn": {"id": "原回合"}}
        self.flow.rpc = self.rpc
        self.flow.ensure_rpc = Mock()
        self.flow.save()
        self.tools = Tools(self.state["id"])


class FileReadChecks(PerformanceFixture):
    def setUp(self):
        super().setUp()
        self.repository = self.root / "repo"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "示例分支")
        git(self.repository, "config", "user.name", "文件检查")
        git(self.repository, "config", "user.email", "files@example.invalid")
        git(self.repository, "config", "core.autocrlf", "false")
        self.path = self.repository / "src" / "业务.txt"
        self.path.parent.mkdir()
        self.path.write_text("\ufeff" + "\r\n".join(f"基线第 {i} 行" for i in range(1, 802)),
                             encoding="utf-8", newline="")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "初始化脱敏资料")
        self.state["repos"] = [{"name": "前端", "path": str(self.repository),
                                "baseline": git(self.repository, "rev-parse", "HEAD")}]
        self.flow.save()
        self.path.write_text("\n".join(f"当前第 {i} 行" for i in range(1, 802)), encoding="utf-8", newline="\n")

    def test_baseline_and_current_files_are_paginated_with_continuation(self):
        for name, expected in (("baseline_file", "基线"), ("read_file", "当前")):
            with self.subTest(name=name):
                first = self.tools.call(name, {"repository": "前端", "path": "src/业务.txt"})
                self.assertEqual((first["total_lines"], first["next_line"]), (801, 201))
                self.assertEqual(len(first["text"].splitlines()), 200)
                self.assertTrue(first["text"].startswith(f"1: {expected}第 1 行"))
                last = self.tools.call(name, {"repository": "前端", "path": "src/业务.txt", "start_line": 801})
                self.assertIsNone(last["next_line"])
                self.assertEqual(last["text"], f"801: {expected}第 801 行")
        self.assertEqual(first["line_ending"], "LF")

    def test_batch_reads_mix_current_and_baseline_in_one_state_read(self):
        with patch.object(self.tools.store, "load", wraps=self.tools.store.load) as load:
            result = self.tools.call("read_files", {"files": [
                {"repository": "前端", "path": "src/业务.txt", "start_line": 30, "line_count": 2},
                {"repository": "前端", "path": "src/业务.txt", "start_line": 30, "line_count": 2, "baseline": True}]})
        load.assert_called_once()
        current, baseline = result["files"]
        self.assertIn("30: 当前第 30 行", current["text"])
        self.assertIn("30: 基线第 30 行", baseline["text"])
        self.assertEqual((baseline["line_ending"], baseline["utf8_bom"], baseline["next_line"]), ("CRLF", True, 32))

    def test_invalid_batch_is_rejected_before_reading_any_file(self):
        valid = {"repository": "前端", "path": "src/业务.txt"}
        for requests in ([], [valid] * 11, [valid, {**valid, "line_count": 3000}],
                         [{**valid, "line_count": 500}] * 5, [valid, {**valid, "path": "../外部.txt"}],
                         [{**valid, "baseline": "true"}]):
            with self.subTest(requests=requests), patch.object(self.tools, "read_segment") as read:
                with self.assertRaises(WorkflowError):
                    self.tools.call("read_files", {"files": requests})
                read.assert_not_called()

    def test_exact_search_and_missing_file_do_not_enumerate_repository(self):
        with patch("workflow.mcp_server.scandir", side_effect=AssertionError("精确路径不得遍历目录")):
            result = self.tools.call("search_text", {"repository": "前端", "pattern": "src/业务.txt", "text": "当前第 77 行"})
            self.assertEqual(result, {"matches": [{"path": "src/业务.txt", "line": 77, "text": "当前第 77 行"}], "truncated": False})
            self.assertEqual(self.tools.call("list_files", {"repository": "前端", "pattern": "不存在.txt"})["files"], [])

    def test_prefixed_glob_skips_unrelated_directories_and_preserves_order(self):
        (self.repository / "其他模块").mkdir()
        (self.repository / "其他模块" / "不应扫描.txt").write_text("当前", encoding="utf-8")
        (self.path.parent / "z.txt").write_text("当前", encoding="utf-8")
        (self.path.parent / "sub").mkdir()
        (self.path.parent / "sub" / "b.txt").write_text("当前", encoding="utf-8")
        (self.path.parent / "node_modules").mkdir()
        (self.path.parent / "node_modules" / "依赖.txt").write_text("当前", encoding="utf-8")
        visited = []
        def enumerate_directory(path):
            visited.append(Path(path))
            return scandir(path)
        with patch("workflow.mcp_server.scandir", side_effect=enumerate_directory):
            result = self.tools.call("list_files", {"repository": "前端", "pattern": "src/*.txt"})
        self.assertEqual(result["files"], ["src/z.txt", "src/业务.txt", "src/sub/b.txt"])
        self.assertEqual(set(visited), {self.path.parent, self.path.parent / "sub"})

    def test_search_keeps_result_limit_and_excludes_protected_directories(self):
        result = self.tools.call("search_text", {"repository": "前端", "pattern": "src/业务.txt", "text": "当前"})
        self.assertEqual(len(result["matches"]), 100)
        self.assertTrue(result["truncated"])
        self.assertEqual(self.tools.call("list_files", {"repository": "前端", "pattern": ".git/config"})["files"], [])
        with self.assertRaises(WorkflowError):
            self.tools.call("list_files", {"repository": "前端", "pattern": "../*.txt"})


class ContextDeliveryChecks(PerformanceFixture):
    def image(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        return {"id": name, "path": str(path), "image": True}

    def start(self, phase="plan"):
        self.state["phase"] = phase
        self.flow._start_turn("处理本轮消息")
        return self.rpc.request.call_args.args[1]["input"]

    def test_same_thread_only_sends_changed_fields_and_new_image_content(self):
        image = self.image("原图.png", b"original")
        duplicate = self.image("重复图.png", b"original")
        self.state["snapshot"]["materials"] = [image]
        self.state["supplement_images"] = [duplicate]
        self.state["approved_plan"] = "唯一计划正文"
        first = self.start()
        self.assertEqual([value["path"] for value in first if value["type"] == "localImage"], [image["path"]])
        self.assertIn("唯一计划正文", first[0]["text"])
        references = loads(first[0]["text"].split("工作流上下文：\n", 1)[1])["image_references"]
        self.assertEqual([image["path"], duplicate["path"]], [value["path"] for value in references])
        self.assertEqual(references[0]["content_hash"], references[1]["content_hash"])
        self.assertFalse(any(value["type"] == "skill" for value in first))
        second = self.start("implement")
        self.assertEqual([value["type"] for value in second], ["text"])
        self.assertNotIn("唯一计划正文", second[0]["text"])
        self.assertNotIn('"items":', second[0]["text"])
        self.assertNotIn('"image_references":', second[0]["text"])
        changed = self.image("新图.png", b"changed")
        self.state["supplement_images"].append(changed)
        third = self.start("review")
        self.assertEqual([value["path"] for value in third if value["type"] == "localImage"], [changed["path"]])

    def test_deduplication_survives_reloading_but_resets_for_another_thread(self):
        image = self.image("原图.png", b"original")
        self.state["snapshot"]["materials"] = [image]
        self.start()
        self.flow.state = self.flow.store.load(self.state["id"])
        self.state = self.flow.state
        self.assertEqual([value["type"] for value in self.start("fix")], ["text"])
        self.state["thread_id"] = "另一个明确创建的线程"
        self.assertIn({"type": "localImage", "path": image["path"]}, self.start())

    def test_failed_start_does_not_record_unsent_context_or_images(self):
        self.state["snapshot"]["materials"] = [self.image("原图.png", b"original")]
        self.rpc.request.side_effect = WorkflowError("模拟请求停止")
        with self.assertRaises(WorkflowError):
            self.start()
        self.assertNotIn("thread_input", self.flow.store.load(self.state["id"]))

    def test_materials_are_explicit_normalized_and_versioned_without_changing_snapshot(self):
        before = deepcopy(self.state["snapshot"])
        default = self.tools.call("task_context", {})
        self.assertFalse(default["materials_included"])
        self.assertNotIn("sources", default["snapshot"])
        full = self.tools.call("task_context", {"include_materials": True})
        source = full["snapshot"]["sources"][0]
        self.assertEqual((source["content"], source["version"], source["material_ids"]), ("验收正文", 2, [9]))
        self.assertNotIn("raw", source)
        self.assertEqual(self.state["snapshot"], before)
        self.state["snapshot"]["sources"][0]["content"] = "更新验收正文"
        self.flow.save()
        updated = self.tools.call("task_context", {"include_materials": True})
        self.assertNotEqual(full["materials_revision"], updated["materials_revision"])

    def test_legacy_material_ownership_is_retained_after_removing_raw_source(self):
        source = self.state["snapshot"]["sources"][0]
        del source["material_ids"]
        source["raw"] = {"files": {"9": {"id": 9}},
                         "desc": '<img src="https://example.invalid/file-read?fileID=11"><img src="?fileID=9">'}
        self.flow.save()
        result = self.tools.call("task_context", {"include_materials": True})
        self.assertEqual(result["snapshot"]["sources"][0]["material_ids"], [9, 11])
        self.assertNotIn("raw", result["snapshot"]["sources"][0])
        self.assertIn("raw", self.flow.store.load(self.state["id"])["snapshot"]["sources"][0])


QUESTIONS = [{"id": "scope", "header": "确认范围", "question": "是否将文件当前内容纳入？",
              "options": [{"label": "纳入", "description": "明确接受当前内容"},
                          {"label": "不纳入", "description": "保留现有范围"}]}]


class ControllerInputChecks(PerformanceFixture):
    def setUp(self):
        super().setUp()
        self.flow._new_plan_round()
        self.flow._start_turn("开始分析")

    def register(self, questions=None):
        return self.tools.call("ask_user", {"questions": questions if questions is not None else QUESTIONS})

    def capture(self):
        self.flow._event({"method": "item/completed", "params": {"threadId": self.state["thread_id"],
            "turnId": self.state["turn_id"], "item": {"type": "mcpToolCall", "server": "workflow", "tool": "ask_user"}}},
            self.flow.connection_epoch)
        self.flow.control_executor.submit(lambda: None).result(timeout=5)

    def complete(self, status="interrupted"):
        self.flow._event({"method": "turn/completed", "params": {"threadId": self.state["thread_id"],
            "turn": {"id": self.state["turn_id"], "status": status}}}, self.flow.connection_epoch)
        self.flow.executor.submit(lambda: None).result(timeout=5)

    def test_question_interrupts_turn_and_blocks_tools_and_empty_answers(self):
        self.assertEqual(self.register()["status"], "awaiting_user")
        with self.assertRaisesRegex(WorkflowError, "等待用户明确答复"):
            self.tools.call("write_file", {"repository": "前端", "path": "业务.txt", "content": "不应写入"})
        with self.assertRaisesRegex(WorkflowError, "等待用户明确答复"):
            self.tools.call("task_context", {})
        self.capture()
        self.assertEqual(self.state["stage"], "等待人工回答")
        self.assertTrue(self.state["plan_round"]["asked"])
        self.rpc.request.assert_called_with("turn/interrupt", {"threadId": "原线程", "turnId": "原回合"})
        with self.assertRaisesRegex(WorkflowError, "正在结束"):
            self.flow.answer("明确答复")
        self.complete()
        self.assertTrue(self.state["pending"]["turn_completed"])
        before = deepcopy(self.state["history"])
        for text, answers in (("", None), ("", {"scope": []}), ("", {"scope": [" "]}), ("", {"other": ["纳入"]})):
            with self.subTest(answers=answers), self.assertRaises(WorkflowError):
                self.flow.answer(text, answers)
        self.assertEqual(self.state["history"], before)
        self.assertTrue(input_request_path(self.flow.store, self.state).is_file())
        with self.assertRaises(WorkflowError):
            self.flow._start_turn("不能绕过未答问题")

    def test_explicit_answer_restarts_original_phase_and_does_not_auto_approve_plan(self):
        self.register()
        self.capture()
        self.complete()
        self.rpc.request.return_value = {"turn": {"id": "回答后的回合"}}
        self.flow.answer("", {"scope": ["纳入"]})
        self.assertIsNone(self.state["pending"])
        self.assertEqual((self.state["thread_id"], self.state["phase"]), ("原线程", "plan"))
        self.assertFalse(input_request_path(self.flow.store, self.state).exists())
        self.assertIn("回答：纳入", self.rpc.request.call_args.args[1]["input"][0]["text"])
        self.flow.last_message = "<proposed_plan>明确范围后的计划</proposed_plan>"
        self.complete("completed")
        self.assertEqual(self.state["stage"], "等待计划确认")
        self.assertFalse(self.state["approved_hash"])

    def test_restart_recovers_even_a_question_not_yet_captured_by_controller(self):
        self.register()
        manager = SessionManager(self.settings, Mock())
        self.addCleanup(manager.close)
        recovered = manager.get(self.state["id"]).engine
        self.assertEqual(recovered.state["stage"], "等待人工回答")
        self.assertTrue(recovered.state["pending"]["turn_completed"])
        self.assertTrue(recovered.state["plan_round"]["asked"])
        self.assertEqual(recovered.state["pending"]["params"]["questions"], QUESTIONS)
        recovered.ensure_rpc = Mock()
        recovered.rpc = Mock()
        recovered.rpc.request.return_value = {"turn": {"id": "重启后回答"}}
        recovered.answer("明确不纳入当前内容")
        self.assertEqual(recovered.rpc.request.call_args.args[1]["threadId"], "原线程")

    def test_pause_and_resume_preserve_unanswered_question(self):
        self.register()
        self.capture()
        self.complete()
        self.flow.pause()
        self.rpc.request.reset_mock()
        self.flow.resume()
        self.rpc.request.assert_not_called()
        self.assertEqual(self.state["stage"], "等待人工回答")
        self.assertTrue(self.state["pending"]["turn_completed"])

    def test_disconnect_after_question_allows_explicit_answer_in_original_thread(self):
        self.register()
        self.flow._event({"method": "client/disconnected", "params": {}}, self.flow.connection_epoch)
        self.assertEqual(self.state["stage"], "等待人工回答")
        self.assertTrue(self.state["pending"]["turn_completed"])
        self.flow.answer("补充：只接受本轮范围", {"scope": ["纳入"]})
        text = self.rpc.request.call_args.args[1]["input"][0]["text"]
        self.assertIn("回答：纳入", text)
        self.assertIn("补充：只接受本轮范围", text)

    def test_multiple_questions_require_every_explicit_answer(self):
        self.register([*QUESTIONS, {"id": "rule", "header": "规则", "question": "本轮业务规则是什么？"}])
        self.capture()
        self.complete()
        for text, answers in (("统一确认", None), ("", {"scope": ["纳入"]})):
            with self.subTest(answers=answers), self.assertRaises(WorkflowError):
                self.flow.answer(text, answers)

    def test_ask_user_control_is_allowed_during_plan_without_write_authorization(self):
        description = next(description for name, description, _ in TOOLS if name == "ask_user")
        params = {"serverName": "workflow", "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call",
                  "tool_description": description, "tool_params": {"questions": QUESTIONS}}}
        self.assertTrue(is_controlled_write(self.state, params))
        self.state["stage"] = "等待人工回答"
        self.assertFalse(is_controlled_write(self.state, params))

    def test_invalid_questions_do_not_create_waiting_request(self):
        for questions in ([], QUESTIONS * 2, [{**QUESTIONS[0], "default": "纳入"}],
                          [{**QUESTIONS[0], "options": [{"label": "纳入"}]}]):
            with self.subTest(questions=questions), self.assertRaises(WorkflowError):
                self.register(questions)
        self.assertFalse(input_request_path(self.flow.store, self.state).exists())

    def test_late_completion_does_not_stop_new_turn(self):
        self.flow._event({"method": "turn/completed", "params": {"threadId": "原线程",
                         "turn": {"id": "已经结束的旧回合", "status": "interrupted"}}}, self.flow.connection_epoch)
        self.assertTrue(self.flow.turn_running)
