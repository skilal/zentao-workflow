"""模型发现、会话选择与恢复隔离；不创建真实 Codex 对话。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.rpc import AppServer, thread_parameters, validate_selection
from workflow.settings import ROOT, WorkflowError
from workflow.web import COOKIE_NAME, LocalServer, WebApplication


CATALOG = {"default_model": "model-a", "models": [
    {"model": "model-a", "display_name": "模型甲", "reasoning_efforts": ["low", "high"], "is_default": True},
    {"model": "model-b", "display_name": "模型乙", "reasoning_efforts": ["low", "medium"], "is_default": False},
]}
SELECTION = {"model": "model-a", "reasoning_effort": "high"}


class CatalogChecks(TestCase):
    def setUp(self):
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))

    def test_pagination_and_cli_default_are_preserved(self):
        rpc = AppServer(self.settings, Mock())
        rpc.configuration = {"model": "model-b"}
        entries = [{"model": entry["model"], "displayName": entry["display_name"], "isDefault": entry["is_default"],
                    "supportedReasoningEfforts": [{"reasoningEffort": value} for value in entry["reasoning_efforts"]]}
                   for entry in CATALOG["models"]]
        rpc.request = Mock(side_effect=[{"data": entries[:1], "nextCursor": "第二页"}, {"data": entries[1:], "nextCursor": None}])
        result = rpc.catalog()
        self.assertEqual(result["models"], CATALOG["models"])
        self.assertEqual(result["default_model"], "model-b")
        self.assertEqual(rpc.request.call_args.args, ("model/list", {"includeHidden": False, "cursor": "第二页"}))

    def test_repeated_cursor_stops_and_empty_list_is_returned(self):
        rpc = AppServer(self.settings, Mock())
        rpc.request = Mock(return_value={"data": [], "nextCursor": "重复"})
        with self.assertRaisesRegex(WorkflowError, "游标重复"):
            rpc.catalog()
        rpc.request.return_value = {"data": []}
        self.assertEqual(rpc.catalog(), {"models": [], "default_model": None})

    def test_selection_resolves_default_and_rejects_invalid_pairs(self):
        self.assertEqual(validate_selection(CATALOG, "", "high", allow_default=True), SELECTION)
        for model, effort in (("", "high"), ("unknown", "high"), ("model-b", "high"), ("model-a", ""), (None, "low"), ("model-a", True)):
            with self.subTest(model=model, effort=effort), self.assertRaises(WorkflowError):
                validate_selection(CATALOG, model, effort)

    def test_explicit_session_model_does_not_read_or_resolve_global_default(self):
        rpc = AppServer(self.settings, Mock())
        rpc.connect = Mock()
        rpc.catalog = Mock(side_effect=AssertionError("会话启动不得重新解析全局默认模型"))
        rpc.request = Mock(return_value={"data": [{"mode": "plan"}, {"mode": "default"}]})
        rpc.start("model-b")
        self.assertEqual(rpc.model, "model-b")


class ModelSessionChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            patched = patch(target, self.root)
            patched.start()
            self.addCleanup(patched.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.app = WebApplication(self.settings)
        self.addCleanup(self.app.close)
        self.app.codex_catalog = deepcopy(CATALOG)

    def preview(self, number=7):
        runtime = self.app.sessions.new_preview({"kind": "task", "id": number, "title": "模型选择示例"})
        runtime.engine.state["snapshot"] = {"sources": [], "materials": [], "blockers": [], "feishu_links": []}
        return runtime

    def request(self, runtime, action="plan", selection=None, preview=True):
        return {"action": action, "request_id": str(uuid4()), "revision": runtime.revision,
                "preview_id" if preview else "session_id": runtime.engine.state["id"],
                "data": {"items": [{"kind": item["kind"], "id": item["id"]} for item in runtime.engine.state["items"]],
                         **(selection if selection is not None else SELECTION)}}

    def start(self, runtime, selection=None):
        with patch("workflow.engine.Workflow.start_plan"):
            result = self.app.dispatch(self.request(runtime, selection=selection))
            formal = self.app.sessions.get(result["session_id"])
            formal.engine.executor.submit(lambda: None).result(timeout=5)
        return formal

    def test_catalog_is_cached_and_explicit_refresh_closes_discovery_process(self):
        self.app.codex_catalog = None
        self.settings["codex"]["model"] = "已不可用的默认模型"
        with patch("workflow.web.AppServer") as factory:
            rpc = factory.return_value
            rpc.catalog.return_value = deepcopy(CATALOG)
            self.assertEqual(self.app.codex_models(), CATALOG)
            self.app.codex_models()
            self.assertEqual(factory.call_count, 1)
            self.app.codex_models(refresh=True)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(rpc.close.call_count, 2)
            rpc.start.assert_not_called()

    def test_discovery_failure_is_not_cached_and_can_be_retried(self):
        with patch("workflow.web.AppServer") as factory:
            factory.return_value.connect.side_effect = WorkflowError("模拟读取失败")
            with self.assertRaisesRegex(WorkflowError, "模拟读取失败"):
                self.app.codex_models(refresh=True)
            self.assertIsNone(self.app.codex_catalog)
            factory.return_value.close.assert_called_once()
            factory.return_value.connect.side_effect = None
            factory.return_value.catalog.return_value = deepcopy(CATALOG)
            self.assertEqual(self.app.codex_models(), CATALOG)

    def test_invalid_choices_do_not_promote_preview_or_create_files(self):
        runtime = self.preview()
        for selection in ({}, {"model": "unknown", "reasoning_effort": "high"}, {"model": "model-b", "reasoning_effort": "high"}):
            with self.subTest(selection=selection), self.assertRaises(WorkflowError):
                self.app.dispatch(self.request(runtime, selection=selection))
            self.assertFalse(self.app.sessions.runtimes)
            self.assertFalse(list(self.root.rglob("state.json")))
            self.assertIn(runtime.engine.state["id"], self.app.sessions.previews)

    def test_duplicate_start_does_not_create_another_session_or_change_model(self):
        runtime = self.preview()
        request = self.request(runtime)
        with patch("workflow.engine.Workflow.start_plan") as start:
            first = self.app.dispatch(request)
            formal = self.app.sessions.get(first["session_id"])
            formal.engine.executor.submit(lambda: None).result(timeout=5)
            repeated = self.app.dispatch({**request, "request_id": str(uuid4())})
            self.assertEqual(first["session_id"], repeated["session_id"])
            with self.assertRaisesRegex(WorkflowError, "已经固定"):
                self.app.dispatch({**request, "request_id": str(uuid4()), "data": {"model": "model-b"}})
            with self.assertRaisesRegex(WorkflowError, "已经固定"):
                self.app.dispatch({**request, "data": {"model": "model-b"}})
            start.assert_called_once()
        self.assertEqual(len(self.app.sessions.runtimes), 1)

    def test_concurrent_sessions_and_restart_preserve_independent_choices(self):
        before = deepcopy(self.settings)
        second_choice = {"model": "model-b", "reasoning_effort": "medium"}
        previews = [self.preview(7), self.preview(8)]
        with patch("workflow.engine.Workflow.start_plan"), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda pair: self.app.dispatch(self.request(pair[0], selection=pair[1])), zip(previews, [SELECTION, second_choice])))
            sessions = [self.app.sessions.get(result["session_id"]) for result in results]
            for runtime in sessions:
                runtime.engine.executor.submit(lambda: None).result(timeout=5)
        self.assertEqual(self.settings, before)
        self.settings["codex"].update(model="unknown", reasoning_effort="ultra")
        restored = WebApplication(self.settings)
        self.addCleanup(restored.close)
        for runtime, expected in zip(sessions, [SELECTION, second_choice]):
            state = restored.sessions.get(runtime.engine.state["id"]).engine.state
            self.assertEqual(state["codex"], expected)

    def test_all_turn_phases_and_thread_parameters_use_saved_configuration(self):
        engine = self.start(self.preview()).engine
        state = engine.state
        self.settings["codex"].update(model="model-b", reasoning_effort="medium")
        engine.ensure_rpc = Mock()
        engine.rpc = Mock()
        engine.rpc.request.return_value = {"turn": {"id": "模拟回合"}}
        state.update(thread_id="模拟线程", project={"id": 41}, branch_resolution={"repositories": []})
        for phase in ("branch", "plan", "implement", "review", "fix"):
            state["phase"] = phase
            engine._start_turn("检查保存配置")
            params = engine.rpc.request.call_args.args[1]
            actual = params["collaborationMode"]["settings"]
            self.assertEqual({key: actual[key] for key in SELECTION}, SELECTION)
            thread = thread_parameters(state, state["codex"]["model"], {})
            self.assertEqual(thread["model"], SELECTION["model"])
            self.assertEqual(thread["config"]["model_reasoning_effort"], "high")

    def test_reconnect_uses_saved_model_for_create_and_resume(self):
        engine = self.start(self.preview()).engine
        with patch("workflow.engine.AppServer") as factory:
            rpc = factory.return_value
            rpc.model = "model-a"
            rpc.disabled_mcp = {}
            rpc.request.return_value = {"thread": {"id": "模拟线程"}}
            engine.ensure_rpc()
            rpc.start.assert_called_once_with("model-a")
            self.assertEqual(rpc.request.call_args.args[0], "thread/start")
            engine.rpc = None
            engine.ensure_rpc()
            self.assertEqual(rpc.request.call_args.args[0], "thread/resume")
            self.assertEqual(rpc.request.call_args.args[1]["model"], "model-a")
            engine.rpc = None

    def test_legacy_requires_explicit_choice_and_saved_choice_cannot_be_overridden(self):
        state = self.app.sessions.store.new({"kind": "task", "id": 9, "title": "历史会话"})
        state["stage"] = "已暂停"
        runtime = self.app.sessions.add(state)
        with self.assertRaisesRegex(WorkflowError, "先选择模型"):
            self.app.dispatch(self.request(runtime, "resume", selection={}, preview=False))
        self.app.dispatch(self.request(runtime, "configure_codex", preview=False))
        runtime.engine.executor.submit(lambda: None).result(timeout=5)
        self.assertEqual(self.app.sessions.store.load(state["id"])["codex"], SELECTION)
        with self.assertRaisesRegex(WorkflowError, "已经固定"):
            self.app.dispatch(self.request(runtime, "resume", {"model": "model-b", "reasoning_effort": "medium"}, preview=False))

    def test_settings_save_validates_before_writing_and_preserves_inheritance(self):
        data = {**self.app.public_settings(), "password": "", "model": "", "reasoning_effort": "high"}
        prompt = self.root / self.settings["review"]["prompt_file"]
        prompt.parent.mkdir(parents=True)
        prompt.write_text(data["review_prompt"], encoding="utf-8")
        with patch("workflow.web.ROOT", self.root), patch("workflow.web.save_settings") as save:
            self.app.update_settings(data)
            save.assert_called_once()
            self.assertEqual(self.settings["codex"]["model"], "")
            self.assertEqual(self.app.public_settings()["reasoning_effort"], "high")
            before = deepcopy(self.settings)
            with self.assertRaises(WorkflowError):
                self.app.update_settings({**data, "model": "model-b", "reasoning_effort": "high"})
            self.assertEqual(self.settings, before)
            save.assert_called_once()

    def test_catalog_endpoint_requires_authentication_and_does_not_create_sessions(self):
        server = LocalServer(("127.0.0.1", 0), self.app)
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.request("GET", "/api/codex-models")
            response = client.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            client.request("GET", "/api/codex-models", headers={"Cookie": f"{COOKIE_NAME}={self.app.cookie_token}"})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(loads(response.read()), CATALOG)
            client.close()
            self.assertFalse(self.app.sessions.runtimes)
        finally:
            server.shutdown()
            server.server_close()
