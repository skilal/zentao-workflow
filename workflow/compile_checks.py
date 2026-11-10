"""控制器专用编译适配器；输出仅在内存保留，模型不能调用命令。"""

from collections import deque
from ctypes import Structure, WinDLL, byref, c_longlong, c_size_t, c_uint32, c_ulonglong, c_void_p, sizeof
from hashlib import sha256
from json import dumps
from os import environ
from pathlib import Path
from re import IGNORECASE, finditer, search, sub
from shutil import which
from subprocess import CREATE_NO_WINDOW, DEVNULL, PIPE, Popen, TimeoutExpired
from threading import Event, Lock, Thread
from xml.etree import ElementTree

from workflow.gitops import git, reject_hooks, safe_file, working_paths
from workflow.settings import WorkflowError


DEPENDENCY_ERRORS = (
    "has not been downloaded", "could not resolve dependencies",
    "could not resolve artifact", "could not find artifact", "non-resolvable parent pom",
    "pluginresolutionexception", "plugin or one of its dependencies could not be resolved",
    "cannot be resolved in offline mode",
)
ENVIRONMENT_ERRORS = (
    "unsupported class file",
    "invalid target release", "release version", "no compiler is provided",
    "java_home", "econnrefused", "econnreset", "enotfound", "etimedout",
    "unable to find valid certification", "pkix", "401 unauthorized", "403 forbidden",
    "could not transfer artifact", "unknownhostexception", "connection timed out",
    "outofmemoryerror", "heap out of memory", "eacces", "eperm", "enospc",
)
CODE_ERRORS = ("compilation failure", "compilation error", "cannot find symbol", "类型", "编译错误")
NS = {"m": "http://maven.apache.org/POM/4.0.0"}


class JobLimits(Structure):
    _fields_ = [("process_time", c_longlong), ("job_time", c_longlong), ("flags", c_uint32),
                ("minimum_working_set", c_size_t), ("maximum_working_set", c_size_t),
                ("active_processes", c_uint32), ("affinity", c_size_t),
                ("priority", c_uint32), ("scheduling", c_uint32)]


class ExtendedJobLimits(Structure):
    _fields_ = [("basic", JobLimits), ("io", c_ulonglong * 6), ("process_memory", c_size_t),
                ("job_memory", c_size_t), ("peak_process_memory", c_size_t), ("peak_job_memory", c_size_t)]


