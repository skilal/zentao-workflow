"""仅提供工作区文件和需求资料工具的本地 STDIO MCP。"""

from fnmatch import fnmatch
from json import dumps, loads
from os import scandir
from pathlib import Path
from sys import stdin, stdout

from workflow.compile_checks import compile_context
from workflow.context import context_hash, requirement_context
from workflow.delivery import previous_task_changes, validate_review_request
from workflow.gitops import differences, git, safe_file
from workflow.settings import WorkflowError
from workflow.store import SessionStore, write_authorized
from workflow.user_input import pending_input, register_input


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string"}
FILE_ARGS = {"repository": STRING, "path": STRING}
LINE_ARGS = {"start_line": {"type": "integer", "minimum": 1},
             "line_count": {"type": "integer", "minimum": 1, "maximum": 500}}
TOOLS = [
    ("task_context", "读取当前授权、计划和工作区；首次分析或资料版本变化时设置 include_materials=true 读取完整归一化资料。", schema({"include_materials": {"type": "boolean"}})),
    ("list_files", "列出指定仓库的文件；pattern 为 glob；不访问依赖、Git 和输出目录。", schema({"repository": STRING, "pattern": STRING}, ("repository",))),
    ("read_file", "读取当前 UTF-8 文件；默认 200 行，最多 500 行；next_line 指明续读位置。多文件优先使用 read_files。", schema({**FILE_ARGS, **LINE_ARGS}, ("repository", "path"))),
    ("read_files", "一次读取多个当前或基线文件片段；最多 10 项、合计 2000 行；每项 baseline=true 表示基线。", schema({
        "files": {"type": "array", "minItems": 1, "maxItems": 10,
                  "items": schema({**FILE_ARGS, **LINE_ARGS, "baseline": {"type": "boolean"}}, ("repository", "path"))}
    }, ("files",))),
    ("search_text", "在指定仓库文本中搜索字面量；最多返回 100 项并明确标记截断。", schema({"repository": STRING, "text": STRING, "pattern": STRING}, ("repository", "text"))),
    ("baseline_file", "按行读取实施前基线；默认 200 行，最多 500 行；返回 next_line；可用 read_files 批量核对。", schema({**FILE_ARGS, **LINE_ARGS}, ("repository", "path"))),
    ("changes", "读取相对初始基线的累计差异；超长时用文件工具逐项查看。", schema({"repository": STRING}, ("repository",))),
    ("ask_user", "将问题交给控制器并立即结束回合；由网页等待明确答复，禁止 sleep、轮询或默认放行。", schema({
        "questions": {"type": "array", "minItems": 1, "maxItems": 3, "items": schema({
            "id": STRING, "header": STRING, "question": STRING,
            "options": {"type": "array", "minItems": 1, "maxItems": 6,
                        "items": schema({"label": STRING, "description": STRING}, ("label", "description"))}
        }, ("id", "header", "question"))}
    }, ("questions",))),
    ("request_review", "仅续聊中用户明确要求审查或重新提交时登记请求；回合结束后由控制器重新 Review，通过后本地提交。不会直接提交。", schema({
        "instruction": STRING, "commit_message": STRING,
        "include_paths": {"type": "array", "items": schema(FILE_ARGS, ("repository", "path"))}
    }, ("instruction", "commit_message", "include_paths"))),
    ("write_file", "仅控制器授权的实施或修复阶段可用：写入一个工作区内 UTF-8 文件；编译修复限诊断文件。", schema({**FILE_ARGS, "content": STRING}, ("repository", "path", "content"))),
    ("replace_text", "仅控制器授权的实施或修复阶段可用：精确替换唯一一处文本；编译修复限诊断文件。", schema({**FILE_ARGS, "old": STRING, "new": STRING}, ("repository", "path", "old", "new"))),
    ("delete_file", "仅控制器授权的实施或修复阶段可用：删除范围内普通文件；编译修复限诊断文件。", schema(FILE_ARGS, ("repository", "path"))),
]
SKIP = {".git", ".local", ".codex", ".idea", ".husky", "node_modules", "target", "dist", "build", "logs", "__pycache__", ".venv", "outputs", "output"}


