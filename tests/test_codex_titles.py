"""仅新会话同步 Codex 名称；覆盖异步改名、重连和历史隔离。"""

from copy import deepcopy
from threading import Event, Thread
from unittest.mock import Mock, patch

from test_session_titles import SessionTitleFixture
from workflow.mcp_server import Tools
from workflow.rpc import CodexRequestError
from workflow.store import session_title
from workflow.web import WebApplication


class CodexTitleChecks(SessionTitleFixture):
    def setUp(self):
        super().setUp()
        self.engine = self.runtime.engine
        self.state["codex"] = {"model": "模拟模型", "reasoning_effort": "high"}
        self.names = {}

    def connect(self, thread_id="正式线程", engine=None):
        engine = engine or self.engine
        rpc = Mock()
        rpc.model, rpc.disabled_mcp = "模拟模型", {}
        rpc.process.poll.return_value = None
        def request(method, params):
            if method in ("thread/start", "thread/resume"):
                return {"thread": {"id": thread_id, "name": self.names.get(thread_id)}}
            if method == "thread/name/set":
                self.names[params["threadId"]] = params["name"]
                return {}
            self.fail("名称同步不应启动其他请求：" + method)
        rpc.request.side_effect = request
        with patch("workflow.engine.AppServer", return_value=rpc):
            engine.ensure_rpc()
        self.drain(engine)
        return rpc

    def drain(self, engine=None):
        (engine or self.engine).title_executor.submit(lambda: None).result(timeout=3)

    def test_creation_sets_pending_name_once_without_a_model_turn(self):
        rpc = self.connect()
        self.assertEqual(self.names, {"正式线程": "工作流会话 · 待命名会话"})
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list],
                         ["thread/start", "thread/name/set"])
        self.engine.save()
        self.engine.save()
        self.drain()
        self.assertEqual(rpc.request.call_count, 2)
        self.assertEqual(session_title(self.state), "待命名会话")
        self.assertEqual(self.engine.store.load(self.state["id"])["codex_title_sync"]["threads"], self.names)

    def test_automatic_multitask_title_and_manual_priority_sync_all_threads(self):
        self.state["phase"] = "branch"
        self.connect("分支线程")
        self.engine._close_rpc()
        self.state.update(thread_id=None, turn_id=None, phase="plan", stage="计划中")
        self.connect()
        self.state["turn_id"] = "计划回合"
        self.state["items"].append({"kind": "task", "id": 8, "title": "另一个任务"})
        self.engine.save()
        Tools(self.state["id"]).call("set_session_title", {"title": "全宗号范围检索"})
        self.engine._event({"method": "item/completed", "params": {
            "threadId": "正式线程", "turnId": "计划回合",
            "item": {"type": "mcpToolCall", "server": "workflow", "tool": "set_session_title"}}},
            self.engine.connection_epoch)
        self.drain()
        expected = "工作流会话 · 全宗号范围检索等 2 项"
        self.assertEqual(self.names, {"分支线程": expected, "正式线程": expected})
        self.rename("人工最终名称")
        self.drain()
        self.assertEqual(set(self.names.values()), {"工作流会话 · 人工最终名称"})
        self.assertEqual(self.state["auto_title"], "全宗号范围检索等 2 项")

    def test_rapid_rename_uses_latest_title_without_blocking_reader_or_control_queue(self):
        rpc = self.connect()
        entered, release, read_done = Event(), Event(), Event()
        self.addCleanup(release.set)
        original = rpc.request.side_effect
        def request(method, params):
            entered.set()
            if not release.wait(3):
                raise AssertionError("等待检查释放名称响应超时")
            return original(method, params)
        rpc.request.side_effect = request
        self.rename("第一版名称")
        self.assertTrue(entered.wait(2))
        def reader():
            self.engine._event({"method": "thread/name/updated", "params": {
                "threadId": "正式线程", "threadName": "工作流会话 · 第一版名称"}},
                self.engine.connection_epoch)
            read_done.set()
        thread = Thread(target=reader)
        thread.start()
        try:
            self.assertTrue(read_done.wait(1), "名称事件不能等待改名请求的响应")
            self.rename("最终名称")
            self.engine.control_executor.submit(lambda: None).result(timeout=1)
            self.assertTrue(self.engine.store.lock.acquire(blocking=False))
            self.engine.store.lock.release()
        finally:
            release.set()
            thread.join(timeout=3)
        self.drain()
        self.assertEqual(self.names["正式线程"], "工作流会话 · 最终名称")

    def test_offline_rename_survives_restart_and_matching_resume_does_not_rename(self):
        self.connect()
        self.engine._close_rpc()
        self.rename("离线保存的新名称")
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        engine = restored.sessions.get(self.state["id"]).engine
        rpc = self.connect(engine=engine)
        self.assertEqual(self.names["正式线程"], "工作流会话 · 离线保存的新名称")
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list],
                         ["thread/resume", "thread/name/set"])
        engine._close_rpc()
        rpc = self.connect(engine=engine)
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list], ["thread/resume"])

    def test_resume_compares_actual_codex_name_instead_of_trusting_saved_name(self):
        self.connect()
        self.engine._close_rpc()
        self.names["正式线程"] = "Codex 中另外修改的名称"
        rpc = self.connect()
        self.assertEqual(self.names["正式线程"], "工作流会话 · 待命名会话")
        self.assertEqual(rpc.request.call_count, 2)

    def test_old_sessions_never_enroll_even_when_renamed_or_resumed(self):
        self.state.pop("codex_title_sync")
        self.state["thread_id"] = "历史线程"
        self.engine.save()
        self.names["历史线程"] = "历史 Codex 原名"
        rpc = self.connect("历史线程")
        self.rename("平台历史会话新名称")
        self.drain()
        self.assertEqual(self.names["历史线程"], "历史 Codex 原名")
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list], ["thread/resume"])
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        self.assertNotIn("codex_title_sync", restored.sessions.get(self.state["id"]).engine.state)

    def test_failure_is_persisted_and_does_not_stop_workflow_or_retry(self):
        rpc = self.connect()
        self.state.update(stage="实施中", phase="implement")
        rpc.request.side_effect = CodexRequestError("不支持 thread/name/set")
        self.rename("平台保留的名称")
        self.drain()
        calls = rpc.request.call_count
        self.assertEqual(self.state["custom_title"], "平台保留的名称")
        self.assertEqual(self.state["stage"], "实施中")
        self.assertEqual(self.state["error"], "")
        self.assertIn("同步已停止", self.state["codex_title_sync"]["error"])
        self.assertIn("thread/name/set", self.state["history"][-1]["text"])
        self.engine.save()
        self.rename("再次保存平台名称")
        self.drain()
        self.assertEqual(rpc.request.call_count, calls)
        self.engine._close_rpc()
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        rpc = self.connect(engine=restored.sessions.get(self.state["id"]).engine)
        self.assertEqual([call.args[0] for call in rpc.request.call_args_list], ["thread/resume"])

    def test_sessions_and_unrelated_or_late_notifications_are_isolated(self):
        self.connect()
        other = self.application.sessions.add(self.application.sessions.store.new(None)).engine
        other.state.update(conversation_title="普通对话", codex=deepcopy(self.state["codex"]))
        self.connect("另一线程", engine=other)
        before = deepcopy(self.state["codex_title_sync"])
        self.engine._event({"method": "thread/name/updated", "params": {
            "threadId": "另一线程", "threadName": "无关名称"}}, self.engine.connection_epoch)
        self.engine._event({"method": "thread/name/updated", "params": {
            "threadId": "正式线程", "threadName": "旧连接名称"}}, self.engine.connection_epoch - 1)
        self.drain()
        self.assertEqual(self.state["codex_title_sync"], before)
        self.assertEqual(self.names["另一线程"], "工作流会话 · 普通对话")
        self.rename("仅修改当前会话")
        self.drain()
        self.assertEqual(self.names["另一线程"], "工作流会话 · 普通对话")