class ProcessTree:
    """Windows Job 管理整个子进程树，启动器退出后仍可终止后代。"""

    def __init__(self):
        self.kernel = WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c_void_p, c_void_p]
        self.kernel.CreateJobObjectW.restype = c_void_p
        self.kernel.SetInformationJobObject.argtypes = [c_void_p, c_uint32, c_void_p, c_uint32]
        self.kernel.AssignProcessToJobObject.argtypes = [c_void_p, c_void_p]
        self.kernel.TerminateJobObject.argtypes = [c_void_p, c_uint32]
        self.kernel.CloseHandle.argtypes = [c_void_p]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = ExtendedJobLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, byref(limits), sizeof(limits)):
            self.close()
            raise WorkflowError("无法创建编译进程组，已停止，不启动未受控检查。")

    def attach(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill()
            process.wait()
            raise WorkflowError("无法接管编译进程，已停止检查。")

    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def redact(text):
    text = sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = sub(r"https?://[^\s<>\"']+", "[地址已隐藏]", text, flags=IGNORECASE)
    text = sub(r'''(?i)(?:bearer\s+\S+|["']?(?:password|passwd|token|secret|authorization|api[_-]?key)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+))''',
               "[凭据已隐藏]", text)
    return text


def compile_context(state):
    """模型只接收当前交付的必要信息，文件及授权摘要留在控制器。"""
    checks = [batch for batch in state.get("compile_checks", [])
              if batch["delivery_round"] == state.get("delivery_round", 1)]
    if not checks:
        return []
    batch = checks[-1]
    backend_names = {repo["name"] for repo in state["repos"] if repo["role"] == "backend"}
    result = {key: batch[key] for key in ("status", "fix_round", "limit")}
    result["repositories"] = [{key: entry[key] for key in
                               ("name", "status", "modules", "summary", "diagnostics", "failure_reasons", "analysis",
                                "compiler_diagnostics", "diagnostics_complete")
                               if key in entry}
                              for entry in batch["repositories"] if entry["name"] in backend_names]
    if batch.get("analysis_repository") in backend_names:
        result["analysis_repository"] = batch["analysis_repository"]
        result["automatic_analysis"] = batch.get("automatic_analysis", False)
        result["task_paths"] = batch.get("task_paths", {})
    if batch.get("repair_repository") in backend_names:
        result.update(repair_repository=batch["repair_repository"], repair_paths=batch["repair_paths"])
    return [result]


def compile_repositories(state):
    """独立检查使用点击时的 HEAD，不改写会话原审查基线。"""
    manual = state.get("manual_compile")
    return [{**repo, "baseline": manual["heads"][repo["name"]]} for repo in state["repos"]] if manual else state["repos"]


def source_snapshot(repository):
    """包含本机保留文件，防止构建脚本偷偷改变不属于任务的源码。"""
    names = git(repository["path"], "ls-files", "--cached", "--others", "--exclude-standard", "-z", binary=True)
    result = {}
    for name in sorted(set(part.decode("utf-8") for part in names.split(b"\0") if part)):
        path = safe_file(repository, name)
        result[name] = sha256(path.read_bytes()).hexdigest() if path.is_file() else "deleted"
    return result


def snapshot_hash(repository):
    return sha256(dumps(source_snapshot(repository), sort_keys=True).encode()).hexdigest()


def dependency_hash(repository):
    root = Path(repository["path"])
    snapshot = source_snapshot(repository)
    dependencies = {name: digest for name, digest in snapshot.items()
                    if Path(name).name == "pom.xml"
                    or name.startswith(".mvn/")}
    # 被忽略的本机配置也属于本轮授权条件，内容只参与摘要，不保存。
    for name in (".mvn/maven.config", ".mvn/jvm.config"):
        path = root / name
        if path.is_file():
            dependencies[name] = sha256(path.read_bytes()).hexdigest()
    # 本机仓库源与认证配置变化同样使下载授权失效，明文只用于计算摘要。
    home = Path.home()
    personal_configs = [home / ".m2" / "settings.xml"]
    for index, path in enumerate(personal_configs):
        if path.is_file():
            dependencies[f"personal-{index}"] = sha256(path.read_bytes()).hexdigest()
    dependencies["environment"] = sha256(dumps({key: environ.get(key, "") for key in
        ("MAVEN_ARGS", "MAVEN_OPTS", "JAVA_HOME")},
        sort_keys=True).encode()).hexdigest()
    return sha256(dumps(dependencies, sort_keys=True).encode()).hexdigest()


def backend_modules(repository):
    root = Path(repository["path"])
    modules, registered = set(), set()

    def visit(relative):
        path = safe_file(repository, str(Path(relative) / "pom.xml"))
        if not path.is_file():
            raise WorkflowError("Maven 模块缺少 pom.xml，不能确定编译范围。")
        try:
            document = ElementTree.parse(path)
        except ElementTree.ParseError:
            raise WorkflowError("pom.xml 格式无效，无法确定编译模块。") from None
        for child in document.findall("m:modules/m:module", NS):
            module = (Path(relative) / (child.text or "").strip()).as_posix()
            resolved = safe_file(repository, module).relative_to(root.resolve()).as_posix()
            if resolved in registered:
                raise WorkflowError("Maven 模块重复或循环引用，无法确定编译范围。")
            registered.add(resolved)
            visit(resolved)

    visit(".")
    for name in working_paths(repository):
        current = Path(name).parent
        if name == "pom.xml" or name.startswith(".mvn/"):
            return []
        while current != Path(".") and not (root / current / "pom.xml").is_file():
            current = current.parent
        if current == Path("."):
            # 根目录说明文档不改变模块；工程级构建配置检查整个工程。
            if name.lower().endswith((".md", ".txt")):
                continue
            return []
        module = current.as_posix()
        if module not in registered:
            raise WorkflowError("改动所属 Maven 模块未登记在当前工程中，请人工确定范围。")
        modules.add(module)
    if not modules:
        return []
    return sorted(modules)


def compiler_diagnostics(repository, output):
    """保留编译器位置证据；支持 Maven 和 javac 的明确错误格式并去重。"""
    normalized = sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output).replace("\\", "/")
    root = Path(repository["path"]).resolve().as_posix()
    diagnostics, locations, complete = {}, 0, True
    lines = normalized.splitlines()
    pattern = (r"^(?P<path>.+?\.java)\s*(?:[:\s]*\[(?P<bracket>\d+)(?:[,：:]\d+)?\]"
               r"|\((?P<paren>\d+)(?:,\d+)?\)|:(?P<colon>\d+)(?::\d+)?):?\s*(?P<message>.*)$")
    for index, raw in enumerate(lines):
        error_prefix = bool(search(r"^\s*\[ERROR\]", raw))
        line = sub(r"^\s*\[ERROR\]\s*", "", raw).strip()
        match = search(pattern, line)
        if not match or (not error_prefix and not search(r"^(?:error|错误)\s*[:：]", match["message"], IGNORECASE)):
            continue
        locations += 1
        filename = match["path"].strip()
        relative = filename[len(root) + 1:] if filename.casefold().startswith(root.casefold() + "/") else filename
        try:
            path = safe_file(repository, relative)
        except WorkflowError:
            complete = False
            continue
        number = int(match["bracket"] or match["paren"] or match["colon"])
        message = match["message"].strip()
        if number < 1 or not path.is_file() or not message:
            complete = False
            continue
        for continuation in lines[index + 1:index + 4]:
            continuation = sub(r"^\s*\[ERROR\]\s*", "", continuation).strip()
            if not search(r"^(?:symbol|location|符号|位置)\s*[:：]", continuation, IGNORECASE):
                break
            message += "；" + continuation
        relative = path.relative_to(Path(repository["path"]).resolve()).as_posix()
        entry = {"path": relative, "line": number, "message": redact(message)[:600]}
        diagnostics[(relative, number, entry["message"])] = entry
    return {"items": list(diagnostics.values())[:24], "complete": complete and locations > 0 and len(diagnostics) <= 24}


