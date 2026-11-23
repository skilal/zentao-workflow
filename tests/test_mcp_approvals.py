"""检查 MCP 调用授权与工具业务校验的边界。"""

from hashlib import sha256
from io import StringIO
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest import TestCase
from unittest.mock import Mock, call, patch

from workflow.engine import Workflow
from workflow.gitops import git
from workflow.mcp_server import TOOLS, Tools, serve
from workflow.store import SessionStore
from workflow.sessions import SessionManager
from workflow.settings import ROOT, WorkflowError


class McpApprovalChecks(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        git(self.root, "init", "-b", "检查分支")
        local = patch("workflow.store.LOCAL", self.root / "sessions")
        local.start()
        self.addCleanup(local.stop)
        self.store = SessionStore()
        self.state = self.store.new(None)
        self.state.update(phase="implement", stage="实施中", approved_plan="已确认的计划",
                          approved_hash=sha256("已确认的计划".encode()).hexdigest(),
                          repos=[{"name": "前端", "path": str(self.root)}])
        self.store.save(self.state)
        self.flow = Workflow.__new__(Workflow)
        self.flow.state = self.state
        self.flow.connection_epoch = 1
        self.flow.closed = False
        self.flow.rpc = Mock()
        self.flow.lock = RLock()
        self.flow.save = Mock()
        self.flow.emit = Mock()
        self.flow.set_progress_status = Mock()
        self.flow.note = Mock()

    def approve(self, name, arguments, **changes):
        description = next(description for tool, description, _ in TOOLS if tool == name)
        params = {"serverName": "workflow", "mode": "form", "_meta": {
            "codex_approval_kind": "mcp_tool_call", "tool_description": description,
            "tool_params": arguments}, **changes}
        self.flow._event({"id": "调用审批", "method": "mcpServer/elicitation/request",
                          "params": params}, 1)
        return self.flow.rpc.respond.call_args.args[1]

    def test_push_accepts_repositories_resolved_from_conversation(self):
        self.state["repos"] = [{"name": "档案前端", "role": "frontend", "branch": "develop", "remote": "origin"},
                               {"name": "档案后端", "role": "backend", "branch": "main", "remote": "origin"}]
        message = "麻烦把本次需求的提交发到远端。"
        digest = sha256(dumps([message, [], []], ensure_ascii=False).encode()).hexdigest()
        self.state.update(phase="followup", stage="已暂停", approval_source="followup",
                          followup={"text": message, "images": [], "attachments": [], "authorization_hash": digest})
        self.store.save(self.state)
        tools = Tools(self.state["id"])
        args = {"repository": "档案前端", "commit": "前端提交"}
        with patch("workflow.mcp_server.push_branch") as push:
            with self.assertRaisesRegex(WorkflowError, "不在可推送的续聊阶段"):
                tools.call("git_push", args)
            self.state["stage"] = "续聊处理中"
            self.store.save(self.state)
            with self.assertRaisesRegex(WorkflowError, "不属于当前会话"):
                tools.call("git_push", {**args, "repository": "外部仓库"})
            tools.call("git_push", args)
            tools.call("git_push", {"repository": "档案后端", "commit": "后端提交"})
            self.assertEqual(push.call_args_list, [call(self.state["repos"][0], "前端提交"),
                                                   call(self.state["repos"][1], "后端提交")])

    def test_registered_tools_are_accepted_without_repeating_business_validation(self):
        self.state["stage"] = "等待计划确认"
        for name, _, _ in TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(self.approve(name, {}), {"action": "accept", "content": {}})

    def test_other_servers_unknown_tools_and_non_tool_elicitations_are_declined(self):
        for changes in ({"mode": "url"}, {"_meta": {}},
                        {"_meta": {"codex_approval_kind": "mcp_tool_call", "tool_description": "未知工具"}},
                        {"_meta": {"codex_approval_kind": "其他请求", "tool_description": TOOLS[0][1]}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.approve("write_file", {}, **changes),
                                 {"action": "decline", "content": None})

    def request_external_approval(self, **changes):
        params = {"serverName": "全局服务", "mode": "form", "message": "读取项目资料",
                  "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_description": "查询资料",
                            "tool_params": {"path": "src/业务.txt", "token": "示例私密内容"}}, **changes}
        self.flow._event({"id": "外部授权", "method": "mcpServer/elicitation/request", "params": params}, 1)

    def test_external_tools_wait_for_explicit_answer_without_saving_credentials(self):
        self.state.update(phase="plan", plan_round={"asked": False})
        self.request_external_approval()
        self.flow.rpc.respond.assert_not_called()
        self.assertEqual(self.state["stage"], "等待人工回答")
        self.assertTrue(self.state["plan_round"]["asked"])
        pending = dumps(self.state["pending"], ensure_ascii=False)
        self.assertNotIn("示例私密内容", pending)
        self.assertIn("src/业务.txt", pending)
        self.assertNotIn("default", pending)

    def test_allow_and_decline_apply_only_to_current_external_call(self):
        self.state["rpc_connected"] = True
        for choice, expected in (("允许本次", {"action": "accept", "content": {}}),
                                 ("拒绝本次", {"action": "decline", "content": None})):
            with self.subTest(choice=choice):
                self.request_external_approval()
                self.flow.answer("", {"tool_approval": [choice]}, attachments=[])
                self.flow.rpc.respond.assert_called_with("外部授权", expected)
                self.assertIsNone(self.state["pending"])
                self.assertEqual(self.state["stage"], "实施中")

    def test_invalid_and_expired_approval_cannot_execute_tools(self):
        self.request_external_approval()
        self.state["rpc_connected"] = True
        with self.assertRaisesRegex(WorkflowError, "明确选择"):
            self.flow.answer("", {"tool_approval": ["随便"]}, attachments=[])
        self.state["rpc_connected"] = False
        with self.assertRaisesRegex(WorkflowError, "失效"):
            self.flow.answer("", {"tool_approval": ["允许本次"]}, attachments=[])
        self.flow.rpc.respond.assert_not_called()

    def test_extended_form_tool_approval_uses_same_explicit_question(self):
        self.request_external_approval(mode="openai/form")
        self.assertEqual(self.state["pending"]["method"], "mcpServer/elicitation/request")
        self.flow.rpc.respond.assert_not_called()

    def test_disconnect_invalidates_authorization_and_pauses_for_recovery(self):
        self.request_external_approval()
        self.flow.sync_artifacts = Mock()
        self.flow._capture_user_input = Mock(return_value=False)
        self.flow._event({"method": "client/disconnected"}, 1)
        self.assertIsNone(self.state["pending"])
        self.assertEqual(self.state["stage"], "已暂停")
        self.flow.rpc.respond.assert_not_called()

    def test_restart_drops_old_authorization_instead_of_replaying_it(self):
        self.request_external_approval()
        self.store.save(self.state)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        manager = SessionManager(settings, Mock())
        self.addCleanup(manager.close)
        restored = manager.get(self.state["id"]).engine.state
        self.assertIsNone(restored["pending"])
        self.assertEqual(restored["stage"], "已暂停")
        self.assertFalse(restored["rpc_connected"])

    def test_approved_write_replace_delete_reach_the_controlled_tools(self):
        path = self.root / "业务.txt"
        for name, arguments in (
            ("write_file", {"content": "原内容"}),
            ("replace_text", {"old": "原内容", "new": "新内容"}),
            ("delete_file", {}),
        ):
            arguments.update(repository="前端", path="业务.txt")
            self.assertEqual(self.approve(name, arguments)["action"], "accept")
            Tools(self.state["id"]).call(name, arguments)
            if name != "delete_file":
                self.assertEqual(path.read_text(encoding="utf-8"), "原内容" if name == "write_file" else "新内容")
        self.assertFalse(path.exists())

    def test_tool_errors_keep_specific_reasons_after_approval(self):
        for changes, path, reason in (
            ({"stage": "计划中"}, "业务.txt", "当前阶段禁止修改文件"),
            ({"stage": "实施中", "approved_hash": "过期版本"}, "业务.txt", "当前阶段禁止修改文件"),
            ({"approved_hash": sha256("已确认的计划".encode()).hexdigest()}, "AGENTS.md", "项目指令文件"),
            ({}, ".codex/config", "不允许访问"),
        ):
            with self.subTest(path=path, changes=changes):
                self.state.update(changes)
                self.store.save(self.state)
                arguments = {"repository": "前端", "path": path, "content": "内容"}
                self.assertEqual(self.approve("write_file", arguments)["action"], "accept")
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "write_file", "arguments": arguments}}
                output = StringIO()
                with patch("workflow.mcp_server.stdin", StringIO(dumps(request) + "\n")), \
                        patch("workflow.mcp_server.stdout", output):
                    serve(self.state["id"])
                result = loads(output.getvalue())["result"]
                self.assertTrue(result["isError"])
                self.assertIn(reason, result["content"][0]["text"])
                self.assertNotIn("user rejected", result["content"][0]["text"])
                self.assertFalse((self.root / path).exists())

    def test_command_and_native_file_approvals_remain_declined(self):
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            self.flow._event({"id": "原生审批", "method": method, "params": {}}, 1)
            self.flow.rpc.respond.assert_called_with("原生审批", {"decision": "decline"})
