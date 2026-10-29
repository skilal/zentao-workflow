"""仅提供工作区文件和需求资料工具的本地 STDIO MCP。"""

from fnmatch import fnmatch
from json import dumps, loads
from pathlib import Path
from sys import stdin, stdout

from workflow.gitops import differences, git, safe_file
from workflow.settings import WorkflowError
from workflow.store import SessionStore, write_authorized


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string"}
FILE_ARGS = {"repository": STRING, "path": STRING}
TOOLS = [
    ("task_context", "读取本次工作项资料、补充说明、确认计划和工作区清单。", schema({})),
    ("list_files", "列出指定仓库的文件；pattern 为 glob；不访问依赖、Git 和输出目录。", schema({"repository": STRING, "pattern": STRING}, ("repository",))),
    ("read_file", "读取 UTF-8 源文件，按行分段返回。", schema({**FILE_ARGS, "start_line": {"type": "integer", "minimum": 1}, "line_count": {"type": "integer", "minimum": 1, "maximum": 500}}, ("repository", "path"))),
    ("search_text", "在指定仓库文本中搜索字面量；最多返回 100 项并明确标记截断。", schema({"repository": STRING, "text": STRING, "pattern": STRING}, ("repository", "text"))),
    ("baseline_file", "读取实施前基线中的指定文件，用于区分原有与新增问题。", schema(FILE_ARGS, ("repository", "path"))),
    ("changes", "读取相对初始基线的累计差异；超长时用文件工具逐项查看。", schema({"repository": STRING}, ("repository",))),
    ("write_file", "仅实施或修复阶段可用：写入一个工作区内 UTF-8 文件。", schema({**FILE_ARGS, "content": STRING}, ("repository", "path", "content"))),
    ("replace_text", "仅实施或修复阶段可用：精确替换唯一一处文本，匹配不唯一则停止。", schema({**FILE_ARGS, "old": STRING, "new": STRING}, ("repository", "path", "old", "new"))),
    ("delete_file", "仅实施或修复阶段可用：删除计划范围内的一个普通文件。", schema(FILE_ARGS, ("repository", "path"))),
]
SKIP = {".git", ".local", ".codex", ".idea", ".husky", "node_modules", "target", "dist", "build", "logs", "__pycache__", ".venv", "outputs", "output"}


def matches(relative, pattern):
    if not pattern:
        raise WorkflowError("文件筛选 pattern 不能为空。")
    return fnmatch(relative, pattern) or (pattern.startswith("**/") and fnmatch(relative, pattern[3:]))


def read_source(path):
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def file_newline(text):
    remaining = text.replace("\r\n", "")
    if "\r\n" in text and ("\n" in remaining or "\r" in remaining):
        raise WorkflowError("文件包含混合行尾，第一版不自动改写，请先人工明确格式处理。")
    if "\r" in remaining:
        raise WorkflowError("第一版不自动改写使用单独 CR 行尾的文件。")
    return "\r\n" if "\r\n" in text else "\n"


def use_newline(text, newline):
    return text.replace("\r\n", "\n").replace("\n", newline)


