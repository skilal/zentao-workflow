"""续聊中显式审查提交请求的范围校验，不执行 Git 提交。"""

from hashlib import sha256
from json import loads
from pathlib import Path

from workflow.gitops import manifest, safe_file
from workflow.settings import WorkflowError
from workflow.store import write_authorized


def previous_task_changes(state):
    recorded = {}
    shared_candidates = []
    for repository in state["repos"]:
        if repository.get("change_record"):
            from workflow.shared_changes import candidates as current_fragments, file_version, journals
            current, records = journals(repository)
            eligible = {entry["path"] for entry in current_fragments(repository)
                        if entry["source"] not in {"其他会话", "归属重叠"}}
            historical = {}
            for other, record in sorted(records, key=lambda item: item[1]["round"]):
                if record["session"] == current["session"] and record["round"] < current["round"]:
                    for edit in record["edits"]:
                        historical[edit["path"]] = edit["after"] or "missing"
            shared_candidates.extend({"repository": repository["name"], "path": relative,
                                      "unchanged": file_version(safe_file(repository, relative)) == version}
                                     for relative, version in historical.items() if relative in eligible)
    for delivery in state.get("deliveries", []):
        snapshots = {**(delivery.get("review_manifest") or {}), **(delivery.get("uncommitted_manifest") or {})}
        for repository in state["repos"]:
            previous = next((repo for repo in delivery["repos"] if repo["name"] == repository["name"]
                             and repo["branch"] == repository["branch"]
                             and Path(repo["path"]).resolve() == Path(repository["path"]).resolve()), None)
            if previous is None:
                continue
            protected = set() if previous.get("change_record") else {value.casefold() for value in previous.get("preserved_paths", [])}
            prefix = repository["name"] + "/"
            for key, digest in snapshots.items():
                if key.startswith(prefix) and key[len(prefix):].casefold() not in protected:
                    recorded[key] = digest
    candidates = []
    for repository in state["repos"]:
        if repository.get("change_record"):
            from workflow.shared_changes import candidates as current_fragments
            eligible = {entry["path"] for entry in current_fragments(repository)
                        if entry["source"] not in {"其他会话", "归属重叠"}}
            known = {(entry["repository"], entry["path"]) for entry in shared_candidates}
            prefix = repository["name"] + "/"
            for key, version in recorded.items():
                relative = key[len(prefix):] if key.startswith(prefix) else None
                if relative in eligible and (repository["name"], relative) not in known:
                    file = safe_file(repository, relative)
                    checksum = sha256(file.read_bytes()).hexdigest() + ":" + oct(file.stat().st_mode) if file.is_file() else "deleted"
                    shared_candidates.append({"repository": repository["name"], "path": relative, "unchanged": checksum == version})
            continue
        prefix = repository["name"] + "/"
        preserved = repository.get("preserved_paths", [])
        paths = [relative for relative in preserved if prefix + relative in recorded]
        if not paths:
            continue
        current = manifest([{**repository, "preserved_paths": [value for value in preserved if value not in paths]}])
        candidates.extend({"repository": repository["name"], "path": relative,
                           "unchanged": current[prefix + relative] == recorded[prefix + relative]}
                          for relative in paths if prefix + relative in current)
    return [*candidates, *shared_candidates]


def followup_replacements(store, state):
    """替换记录独立落盘，避免 MCP 子进程更新被控制器内存状态覆盖。"""
    followup = state["followup"]
    path = store.directory(state["id"]) / f"followup-replacements-{state['delivery_round']}.json"
    if not path.exists():
        return path, {"request_id": followup["request_id"],
                      "authorization_hash": followup["authorization_hash"], "files": {}}
    record = loads(path.read_text(encoding="utf-8"))
    if (record["request_id"] != followup["request_id"]
            or record["authorization_hash"] != followup["authorization_hash"]):
        raise WorkflowError("历史任务文件替换记录不属于本轮授权，已停止。")
    return path, record


def apply_followup_replacements(store, state):
    if state["phase"] != "followup":
        return
    path = store.directory(state["id"]) / f"followup-replacements-{state['delivery_round']}.json"
    if not path.exists():
        return
    _, record = followup_replacements(store, state)
    if not record["files"]:
        return
    candidates = {(entry["repository"], entry["path"]) for entry in previous_task_changes(state)}
    adopted = []
    for repository in state["repos"]:
        for relative, digest in record["files"].get(repository["name"], {}).items():
            if relative in repository.get("adopted_paths", []):
                continue
            if (repository["name"], relative) not in candidates:
                raise WorkflowError("替换文件不属于本会话可追溯的历史任务改动，已停止。")
            path = safe_file(repository, relative)
            if not path.is_file() or sha256(path.read_bytes()).hexdigest() != digest:
                raise WorkflowError("历史任务文件在替换后发生变化，请人工核对后继续。")
            adopted.append((repository, relative))
    for repository, relative in adopted:
        repository["preserved_paths"] = [value for value in repository["preserved_paths"] if value != relative]
        repository["adopted_paths"] = sorted({*repository.get("adopted_paths", []), relative})


def validate_review_request(state, arguments):
    if (state["phase"] != "followup" or state["stage"] != "续聊处理中"
            or state.get("approval_source") != "followup" or not write_authorized(state)):
        raise WorkflowError("只有当前已授权的续聊回合可以请求审查提交。")
    instruction = arguments.get("instruction")
    message = arguments.get("commit_message")
    paths = arguments.get("include_paths")
    messages = [entry["text"] for entry in state["history"][state["followup"]["history_index"]:]
                if entry["role"] == "用户"]
    if (not isinstance(instruction, str) or not instruction.strip()
            or not any(instruction in text for text in messages)):
        raise WorkflowError("请引用本轮用户明确要求审查或提交的原话。")
    if not isinstance(message, str) or len(message) > 200 or any(value in message for value in ("\n", "\r", "\0")):
        raise WorkflowError("提交标题必须为不超过 200 字的单行文字；未指定时传空字符串。")
    if not isinstance(paths, list):
        raise WorkflowError("请明确提供需要纳入审查的历史任务文件清单。")
    candidates = {(entry["repository"], entry["path"]) for entry in previous_task_changes(state)}
    candidates.update((repository["name"], relative) for repository in state["repos"]
                      for relative in repository.get("adopted_paths", []))
    selected = set()
    for entry in paths:
        if (not isinstance(entry, dict) or set(entry) != {"repository", "path"}
                or any(not isinstance(value, str) for value in entry.values())):
            raise WorkflowError("历史任务文件必须包含仓库名称及相对路径。")
        key = (entry["repository"], entry["path"])
        if key in selected or key not in candidates:
            raise WorkflowError("纳入文件不属于可核对的历史任务改动，不能提交本机保留改动。")
        selected.add(key)
    included_manifest = {}
    for repository in state["repos"]:
        included = {relative for name, relative in selected if name == repository["name"]}
        if included:
            if repository.get("change_record"):
                from workflow.shared_changes import requested_manifest
                current = requested_manifest(repository, included)
            else:
                current = manifest([{**repository, "preserved_paths": [value for value in repository.get("preserved_paths", []) if value not in included]}])
            included_manifest.update({key: digest for key, digest in current.items()
                                      if key in {repository["name"] + "/" + relative for relative in included}})
    return {"instruction": instruction, "commit_message": message.strip(), "include_paths": paths,
            "included_manifest": included_manifest,
            "request_id": state["followup"]["request_id"],
            "authorization_hash": state["followup"]["authorization_hash"]}
