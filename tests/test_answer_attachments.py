"""隔离检查逐题附件、回答协议、持久化和模拟 Codex 输入。"""

from copy import deepcopy
from http.client import HTTPConnection
from io import BytesIO, StringIO
from json import dumps, loads
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from zipfile import ZipFile

from test_supplement_images import SupplementImageFixture, PNG
from workflow.answer_attachments import answer_request_id, prepare_answer, submitted_attachments
from workflow.mcp_server import Tools, serve
from workflow.rpc import thread_parameters
from workflow.settings import WorkflowError
from workflow.user_input import input_request_path, register_input
from workflow.web import COOKIE_NAME, LocalServer, WebApplication, public_state


class AnswerAttachmentFixture(SupplementImageFixture):
    def setUp(self):
        super().setUp()
        self.engine = self.runtime.engine
        self.state.update(stage="计划中", phase="plan", thread_id="原线程", turn_id="提问回合", prepare_complete=True)
        self.questions = [{"id": "template", "header": "模板", "question": "请提供参考模板"},
                          {"id": "rules", "header": "规则", "question": "请说明验收规则"}]
        request = register_input(self.engine.store, self.state, self.questions)
        self.state.update(stage="等待人工回答", pending={"id": request["request_id"], "method": "workflow/ask_user",
                          "params": {"questions": self.questions}, "turn_completed": True})
        self.engine.rpc = Mock(model="模拟模型")
        self.engine.rpc.request.return_value = {"turn": {"id": "回答后的回合"}}
        self.engine.ensure_rpc = Mock()
        self.engine.save()

    def upload_file(self, name="参考.txt", data="参考内容".encode(), question_id="template", **changes):
        values = {"session_id": self.state["id"], "revision": self.runtime.revision,
                  "request_id": answer_request_id(self.state), "question_id": question_id, "name": name, "data": data, **changes}
        self.application.upload_answer_attachment(**values)
        return self.state["answer_attachments"][-1]

    def submit(self, attachment_ids, answers=None):
        answers, files = prepare_answer(self.state, "", answers or {"template": [], "rules": ["按现有规则"]},
                                       attachment_ids, answer_request_id(self.state))
        self.engine.answer("", answers, files)

    def action(self, **changes):
        return {"action": "answer", "session_id": self.state["id"], "revision": self.runtime.revision,
                "request_id": "本次回答操作", "data": {"text": "", "answers": {"template": ["模板内容"], "rules": ["规则内容"]}}, **changes}