class Tools:
    def __init__(self, session_id):
        self.store = SessionStore()
        self.session_id = session_id

    def files(self, repository, pattern="*"):
        root = Path(repository["path"])
        pending = [root]
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir()):
                if path.name.casefold() in SKIP or path.is_symlink():
                    continue
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    relative = path.relative_to(root).as_posix()
                    if matches(relative, pattern):
                        yield relative, path

    def call(self, name, arguments):
        state = self.store.load(self.session_id)
        if name == "task_context":
            return {**{key: state.get(key) for key in ("item", "items", "project", "snapshot", "supplement", "repos", "approved_plan", "approval_source", "plan_round", "phase", "stage")},
                    "decisions": [entry for entry in state["decisions"] if entry.get("delivery_round", 1) == state.get("delivery_round", 1)],
                    "supplement_images": state.get("supplement_images", []),
                    "delivery_round": state.get("delivery_round", 1), "followup": state.get("followup")}
        repositories = {repository["name"]: repository for repository in state["repos"]}
        repository = repositories.get(arguments.get("repository"))
        if repository is None:
            raise WorkflowError("仓库不属于当前会话的工作区。")
        if name == "list_files":
            results = []
            for relative, _ in self.files(repository, arguments.get("pattern", "*")):
                results.append(relative)
                if len(results) > 2000:
                    return {"files": results[:2000], "truncated": True, "note": "请缩小 pattern。"}
            return {"files": results, "truncated": False}
        if name == "search_text":
            query = arguments["text"]
            if not query:
                raise WorkflowError("搜索文本不能为空。")
            results = []
            for relative, path in self.files(repository, arguments.get("pattern", "*")):
                if path.stat().st_size > 2 * 1024 * 1024:
                    continue
                try:
                    text = path.read_text(encoding="utf-8-sig")
                except UnicodeError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if query in line:
                        results.append({"path": relative, "line": number, "text": line[:800]})
                        if len(results) > 100:
                            return {"matches": results[:100], "truncated": True}
            return {"matches": results, "truncated": False}
        if name == "changes":
            data = differences(repository)
            if len(data["patch"]) > 100000:
                data["patch"] = data["patch"][:100000]
                data["truncated"] = True
                data["note"] = "必须结合文件清单逐个读取，不得声称已审查截断部分。"
            return data
        relative = arguments["path"]
        path = safe_file(repository, relative)
        if any(part.casefold() in SKIP for part in Path(relative).parts):
            raise WorkflowError("不允许访问依赖、Git 或工作流配置目录。")
        if name == "baseline_file":
            return git(repository["path"], "show", repository["baseline"] + ":" + Path(relative).as_posix())
        if name == "read_file":
            if path.stat().st_size > 4 * 1024 * 1024:
                raise WorkflowError("单文件过大，第一版不自动读取。")
            source = read_source(path)
            lines = source.lstrip("\ufeff").splitlines()
            start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
            if start < 1 or not 1 <= count <= 500:
                raise WorkflowError("行号或读取行数无效。")
            return {"total_lines": len(lines), "start_line": start, "line_ending": "CRLF" if "\r\n" in source else "LF", "utf8_bom": source.startswith("\ufeff"),
                    "text": "\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, min(len(lines), start - 1 + count)))}
        if name not in ("write_file", "replace_text", "delete_file"):
            raise WorkflowError("未知 MCP 工具。")
        authorized = write_authorized(state) if state.get("approval_source") == "followup" else state.get("approved_hash")
        if state["stage"] not in ("实施中", "修复中") or not authorized:
            raise WorkflowError("当前阶段禁止修改文件，必须等待计划确认或修复选择。")
        if path.relative_to(Path(repository["path"]).resolve()).as_posix().casefold() in {value.casefold() for value in repository.get("preserved_paths", [])}:
            raise WorkflowError("该文件包含任务开始前的本机保留改动，不会自动修改或提交；需要修改时请先取得用户明确指示。")
        if path.name.casefold() in ("agents.md", "claude.md", "qwen.md"):
            raise WorkflowError("工作流不允许通过修改项目指令文件扩大执行权限。")
        if name == "delete_file":
            if not path.is_file():
                raise WorkflowError("删除目标不是普通文件。")
            path.unlink()
            return "文件已删除。"
        if name == "replace_text":
            old, new = arguments["old"], arguments["new"]
            text = read_source(path)
            newline = file_newline(text)
            old, new = use_newline(old, newline), use_newline(new, newline)
            if not old or text.count(old) != 1:
                raise WorkflowError("待替换文本必须恰好匹配一处，未修改文件。")
            text = text.replace(old, new, 1)
        else:
            text = arguments["content"]
            if path.is_file():
                previous = read_source(path)
                text = use_newline(text, file_newline(previous))
                if previous.startswith("\ufeff") and not text.startswith("\ufeff"):
                    text = "\ufeff" + text
        if len(text.encode("utf-8")) > 4 * 1024 * 1024:
            raise WorkflowError("写入内容超出单文件上限。")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return "文件已保存，仅允许静态检查。"


def serve(session_id):
    tools = Tools(session_id)
    for line in stdin:
        request_id = None
        try:
            request = loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": request["params"]["protocolVersion"],
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "workflow", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": name, "description": description, "inputSchema": definition,
                                     "annotations": {"readOnlyHint": name not in ("write_file", "replace_text", "delete_file"),
                                                     "openWorldHint": False}} for name, description, definition in TOOLS]}
            elif method == "tools/call":
                try:
                    value = tools.call(request["params"]["name"], request["params"].get("arguments", {}))
                    result = {"content": [{"type": "text", "text": value if isinstance(value, str) else dumps(value, ensure_ascii=False)}]}
                except (WorkflowError, OSError, ValueError, KeyError) as error:
                    result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
            elif method == "ping":
                result = {}
            elif method and method.startswith("notifications/"):
                continue
            else:
                raise WorkflowError("不支持该 MCP 方法。")
            if request_id is not None:
                stdout.write(dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False) + "\n")
                stdout.flush()
        except (ValueError, WorkflowError, KeyError) as error:
            stdout.write(dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(error)}}, ensure_ascii=False) + "\n")
            stdout.flush()
