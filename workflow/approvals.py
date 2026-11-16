"""工作流 MCP 在入口校验业务授权，其他 MCP 调用通过通用问题卡片明确确认。"""

from json import dumps

from workflow.compile_checks import redact
from workflow.mcp_server import TOOLS


WORKFLOW_TOOL_DESCRIPTIONS = {description for _, description, _ in TOOLS}


def is_workflow_tool_call(params):
    if params.get("serverName") != "workflow" or params.get("mode") != "form":
        return False
    metadata = params.get("_meta") or {}
    return (metadata.get("codex_approval_kind") == "mcp_tool_call"
            and metadata.get("tool_description") in WORKFLOW_TOOL_DESCRIPTIONS)


def mcp_approval_question(params):
    metadata = params.get("_meta") or {}
    if (params.get("mode") not in ("form", "openai/form")
            or metadata.get("codex_approval_kind") != "mcp_tool_call"
            or params.get("serverName") == "workflow"):
        return None
    details = "\n".join(str(value) for value in (
        params.get("message", ""), metadata.get("tool_description", ""),
        dumps(metadata.get("tool_params", {}), ensure_ascii=False)) if value)
    return {"id": "tool_approval", "header": "工具授权",
            "question": redact(f"是否允许服务 {params.get('serverName', '')} 的这次工具调用？\n{details}")[:6000],
            "options": [{"label": "允许本次", "description": "只授权当前这一次调用。"},
                        {"label": "拒绝本次", "description": "取消当前调用，不执行工具。"}]}