class AnswerAttachmentChecks(AnswerAttachmentFixture):
    def test_multiple_arbitrary_files_same_name_and_question_association(self):
        first = self.upload_file("参考.PDF", b"%PDF-reference")
        second = self.upload_file("参考.PDF", b"another reference", "rules")
        third = self.upload_file("无扩展名", b"reference")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual([first["question_id"], second["question_id"]], ["template", "rules"])
        for entry in (first, second, third):
            self.assertTrue(Path(entry["path"]).is_file())
            self.assertEqual(entry["size"], Path(entry["path"]).stat().st_size)
        self.assertEqual(submitted_attachments(self.state), [])
        self.assertEqual(public_state(self.state)["pending"]["attachment_request_id"], first["request_id"])

    def test_attachment_only_answer_starts_original_thread_and_saves_history(self):
        entry = self.upload_file()
        self.submit({"template": [entry["id"]]})
        params = self.engine.rpc.request.call_args.args[1]
        self.assertEqual(params["threadId"], "原线程")
        self.assertEqual(self.state["phase"], "plan")
        history = next(item for item in self.state["history"] if item["role"] == "用户")
        self.assertEqual(history["attachments"][0]["id"], entry["id"])
        self.assertIn("请提供参考模板", history["text"])
        self.assertIn(entry["id"], history["text"])
        self.assertEqual(Tools(self.state["id"]).call("task_context", {})["answer_attachments"], [entry])
        self.assertIn(entry["id"], params["input"][0]["text"])

    def test_two_questions_can_both_be_answered_with_files(self):
        first = self.upload_file()
        second = self.upload_file("验收.txt", b"rules", "rules")
        self.submit({"template": [first["id"]], "rules": [second["id"]]}, {"template": [], "rules": []})
        self.assertEqual(len(submitted_attachments(self.state)), 2)

    def test_misbound_duplicate_unknown_and_stale_files_are_rejected_without_mutation(self):
        entry = self.upload_file()
        cases = [({"rules": [entry["id"]]}, {"template": ["模板"], "rules": []}),
                 ({"template": [entry["id"], entry["id"]]}, {"template": [], "rules": ["规则"]}),
                 ({"template": ["另一会话的编号"]}, {"template": [], "rules": ["规则"]}),
                 ({"missing": [entry["id"]]}, {"template": [], "rules": ["规则"]})]
        before = deepcopy(self.state)
        for ids, answers in cases:
            with self.subTest(ids=ids), self.assertRaises(WorkflowError):
                prepare_answer(self.state, "", answers, ids, answer_request_id(self.state))
        with self.assertRaises(WorkflowError):
            prepare_answer(self.state, "", {"template": [], "rules": ["规则"]}, {"template": [entry["id"]]}, "过期请求")
        self.assertEqual(before, self.state)

    def test_question_completeness_and_text_compatibility(self):
        for answers in ({"template": [], "rules": ["规则"]}, {"template": ["模板"]},
                        {"template": [" "], "rules": ["规则"]}, {"template": "模板", "rules": ["规则"]}):
            with self.subTest(answers=answers), self.assertRaises(WorkflowError):
                prepare_answer(self.state, "", answers)
        normalized, files = prepare_answer(self.state, "", {"template": ["模板"], "rules": ["规则"]})
        self.assertEqual(normalized["template"], ["模板"])
        self.assertEqual(files, [])
        self.state["pending"]["params"]["questions"] = self.questions[:1]
        normalized, _ = prepare_answer(self.state, "单题文字", None)
        self.assertEqual(normalized, {"template": ["单题文字"]})

    def test_upload_rejects_empty_oversize_bad_revision_request_question_and_busy(self):
        self.settings["zentao"]["max_attachment_mb"] = 1
        for changes in ({"data": b""}, {"data": b"x" * (1024 * 1024 + 1)}, {"name": " "},
                        {"revision": -1}, {"request_id": "过期"}, {"question_id": "不存在"}):
            with self.subTest(changes=list(changes)), self.assertRaises(WorkflowError):
                self.upload_file(**changes)
        for stage in ("计划中", "已暂停", "等待计划确认"):
            self.state["stage"] = stage
            with self.subTest(stage=stage), self.assertRaises(WorkflowError):
                self.upload_file()
        self.state["stage"] = "等待人工回答"
        self.runtime.busy = "answer"
        with self.assertRaises(WorkflowError):
            self.upload_file()

    def test_remove_only_current_unsubmitted_question_file(self):
        entry = self.upload_file()
        with self.assertRaises(WorkflowError):
            self.application.remove_answer_attachment(self.state["id"], self.runtime.revision, entry["request_id"], "rules", entry["id"])
        self.application.remove_answer_attachment(self.state["id"], self.runtime.revision, entry["request_id"], "template", entry["id"])
        self.assertFalse(Path(entry["path"]).exists())
        self.assertEqual(self.state["answer_attachments"], [])

    def test_draft_and_question_survive_restart(self):
        entry = self.upload_file()
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        state = restored.sessions.get(self.state["id"]).engine.state
        self.assertEqual(state["stage"], "等待人工回答")
        self.assertEqual(state["answer_attachments"], [entry])
        self.assertEqual(answer_request_id(state), entry["request_id"])
        self.assertEqual(Path(entry["path"]).read_text(encoding="utf-8"), "参考内容")

    def test_text_reading_pagination_and_unreadable_original(self):
        text = self.upload_file(data=b"one\ntwo\nthree\n")
        unknown = self.upload_file("原件.pdf", b"%PDF-reference")
        self.submit({"template": [text["id"], unknown["id"]]})
        tools = Tools(self.state["id"])
        result = tools.call("read_answer_attachment", {"attachment_id": text["id"], "line_count": 2})
        self.assertEqual(result["text"], "1: one\n2: two")
        self.assertEqual(result["next_line"], 3)
        result = tools.call("read_answer_attachment", {"attachment_id": unknown["id"]})
        self.assertFalse(result["readable"])
        self.assertIn("内容未读取", result["issue"])
        self.assertTrue(Path(unknown["path"]).exists())

    def test_existing_docx_extraction_and_broken_docx(self):
        stream = BytesIO()
        with ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", '<document><t>模板内容</t></document>')
        doc = self.upload_file("模板.docx", stream.getvalue())
        broken = self.upload_file("损坏.docx", b"broken")
        self.submit({"template": [doc["id"], broken["id"]]})
        tools = Tools(self.state["id"])
        self.assertIn("模板内容", tools.call("read_answer_attachment", {"attachment_id": doc["id"]})["text"])
        self.assertFalse(tools.call("read_answer_attachment", {"attachment_id": broken["id"]})["readable"])

    def test_images_reach_codex_once_and_mcp_returns_native_image_content(self):
        image = self.upload_file("参考.png", PNG)
        duplicate = self.upload_file("相同图片.png", PNG)
        self.submit({"template": [image["id"], duplicate["id"]]})
        inputs = self.engine.rpc.request.call_args.args[1]["input"]
        self.assertEqual([item["path"] for item in inputs if item["type"] == "localImage"], [image["path"]])
        self.engine._start_turn("再次读取当前计划")
        self.assertFalse(any(item["type"] == "localImage" for item in self.engine.rpc.request.call_args.args[1]["input"]))
        request = dumps({"id": 1, "method": "tools/call", "params": {"name": "read_answer_attachment", "arguments": {"attachment_id": image["id"]}}}) + "\n"
        output = StringIO()
        with patch("workflow.mcp_server.stdin", StringIO(request)), patch("workflow.mcp_server.stdout", output):
            serve(self.state["id"])
        content = loads(output.getvalue())["result"]["content"]
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["mimeType"], "image/png")

    def test_native_question_uses_answer_references_and_read_tool(self):
        input_request_path(self.engine.store, self.state).unlink()
        self.state["pending"].update(method="item/tool/requestUserInput", turn_completed=False)
        self.state["rpc_connected"] = True
        self.engine.turn_running = True
        self.engine.save()
        entry = self.upload_file()
        self.submit({"template": [entry["id"]]})
        result = self.engine.rpc.respond.call_args.args[1]
        self.assertIn(entry["id"], result["answers"]["template"]["answers"][0])
        self.assertIn("参考内容", Tools(self.state["id"]).call("read_answer_attachment", {"attachment_id": entry["id"]})["text"])
        self.assertEqual(self.state["stage"], "计划中")

    def test_stale_questions_and_other_rounds_do_not_reuse_files(self):
        entry = self.upload_file()
        self.state["turn_id"] = "新提问回合"
        with self.assertRaises(WorkflowError):
            prepare_answer(self.state, "", {"template": [], "rules": ["规则"]}, {"template": [entry["id"]]}, answer_request_id(self.state))
        entry["history_index"] = 0
        self.state["delivery_round"] = 2
        self.assertEqual(submitted_attachments(self.state), [])

    def test_read_tool_cannot_access_drafts_other_sessions_or_outside_paths(self):
        entry = self.upload_file()
        self.engine.store.save(self.state)
        tools = Tools(self.state["id"])
        for identifier in (entry["id"], "其他会话的文件"):
            with self.assertRaises(WorkflowError):
                tools.call("read_answer_attachment", {"attachment_id": identifier})
        self.submit({"template": [entry["id"]]})
        entry["path"] = str(self.root / "外部.txt")
        self.engine.save()
        with self.assertRaises(WorkflowError):
            tools.call("read_answer_attachment", {"attachment_id": entry["id"]})

    def test_upload_counter_and_stale_revision_block_answer_before_dispatch(self):
        self.runtime.uploads = 1
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.action())
        self.runtime.uploads = 0
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.action(revision=-1))
        self.assertIsNotNone(self.state["pending"])

    def test_new_read_tool_is_enabled_on_threads_and_old_sessions_remain_compatible(self):
        self.assertIn("read_answer_attachment", thread_parameters(self.state, "模拟模型", {})["config"]["mcp_servers"]["workflow"]["enabled_tools"])
        self.state.pop("answer_attachments")
        self.assertEqual(public_state(self.state)["answer_attachments"], [])


