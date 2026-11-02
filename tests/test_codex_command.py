"""Codex 命令配置及独立于终端 PATH 的启动检查。"""

from copy import deepcopy
from json import loads
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.rpc import AppServer
from workflow.settings import ROOT, WorkflowError


class CodexCommandChecks(TestCase):
    def setUp(self):
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))

    def connect(self):
        rpc = AppServer(self.settings, Mock())
        rpc.request = Mock(side_effect=[{}, {"config": {}}])
        with patch("workflow.rpc.Popen") as process, patch("workflow.rpc.Thread"), patch.object(rpc, "notify"):
            rpc.connect()
        return process.call_args.args[0]

    def test_absolute_executable_works_without_path_and_preserves_settings(self):
        with TemporaryDirectory(prefix="Codex 命令 ") as directory:
            executable = Path(directory) / "codex.exe"
            executable.touch()
            self.settings["codex"]["command"] = [str(executable)]
            original = deepcopy(self.settings)
            with patch.dict(environ, {"PATH": ""}):
                command = self.connect()
            self.assertEqual(Path(command[0]), executable)
            self.assertEqual(command.count("app-server"), 1)
            self.assertEqual(command[-3:], ["app-server", "--listen", "stdio://"])
            self.assertEqual(self.settings, original)

    def test_missing_command_explains_real_configuration_location_and_does_not_start(self):
        rpc = AppServer(self.settings, Mock())
        with patch("workflow.rpc.which", return_value=None), patch("workflow.rpc.Popen") as process:
            with self.assertRaisesRegex(WorkflowError, "settings.json.*codex.command.*绝对路径.*重启"):
                rpc.connect()
            process.assert_not_called()

    def test_example_does_not_duplicate_app_server(self):
        self.assertEqual(self.settings["codex"]["command"], ["codex.cmd"])
        with patch("workflow.rpc.which", return_value="C:/Codex/codex.exe"):
            command = self.connect()
        self.assertEqual(command.count("app-server"), 1)

    def test_state_write_error_is_distinguished_from_malformed_channel_json(self):
        for output, failure, expected in (
            ('{"method":"item/started"}\n', PermissionError("state.json 替换被拒绝"), "事件处理失败"),
            ('{"method":"item/started"}\n', WorkflowError("等待会话状态读写锁超时"), "事件处理失败"),
            ('不是 JSON\n', None, "通道解析中断"),
        ):
            with self.subTest(expected=expected):
                callback = Mock(side_effect=[failure, None] if failure else None)
                rpc = AppServer(self.settings, callback)
                rpc.process = Mock(stdout=iter([output]))
                rpc.stderr_ready.set()
                rpc.closing = True
                rpc._read()
                event = callback.call_args.args[0]
                self.assertEqual(event["method"], "client/error")
                self.assertIn(expected, event["params"]["message"])
