"""用现有隔离会话验证过程事件、持久化与模型请求边界。"""

from copy import deepcopy
from json import dumps
from unittest.mock import Mock, patch

from test_workflow_performance import PerformanceFixture, QUESTIONS
from test_sessions import SessionFixture
from workflow.progress import settle_progress
from workflow.sessions import SessionManager


class ProgressChecks(PerformanceFixture):
    def setUp(self):
        super().setUp()
        self.flow._new_plan_round()
        self.flow._start_turn("分析需求")

    def event(self, method, **params):
        self.flow._event({"method": method, "params": {"threadId": self.state["thread_id"],
                         "turnId": self.state["turn_id"], **params}}, self.flow.connection_epoch)

    def item(self, kind, item_id, method="item/started", **fields):
        self.event(method, item={"id": item_id, "type": kind, **fields})

    def test_summaries_are_segmented_authoritative_and_raw_reasoning_is_ignored(self):
        self.item("reasoning", "思考", content=["隐藏内容"])
        self.event("item/reasoning/summaryTextDelta", itemId="思考", summaryIndex=0, delta="摘要一")
        self.event("item/reasoning/summaryPartAdded", itemId="思考", summaryIndex=1)
        self.event("item/reasoning/summaryTextDelta", itemId="思考", summaryIndex=1, delta="摘要二")
        self.event("item/reasoning/textDelta", itemId="思考", delta="不可展示的内容")
        turn = self.state["progress_turns"][-1]
        self.assertEqual(turn["items"][0]["summary"], ["摘要一", "摘要二"])
        self.item("reasoning", "思考", "item/completed", summary=["最终摘要"], content=["隐藏内容"])
        self.item("reasoning", "思考", "item/completed", summary=["最终摘要"])
        self.assertEqual(len(turn["items"]), 1)
        self.assertEqual(turn["items"][0]["summary"], ["最终摘要"])
        self.assertNotIn("隐藏", dumps(turn, ensure_ascii=False))
        self.assertNotIn("不可展示", dumps(turn, ensure_ascii=False))
        self.assertEqual(self.flow.store.load(self.state["id"])["progress_turns"], self.state["progress_turns"])

    def test_parallel_tools_only_expose_names_paths_and_status(self):
        for item_id in ("工具一", "工具二"):
            self.item("mcpToolCall", item_id, tool="read_file", server="workflow",
                      arguments={"path": item_id + ".py", "content": "不应暴露", "password": "不应暴露"})
        self.item("mcpToolCall", "工具二", "item/completed", tool="read_file", status="failed",
                  result={"content": "文件正文不应暴露"}, error={"message": "敏感原始错误"})
        items = self.state["progress_turns"][-1]["items"]
        self.assertEqual([item["status"] for item in items], ["running", "failed"])
        self.assertEqual(items[0]["paths"], ["工具一.py"])
        self.assertNotIn("不应暴露", dumps(items, ensure_ascii=False))
        self.assertNotIn("敏感原始错误", dumps(items, ensure_ascii=False))
        self.item("contextCompaction", "压缩")
        self.item("contextCompaction", "压缩", "item/completed")
        self.assertEqual(items[-1]["label"], "压缩上下文")
        self.assertEqual(items[-1]["status"], "completed")

    def test_message_completion_corrects_delta_without_changing_business_output(self):
        self.item("agentMessage", "说明", phase="commentary")
        self.event("item/agentMessage/delta", itemId="说明", delta="过程")
        self.item("agentMessage", "说明", "item/completed", text="过程说明", phase="commentary")
        self.item("agentMessage", "最终", phase="final_answer")
        self.event("item/agentMessage/delta", itemId="最终", delta="初稿")
        self.item("agentMessage", "最终", "item/completed", text="最终回复", phase="final_answer")
        self.assertEqual(self.flow.last_message, "最终回复")
        self.assertEqual([item["text"] for item in self.state["progress_turns"][-1]["items"]], ["过程说明", "最终回复"])
        self.assertFalse(any(call.args[0] == "delta" for call in self.flow.emit.call_args_list))

    def test_stream_does_not_save_per_delta_or_call_model_or_change_context(self):
        before = [(call.args[0], deepcopy(call.args[1])) for call in self.rpc.request.call_args_list]
        context = self.tools.call("task_context", {})
        history = deepcopy(self.state["history"])
        self.item("reasoning", "连续摘要")
        self.flow.emit.reset_mock()
        with patch.object(self.flow.store, "save", wraps=self.flow.store.save) as save:
            for _ in range(1000):
                self.event("item/reasoning/summaryTextDelta", itemId="连续摘要", summaryIndex=0, delta="测试")
            save.assert_not_called()
            self.item("reasoning", "连续摘要", "item/completed", summary=["测试" * 1000])
            save.assert_called_once()
        self.assertEqual([(call.args[0], call.args[1]) for call in self.rpc.request.call_args_list], before)
        self.assertEqual(self.tools.call("task_context", {}), context)
        self.assertEqual(self.state["history"], history)
        packets = [call.args[1] for call in self.flow.emit.call_args_list if call.args[0] == "progress"]
        self.assertTrue(all(set(packet) == {"op", "turn_id", "item_id", "summary_index", "delta"}
                            for packet in packets[:-1]))
        self.assertLess(sum(len(dumps(packet).encode()) for packet in packets), 200000)

    def test_wrong_connection_thread_turn_and_late_delta_are_ignored(self):
        original = deepcopy(self.state["progress_turns"])
        for fields, epoch in (({"threadId": "其他线程"}, self.flow.connection_epoch),
                              ({"turnId": "其他回合"}, self.flow.connection_epoch), ({}, self.flow.connection_epoch - 1)):
            self.flow._event({"method": "item/started", "params": {"threadId": self.state["thread_id"],
                             "turnId": self.state["turn_id"], "item": {"id": "旧思考", "type": "reasoning"}, **fields}}, epoch)
        self.assertEqual(self.state["progress_turns"], original)
        self.item("reasoning", "思考")
        self.flow.set_progress_status("interrupted")
        self.event("item/reasoning/summaryTextDelta", itemId="思考", delta="迟到摘要")
        self.event("turn/started", turn={"id": self.state["turn_id"]})
        self.assertEqual(self.state["progress_turns"][-1]["status"], "interrupted")
        self.assertEqual(self.state["progress_turns"][-1]["items"][0]["summary"], [])

    def test_question_pause_disconnect_and_reload_end_active_items(self):
        self.item("reasoning", "思考")
        self.flow._event({"id": 17, "method": "item/tool/requestUserInput", "params": {
            "threadId": self.state["thread_id"], "turnId": self.state["turn_id"], "questions": QUESTIONS}}, self.flow.connection_epoch)
        self.assertEqual(self.state["progress_turns"][-1]["status"], "waiting")
        self.assertEqual(self.state["progress_turns"][-1]["items"][0]["status"], "waiting")
        self.flow.pause()
        self.assertEqual(self.state["progress_turns"][-1]["status"], "interrupted")
        self.state.update(turn_id="新回合", stage="计划中")
        self.flow.start_progress("新回合")
        self.item("reasoning", "新思考")
        self.flow._event({"method": "client/disconnected", "params": {}}, self.flow.connection_epoch)
        self.assertEqual(self.state["progress_turns"][-1]["status"], "disconnected")
        self.assertTrue(all(item["status"] != "running" for item in self.state["progress_turns"][-1]["items"]))
        self.state["progress_turns"][-1]["status"] = "running"
        self.flow.store.save(self.state)
        manager = SessionManager(self.settings, Mock())
        try:
            restored = manager.get(self.state["id"]).engine.state
            self.assertEqual(restored["progress_turns"][-1]["status"], "interrupted")
        finally:
            manager.close()
        old = {"history": []}
        self.assertFalse(settle_progress(old, "interrupted"))
        self.assertNotIn("progress_turns", old)

    def test_native_answer_continues_the_same_turn_and_summary_item(self):
        self.state["rpc_connected"] = True
        self.item("reasoning", "思考")
        self.event("item/reasoning/summaryTextDelta", itemId="思考", summaryIndex=0, delta="回答前")
        self.flow._event({"id": 17, "method": "item/tool/requestUserInput", "params": {
            "threadId": self.state["thread_id"], "turnId": self.state["turn_id"], "questions": QUESTIONS}}, self.flow.connection_epoch)
        self.flow.answer("按原范围处理")
        self.rpc.respond.assert_called_once()
        self.event("item/reasoning/summaryTextDelta", itemId="思考", summaryIndex=0, delta="回答后")
        self.assertEqual(len(self.state["progress_turns"]), 1)
        self.assertEqual(self.state["progress_turns"][-1]["status"], "running")
        self.assertEqual(self.state["progress_turns"][-1]["items"][0]["summary"], ["回答前回答后"])
        self.assertEqual(self.rpc.request.call_count, 1)


class ProgressWebChecks(SessionFixture):
    def test_progress_snapshots_and_sse_are_isolated_without_revision_changes(self):
        engine = self.first.engine
        engine.state.update(thread_id="线程", turn_id="回合")
        revision = self.first.revision
        engine.start_progress("回合")
        engine.progress_event("item/reasoning/summaryTextDelta", {"itemId": "摘要", "summaryIndex": 0, "delta": "正在核对业务"})
        payload = self.application.session(engine.state["id"])
        self.assertEqual(payload["state"]["progress_turns"][-1]["items"][0]["summary"], ["正在核对业务"])
        self.assertEqual(self.first.revision, revision)
        self.assertNotIn("progress_turns", self.application.session(self.second.engine.state["id"])["state"])
        packets = [packet.decode() for _, packet in self.application.events if b"event: progress" in packet]
        self.assertTrue(all(engine.state["id"] in packet for packet in packets))
        self.assertTrue(all(self.second.engine.state["id"] not in packet for packet in packets))
