"""分支元数据、工作目录分配和本地提交；不执行项目脚本。"""

from hashlib import sha256
from contextlib import ExitStack
from functools import wraps
from json import dumps
from os import environ
from pathlib import Path, PureWindowsPath
from subprocess import CREATE_NO_WINDOW, PIPE, run
from threading import Lock, RLock

from workflow.settings import WorkflowError
from workflow.store import session_repositories


HOOK_NAMES = ("post-checkout", "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-auto-gc",
              "post-index-change", "reference-transaction")
REPOSITORY_LOCKS = {}
LOCK_REGISTRY = Lock()


def git(repository, *arguments, optional=False, binary=False, input_data=None):
    environment = dict(environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    process = run(["git", "-c", "core.fsmonitor=false", "-C", str(repository), *arguments],
                  input=input_data or b"", stdout=PIPE, stderr=PIPE, env=environment, creationflags=CREATE_NO_WINDOW)
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
            entry.update(commit=commit_id, date=date, subject=subject, metadata_available=True)
    return sorted(result.values(), key=lambda entry: entry["name"])


@locked_repository
def refresh_branches(repository):
    reject_hooks(repository["path"])
    heads = git(repository["path"], "ls-remote", "--heads", "--", repository["remote"])
    live = {}
    for line in heads.splitlines():
        commit_id, separator, ref = line.partition("\t")
        if not commit_id or not separator or not ref.startswith("refs/heads/"):
            raise WorkflowError("远程分支清单格式不符合预期，已停止刷新。")
        live[ref[len("refs/heads/"):]] = commit_id
    catalog = {entry["name"]: entry for entry in branch_catalog(repository)}
    # 在线提交已变化时清空旧说明，候选分支定向拉取后再读取实际元数据。
    for name, commit_id in live.items():
        entry = catalog.setdefault(name, {"name": name, "local": False})
        if not entry["local"] and entry.get("commit") != commit_id:
            entry.update(commit=commit_id, date="", subject="", metadata_available=False)
    for name, entry in catalog.items():
        entry["remote"] = name in live
        if entry["remote"]:
            entry["remote_commit"] = live[name]
    return sorted((entry for entry in catalog.values() if entry["local"] or entry["remote"]),
                  key=lambda entry: entry["name"])


@locked_repository
def fetch_branch(repository, name):
    reject_hooks(repository["path"])
    git(repository["path"], "check-ref-format", "--branch", name)
    refspec = f"+refs/heads/{name}:refs/remotes/{repository['remote']}/{name}"
    git(repository["path"], "fetch", "--no-tags", "--", repository["remote"], refspec)
    entry = next(entry for entry in branch_catalog(repository) if entry["name"] == name)
    entry["remote_commit"] = git(repository["path"], "rev-parse", f"refs/remotes/{repository['remote']}/{name}")
    return entry


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


def workspace_claims(sessions, excluded_session):
    return {str(Path(repository["path"]).resolve()).casefold(): session["id"]
            for session in sessions if session["id"] != excluded_session and session["phase"] != "complete"
            for repository in [*session["repos"], *(repo for repo in session.get("retired_repos", [])
                                                   if repo.get("workspace_owned", True))]}


def switch_branch(path, selection, baseline):
    if selection["create"]:
        if selection.get("track"):
            git(path, "switch", "--track", "-c", selection["branch"], selection["base"])
        else:
            git(path, "switch", "-c", selection["branch"], baseline)
    else:
        git(path, "switch", selection["branch"])


def checkout_snapshot(path):
    staged = git(path, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames",
                 "--name-only", "-z", binary=True)
    paths = sorted(set(working_paths({"path": str(path)})) |
                   {value.decode("utf-8") for value in staged.split(b"\0") if value})
    contents = {}
    for relative in paths:
        file = safe_file({"path": str(path)}, relative)
        contents[relative] = sha256(file.read_bytes()).hexdigest() if file.is_file() else "deleted"
    index = git(path, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", binary=True)
    return {"paths": paths, "fingerprint": sha256(index + dumps(contents, sort_keys=True).encode()).hexdigest()}


@locked_repository
def carry_checkout(repository, store, state, paths):
    request = state["checkout_request"]
    path, selection = Path(request["path"]), request["selection"]
    if (request["status"] != "waiting" or not paths or len(paths) != len(set(paths))
            or not set(paths).issubset(request["snapshot"]["paths"])):
        raise WorkflowError("请从本次切换受阻的文件中明确选择要携带的改动。")
    reject_hooks(path)
    if workspace_claims(store.sessions(), state["id"]).get(str(path.resolve()).casefold()):
        raise WorkflowError("该目录已被其他会话占用，不能智能切换。")
    if (git(path, "rev-parse", "HEAD") != request["head"]
            or git(path, "branch", "--show-current") != request["from_branch"]
            or git(path, "rev-parse", "--verify", selection["base"] + "^{commit}") != request["baseline"]
            or checkout_snapshot(path) != request["snapshot"]):
        raise WorkflowError("分支或本机改动已变化，请重新尝试普通切换并核对文件。")
    if git(path, "ls-files", "--unmerged", "-z", binary=True):
        raise WorkflowError("仓库仍有未解决的合并冲突，请先手工处理。")
    staged = git(path, "--literal-pathspecs", "diff", "--cached", "--no-ext-diff", "--no-textconv",
                 "--binary", "--", *paths, binary=True)
    git(path, "--literal-pathspecs", "stash", "push", "--include-untracked", "-m",
        f"workflow-smart-checkout:{state['id']}", "--", *paths)
    request.update(status="stashed", selected_paths=paths, stash=git(path, "rev-parse", "refs/stash"))
    store.save(state)
    try:
        switch_branch(path, selection, request["baseline"])
        request["status"] = "switched"
        store.save(state)
        # 按文件 stash 也会记录其他文件的索引，单独恢复选中文件的暂存差异。
        git(path, "stash", "apply", request["stash"])
        if staged:
            git(path, "apply", "--cached", "--3way", "--binary", "--whitespace=nowarn", input_data=staged)
    except WorkflowError as error:
        request["status"] = "restore_failed" if request["status"] == "switched" else "switch_failed"
        store.save(state)
        raise WorkflowError(f"智能切换未完成，已停止；本机改动备份保留在 stash {request['stash']}。"
                            "请手工完成切换及改动恢复，再确认继续。\n" + str(error)) from error
    request["status"] = "restored"
    store.save(state)


@locked_repository
def confirm_checkout(repository, store, state, baseline=None):
    request = state["checkout_request"]
    if request["status"] not in {"stashed", "switched", "restore_failed", "switch_failed"}:
        raise WorkflowError("当前没有需要手工确认的智能切换。")
    branch = git(repository, "branch", "--show-current")
    if branch != request["selection"]["branch"]:
        raise WorkflowError(f"当前分支为 {branch or '分离 HEAD'}，目标分支为 {request['selection']['branch']}。"
                            "请先切换到目标分支，再确认改动已恢复。")
    if git(repository, "ls-files", "--unmerged", "-z", binary=True):
        raise WorkflowError("仓库仍有未解决的合并冲突，请先解决合并冲突，再确认改动已恢复。")
    head = git(repository, "rev-parse", "HEAD")
    change = request.get("baseline_change")
    if baseline is not None and (not change or baseline != change["current"]):
        raise WorkflowError("请先核对页面展示的提交变化，再明确确认当前新基准。")
    if head != request["baseline"]:
        # plan_started_at 在分支识别前设置，以实际计划线程和产物界定准备阶段。
        if (state["stage"] != "准备工作区失败" or state["phase"] != "plan"
                or any(state.get(key) for key in ("prepare_complete", "thread_id", "plan_round", "plan",
                                                  "approved_plan", "reviews", "commits"))):
            raise WorkflowError("目标分支提交已变化；仅尚未开始实际计划的工作区准备阶段允许确认新基准。"
                                "当前会话必须停止并重新核对原计划与代码。")
        if git(repository, "merge-base", request["baseline"], head, optional=True) != request["baseline"]:
            raise WorkflowError("目标分支当前提交不是旧基准的快进更新，不能直接确认新基准，请先核对提交历史。")
        if baseline != head:
            request["baseline_change"] = {"previous": request["baseline"], "current": head, "confirmed": False}
            store.save(state)
            return False
        request["baseline"] = head
        change["confirmed"] = True
    else:
        if baseline is not None:
            raise WorkflowError("目标分支已回到旧基准，页面展示的新基准已失效，请重新核对改动恢复情况。")
        request.pop("baseline_change", None)
    request["status"] = "restored"
    store.save(state)
    return True


@locked_repository
def acquire_workspace(selection, destination, store, state):
    """优先沿用目标分支目录，其次切换空闲目录，最后创建；登记与分配共用仓库锁。"""
    source, destination = Path(selection["path"]).resolve(), Path(destination).resolve()
    sessions = store.sessions()
    claims = workspace_claims(sessions, state["id"])
    session_states = {session["id"]: session for session in sessions}
    entries = worktrees(source)
    available = []
    for entry in entries:
        path = Path(entry["worktree"]).resolve()
        owner = claims.get(str(path).casefold())
        managed = None
        if path.is_relative_to(store.root.resolve()):
            managed = path.relative_to(store.root.resolve()).parts[0]
        reusable = not owner and (path == source or managed == state["id"] or
            (managed in session_states and session_states[managed]["phase"] == "complete"))
        if entry.get("branch") == "refs/heads/" + selection["branch"]:
            if "locked" in entry or "prunable" in entry:
                raise WorkflowError(f"目标分支工作区被锁定或登记失效，已停止：{path}")
            if not reusable:
                if owner or managed:
                    raise WorkflowError(f"分支 {selection['branch']} 已被占用：{path}（会话：{owner or managed}）。请先完成或删除占用会话，或明确选择其他分支。")
                raise WorkflowError(f"分支 {selection['branch']} 已在外部工作区检出：{path}。"
                                    "请点击“预览复用已有工作区”，核对后明确确认复用；不会自动接管该目录。")
            available = [entry]
            break
        if reusable and "locked" not in entry and "prunable" not in entry:
            available.append(entry)
    else:
        available.sort(key=lambda entry: Path(entry["worktree"]).resolve() != source)
    if not available:
        record = prepare(source, destination, selection["branch"], selection["base"],
                         selection["create"], selection.get("track", False))
        record.update(workspace_owned=True, workspace_mode="created", preserved_paths=[])
    else:
        entry = available[0]
        path = Path(entry["worktree"]).resolve()
        branch, base = selection["branch"], selection["base"]
        if not branch.strip() or not base.strip():
            raise WorkflowError("目标分支和代码基准尚未确定。")
        git(source, "check-ref-format", "--branch", branch)
        baseline = git(source, "rev-parse", "--verify", base + "^{commit}")
        if not selection["create"] and git(source, "rev-parse", "--verify", "refs/heads/" + branch + "^{commit}") != baseline:
            raise WorkflowError("现有目标分支与所选代码基准不一致，请重新选择。")
        reject_hooks(path)
        configured_hooks = git(path, "config", "--path", "--get", "core.hooksPath", optional=True)
        if configured_hooks and not Path(configured_hooks).is_absolute():
            hook_paths = [(Path(configured_hooks) / name).as_posix() for name in HOOK_NAMES]
            if git(path, "ls-tree", "--name-only", baseline, "--", *hook_paths):
                raise WorkflowError("所选代码基准包含 Git 钩子，切换分支前已停止，不会自动绕过。")
        preserved = working_paths({"path": str(path)})
        switching = entry.get("branch") != "refs/heads/" + branch
        if switching:
            try:
                switch_branch(path, selection, baseline)
            except WorkflowError as error:
                if "would be overwritten by checkout" in str(error):
                    state["checkout_request"] = {"path": str(path), "selection": dict(selection),
                        "head": git(path, "rev-parse", "HEAD"), "from_branch": git(path, "branch", "--show-current"),
                        "baseline": baseline, "status": "waiting", "snapshot": checkout_snapshot(path)}
                    store.save(state)
                raise
        request = state.get("checkout_request")
        if request and request["selection"] == selection and Path(request["path"]).resolve() == path:
            if request["status"] == "restored":
                preserved += request["selected_paths"]
            elif request["status"] == "waiting":
                state.pop("checkout_request")
        reject_hooks(path)
        record = {"source": str(source), "path": str(path), "branch": branch, "base": base,
                  "baseline": baseline, "create": bool(selection["create"]),
                  "workspace_owned": path.parent == store.directory(state["id"]) / "workspace",
                  "workspace_mode": "switched" if switching else "reused",
                  "preserved_paths": sorted(set(preserved + working_paths({"path": str(path)})))}
        if request and request["status"] == "restored" and request["selection"] == selection:
            record["checkout_backup"] = request["stash"]
    record.update(name=selection["name"], role=selection["role"])
    state["repos"].append(record)
    store.save(state)
    return record


def ensure_baseline(repository):
    if git(repository["path"], "rev-parse", "HEAD") != repository["baseline"]:
        raise WorkflowError("工作区 HEAD 已改变，不能继续使用原审查基线。")
    if git(repository["path"], "branch", "--show-current") != repository["branch"]:
        raise WorkflowError("工作区分支已改变，已停止。")


def changed_paths(repository):
    ensure_baseline(repository)
    return working_paths(repository)


def working_paths(repository):
    path = repository["path"]
    tracked = git(path, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", "HEAD", "--", binary=True)
    added = git(path, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    preserved = {relative.casefold() for relative in repository.get("preserved_paths", [])}
    paths = {part.decode("utf-8", errors="strict") for part in (tracked + added).split(b"\0") if part}
    return sorted(relative for relative in paths if relative.casefold() not in preserved)


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
    paths = changed_paths(repository)
    text = git(repository["path"], "diff", "--no-ext-diff", "--no-textconv", "--stat", repository["baseline"], "--", *paths) if paths else ""
    patch = git(repository["path"], "diff", "--no-ext-diff", "--no-textconv", "--no-color", repository["baseline"], "--", *paths) if paths else ""
    new_files = git(repository["path"], "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    return {"repository": repository["name"], "baseline": repository["baseline"], "stat": text,
            "patch": patch, "untracked": [part.decode("utf-8") for part in new_files.split(b"\0") if part and part.decode("utf-8") in paths]}


@locked_repository
def commit(repository, message, expected_manifest):
    ensure_baseline(repository)
    reject_hooks(repository["path"])
    if manifest([repository]) != expected_manifest:
        raise WorkflowError("代码在最后一次 Review 后发生变化，必须重新 Review 后再提交。")
    paths = changed_paths(repository)
    if not paths:
        return None
    staged_before = git(repository["path"], "diff", "--cached", "--no-renames", "--name-only", "-z", binary=True)
    preserved = {relative.casefold() for relative in repository.get("preserved_paths", [])}
    allowed_staged = preserved | {relative.casefold() for relative in repository.get("adopted_paths", [])}
    if any(part.decode("utf-8").casefold() not in allowed_staged for part in staged_before.split(b"\0") if part):
        raise WorkflowError("暂存区存在外部改动，已停止，未重新暂存。")
    git(repository["path"], "add", "--", *paths)
    staged = git(repository["path"], "diff", "--cached", "--no-renames", "--name-only", "-z", binary=True)
    if sorted(part.decode("utf-8") for part in staged.split(b"\0") if part and part.decode("utf-8").casefold() not in preserved) != paths:
        raise WorkflowError("实际暂存文件与本次修改清单不同，已停止。")
    if manifest([repository]) != expected_manifest:
        raise WorkflowError("暂存期间文件发生变化，已停止提交。")
    if preserved:
        git(repository["path"], "commit", "--only", "-m", message, "--", *paths)
    else:
        git(repository["path"], "commit", "-m", message)
    return git(repository["path"], "rev-parse", "HEAD")


def cleanup_workspaces(store, state, repositories):
    """全部预检后逐个移除；每次成功都保存进度，不强制清理代码。"""
    directory = store.directory(state["id"])
    workspace = directory / "workspace"
    if not state["repos"] and not state.get("selections") and not workspace.exists():
        return
    sources = {str(Path(entry["path"]).resolve()) for entry in repositories}
    all_repositories = session_repositories(state)
    sources.update(str(Path(entry["source"]).resolve()) for entry in all_repositories)
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
        expected = {str(Path(entry["path"]).resolve()): entry["source"] for entry in all_repositories if entry.get("workspace_owned", True)}
        prepared = {entry["name"] for entry in state["repos"]}
        for index, selection in enumerate(state.get("selections", [])):
            if selection["name"] not in prepared:
                suffix = f"-delivery-{state['delivery_round']}" if state.get("followup") else ""
                expected.setdefault(str(workspace / f"repo-{index + 1}{suffix}"), selection["path"])
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
        claims = workspace_claims(store.sessions(), state["id"])
        for name, source in registered.items():
            if name.casefold() in claims:
                raise WorkflowError(f"工作区正在被会话 {claims[name.casefold()]} 使用，不能删除：{name}")
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
