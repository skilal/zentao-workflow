"""读取空闲判定证据，以及可恢复的本机改动携带切换。"""

from hashlib import sha256
from json import loads
from pathlib import Path
from re import sub

from workflow.compile_checks import redact
from workflow.gitops import checkout_snapshot, git, reject_hooks, safe_file, switch_branch
from workflow.settings import WorkflowError


IDLE_SCHEMA = {"type": "object", "properties": {"files": {"type": "array", "items": {
    "type": "object", "properties": {"path": {"type": "string"},
    "impact": {"type": "string", "enum": ["nonfunctional", "functional", "unknown"]},
    "reason": {"type": "string"}}, "required": ["path", "impact", "reason"], "additionalProperties": False}}},
    "required": ["files"], "additionalProperties": False}


class IdleAnalysisRequired(Exception):
    def __init__(self, request):
        self.request = request


def redact_evidence(text):
    text = sub(r"(?is)(<(?:password|passwd|token|secret|authorization|api[_-]?key)\b[^>]*>).*?(</[^>]+>)",
               r"\1[凭据已隐藏]\2", text)
    text = sub(r'''(?i)((?:name|key)\s*=\s*["'](?:password|passwd|token|secret|authorization|api[_-]?key)["'][^>]*?value\s*=\s*)["'][^"']*["']''',
               r'\1"[凭据已隐藏]"', text)
    return redact(text)


def idle_evidence(path, snapshot):
    files = []
    for relative in snapshot["paths"]:
        file = safe_file({"path": str(path)}, relative)
        diff = git(path, "--literal-pathspecs", "diff", "HEAD", "--no-ext-diff", "--no-textconv",
                   "--no-color", "--", relative)
        staged = git(path, "--literal-pathspecs", "diff", "--cached", "--no-ext-diff", "--no-textconv",
                     "--no-color", "--", relative)
        entry = {"path": relative, "diff": redact_evidence(diff), "staged_diff": redact_evidence(staged)}
        if file.is_file():
            data = file.read_bytes()
            entry.update(size=len(data), sha256=sha256(data).hexdigest(), magic=data[:16].hex())
            if not diff and not staged:
                if len(data) <= 128000 and b"\0" not in data:
                    try:
                        entry["content"] = redact_evidence(data.decode("utf-8"))
                    except UnicodeError:
                        entry["binary"] = True
                else:
                    entry["binary"] = True
        files.append(entry)
    return files


def idle_decision(path, state):
    snapshot = checkout_snapshot(path)
    head, branch = git(path, "rev-parse", "HEAD"), git(path, "branch", "--show-current")
    if not branch:
        return False
    if git(path, "ls-files", "--unmerged", "-z", binary=True):
        return False
    if not snapshot["paths"]:
        return True
    request = {"path": str(Path(path).resolve()), "snapshot": snapshot, "head": head, "branch": branch}
    saved = state.get("idle_assessments", {}).get(request["path"].casefold())
    if saved and all(saved[key] == request[key] for key in request):
        return all(file["impact"] == "nonfunctional" for file in saved["files"])
    raise IdleAnalysisRequired(request)


def validate_idle(text, paths):
    result = loads(text)
    files = result.get("files") if isinstance(result, dict) else None
    if (not isinstance(files, list) or len(files) != len(paths) or any(not isinstance(file, dict) for file in files)
            or {file.get("path") for file in files} != set(paths)
            or any(file.get("impact") not in {"nonfunctional", "functional", "unknown"}
                   or not isinstance(file.get("reason"), str) or not file["reason"].strip() for file in files)):
        raise WorkflowError("空闲判定必须逐文件返回功能影响及依据，不能遗漏或重复文件。")
    return [{"path": file["path"], "impact": file["impact"], "reason": redact(file["reason"])} for file in files]


def automatic_checkout(path, selection, baseline, store, state):
    """完整 stash 保留暂存状态；冲突时仅撤销本次恢复写入并回到原分支。"""
    reject_hooks(path)
    snapshot = checkout_snapshot(path)
    if snapshot["paths"]:
        assessed = state.get("idle_assessments", {}).get(str(Path(path).resolve()).casefold())
        if (not assessed or assessed["snapshot"] != snapshot or assessed["head"] != git(path, "rev-parse", "HEAD")
                or assessed["branch"] != git(path, "branch", "--show-current")
                or any(file["impact"] != "nonfunctional" for file in assessed["files"])):
            raise WorkflowError("本机改动与空闲判定不一致，请重新分析后切换。")
    request = {"path": str(path), "selection": dict(selection), "head": git(path, "rev-parse", "HEAD"),
               "from_branch": git(path, "branch", "--show-current"), "baseline": baseline,
               "snapshot": snapshot, "selected_paths": snapshot["paths"], "status": "waiting", "automatic": True}
    state["checkout_request"] = request
    store.save(state)
    if not snapshot["paths"]:
        switch_branch(path, selection, baseline)
        state.pop("checkout_request")
        return
    untracked = git(path, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    untracked = [value.decode("utf-8") for value in untracked.split(b"\0") if value]
    git(path, "stash", "push", "--include-untracked", "-m", f"workflow-auto-checkout:{state['id']}")
    request.update(stash=git(path, "rev-parse", "refs/stash"), status="stashed")
    store.save(state)
    try:
        if checkout_snapshot(path)["paths"]:
            raise WorkflowError("备份后出现新的本机改动，停止切换。")
        switch_branch(path, selection, baseline)
        request["status"] = "switched"
        store.save(state)
        git(path, "stash", "apply", "--index", request["stash"])
    except WorkflowError as error:
        try:
            # 不清理其他进程新写入的路径，无法完整核实时保留备份等待人工。
            current = checkout_snapshot(path)
            if not set(current["paths"]).issubset(snapshot["paths"]):
                raise WorkflowError("恢复期间出现备份范围外改动，停止自动回退。")
            tracked = git(path, "ls-files", "-z", binary=True)
            tracked = {value.decode("utf-8") for value in tracked.split(b"\0") if value}
            for relative in untracked:
                file = safe_file({"path": str(path)}, relative)
                if relative not in tracked and file.is_file():
                    saved = git(path, "show", f"{request['stash']}^3:{relative}", binary=True)
                    if file.read_bytes() != saved:
                        raise WorkflowError("未跟踪文件在恢复期间发生变化，停止自动回退。")
                    file.unlink()
            restore = sorted(set(snapshot["paths"]) & tracked)
            if restore:
                git(path, "--literal-pathspecs", "restore", "--source=HEAD", "--staged", "--worktree", "--", *restore)
            git(path, "switch", "--no-overwrite-ignore", request["from_branch"])
            git(path, "stash", "apply", "--index", request["stash"])
            if checkout_snapshot(path) != snapshot or git(path, "rev-parse", "HEAD") != request["head"]:
                raise WorkflowError("回退后的文件或暂存状态与原始备份不一致。")
            request["status"] = "rolled_back"
            store.save(state)
        except WorkflowError as restore_error:
            request.update(status="rollback_failed", restore_error=redact(str(restore_error)))
            store.save(state)
            raise WorkflowError(f"智能切换回退未完成，备份保留在 stash {request['stash']}，请人工核对。") from restore_error
        raise WorkflowError("智能切换发生冲突或受阻，已恢复原分支、文件及暂存状态，请人工处理后继续。") from error
    request["status"] = "restored"
    store.save(state)