def code_diagnostics(repository, output, parsed=None):
    """直接自动修复仍限本轮变更行，其他位置交给后续因果分析。"""
    parsed = compiler_diagnostics(repository, output) if parsed is None else parsed
    if not parsed["complete"] or not parsed["items"]:
        return []
    changed = set(working_paths(repository))
    ranges = {}
    for item in parsed["items"]:
        relative = item["path"]
        if relative not in changed:
            return []
        if relative not in ranges:
            tracked = git(repository["path"], "ls-files", "--error-unmatch", "--", relative, optional=True)
            ranges[relative] = None
            if tracked:
                patch = git(repository["path"], "diff", "--no-ext-diff", "--no-textconv", "--unified=0", "HEAD", "--", relative)
                ranges[relative] = [(int(hunk[1]), int(hunk[1]) + int(hunk[2] or 1)) for hunk in
                                    finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", patch)]
        if ranges[relative] is not None and not any(start <= item["line"] < end for start, end in ranges[relative]):
            return []
    return parsed["items"]


def failure_reasons(repository, output):
    """只保留必要的脱敏错误原因，不持久化整段输出、堆栈或构建进度。"""
    cleaned = redact(output).replace("\\", "/")
    cleaned = cleaned.replace(str(Path(repository["path"])).replace("\\", "/") + "/", "")
    reasons = []
    for line in cleaned.splitlines():
        if not search(r"^\s*\[(?:ERROR|FATAL)\]|^\s*(?:Caused by:|Error:|错误[:：])", line, IGNORECASE) and not any(
                marker in line.lower() for marker in ENVIRONMENT_ERRORS + DEPENDENCY_ERRORS):
            continue
        reason = sub(r"^\s*\[(?:ERROR|FATAL)\]\s*", "", line).strip()
        if (not reason or search(r"^(?:->\s*\[Help|\[Help|For more information|To see the full|Re-run Maven|"
                                 r"After correcting|mvn\s|at\s)", reason, IGNORECASE)):
            continue
        reason = reason[:600]
        if reason not in reasons:
            reasons.append(reason)
        if len(reasons) == 8:
            break
    return reasons


def classify(repository, returncode, output):
    lower = output.lower()
    if not returncode:
        return {"status": "passed", "summary": "编译检查通过。", "diagnostics": []}
    reasons = failure_reasons(repository, output)
    if any(marker in lower for marker in ENVIRONMENT_ERRORS):
        return {"status": "blocked", "summary": "环境、网络或工具配置异常，具体原因见下方；处理后可重新检查。",
                "diagnostics": [], "failure_reasons": reasons, "failure_kind": "environment"}
    if any(marker in lower for marker in DEPENDENCY_ERRORS) or (
            "offline" in lower and any(marker in lower for marker in ("cannot access", "cannot resolve"))):
        return {"status": "dependencies", "summary": "本机缺少依赖或 Maven 插件，可选择补齐依赖并重新检查；下载须明确授权。",
                "diagnostics": [], "failure_reasons": reasons, "failure_kind": "dependencies"}
    has_code_error = any(marker in lower for marker in CODE_ERRORS) or search(
        r"(?m)^\s*\[ERROR\][^\r\n]*\.java[:\s]*[\[(:]\d+", output.replace("\\", "/"))
    parsed = compiler_diagnostics(repository, output)
    has_code_error = has_code_error or bool(parsed["items"])
    evidence = code_diagnostics(repository, output, parsed) if has_code_error else []
    if evidence:
        return {"status": "code_error", "summary": "编译诊断明确指向本轮修改的代码。", "diagnostics": evidence,
                "failure_reasons": reasons}
    return {"status": "blocked", "summary": "检查未通过，尚不能确认属于本次引入的代码错误，可先分析失败原因。",
            "diagnostics": [], "failure_reasons": reasons,
            "compiler_diagnostics": parsed["items"], "diagnostics_complete": parsed["complete"],
            "analysis_required": bool(reasons or parsed["items"]), "failure_kind": "code" if has_code_error else "unknown"}


class DiagnosticOutput:
    """每个输出流保留诊断块和末尾摘要，避免后续进度挤掉前面的错误。"""

    def __init__(self):
        self.tail = deque(maxlen=32)
        self.errors = []
        self.error_bytes = 0
        self.remaining = b""
        self.context = 0

    def append(self, chunk):
        self.tail.append(chunk)
        lines = (self.remaining + chunk).split(b"\n")
        self.remaining = lines.pop()
        for line in lines:
            self.capture(line)

    def capture(self, line):
        text = sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line.decode("utf-8", errors="replace"))
        error = bool(search(r"^\s*\[(?:ERROR|FATAL)\]|\.java[:(].*\berror\s*:", text, IGNORECASE))
        if error:
            self.context = 4
        if (error or self.context) and self.error_bytes + len(line) < 128 * 1024:
            self.errors.append(line)
            self.error_bytes += len(line) + 1
        self.context = max(0, self.context - 1)

    def text(self):
        if self.remaining:
            self.capture(self.remaining)
            self.remaining = b""
        return (b"\n".join(self.errors) + b"\n" + b"".join(self.tail)).decode("utf-8", errors="replace")


