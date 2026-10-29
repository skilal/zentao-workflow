"""分支元数据、独立工作区和本地提交；不执行项目脚本。"""

from hashlib import sha256
from contextlib import ExitStack
from functools import wraps
from os import environ
from pathlib import Path, PureWindowsPath
from subprocess import CREATE_NO_WINDOW, PIPE, run
from threading import Lock, RLock

from workflow.settings import WorkflowError


HOOK_NAMES = ("post-checkout", "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-auto-gc",
              "post-index-change", "reference-transaction")
REPOSITORY_LOCKS = {}
LOCK_REGISTRY = Lock()


def git(repository, *arguments, optional=False, binary=False):
    environment = dict(environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    process = run(["git", "-c", "core.fsmonitor=false", "-C", str(repository), *arguments],
                  stdin=PIPE, stdout=PIPE, stderr=PIPE, env=environment, creationflags=CREATE_NO_WINDOW)
    if process.returncode and not optional:
        raise WorkflowError("Git 操作停止：" + process.stderr.decode("utf-8", errors="replace").strip())
    return process.stdout if binary else process.stdout.decode("utf-8", errors="replace").strip()


def repository_lock(repository):
    root = Path(repository).resolve()
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    common = (root / common).resolve() if not common.is_absolute() else common.resolve()
    with LOCK_REGISTRY:
        return REPOSITORY_LOCKS.setdefault(str(common).casefold(), RLock())


def locked_repository(operation):
    @wraps(operation)
    def locked(repository, *args, **kwargs):
        path = repository["path"] if isinstance(repository, dict) else repository
        with repository_lock(path):
            return operation(repository, *args, **kwargs)
    return locked


def worktrees(repository):
    output = git(repository, "worktree", "list", "--porcelain", "-z", binary=True)
    result = []
    for record in output.split(b"\0\0"):
        entry = {}
        for field in record.split(b"\0"):
            key, _, value = field.decode("utf-8").partition(" ")
            if key:
                entry[key] = value
        if "worktree" in entry:
            result.append(entry)
    return result


def branches(repository):
    local = git(repository, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
    remote = git(repository, "for-each-ref", "--format=%(refname:short)", "refs/remotes").splitlines()
    return {"local": local, "bases": local + [branch for branch in remote if not branch.endswith("/HEAD")]}


def branch_catalog(repository):
    remote_prefix = f"refs/remotes/{repository['remote']}/"
    output = git(repository["path"], "for-each-ref",
                 "--format=%(refname)%00%(objectname)%00%(committerdate:iso-strict)%00%(contents:subject)%00%(symref)",
                 "refs/heads", remote_prefix)
    result = {}
    for line in output.splitlines():
        ref, commit_id, date, subject, symbolic = line.split("\0", 4)
        if symbolic:
            continue
        local = ref.startswith("refs/heads/")
        name = ref[len("refs/heads/"):] if local else ref[len(remote_prefix):]
        entry = result.setdefault(name, {"name": name, "local": False, "remote": False})
        entry["local" if local else "remote"] = True
        if local or "commit" not in entry:
            entry.update(commit=commit_id, date=date, subject=subject)
    return sorted(result.values(), key=lambda entry: entry["name"])


@locked_repository
def refresh_branches(repository):
    reject_hooks(repository["path"])
    git(repository["path"], "fetch", "--no-tags", "--", repository["remote"])
    heads = git(repository["path"], "ls-remote", "--heads", "--", repository["remote"])
    live = {line.split("\t", 1)[1][len("refs/heads/"):] for line in heads.splitlines()}
    catalog = branch_catalog(repository)
    for entry in catalog:
        entry["remote"] = entry["remote"] and entry["name"] in live
    return [entry for entry in catalog if entry["local"] or entry["remote"]]


def reject_hooks(repository):
    configured = git(repository, "config", "--path", "--get", "core.hooksPath", optional=True)
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = Path(repository) / path
    else:
        path = Path(git(repository, "rev-parse", "--git-path", "hooks"))
        if not path.is_absolute():
            path = Path(repository) / path
    active = [name for name in HOOK_NAMES if (path / name).is_file()]
    if active:
        raise WorkflowError("仓库存在可能执行程序的 Git 钩子：" + "、".join(active) +
                            "。按禁止运行验证的规范停止；不会自动禁用钩子。")


@locked_repository
def prepare(repository, destination, branch, base, create, track=False):
    repository, destination = Path(repository).resolve(), Path(destination).resolve()
    if not branch.strip() or not base.strip():
        raise WorkflowError("目标分支和代码基准尚未确定。")
    git(repository, "check-ref-format", "--branch", branch)
    reject_hooks(repository)
    occupied = next((entry for entry in worktrees(repository) if entry.get("branch") == "refs/heads/" + branch), None)
    if occupied:
        path = Path(occupied["worktree"])
        owner = next((path.parts[index + 1] for index, part in enumerate(path.parts[:-1]) if part == "sessions"), "外部工作区")
        raise WorkflowError(f"分支 {branch} 已被占用：{path}（会话：{owner}）。请切换到该会话或明确选择其他分支。")
    baseline = git(repository, "rev-parse", "--verify", base + "^{commit}")
    configured_hooks = git(repository, "config", "--path", "--get", "core.hooksPath", optional=True)
    if configured_hooks and not Path(configured_hooks).is_absolute():
        hook_paths = [(Path(configured_hooks) / name).as_posix() for name in HOOK_NAMES]
        tracked_hooks = git(repository, "ls-tree", "--name-only", baseline, "--", *hook_paths)
        if tracked_hooks:
            raise WorkflowError("所选代码基准包含 Git 钩子，创建工作区前已停止，不会自动绕过。")
    if destination.exists():
        raise WorkflowError("独立工作区已经存在，不会覆盖或自动清理。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if create:
        if track:
            git(repository, "worktree", "add", "--track", "-b", branch, str(destination), base)
        else:
            git(repository, "worktree", "add", "-b", branch, str(destination), baseline)
    else:
        expected = git(repository, "rev-parse", "--verify", "refs/heads/" + branch + "^{commit}")
        if expected != baseline:
            raise WorkflowError("现有目标分支与所选代码基准不一致，请重新选择。")
        git(repository, "worktree", "add", str(destination), branch)
    reject_hooks(destination)
    return {"source": str(repository), "path": str(destination), "branch": branch,
            "base": base, "baseline": baseline, "create": bool(create)}


def ensure_baseline(repository):
    if git(repository["path"], "rev-parse", "HEAD") != repository["baseline"]:
        raise WorkflowError("工作区 HEAD 已改变，不能继续使用原审查基线。")
    if git(repository["path"], "branch", "--show-current") != repository["branch"]:
        raise WorkflowError("工作区分支已改变，已停止。")


def changed_paths(repository):
    path = repository["path"]
    ensure_baseline(repository)
    tracked = git(path, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--", binary=True)
    added = git(path, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    return sorted(set(part.decode("utf-8", errors="strict") for part in (tracked + added).split(b"\0") if part))


def safe_file(repository, relative):
    root = Path(repository["path"]).resolve()
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or Path(relative).drive:
        raise WorkflowError("必须使用工作区内的相对文件路径。")
    if ":" in relative or "\x00" in relative or PureWindowsPath(relative).is_reserved():
        raise WorkflowError("文件路径包含不允许的 Windows 特殊名称。")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or ".git" in [part.casefold() for part in Path(relative).parts]:
        raise WorkflowError("文件路径不在选定代码工作区内。")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise WorkflowError("第一版不修改符号链接路径。")
    return candidate


def manifest(repositories):
    result = {}
    for repository in repositories:
        for relative in changed_paths(repository):
            path = safe_file(repository, relative)
            if path.exists() and not path.is_file():
                raise WorkflowError("第一版不提交目录、子模块或特殊文件。")
            digest = sha256(path.read_bytes()).hexdigest() + ":" + oct(path.stat().st_mode) if path.exists() else "deleted"
            result[repository["name"] + "/" + relative] = digest
    return result


def differences(repository):
    ensure_baseline(repository)
    text = git(repository["path"], "diff", "--no-ext-diff", "--no-textconv", "--stat", repository["baseline"], "--")
    patch = git(repository["path"], "diff", "--no-ext-diff", "--no-textconv", "--no-color", repository["baseline"], "--")
    new_files = git(repository["path"], "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    return {"repository": repository["name"], "baseline": repository["baseline"], "stat": text,
            "patch": patch, "untracked": [part.decode("utf-8") for part in new_files.split(b"\0") if part]}


@locked_repository
def commit(repository, message, expected_manifest):
    ensure_baseline(repository)
    reject_hooks(repository["path"])
    if manifest([repository]) != expected_manifest:
        raise WorkflowError("代码在最后一次 Review 后发生变化，必须重新 Review 后再提交。")
    paths = changed_paths(repository)
    if not paths:
        return None
    if git(repository["path"], "diff", "--cached", "--name-only"):
        raise WorkflowError("暂存区存在外部改动，已停止，未重新暂存。")
    git(repository["path"], "add", "--", *paths)
    staged = git(repository["path"], "diff", "--cached", "--name-only", "-z", binary=True)
    if sorted(part.decode("utf-8") for part in staged.split(b"\0") if part) != paths:
        raise WorkflowError("实际暂存文件与本次修改清单不同，已停止。")
    if manifest([repository]) != expected_manifest:
        raise WorkflowError("暂存期间文件发生变化，已停止提交。")
    git(repository["path"], "commit", "-m", message)
    return git(repository["path"], "rev-parse", "HEAD")


def cleanup_workspaces(store, state, repositories):
    """全部预检后逐个移除；每次成功都保存进度，不强制清理代码。"""
    directory = store.directory(state["id"])
    workspace = directory / "workspace"
    if not state["repos"] and not state.get("selections") and not workspace.exists():
        return
    sources = {str(Path(entry["path"]).resolve()) for entry in repositories}
    sources.update(str(Path(entry["source"]).resolve()) for entry in state["repos"])
    sources.update(str(Path(entry["path"]).resolve()) for entry in state.get("selections", []))
    removed = state.setdefault("cleanup", {}).setdefault("removed", [])
    with ExitStack() as stack:
        for source in sorted(sources, key=str.casefold):
            stack.enter_context(repository_lock(source))
        registered = {}
        for source in sorted(sources, key=str.casefold):
            reject_hooks(source)
            for entry in worktrees(source):
                raw_path = Path(entry["worktree"])
                path = raw_path.resolve()
                if path.is_relative_to(directory):
                    if path.parent != workspace or raw_path.is_symlink() or (raw_path.exists() and getattr(raw_path.lstat(), "st_file_attributes", 0) & 0x400):
                        raise WorkflowError(f"工作区路径不符合会话目录约束：{raw_path}")
                    if "locked" in entry or "prunable" in entry:
                        raise WorkflowError(f"工作区被锁定或注册失效，已停止删除：{path}")
                    registered[str(path)] = source
        expected = {str(Path(entry["path"]).resolve()): entry["source"] for entry in state["repos"]}
        for index, selection in enumerate(state.get("selections", [])):
            expected.setdefault(str(workspace / f"repo-{index + 1}"), selection["path"])
        for name, source in expected.items():
            path = Path(name)
            if path.parent != workspace:
                raise WorkflowError(f"工作区不在目标会话内，已停止删除：{path}")
            if str(path) in removed and not path.exists() and str(path) not in registered:
                continue
            if not path.exists() and str(path) not in registered:
                continue
            if registered.get(str(path)) != str(Path(source).resolve()):
                raise WorkflowError(f"工作区与来源仓库的注册信息不匹配：{path}")
        if workspace.exists():
            for path in workspace.iterdir():
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise WorkflowError(f"工作区目录包含链接：{path}")
                if path.is_dir() and str(path.resolve()) not in registered:
                    raise WorkflowError(f"发现未登记的工作区目录，已停止删除：{path}")
        for name, source in registered.items():
            path = Path(name)
            common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            expected_common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            if common != expected_common or not (path / ".git").is_file():
                raise WorkflowError(f"工作区与来源仓库不匹配：{path}")
            if git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored"):
                raise WorkflowError(f"工作区存在未提交或未跟踪文件，请处理后再删除：{path}")
        for name, source in sorted(registered.items()):
            try:
                git(source, "worktree", "remove", "--", name)
            except WorkflowError as error:
                remaining = [path for path in registered if path not in removed]
                raise WorkflowError(f"工作区清理停止。已清理：{'、'.join(removed) or '无'}；未清理：{'、'.join(remaining)}。{error}") from None
            if name not in removed:
                removed.append(name)
            store.save(state)
