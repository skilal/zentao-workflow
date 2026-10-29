"""只承接已确认阶段、已选仓库的本地 MCP 文件写入授权。"""

from hashlib import sha256
from pathlib import Path

from workflow.gitops import safe_file
from workflow.mcp_server import SKIP, TOOLS
from workflow.settings import WorkflowError


WRITE_TOOL_DESCRIPTIONS = {description: (name, definition) for name, description, definition in TOOLS
                           if name in ("write_file", "replace_text", "delete_file")}


def is_controlled_write(state, params):
    if params.get("serverName") != "workflow" or params.get("mode") != "form":
        return False
    metadata = params.get("_meta") or {}
    if metadata.get("codex_approval_kind") != "mcp_tool_call":
        return False
    tool = WRITE_TOOL_DESCRIPTIONS.get(metadata.get("tool_description"))
    if not tool or state["stage"] not in ("实施中", "修复中"):
        return False
    approved_plan = state.get("approved_plan", "")
    if not approved_plan or sha256(approved_plan.encode("utf-8")).hexdigest() != state.get("approved_hash"):
        return False
    arguments = metadata.get("tool_params")
    if not isinstance(arguments, dict) or any(key not in arguments for key in tool[1]["required"]):
        return False
    if any(not isinstance(value, str) for value in arguments.values()):
        return False
    repository = next((repository for repository in state["repos"] if repository["name"] == arguments.get("repository")), None)
    if repository is None:
        return False
    relative = arguments.get("path", "")
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