class CompileRunner:
    def __init__(self):
        self.cancelled = Event()
        self.lock = Lock()
        self.process = None
        self.tree = None

    def stop(self):
        self.cancelled.set()
        with self.lock:
            if self.tree:
                self.tree.terminate()

    def execute(self, command, directory, download):
        if self.cancelled.is_set():
            raise WorkflowError("编译检查已暂停。")
        # Windows 批处理启动器会经 cmd 解释参数，不允许仓库模块名注入命令。
        if any(search(r'[&|<>^%!"\r\n]', argument) for argument in command):
            raise WorkflowError("编译工具路径或参数包含不支持的命令字符，请人工处理。")
        environment = dict(environ)
        environment["CI"] = "true"
        streams = [DiagnosticOutput(), DiagnosticOutput()]
        with self.lock:
            if self.cancelled.is_set():
                raise WorkflowError("编译检查已暂停。")
            self.tree = ProcessTree()
            try:
                self.process = Popen(command, cwd=directory, stdin=DEVNULL, stdout=PIPE, stderr=PIPE,
                                     env=environment, creationflags=CREATE_NO_WINDOW)
                process = self.process
                self.tree.attach(process)
            except Exception:
                self.tree.close()
                self.tree = None
                raise

        def collect(stream, output):
            while chunk := stream.read(2048):
                output.append(chunk)
            stream.close()

        readers = [Thread(target=collect, args=(stream, output), daemon=True)
                   for stream, output in zip((process.stdout, process.stderr), streams)]
        for reader in readers:
            reader.start()
        while process.poll() is None:
            try:
                process.wait(timeout=0.2)
            except TimeoutExpired:
                continue
        with self.lock:
            self.process = None
            self.tree.close()
            self.tree = None
        for reader in readers:
            reader.join()
        if self.cancelled.is_set():
            raise WorkflowError("编译检查已暂停。")
        return process.returncode, "\n".join(output.text() for output in streams)

    def check(self, repository, modules, download=False):
        if repository["role"] != "backend":
            raise WorkflowError("编译检查仅支持后端仓库，前端继续参与代码 Review。")
        reject_hooks(repository["path"])
        before = source_snapshot(repository)
        program = which("mvn.cmd")
        if not program:
            raise WorkflowError("Maven 编译工具不存在，请人工配置；不会自动安装或切换工具。")
        try:
            command = [program, "-B", "-ntp", "-Dmaven.test.skip=true"]
            if not download:
                command.append("-o")
            if modules:
                command.extend(["-pl", ",".join(modules), "-am"])
            code, output = self.execute([*command, "compile"], repository["path"], download)
            return classify(repository, code, output)
        finally:
            if source_snapshot(repository) != before:
                if repository.get("change_record"):
                    raise WorkflowError("检查期间共享源码已变化，本次结果失效，请重新检查；未自动回滚或扩大修复范围。")
                raise WorkflowError("编译过程改变了源码或非忽略文件，已停止；请人工检查，未自动回滚。")
