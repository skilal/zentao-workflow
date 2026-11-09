"""检查 MCP 调用授权与工具业务校验的边界。"""

from hashlib import sha256
from io import StringIO
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.engine import Workflow
from workflow.gitops import git
from workflow.mcp_server import TOOLS, Tools, serve
from workflow.store import SessionStore


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

    def approve(self, name, arguments, **changes):
        description = next(description for tool, description, _ in TOOLS if tool == name)
        params = {"serverName": "workflow", "mode": "form", "_meta": {
            "codex_approval_kind": "mcp_tool_call", "tool_description": description,
            "tool_params": arguments}, **changes}
        self.flow._event({"id": "调用审批", "method": "mcpServer/elicitation/request",
                          "params": params}, 1)
        return self.flow.rpc.respond.call_args.args[1]

    def test_registered_tools_are_accepted_without_repeating_business_validation(self):
        self.state["stage"] = "等待计划确认"
        for name, _, _ in TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(self.approve(name, {}), {"action": "accept", "content": {}})

    def test_other_servers_unknown_tools_and_non_tool_elicitations_are_declined(self):
        for changes in ({"serverName": "其他服务"}, {"mode": "url"}, {"_meta": {}},
                        {"_meta": {"codex_approval_kind": "mcp_tool_call", "tool_description": "未知工具"}},
                        {"_meta": {"codex_approval_kind": "其他请求", "tool_description": TOOLS[0][1]}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.approve("write_file", {}, **changes),
                                 {"action": "decline", "content": None})

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
