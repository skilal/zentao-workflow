"""续聊中显式审查提交请求的范围校验，不执行 Git 提交。"""

from pathlib import Path

from workflow.gitops import manifest
from workflow.settings import WorkflowError
from workflow.store import write_authorized


def previous_task_changes(state):
    recorded = {}
    for delivery in state.get("deliveries", []):
        snapshots = {**(delivery.get("review_manifest") or {}), **(delivery.get("uncommitted_manifest") or {})}
        for repository in state["repos"]:
            previous = next((repo for repo in delivery["repos"] if repo["name"] == repository["name"]
                             and repo["branch"] == repository["branch"]
                             and Path(repo["path"]).resolve() == Path(repository["path"]).resolve()), None)
            if previous is None:
                continue
            protected = {value.casefold() for value in previous.get("preserved_paths", [])}
            prefix = repository["name"] + "/"
            for key, digest in snapshots.items():
                if key.startswith(prefix) and key[len(prefix):].casefold() not in protected:
                    recorded[key] = digest
    candidates = []
    for repository in state["repos"]:
        prefix = repository["name"] + "/"
        preserved = repository.get("preserved_paths", [])
        paths = [relative for relative in preserved if prefix + relative in recorded]
        if not paths:
            continue
        current = manifest([{**repository, "preserved_paths": [value for value in preserved if value not in paths]}])
        candidates.extend({"repository": repository["name"], "path": relative,
                           "unchanged": current[prefix + relative] == recorded[prefix + relative]}
                          for relative in paths if prefix + relative in current)
    return candidates


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
    candidates = {(entry["repository"], entry["path"]): entry["unchanged"] for entry in previous_task_changes(state)}
    selected = set()
    for entry in paths:
        if (not isinstance(entry, dict) or set(entry) != {"repository", "path"}
                or any(not isinstance(value, str) for value in entry.values())):
            raise WorkflowError("历史任务文件必须包含仓库名称及相对路径。")
        key = (entry["repository"], entry["path"])
        if key in selected or key not in candidates:
            raise WorkflowError("纳入文件不属于可核对的历史任务改动，不能提交本机保留改动。")
        if not candidates[key] and not any(instruction in text for text in messages[1:]):
            raise WorkflowError("历史任务文件内容已改变，请先向用户确认是否纳入当前内容，再引用明确答复登记请求。")
        selected.add(key)
    included_manifest = {}
    for repository in state["repos"]:
        included = {relative for name, relative in selected if name == repository["name"]}
        if included:
            current = manifest([{**repository, "preserved_paths": [value for value in repository.get("preserved_paths", []) if value not in included]}])
            included_manifest.update({key: digest for key, digest in current.items()
                                      if key in {repository["name"] + "/" + relative for relative in included}})
    return {"instruction": instruction, "commit_message": message.strip(), "include_paths": paths,
            "included_manifest": included_manifest,
            "request_id": state["followup"]["request_id"],
            "authorization_hash": state["followup"]["authorization_hash"]}
