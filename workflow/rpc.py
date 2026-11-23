"""Codex App Server 双向 JSONL 通道；继承原生工具，保留工作流授权边界。"""

from itertools import count
from collections import deque
from json import dumps, loads
from os import environ
from pathlib import Path
from queue import Empty, Queue
from shutil import which
from subprocess import CREATE_NO_WINDOW, PIPE, Popen, TimeoutExpired, list2cmdline
from sys import executable as python_executable
from threading import Event, Lock, Thread, current_thread

from workflow.branching import BRANCH_INSTRUCTIONS
from workflow.mcp_server import BRANCH_COMPARE_TOOL, TOOLS
from workflow.settings import ROOT, WorkflowError
from workflow.workspace_paths import working_directory


MAX_INPUT_CHARACTERS = 1048576


class CodexRequestError(WorkflowError):
    """保留服务端错误原文，供调用方识别明确的状态冲突。"""

    def __init__(self, message):
        self.message = message
        super().__init__("Codex 接口拒绝请求：" + message)


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
        self.configuration = {}
        self.reader = None
        self.stderr_reader = None

    def connect(self):
        if self.process and self.process.poll() is None:
            return
        self.closing = False
        command = list(self.settings["codex"]["command"])
        executable = which(command[0])
        if not executable:
            raise WorkflowError("找不到配置中的 Codex 命令。请编辑 settings.json 的 codex.command，"
                                "将第一个元素设为本机 Codex 可执行文件的绝对路径，保存后重启本地服务。")
        command[0] = executable
        command += ["-c", "features.hooks=false", "-c", "features.multi_agent=false", "-c", "features.goals=false",
                    "app-server", "--listen", "stdio://"]
        if Path(executable).suffix.lower() in (".cmd", ".bat"):
            # npm 的 Windows 命令包装器需要 cmd；参数全部来自本地配置，不含需求文本或密码。
            if any(character in value for value in command for character in ("%", "\r", "\n")):
                raise WorkflowError("Codex 启动参数包含不允许的批处理展开字符。")
            command = [environ["COMSPEC"], "/d", "/s", "/c", '"' + list2cmdline(command) + '"']
        self.process = Popen(command, cwd=ROOT, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                             text=True, encoding="utf-8", errors="strict", bufsize=1,
                             creationflags=CREATE_NO_WINDOW)
        self.reader = Thread(target=self._read, daemon=True)
        self.stderr_reader = Thread(target=self._stderr, daemon=True)
        self.reader.start()
        self.stderr_reader.start()
        self.request("initialize", {"clientInfo": {"name": "zentao_local_workflow", "version": "0.1.0"},
                                    "capabilities": {"experimentalApi": True}})
        self.notify("initialized", {})
        self.initialized = True
        configuration = self.request("config/read", {"includeLayers": False}).get("config", {})
        self.configuration = configuration
        # 分支识别只启用受控的候选比较工具；业务线程直接继承原生配置。
        # config/read 的未设置字段为 null，不能作为 TOML 覆盖传回。
        self.disabled_mcp = {name: {**{key: value for key, value in server.items() if value is not None}, "enabled": False}
                             for name, server in configuration.get("mcp_servers", {}).items() if name != "workflow"}

    def catalog(self):
        models, cursor, cursors = [], None, set()
        while True:
            page = self.request("model/list", {"includeHidden": False, "cursor": cursor})
            for entry in page["data"]:
                models.append({"model": entry["model"], "display_name": entry["displayName"],
                               "reasoning_efforts": [option["reasoningEffort"] for option in entry["supportedReasoningEfforts"]],
                               "is_default": entry["isDefault"]})
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if cursor in cursors:
                raise WorkflowError("Codex 模型列表分页游标重复，已停止读取。")
            cursors.add(cursor)
        default = self.configuration.get("model")
        if not default:
            defaults = [entry["model"] for entry in models if entry["is_default"]]
            default = defaults[0] if len(defaults) == 1 else None
        return {"models": models, "default_model": default}

    def start(self, model):
        self.connect()
        self.model = model
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
        if method == "turn/start":
            characters = sum(len(entry["text"]) for entry in params["input"] if entry["type"] == "text")
            if characters > MAX_INPUT_CHARACTERS:
                raise WorkflowError(f"Codex 本轮输入共 {characters:,} 字符，超过 {MAX_INPUT_CHARACTERS:,} 字符上限，未发送请求。"
                                    "分支识别阶段可手动选择目标分支；其他阶段请缩小本轮输入范围后继续。未截断资料或自动重试。")
        request_id = next(self.ids)
        waiter = Queue(maxsize=1)
        with self.lock:
            self.waiters[request_id] = waiter
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                response = waiter.get(timeout=self.settings["codex"]["rpc_timeout_seconds"])
            except Empty:
                if method == "turn/steer":
                    raise WorkflowError("补充消息的发送结果未确认，请核对当前会话后再决定是否重发。") from None
                raise WorkflowError("Codex 接口响应超时，流程已停止，未自动重试。") from None
            if "error" in response:
                raise CodexRequestError(str(response["error"].get("message", "未知错误")))
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
        except (OSError, WorkflowError) as error:
            self.callback({"method": "client/error", "params": {"message": "Codex 通道读取或事件处理失败：" + str(error)}})
        except (ValueError, UnicodeError) as error:
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
                self.process.wait(timeout=3)
        for thread in (self.reader, self.stderr_reader):
            if thread and thread is not current_thread():
                thread.join(timeout=3)
                if thread.is_alive():
                    raise WorkflowError("Codex 通道仍未退出，已停止清理会话。")


def validate_selection(catalog, model, effort, *, allow_default=False):
    if not isinstance(model, str) or not isinstance(effort, str) or not effort:
        raise WorkflowError("请明确选择模型和推理强度。")
    selected = catalog["default_model"] if allow_default and model == "" else model
    candidates = [entry for entry in catalog["models"] if entry["model"] == selected]
    if len(candidates) != 1:
        raise WorkflowError("所选模型不可用，请刷新模型列表并重新选择。")
    if effort not in candidates[0]["reasoning_efforts"]:
        raise WorkflowError("所选模型不支持该推理强度，请重新选择。")
    return {"model": selected, "reasoning_effort": effort}


def thread_parameters(state, model, disabled_mcp):
    session_path = ROOT / ".local" / "sessions" / state["id"]
    server = {"command": python_executable,
              "args": [str(ROOT / "mcp.py"), state["id"]], "cwd": str(ROOT),
              "required": True, "enabled": True, "startup_timeout_sec": 30,
              "enabled_tools": [name for name, _, _ in TOOLS if name != BRANCH_COMPARE_TOOL]}
    if state["phase"] == "branch":
        server["enabled_tools"] = [BRANCH_COMPARE_TOOL]
        return {"cwd": str(session_path), "model": model,
                "approvalPolicy": "on-request", "sandbox": "read-only",
                "config": {"model_reasoning_effort": state["codex"]["reasoning_effort"],
                           "features.shell_tool": False, "features.apps": False,
                           "features.remote_plugin": False, "web_search": "disabled",
                           "mcp_servers": {**disabled_mcp, "workflow": server}},
                "developerInstructions": BRANCH_INSTRUCTIONS}
    workspace = working_directory(state, session_path)
    return {"cwd": str(workspace), "model": model,
            "approvalPolicy": "on-request", "sandbox": "read-only",
            "config": {"model_reasoning_effort": state["codex"]["reasoning_effort"],
                       "mcp_servers": {"workflow": server}},
            "developerInstructions": (ROOT / "skills" / "zentao-delivery" / "SKILL.md").read_text(encoding="utf-8")}
