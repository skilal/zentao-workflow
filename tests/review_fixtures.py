"""现有流程检查中模拟模型返回的逐仓库提交标题。"""

from json import dumps, loads, JSONDecodeError
from pathlib import Path

from workflow.mcp_server import Tools
from workflow.shared_changes import candidates, file_version, snapshot_review, select_scope


class VersionedTools(Tools):
    """模拟模型读取文件后使用其版本写入，缺版本拒绝由共享专项检查覆盖。"""

    def call(self, name, arguments):
        if name in {"write_file", "replace_text", "delete_file"} and "expected_version" not in arguments:
            state = self.store.load(self.session_id)
            repo = next(repo for repo in state["repos"] if repo["name"] == arguments["repository"])
            path = Path(repo["path"]) / arguments["path"]
            arguments = {**arguments, "expected_version": file_version(path)}
        return super().call(name, arguments)


def selected_fragments(repo):
    protected = {path.casefold() for path in repo.get("preserved_paths", [])}
    return [entry for entry in candidates(repo) if entry["source"] == "本轮"
            or (entry["source"] in {"待判断", "历史任务"}
                and (entry["path"].casefold() not in protected or entry["path"] in repo.get("review_paths", [])))]


def scoped_result(state, text):
    if state["phase"] != "review" or not any(repo.get("change_record") for repo in state["repos"]):
        return text
    try:
        result = loads(text)
    except JSONDecodeError:
        return text
    if not isinstance(result, dict) or "commit_messages" not in result or "commit_scope" in result:
        return text
    result["commit_scope"] = [{"repository": repo["name"], "include": [entry["id"] for entry in selected_fragments(repo)], "exclude": []}
                              for repo in state["repos"] if repo.get("change_record")]
    return dumps(result, ensure_ascii=False)


def approve_fragments(repo):
    repo.pop("commit_scope", None)
    snapshot_review(repo)
    selected = selected_fragments(repo)
    return select_scope([repo], [{"repository": repo["name"], "include": [entry["id"] for entry in selected], "exclude": []}])


def commit_messages(state):
    return [{"repository": repo["name"], "type": "fix", "scope": "业务",
             "summary": state.get("commit_message") or "完善业务处理"}
            for repo in state["repos"] if (bool(selected_fragments(repo)) if repo.get("change_record") else
                any(key.startswith(repo["name"] + "/") for key in state["review_manifest"]))]
