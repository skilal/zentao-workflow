"""将模型问题持久交给控制器；只有明确答复才能解除等待。"""

from json import dumps, loads
from uuid import uuid4

from workflow.settings import WorkflowError


INPUT_STAGES = {"计划中", "实施中", "修复中", "续聊处理中", "编译修复中", "审查中"}


def input_request_path(store, state):
    return store.directory(state["id"]) / "user-input-request.json"


def pending_input(store, state):
    path = input_request_path(store, state)
    if not path.is_file():
        return None
    request = loads(path.read_text(encoding="utf-8"))
    if (request["thread_id"] != state.get("thread_id") or request["turn_id"] != state.get("turn_id")
            or request["delivery_round"] != state.get("delivery_round", 1)):
        return None
    return request


def register_input(store, state, questions):
    if state["stage"] not in INPUT_STAGES or not state.get("thread_id") or not state.get("turn_id"):
        raise WorkflowError("只有正在处理的业务回合可以登记问题。")
    if pending_input(store, state):
        raise WorkflowError("当前问题尚未回答，请结束回合并等待用户明确答复。")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise WorkflowError("每次必须登记 1 至 3 个明确问题。")
    identifiers = set()
    for question in questions:
        if (not isinstance(question, dict) or set(question) - {"id", "header", "question", "options"}
                or any(not isinstance(question.get(key), str) or not question[key].strip()
                       for key in ("id", "header", "question"))):
            raise WorkflowError("问题必须包含非空的 id、header 和 question，不允许默认答案。")
        if question["id"] in identifiers:
            raise WorkflowError("问题 id 不能重复。")
        identifiers.add(question["id"])
        if "options" in question:
            options = question["options"]
            if (not isinstance(options, list) or not 1 <= len(options) <= 6
                    or any(not isinstance(option, dict) or set(option) != {"label", "description"}
                           or not isinstance(option["label"], str) or not option["label"].strip()
                           or not isinstance(option["description"], str) for option in options)
                    or len({option["label"] for option in options}) != len(options)):
                raise WorkflowError("问题选项须包含唯一的 label 和 description，最多 6 项。")
    request = {"id": uuid4().hex, "thread_id": state["thread_id"], "turn_id": state["turn_id"],
               "delivery_round": state.get("delivery_round", 1), "questions": questions}
    path = input_request_path(store, state)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(dumps(request, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return {"status": "awaiting_user", "request_id": request["id"],
            "message": "问题已登记给控制器。立即结束本回合，不调用其他工具，不轮询或 sleep；等待用户明确答复后由控制器恢复。"}
