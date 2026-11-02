"""逐题附件的归属、回答校验及只读内容视图。"""

from base64 import b64encode
from hashlib import sha256
from json import dumps
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from workflow.settings import WorkflowError
from workflow.zentao import extract_text


def answer_request_id(state):
    pending = state.get("pending")
    if not pending:
        return None
    scope = [state.get("thread_id"), state.get("turn_id"), state.get("delivery_round", 1), pending["id"]]
    return sha256(dumps(scope, ensure_ascii=False).encode("utf-8")).hexdigest()


def submitted_attachments(state):
    return [entry for entry in state.get("answer_attachments", [])
            if "history_index" in entry and entry["delivery_round"] == state.get("delivery_round", 1)]


def attachment_file(store, state, entry):
    path = Path(entry["path"]).resolve()
    directory = (store.directory(state["id"]) / "attachments" / "answers").resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise WorkflowError("问题附件路径无效或文件已不存在。")
    return path


def prepare_answer(state, text, answers, attachment_ids=None, request_id=None):
    """在回答入口一次完成逐题完整性和附件归属校验。"""
    if not isinstance(text, str):
        raise WorkflowError("回答文字必须为字符串。")
    pending = state.get("pending")
    if not pending:
        if attachment_ids or request_id is not None:
            raise WorkflowError("当前没有可以关联附件的问题。")
        if answers is not None and not isinstance(answers, dict):
            raise WorkflowError("回答参数无效。")
        if not text.strip() and not answers:
            raise WorkflowError("请明确回答，不能以空内容继续。")
        return answers, []
    if pending["method"] == "workflow/ask_user" and not pending.get("turn_completed"):
        raise WorkflowError("控制器正在结束当前回合，请稍后提交回答。")
    if request_id is not None and request_id != answer_request_id(state):
        raise WorkflowError("当前问题已经变化，请查看最新问题后再回答。")
    questions = pending["params"].get("questions", [])
    identifiers = {question["id"] for question in questions}
    if answers is None and len(questions) == 1:
        answers = {questions[0]["id"]: [text.strip()] if text.strip() else []}
        text = ""
    if not isinstance(answers, dict) or set(answers) != identifiers:
        raise WorkflowError("请逐项明确回答当前所有问题。")
    attachment_ids = {} if attachment_ids is None else attachment_ids
    if not isinstance(attachment_ids, dict) or set(attachment_ids) - identifiers:
        raise WorkflowError("附件必须按当前问题编号关联。")
    if attachment_ids and request_id != answer_request_id(state):
        raise WorkflowError("提交附件必须指定当前提问请求。")
    available = {entry["id"]: entry for entry in state.get("answer_attachments", [])
                 if "history_index" not in entry and entry["request_id"] == answer_request_id(state)}
    normalized, selected, seen = {}, [], set()
    for question in questions:
        key = question["id"]
        values, ids = answers[key], attachment_ids.get(key, [])
        if (not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values)
                or not isinstance(ids, list) or any(not isinstance(value, str) for value in ids)):
            raise WorkflowError("每题须提供有效文字、选项或附件。")
        if not values and not ids:
            raise WorkflowError("每个问题都需要明确回答，不能自动选择或留空。")
        entries = []
        for value in ids:
            entry = available.get(value)
            if entry is None or entry["question_id"] != key or value in seen:
                raise WorkflowError("附件不属于当前问题、已经提交或被重复引用。")
            seen.add(value)
            entries.append(entry)
        selected.extend(entries)
        references = ["附件：" + entry["name"] + "（编号：" + entry["id"] + "）；"
                      "使用 workflow.read_answer_attachment 按编号读取，收到附件不代表已读取内容。" for entry in entries]
        normalized[key] = [*values, *references]
    return normalized, selected


def read_answer_attachment(store, state, arguments):
    identifier = arguments.get("attachment_id")
    entry = next((entry for entry in state.get("answer_attachments", [])
                  if entry["id"] == identifier and "history_index" in entry), None)
    if entry is None:
        raise WorkflowError("附件不在当前会话已提交的回答中。")
    start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
    if type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 500:
        raise WorkflowError("start_line 必须大于等于 1，line_count 必须为 1 至 500。")
    path = attachment_file(store, state, entry)
    metadata = {key: entry[key] for key in ("id", "name", "question_id", "question", "request_id", "size", "path")}
    if entry.get("image"):
        return {"attachment": metadata, "content": [
            {"type": "text", "text": dumps(metadata, ensure_ascii=False)},
            {"type": "image", "mimeType": entry["content_type"], "data": b64encode(path.read_bytes()).decode("ascii")}]}
    try:
        text = extract_text(path)
    except (ValueError, OSError, KeyError, BadZipFile, ParseError) as error:
        return {"attachment": metadata, "readable": False,
                "issue": f"原文件已保存，内容未读取：{error}。请人工提供必要内容；不得声称已经读取。"}
    lines = text.splitlines()
    end = min(len(lines), start - 1 + count)
    return {"attachment": metadata, "readable": True, "total_lines": len(lines), "start_line": start,
            "next_line": end + 1 if end < len(lines) else None,
            "text": "\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end))}
