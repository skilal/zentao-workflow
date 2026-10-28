"""人工选择分支后的独立工作区和本地提交；不执行项目脚本。"""

from hashlib import sha256
from os import environ
from pathlib import Path, PureWindowsPath
from subprocess import CREATE_NO_WINDOW, PIPE, run

from workflow.settings import WorkflowError


HOOK_NAMES = ("post-checkout", "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-auto-gc",
              "post-index-change", "reference-transaction")


def git(repository, *arguments, optional=False, binary=False):
    environment = dict(environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    process = run(["git", "-c", "core.fsmonitor=false", "-C", str(repository), *arguments],
                  stdin=PIPE, stdout=PIPE, stderr=PIPE, env=environment, creationflags=CREATE_NO_WINDOW)
    if process.returncode and not optional:
        raise WorkflowError("Git 操作停止：" + process.stderr.decode("utf-8", errors="replace").strip())
    return process.stdout if binary else process.stdout.decode("utf-8", errors="replace").strip()


def branches(repository):
    local = git(repository, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
    remote = git(repository, "for-each-ref", "--format=%(refname:short)", "refs/remotes").splitlines()
    return {"local": local, "bases": local + [branch for branch in remote if not branch.endswith("/HEAD")]}


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


def prepare(repository, destination, branch, base, create):
    repository, destination = Path(repository).resolve(), Path(destination).resolve()
    if not branch.strip() or not base.strip():
        raise WorkflowError("目标分支和代码基准必须由人工明确选择。")
    git(repository, "check-ref-format", "--branch", branch)
    reject_hooks(repository)
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
