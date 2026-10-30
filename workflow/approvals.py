"""只承接已确认阶段、已选仓库的本地 MCP 文件写入授权。"""

from pathlib import Path

from workflow.gitops import safe_file
from workflow.mcp_server import SKIP, TOOLS
from workflow.settings import WorkflowError
from workflow.store import write_authorized
from workflow.user_input import INPUT_STAGES


CONTROL_TOOL_DESCRIPTIONS = {description: (name, definition) for name, description, definition in TOOLS
                            if name in ("write_file", "replace_text", "delete_file", "request_review", "ask_user")}


def is_controlled_write(state, params):
    if params.get("serverName") != "workflow" or params.get("mode") != "form":
        return False
    metadata = params.get("_meta") or {}
    if metadata.get("codex_approval_kind") != "mcp_tool_call":
        return False
    tool = CONTROL_TOOL_DESCRIPTIONS.get(metadata.get("tool_description"))
    if not tool:
        return False
    arguments = metadata.get("tool_params")
    if not isinstance(arguments, dict) or any(key not in arguments for key in tool[1]["required"]):
        return False
    if tool[0] == "ask_user":
        return state["stage"] in INPUT_STAGES
    if state["stage"] not in ("实施中", "修复中", "续聊处理中", "编译修复中") or not write_authorized(state):
        return False
    if tool[0] == "request_review":
        # 此工具只登记请求。范围校验由 MCP 返回具体错误，不能转成“用户拒绝”。
        return state["phase"] == "followup" and state["stage"] == "续聊处理中"
    if any(not isinstance(value, str) for value in arguments.values()):
        return False
    repository = next((repository for repository in state["repos"] if repository["name"] == arguments.get("repository")), None)
    if repository is None:
        return False
    relative = arguments.get("path", "")
    if state["stage"] == "编译修复中":
        checks = state.get("compile_checks", [])
        if (repository["role"] != "backend" or not state.get("compile_check", {}).get("enabled")
                or not checks or checks[-1]["status"] != "repairing"
                or arguments.get("repository") != checks[-1].get("repair_repository")
                or Path(relative).as_posix() not in checks[-1].get("repair_paths", [])):
            return False
    if not relative or any(part.casefold() in SKIP for part in Path(relative).parts):
        return False
    if Path(relative).name.casefold() in ("agents.md", "claude.md", "qwen.md"):
        return False
    try:
        path = safe_file(repository, relative)
    except WorkflowError:
        return False
    if path.relative_to(Path(repository["path"]).resolve()).as_posix().casefold() in {value.casefold() for value in repository.get("preserved_paths", [])}:
        return False
    return True
