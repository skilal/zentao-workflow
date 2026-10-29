"""控制器专用编译适配器；输出仅在内存保留，模型不能调用命令。"""

from collections import deque
from ctypes import Structure, WinDLL, byref, c_longlong, c_size_t, c_uint32, c_ulonglong, c_void_p, sizeof
from hashlib import sha256
from json import dumps
from os import environ
from pathlib import Path
from re import IGNORECASE, escape, finditer, search, sub
from shutil import which
from subprocess import CREATE_NO_WINDOW, DEVNULL, PIPE, Popen, TimeoutExpired
from threading import Event, Lock, Thread
from xml.etree import ElementTree

from workflow.gitops import git, reject_hooks, safe_file, working_paths
from workflow.settings import WorkflowError


DEPENDENCY_ERRORS = (
    "cannot access", "has not been downloaded", "could not resolve dependencies",
    "could not resolve artifact", "could not find artifact", "non-resolvable parent pom",
    "pluginresolutionexception", "plugin or one of its dependencies could not be resolved",
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
    result["repositories"] = [{key: entry[key] for key in ("name", "status", "modules", "summary", "diagnostics")}
                              for entry in batch["repositories"] if entry["name"] in backend_names]
    if batch.get("repair_repository") in backend_names:
        result.update(repair_repository=batch["repair_repository"], repair_paths=batch["repair_paths"])
    return [result]


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


def code_diagnostics(repository, output):
    """只把明确位于本次新增/修改行的编译诊断判为可自动修复。"""
    paths = working_paths(repository)
    evidence = []
    for relative in paths:
        if not relative.endswith(".java"):
            continue
        path = safe_file(repository, relative)
        if not path.is_file():
            continue
        filename = relative.replace("\\", "/")
        normalized = output.replace("\\", "/")
        pattern = r"(?:" + escape(filename) + r")[\s:]*[\[(:]\s*(\d+)(?:[,：:]\d+)?[\])]?"
        matches = list(finditer(pattern, normalized))
        if not matches:
            continue
        tracked = git(repository["path"], "ls-files", "--error-unmatch", "--", relative, optional=True)
        ranges = []
        if tracked:
            patch = git(repository["path"], "diff", "--no-ext-diff", "--no-textconv", "--unified=0", "HEAD", "--", relative)
            for hunk in finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", patch):
                start, count = int(hunk[1]), int(hunk[2] or 1)
                ranges.append((start, start + count))
        for match in matches:
            line = int(match[1])
            if tracked and not any(start <= line < end for start, end in ranges):
                return []
            excerpt = normalized[match.start():].splitlines()[0][:350]
            evidence.append({"path": relative, "line": line, "message": redact(excerpt)})
    # 诊断指向其他文件时，不能仅凭部分证据自动修复。
    all_locations = list(finditer(r"[^\s\[\]():]+\.java[:\s]*[\[(:]\d+", output.replace("\\", "/")))
    if not evidence or len(all_locations) > len(evidence):
        return []
    return evidence[:12]


def classify(repository, returncode, output):
    lower = output.lower()
    if not returncode:
        return {"status": "passed", "summary": "编译检查通过。", "diagnostics": []}
    if any(marker in lower for marker in ENVIRONMENT_ERRORS):
        return {"status": "blocked", "summary": "环境、网络或工具配置异常，请人工处理后重新检查。", "diagnostics": []}
    if any(marker in lower for marker in DEPENDENCY_ERRORS):
        return {"status": "dependencies", "summary": "本机依赖或缓存不足，需要人工选择是否允许下载。", "diagnostics": []}
    evidence = code_diagnostics(repository, output) if any(marker in lower for marker in CODE_ERRORS) else []
    if evidence:
        return {"status": "code_error", "summary": "编译诊断明确指向本轮修改的代码。", "diagnostics": evidence}
    return {"status": "blocked", "summary": "检查未通过，尚不能确认属于本次引入的代码错误，请人工处理。", "diagnostics": []}


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
        chunks = deque(maxlen=32)
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

        def collect(stream):
            while chunk := stream.read(2048):
                chunks.append(chunk)
            stream.close()

        readers = [Thread(target=collect, args=(stream,), daemon=True) for stream in (process.stdout, process.stderr)]
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
        return process.returncode, b"".join(chunks).decode("utf-8", errors="replace")

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
                raise WorkflowError("编译过程改变了源码或非忽略文件，已停止；请人工检查，未自动回滚。")
