"""由计划回合登记自动标题，控制器只合并会话名称元数据。"""

from json import dumps, loads

from workflow.settings import WorkflowError
from workflow.store import session_lock


def title_generation_context(state):
    if (not state.get("title_pending") or state.get("custom_title") or state.get("auto_title")
            or state["item"] is None or state.get("followup") or state.get("delivery_round", 1) != 1):
        return None
    items = state.get("items", [state["item"]])
    suffix = f"等 {len(items)} 项" if len(items) > 1 else ""
    return {"max_length": 20 - len(suffix), "suffix": suffix, "primary_item": items[0]}


def title_tool_authorized(state):
    return bool("title_pending" in state and state["item"] is not None
                and state["phase"] == "plan" and state["stage"] == "计划中"
                and state.get("thread_id") and state.get("turn_id")
                and state.get("delivery_round", 1) == 1 and not state.get("followup")
                and not state.get("pending") and not state.get("pause_requested") and not state.get("cleanup"))


def register_session_title(store, state, title):
    if not title_tool_authorized(state):
        raise WorkflowError("只有新建工作项会话的计划分析回合可以登记自动标题。")
    context = title_generation_context(state)
    if context is None:
        return {"status": "unchanged", "title": state.get("custom_title") or state.get("auto_title", "")}
    if (not isinstance(title, str) or not title.strip() or len(title.strip()) > context["max_length"]
            or len(title.splitlines()) != 1 or any(ord(character) < 32 for character in title)):
        raise WorkflowError(f"自动标题必须是非空单行文字，最多 {context['max_length']} 个字符；多任务后缀由控制器添加。")
    path = store.directory(state["id"]) / "session-title-request.json"
    with store.lock, session_lock(store.root):
        if path.is_file():
            request = loads(path.read_text(encoding="utf-8"))
        else:
            request = {"session_id": state["id"], "title": title.strip() + context["suffix"]}
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(dumps(request, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
    return {"status": "registered", "title": request["title"]}


def collect_session_title(store, state):
    if title_generation_context(state) is None:
        return False
    path = store.directory(state["id"]) / "session-title-request.json"
    if not path.is_file():
        return False
    request = loads(path.read_text(encoding="utf-8"))
    if request["session_id"] != state["id"]:
        raise WorkflowError("自动标题结果不属于当前会话。")
    state.update(auto_title=request["title"], title_pending=False)
    return True