def matches(relative, pattern):
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
        if not isinstance(pattern, str) or not pattern:
            raise WorkflowError("文件筛选 pattern 不能为空。")
        pattern = pattern.replace("\\", "/")
        parts = pattern.split("/")
        if Path(pattern).is_absolute() or Path(pattern).drive or ".." in parts:
            raise WorkflowError("文件筛选必须位于当前工作区内。")
        root = Path(repository["path"]).resolve()
        prefix = []
        for part in parts:
            if any(character in part for character in "*?["):
                break
            prefix.append(part)
        directory = root
        for part in prefix:
            directory = directory / part
            if part.casefold() in SKIP or directory.is_symlink():
                return
        if len(prefix) == len(parts):
            path = safe_file(repository, pattern)
            if path.is_file():
                yield path.relative_to(root).as_posix(), path
            return
        directory = safe_file(repository, "/".join(prefix)) if prefix else root
        if not directory.is_dir():
            return
        pending = [directory]
        while pending:
            directory = pending.pop()
            with scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: Path(entry.name))
            for entry in children:
                if entry.name.casefold() in SKIP or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    relative = path.relative_to(root).as_posix()
                    if matches(relative, pattern):
                        yield relative, path

    def call(self, name, arguments):
        state = self.store.load(self.session_id)
        if name != "ask_user" and pending_input(self.store, state):
            raise WorkflowError("问题已交给控制器，等待用户明确答复期间不能继续调用工具。")
        if name == "task_context":
            include = arguments.get("include_materials", False)
            if type(include) is not bool:
                raise WorkflowError("include_materials 必须为布尔值。")
            snapshot = requirement_context(state)
            return {**{key: state.get(key) for key in ("item", "items", "project", "supplement", "repos", "approved_plan", "approval_source", "plan_round", "phase", "stage")},
                    "snapshot": snapshot if include else {key: snapshot[key] for key in ("blockers", "feishu_links")},
                    "materials_revision": context_hash(snapshot), "materials_included": include,
                    "decisions": [entry for entry in state["decisions"] if entry.get("delivery_round", 1) == state.get("delivery_round", 1)],
                    "supplement_images": state.get("supplement_images", []),
                    "delivery_round": state.get("delivery_round", 1), "followup": state.get("followup"),
                    "review_enabled": state.get("review_enabled", True),
                    "previous_task_changes": previous_task_changes(state) if state["phase"] == "followup" else [],
                    "compile_check": state.get("compile_check", {"enabled": False}),
                    "compile_checks": compile_context(state)}
        if name == "ask_user":
            return register_input(self.store, state, arguments.get("questions"))
        if name == "request_review":
            request = validate_review_request(state, arguments)
            path = self.store.directory(self.session_id) / f"review-request-{state['delivery_round']}.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(dumps(request, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            return "审查提交请求已登记。回合结束后由控制器核对文件范围并重新 Review，无遗留问题后执行本地提交。"
        repositories = {repository["name"]: repository for repository in state["repos"]}
        if name == "read_files":
            requests = arguments.get("files")
            if not isinstance(requests, list) or not 1 <= len(requests) <= 10:
                raise WorkflowError("批量读取必须包含 1 至 10 个文件片段。")
            prepared = [self.read_request(repositories, request) for request in requests]
            if sum(request[4] for request in prepared) > 2000:
                raise WorkflowError("批量读取合计最多 2000 行，请缩小读取范围。")
            return {"files": [self.read_segment(*request) for request in prepared]}
        if name in ("read_file", "baseline_file"):
            request = self.read_request(repositories, {**arguments, "baseline": name == "baseline_file"})
            return self.read_segment(*request)
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
        if name not in ("write_file", "replace_text", "delete_file"):
            raise WorkflowError("未知 MCP 工具。")
        authorized = write_authorized(state) if state.get("approval_source") == "followup" else state.get("approved_hash")
        if state["stage"] not in ("实施中", "修复中", "续聊处理中", "编译修复中") or not authorized:
            raise WorkflowError("当前阶段禁止修改文件，必须等待计划确认或修复选择。")
        if state["stage"] == "编译修复中":
            checks = state.get("compile_checks", [])
            if (repository["role"] != "backend" or not write_authorized(state)
                    or not state.get("compile_check", {}).get("enabled") or not checks
                    or checks[-1]["status"] != "repairing" or repository["name"] != checks[-1].get("repair_repository")
                    or Path(relative).as_posix() not in checks[-1].get("repair_paths", [])):
                raise WorkflowError("编译修复只能修改控制器授权的本次错误文件。")
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

    def read_request(self, repositories, arguments):
        if not isinstance(arguments, dict):
            raise WorkflowError("文件片段必须指定仓库和相对路径。")
        repository = repositories.get(arguments.get("repository"))
        if repository is None:
            raise WorkflowError("仓库不属于当前会话的工作区。")
        relative = arguments.get("path")
        path = safe_file(repository, relative)
        if any(part.casefold() in SKIP for part in Path(relative).parts):
            raise WorkflowError("不允许访问依赖、Git 或工作流配置目录。")
        start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
        if type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 500:
            raise WorkflowError("start_line 必须大于等于 1，line_count 必须为 1 至 500；请按 next_line 分段读取。")
        baseline = arguments.get("baseline", False)
        if type(baseline) is not bool:
            raise WorkflowError("baseline 必须为布尔值。")
        return repository, relative, path, start, count, baseline

    def read_segment(self, repository, relative, path, start, count, baseline):
        if baseline:
            data = git(repository["path"], "show", repository["baseline"] + ":" + Path(relative).as_posix(), binary=True)
            if len(data) > 4 * 1024 * 1024:
                raise WorkflowError("单文件过大，第一版不自动读取。")
            source = data.decode("utf-8")
        else:
            if path.stat().st_size > 4 * 1024 * 1024:
                raise WorkflowError("单文件过大，第一版不自动读取。")
            source = read_source(path)
        lines = source.lstrip("\ufeff").splitlines()
        end = min(len(lines), start - 1 + count)
        return {"repository": repository["name"], "path": relative, "baseline": baseline,
                "total_lines": len(lines), "start_line": start,
                "next_line": end + 1 if end < len(lines) else None,
                "line_ending": "CRLF" if "\r\n" in source else "LF", "utf8_bom": source.startswith("\ufeff"),
                "text": "\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end))}


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
                                     "annotations": {"readOnlyHint": name not in ("write_file", "replace_text", "delete_file", "request_review", "ask_user"),
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
