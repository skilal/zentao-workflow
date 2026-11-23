"""工作区实际位置与归属；兼容旧会话的目录布局。"""

from pathlib import Path

from workflow.settings import WorkflowError
from workflow.store import session_lock, session_repositories


DEFAULT_WORKSPACE_ROOT = "D:/wkproj/worktrees"


def validate_workspace_root(value):
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise WorkflowError("workspace_root 必须是有效的工作区绝对目录。")
    path = Path(value).resolve()
    if path.drive.casefold() == "c:":
        raise WorkflowError("工作区不能创建在 C 盘，请将 workspace_root 配置到 D 盘。")
    return str(path)


def workspace_directory(store, state):
    if state.get("workspace_root"):
        return Path(state["workspace_root"]) / state.get("workspace_name", state["id"])
    return store.directory(state["id"]) / "workspace"


def reserve_workspace_directory(store, state):
    """首次准备按工作项编号登记名称，不创建目录；历史目录保持原位置。"""
    with store.lock, session_lock(store.root):
        workspace = workspace_directory(store, state)
        legacy = store.directory(state["id"]) / "workspace"
        if (not state.get("workspace_root") or state.get("workspace_name") or workspace.exists()
                or legacy.exists() or session_repositories(state)):
            return workspace
        item = state["item"]
        prefix = f"{item['kind']}-{item['id']}" if item is not None else "conversation"
        reserved = {str(workspace_directory(store, session).resolve()).casefold()
                    for session in store.sessions() if session["id"] != state["id"]}
        number, name = 1, prefix
        workspace = Path(state["workspace_root"]) / name
        while workspace.exists() or str(workspace.resolve()).casefold() in reserved:
            number += 1
            name = f"{prefix}-{number}"
            workspace = Path(state["workspace_root"]) / name
        state["workspace_name"] = name
        store.save(state)
        return workspace


def working_directory(state, session_path):
    """已绑定仓库直接作为模型工作目录，准备期间使用会话资料目录。"""
    return Path(state["repos"][0]["path"]) if state["repos"] else Path(session_path)


def workspace_directories(store, state):
    return {workspace_directory(store, state).resolve(), (store.directory(state["id"]) / "workspace").resolve()}


def managed_workspace(store, path, sessions):
    path = Path(path).resolve()
    for session in sessions:
        if path.parent in workspace_directories(store, session) or any(
                repo.get("workspace_owned", True) and Path(repo["path"]).resolve() == path
                for repo in session_repositories(session)):
            return session["id"]
    return None
