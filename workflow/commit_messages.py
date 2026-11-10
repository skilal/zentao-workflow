"""Review 生成逐仓库标题，控制器登记规范消息及工作项关联。"""

from re import fullmatch, search

from workflow.settings import WorkflowError


COMMIT_TYPES = ("feat", "fix", "refactor", "perf", "docs", "test", "chore")
COMMIT_MESSAGE_SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "repository": {"type": "string"}, "type": {"type": "string", "enum": list(COMMIT_TYPES)},
    "scope": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["repository", "type", "scope", "summary"], "additionalProperties": False}}
COMMIT_INSTRUCTIONS = (
    "同时在 commit_messages 中为本轮有代码改动的每个仓库分别生成提交标题字段 repository、type、scope、summary；"
    "无改动仓库不填写。根据本轮授权及该仓库实际累计差异概括，不拼接工作项原标题。"
    "type 只能为 feat、fix、refactor、perf、docs、test、chore，按实际变化判断，续聊不等于 fix。"
    "scope 使用实际业务或技术模块，可用中文或已有英文模块名，不能使用工作项编号、zentao-task 等编号前缀或前后端角色。"
    "summary 用简短中文描述改动结果，不带工作项编号、前端开发或后端开发后缀。"
    "完整 type(scope): summary 标题建议不超过 50 个字符，最多 72 个字符，不截断。"
    "上下文 commit_message 为人工指定内容：若已符合完整规范则保持原样；否则逐字保留为 summary，并补全 type 和 scope。"
    "工作项关联行由控制器添加，不写入标题。仅返回结构化结果，不执行 Git，不单独增加命名回合。"
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
