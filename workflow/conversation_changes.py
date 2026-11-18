"""从受控写入记录读取各次对话的文件变更。"""

from collections import defaultdict
from difflib import unified_diff

from workflow.settings import WorkflowError
from workflow.shared_changes import blob_directory, load_record_file, read_blob


def conversation_edits(directory):
    groups = defaultdict(dict)
    for path in sorted((directory / "changes").glob("*.json")):
        if path.name.endswith(".commit.json"):
            continue
        record = load_record_file(path)
        if record["session"] != directory.name:
            raise WorkflowError("文件修改记录不属于当前会话。")
        repository = {"id": path.stem, "name": record["repository"], "branch": record["branch"],
                      "change_record": str(path)}
        for edit in record["edits"]:
            relative = edit["path"]
            entry = groups[edit.get("message_index")].setdefault((repository["id"], relative), {
                "repository": repository, "path": relative, "versions": []})
            versions = entry["versions"]
            if versions and versions[-1][1] == edit["before"]:
                versions[-1][1] = edit["after"]
            else:
                # 共享目录中两次写入之间的外部差异不纳入本次对话。
                versions.append([edit["before"], edit["after"]])
    for entries in groups.values():
        for key, entry in list(entries.items()):
            entry["versions"] = [pair for pair in entry["versions"] if pair[0] != pair[1]]
            if not entry["versions"]:
                del entries[key]
    return groups


def change_groups(edits):
    groups = []
    for index, entries in sorted((index, entries) for index, entries in edits.items() if index is not None and entries):
        repositories = {}
        for entry in sorted(entries.values(), key=lambda entry: entry["path"]):
            repo = entry["repository"]
            repository = repositories.setdefault(repo["id"], {
                "id": repo["id"], "name": repo["name"], "branch": repo["branch"], "files": []})
            before, after = entry["versions"][0][0], entry["versions"][-1][1]
            kind = "修改"
            if before is None and after is not None:
                kind = "新增"
            elif after is None and before is not None:
                kind = "删除"
            repository["files"].append({"path": entry["path"], "kind": kind})
        groups.append({"message_index": index, "total": len(entries), "repositories": list(repositories.values())})
    return groups


def conversation_patch(entry):
    if entry is None:
        raise WorkflowError("所选文件没有这次对话的改动记录。")
    repo, relative = entry["repository"], entry["path"]
    versions = entry["versions"]
    blobs = blob_directory(repo)
    if sum((blobs / key).stat().st_size for pair in versions for key in pair if key is not None) > 512 * 1024:
        return {"note": "文件差异过大，无法行内展示。"}
    patches = []
    for before, after in versions:
        original, updated = read_blob(repo, before) or b"", read_blob(repo, after) or b""
        try:
            if b"\0" in original or b"\0" in updated:
                return {"note": "该文件包含二进制内容，无法展示文本差异。"}
            old_lines = original.decode("utf-8").splitlines(keepends=True)
            new_lines = updated.decode("utf-8").splitlines(keepends=True)
        except UnicodeError:
            return {"note": "该文件包含非 UTF-8 内容，无法展示文本差异。"}
        old_name = "/dev/null" if before is None else "a/" + relative
        new_name = "/dev/null" if after is None else "b/" + relative
        patch = "".join(line if line.endswith("\n") else line + "\n\\ 文件末尾无换行\n"
                        for line in unified_diff(old_lines, new_lines, fromfile=old_name, tofile=new_name))
        patches.append(patch or f"--- {old_name}\n+++ {new_name}\n")
    return {"patch": "\n".join(patches)}