class AnswerAttachmentHttpChecks(AnswerAttachmentFixture):
    def setUp(self):
        super().setUp()
        self.server = LocalServer(("127.0.0.1", 0), self.application)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(3)
        self.addCleanup(stop)

    def request(self, method, path, body=None, **changes):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers={"Cookie": f"{COOKIE_NAME}={self.application.cookie_token}",
                "X-Workflow-Token": self.application.csrf_token, "Connection": "close", **changes})
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type"), response.getheader("Content-Disposition")
        finally:
            connection.close()

    def upload_url(self, **changes):
        return "/api/answer-attachments?" + urlencode({"session_id": self.state["id"], "revision": self.runtime.revision,
            "answer_request_id": answer_request_id(self.state), "question_id": "template", "name": "模板.pdf", **changes})

    def test_http_upload_download_remove_and_authentication(self):
        url = self.upload_url()
        for headers in ({"Cookie": ""}, {"X-Workflow-Token": "invalid-token"}, {"Origin": "https://other.invalid"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", url, b"%PDF-reference", **headers)[0], 400)
        status, raw, _, _ = self.request("POST", url, b"%PDF-reference")
        self.assertEqual(status, 200)
        entry = loads(raw)["state"]["answer_attachments"][0]
        download = "/api/answer-attachments?" + urlencode({"session_id": self.state["id"], "attachment_id": entry["id"]})
        status, raw, content_type, disposition = self.request("GET", download)
        self.assertEqual((status, raw, content_type), (200, b"%PDF-reference", "application/octet-stream"))
        self.assertTrue(disposition.startswith("attachment;"))
        self.assertEqual(self.request("GET", download, Cookie="")[0], 400)
        other = self.application.sessions.store.new({"kind": "task", "id": 8})
        self.application.sessions.add(other)
        self.assertEqual(self.request("GET", download.replace(self.state["id"], other["id"]))[0], 400)
        body = dumps({"session_id": self.state["id"], "revision": self.runtime.revision,
                      "answer_request_id": entry["request_id"], "question_id": "template", "attachment_id": entry["id"]})
        self.assertEqual(self.request("POST", "/api/answer-attachments/remove", body, **{"Content-Type": "application/json"})[0], 200)
        self.assertEqual(self.request("GET", download)[0], 400)

    def test_http_size_limit_and_old_request_are_rejected(self):
        self.settings["zentao"]["max_attachment_mb"] = 1
        self.assertEqual(self.request("POST", self.upload_url(), b"", **{"Content-Length": str(1024 * 1024 + 1)})[0], 400)
        self.assertEqual(self.request("POST", self.upload_url(answer_request_id="旧问题"), b"reference")[0], 400)
        self.assertEqual(self.request("POST", self.upload_url(revision=-1), b"reference")[0], 400)
        self.assertEqual(self.runtime.uploads, 0)
