"""Review 生成逐仓库标题，控制器登记规范消息及工作项关联。"""

from re import fullmatch, search

from workflow.settings import WorkflowError


COMMIT_TYPES = ("feat", "fix", "refactor", "perf", "docs", "test", "chore")
COMMIT_MESSAGE_SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "repository": {"type": "string"}, "type": {"type": "string", "enum": list(COMMIT_TYPES)},
    "scope": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["repository", "type", "scope", "summary"], "additionalProperties": False}}
COMMIT_INSTRUCTIONS = (
    "commit_messages 逐仓库对应实际纳入的差异，无改动不填；返回 repository/type/scope/summary。"
    "标题 type(实际模块): 简短中文摘要，最多 72 字符；类型按实际变化选择，续聊不等于 fix。"
    "模块、摘要不带工作项编号或前后端角色，不照搬任务标题。人工 commit_message 已规范则原样保留，否则逐字保留摘要并补全格式。"
    "关联工作项由控制器加入正文；不单独命名、不执行 Git。"
)


def prepare_commit_messages(state, entries):
    names = {repo["name"] for repo in state["repos"]
             if any(key.startswith(repo["name"] + "/") for key in state["review_manifest"])}
    provided = [entry["repository"] for entry in entries]
    if len(set(provided)) != len(provided) or set(provided) != names:
        raise WorkflowError("提交标题须与本轮有改动的仓库逐一对应，请重新 Review。")
    requested = state.get("commit_message", "")
    complete_title = fullmatch(r"(?:" + "|".join(COMMIT_TYPES) + r")\([^()\r\n]+\): .+", requested)
    labels = {"epic": "业务需求", "requirement": "用户需求", "task": "任务", "bug": "Bug", "story": "需求"}
    references = list(dict.fromkeys(f"{labels[item['kind']]} #{item['id']}"
                                   for item in state.get("items", [state["item"]]) if item is not None))
    footer = "\n\n关联工作项: " + "、".join(references) if references else ""
    messages = {}
    for entry in entries:
        scope, summary = entry["scope"], entry["summary"]
        title = f"{entry['type']}({scope}): {summary}"
        if (not scope or not summary or scope != scope.strip() or summary != summary.strip()
                or any(character in scope for character in "():：")
                or len(title.splitlines()) != 1 or any(ord(character) < 32 or ord(character) == 127 for character in title)
                or len(title) > 72 or scope.casefold() in {"前端", "后端", "frontend", "backend"}
                or search(r"(?i)(?:任务|bugs?|需求|tasks?|story|stories)\s*[-_#]?\s*\d|#\s*\d", scope + " " + summary)
                or fullmatch(r"[\d\s,，、#-]+", scope) or summary.endswith(("前端开发", "后端开发"))):
            raise WorkflowError("提交标题必须为 type(实际模块): 中文摘要，最多 72 个字符，不带工作项编号或前后端角色，请重新 Review。")
        if requested and (title != requested if complete_title else summary != requested):
            raise WorkflowError("提交标题未保留人工指定的内容，请重新 Review。")
        messages[entry["repository"]] = title + footer
    return messages
