"""工作区实际位置与归属；兼容旧会话的目录布局。"""

from pathlib import Path

from workflow.settings import WorkflowError
from workflow.store import session_repositories


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
        return Path(state["workspace_root"]) / state["id"]
    return store.directory(state["id"]) / "workspace"


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
