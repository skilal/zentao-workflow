"""允许已登记的工作流 MCP 调用；业务授权统一由工具入口校验。"""

from workflow.mcp_server import TOOLS


WORKFLOW_TOOL_DESCRIPTIONS = {description for _, description, _ in TOOLS}


def is_workflow_tool_call(params):
    if params.get("serverName") != "workflow" or params.get("mode") != "form":
        return False
    metadata = params.get("_meta") or {}
    return (metadata.get("codex_approval_kind") == "mcp_tool_call"
            and metadata.get("tool_description") in WORKFLOW_TOOL_DESCRIPTIONS)
