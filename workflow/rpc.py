"""Codex App Server 双向 JSONL 通道；不执行模型提出的系统命令。"""

from itertools import count
from collections import deque
from json import dumps, loads
from os import environ
from pathlib import Path
from queue import Empty, Queue
from shutil import which
from subprocess import CREATE_NO_WINDOW, PIPE, Popen, TimeoutExpired, list2cmdline
from sys import executable as python_executable
from threading import Event, Lock, Thread

from workflow.branching import BRANCH_INSTRUCTIONS
from workflow.settings import ROOT, WorkflowError


class AppServer:
    def __init__(self, settings, callback):
        self.settings = settings
        self.callback = callback
        self.process = None
        self.ids = count(1)
        self.waiters = {}
        self.lock = Lock()
        self.write_lock = Lock()
        self.closing = False
        self.model = None
        self.modes = {}
        self.startup_lines = deque(maxlen=12)
        self.stderr_ready = Event()
        self.initialized = False
        self.disabled_mcp = {}

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.closing = False
        command = list(self.settings["codex"]["command"])
        executable = which(command[0])
        if not executable:
            raise WorkflowError("找不到配置中的 Codex 命令，请在设置中填写已安装命令的路径。")
        command[0] = executable
        command += ["-c", "features.shell_tool=false", "-c", "features.hooks=false",
                    "-c", "features.multi_agent=false", "-c", "features.apps=false", "-c", "features.goals=false",
                    "-c", "features.remote_plugin=false", "-c", 'web_search="disabled"',
                    "app-server", "--listen", "stdio://"]
        if Path(executable).suffix.lower() in (".cmd", ".bat"):
            # npm 的 Windows 命令包装器需要 cmd；参数全部来自本地配置，不含需求文本或密码。
            if any(character in value for value in command for character in ("%", "\r", "\n")):
                raise WorkflowError("Codex 启动参数包含不允许的批处理展开字符。")
            command = [environ["COMSPEC"], "/d", "/s", "/c", '"' + list2cmdline(command) + '"']
        self.process = Popen(command, cwd=ROOT, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                             text=True, encoding="utf-8", errors="strict", bufsize=1,
                             creationflags=CREATE_NO_WINDOW)
        Thread(target=self._read, daemon=True).start()
        Thread(target=self._stderr, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "zentao_local_workflow", "version": "0.1.0"},
                                    "capabilities": {"experimentalApi": True}})
        self.notify("initialized", {})
        self.initialized = True
        configuration = self.request("config/read", {"includeLayers": False}).get("config", {})
        self.disabled_mcp = {name: {"enabled": False} for name in configuration.get("mcp_servers", {}) if name != "workflow"}
        models = self.request("model/list", {"includeHidden": False}).get("data", [])
        configured = self.settings["codex"]["model"] or configuration.get("model", "")
        candidates = [model for model in models if model.get("model", model.get("id")) == configured] if configured else [model for model in models if model.get("isDefault")]
        if len(candidates) != 1:
            raise WorkflowError("无法唯一确定可用模型，请在设置中明确填写账号可用的模型标识。")
        self.model = candidates[0].get("model", candidates[0].get("id"))
        modes = self.request("collaborationMode/list", {}).get("data", [])
        self.modes = {mode.get("mode"): mode for mode in modes if mode.get("mode")}
        if "plan" not in self.modes or "default" not in self.modes:
            raise WorkflowError("当前 Codex 协议未提供计划及默认模式，已停止，不模拟或替换模式。")

    def _write(self, message):
        with self.write_lock:
            if not self.process or self.process.poll() is not None:
                raise WorkflowError("Codex 连接已经断开。")
            self.process.stdin.write(dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def request(self, method, params):
        request_id = next(self.ids)
        waiter = Queue(maxsize=1)
        with self.lock:
            self.waiters[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                response = waiter.get(timeout=self.settings["codex"]["rpc_timeout_seconds"])
            except Empty:
                raise WorkflowError("Codex 接口响应超时，流程已停止，未自动重试。") from None
            if "error" in response:
                raise WorkflowError("Codex 接口拒绝请求：" + str(response["error"].get("message", "未知错误")))
            return response.get("result", {})
        finally:
            with self.lock:
                self.waiters.pop(request_id, None)

    def notify(self, method, params):
        self._write({"method": method, "params": params})

    def respond(self, request_id, result):
        self._write({"id": request_id, "result": result})

    def _read(self):
        try:
            for line in self.process.stdout:
                message = loads(line)
                if "method" in message:
                    self.callback(message)
                else:
                    with self.lock:
                        waiter = self.waiters.get(message.get("id"))
                    if waiter:
                        waiter.put(message)
        except (OSError, ValueError, UnicodeError) as error:
            self.callback({"method": "client/error", "params": {"message": "Codex 通道解析中断：" + str(error)}})
        finally:
            self.stderr_ready.wait(timeout=1)
            diagnostic = "\n".join(self.startup_lines).strip()
            with self.lock:
                waiters = list(self.waiters.values())
            for waiter in waiters:
                if waiter.empty():
                    waiter.put({"error": {"message": "Codex 进程已退出。" + ("\n" + diagnostic if diagnostic else "")}})
            if not self.closing:
                self.callback({"method": "client/disconnected", "params": {}})

    def _stderr(self):
        # 只保留初始化前的 CLI 参数错误；不保存模型、工具或第三方服务的 stderr。
        for line in self.process.stderr:
            if not self.initialized:
                self.startup_lines.append(line.strip()[:600])
                self.stderr_ready.set()

    def close(self):
        self.closing = True
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except (OSError, TimeoutExpired):
                self.process.terminate()


def thread_parameters(state, model, disabled_mcp):
    session_path = ROOT / ".local" / "sessions" / state["id"]
    if state["phase"] == "branch":
        return {"cwd": str(session_path), "model": model,
                "approvalPolicy": "on-request", "sandbox": "read-only",
                "config": {"mcp_servers": {**disabled_mcp, "workflow": {"enabled": False}}},
                "developerInstructions": BRANCH_INSTRUCTIONS}
    server = {"command": python_executable,
              "args": [str(ROOT / "mcp.py"), state["id"]], "cwd": str(ROOT),
              "required": True, "enabled": True, "startup_timeout_sec": 30,
              "enabled_tools": ["task_context", "list_files", "read_file", "search_text", "baseline_file", "changes", "write_file", "replace_text", "delete_file"]}
    return {"cwd": str(session_path / "workspace"), "model": model,
            "approvalPolicy": "on-request", "sandbox": "read-only",
            "config": {"mcp_servers": {**disabled_mcp, "workflow": server}},
            "developerInstructions": (ROOT / "skills" / "zentao-delivery" / "SKILL.md").read_text(encoding="utf-8")}
