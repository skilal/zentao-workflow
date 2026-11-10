"""修改回合的位置报告；MCP 独立落盘，控制器只合并完整报告。"""

from hashlib import sha256
from json import dumps, loads

from workflow.gitops import safe_file
from workflow.settings import WorkflowError
from workflow.store import timestamp, write_authorized


EDIT_PHASES = {"implement", "followup", "fix", "compile_fix"}
LOCATION_INSTRUCTIONS = (
    "本回合实际修改文件后，结束前必须调用 workflow.report_change_locations 登记修改位置。"
    "从菜单、路由、页面、按钮及接口调用代码核实操作路径；后端改动也追踪前端入口。"
    "同一位置可关联多个文件，多入口分别列出；提供改动、查看步骤及源码依据。"
    "确无直接页面入口用 no_ui 并说明影响；资料不足用 unresolved 并说明缺失，不猜测名称。"
    "登记后再次修改须更新报告；纯问答无需登记。不声称运行验收通过。"
    "task_context.change_locations 提供本回合及中断回合待说明的文件。"
)


def journal_path(store, state):
    key = sha256(str(state["turn_id"]).encode()).hexdigest()
    return store.directory(state["id"]) / "change-locations" / f"{key}.json"


def save_journal(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_journal(store, state):
    path = journal_path(store, state)
    if path.exists():
        record = loads(path.read_text(encoding="utf-8"))
        if (record["session_id"], record["delivery_round"], record["turn_id"]) != (
                state["id"], state.get("delivery_round", 1), state["turn_id"]):
            raise WorkflowError("修改位置记录不属于当前回合，已停止。")
        return record
    return {"session_id": state["id"], "delivery_round": state.get("delivery_round", 1),
            "turn_id": state["turn_id"], "phase": state["phase"], "files": [],
            "revision": 0, "report_revision": None, "items": [], "carried_from": [], "completed": False}


def begin_location_turn(store, state):
    """人工继续时承接中断或缺少报告的文件，不把中断报告当成完成结果。"""
    record = load_journal(store, state)
    directory = journal_path(store, state).parent
    saved = [loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]
    carried = {turn for entry in saved for turn in entry["carried_from"]}
    pending = [entry for entry in saved if entry["session_id"] == state["id"]
               and entry["delivery_round"] == state.get("delivery_round", 1)
               and not entry["completed"] and entry["turn_id"] not in carried
               and entry["turn_id"] != state["turn_id"]]
    for entry in pending:
        record["carried_from"].append(entry["turn_id"])
        for file in entry["files"]:
            existing = next((value for value in record["files"] if file_key(value) == file_key(file)), None)
            if existing is None:
                record["files"].append(file)
            else:
                existing["after"] = file["after"]
    save_journal(journal_path(store, state), record)


def file_key(file):
    return file["repository"], file["path"]


def content_version(content):
    return sha256(content).hexdigest() if content is not None else "missing"


def record_location_write(store, state, repository, relative, before, after):
    if before == after:
        return
    record = load_journal(store, state)
    key = (repository["name"], relative)
    file = next((file for file in record["files"] if file_key(file) == key), None)
    if file is None:
        record["files"].append({"repository": key[0], "path": key[1],
                                "before": content_version(before), "after": content_version(after)})
    else:
        file["after"] = content_version(after)
    record["revision"] += 1
    save_journal(journal_path(store, state), record)


def changed_files(record):
    return [file for file in record["files"] if file["before"] != file["after"]]


def location_context(store, state):
    if state["phase"] not in EDIT_PHASES or not state.get("turn_id"):
        return None
    record = load_journal(store, state)
    return {"files": [{"repository": file["repository"], "path": file["path"]} for file in changed_files(record)],
            "report_current": record["report_revision"] == record["revision"]}


def register_locations(store, state, items):
    if (state["phase"] not in EDIT_PHASES or state["stage"] not in {"实施中", "续聊处理中", "修复中", "编译修复中"}
            or not state.get("turn_id") or state.get("pause_requested") or not write_authorized(state)):
        raise WorkflowError("只有已授权的修改回合可以登记修改位置。")
    record = load_journal(store, state)
    expected = {file_key(file) for file in changed_files(record)}
    if not expected:
        raise WorkflowError("本回合没有实际文件变化，无需登记修改位置。")
    if not isinstance(items, list) or not items:
        raise WorkflowError("修改位置报告必须包含至少一项。")
    covered = set()
    repositories = {repo["name"]: repo for repo in state["repos"]}
    for item in items:
        required = {"status", "location_path", "change", "steps", "files", "evidence", "reason"}
        if not isinstance(item, dict) or not required <= item.keys() or item.keys() - required - {"route"}:
            raise WorkflowError("修改位置字段不完整或存在未知字段。")
        if not isinstance(item["status"], str) or item["status"] not in {"located", "no_ui", "unresolved"}:
            raise WorkflowError("修改位置状态无效。")
        for field in ("change", "reason", "route"):
            value = item.get(field, "")
            if not isinstance(value, str) or (field == "change" and not value.strip()):
                raise WorkflowError("改动说明不能为空，原因及路由须为文字。")
        for field in ("location_path", "steps"):
            if not isinstance(item[field], list) or any(not isinstance(text, str) or not text.strip() for text in item[field]):
                raise WorkflowError("操作路径和查看步骤必须是非空文字列表。")
        if item["status"] == "located" and (not item["location_path"] or not item["steps"]):
            raise WorkflowError("已定位的修改须提供操作路径和查看步骤。")
        if item["status"] != "located" and not item["reason"].strip():
            raise WorkflowError("无直接前端入口或尚未定位时必须说明原因。")
        files = item["files"]
        if (not isinstance(files, list) or not files
                or any(not isinstance(file, dict) or set(file) != {"repository", "path"}
                       or any(not isinstance(value, str) for value in file.values()) for file in files)):
            raise WorkflowError("关联文件必须包含仓库名称和相对路径。")
        keys = {file_key(file) for file in files}
        if not keys <= expected:
            raise WorkflowError("位置报告只能关联本回合实际变化的文件。")
        covered.update(keys)
        evidence = item["evidence"]
        if not isinstance(evidence, list) or (item["status"] != "unresolved" and not evidence):
            raise WorkflowError("已确认的位置或无入口结论须提供源码依据。")
        for source in evidence:
            if (not isinstance(source, dict) or set(source) != {"repository", "path", "line", "description"}
                    or not all(isinstance(source[field], str) and source[field].strip()
                               for field in ("repository", "path", "description"))
                    or type(source["line"]) is not int or source["line"] < 1
                    or source["repository"] not in repositories):
                raise WorkflowError("源码依据须包含有效仓库、路径、行号及说明。")
            safe_file(repositories[source["repository"]], source["path"])
    if covered != expected:
        raise WorkflowError("修改位置报告未覆盖全部实际变化文件，请补全后重新登记。")
    record.update(items=items, report_revision=record["revision"])
    save_journal(journal_path(store, state), record)
    return {"status": "registered", "locations": len(items), "files": len(expected)}


def complete_locations(store, state):
    record = load_journal(store, state)
    files = changed_files(record)
    if files:
        if record["report_revision"] != record["revision"]:
            raise WorkflowError("本回合修改位置报告缺失或已过期，请点击继续，补全位置报告后再推进。")
        reports = state.setdefault("change_locations", [])
        if not any(report["turn_id"] == record["turn_id"] for report in reports):
            reports.append({key: record[key] for key in ("delivery_round", "turn_id", "phase", "items")})
            reports[-1]["completed_at"] = timestamp()
    record["completed"] = True
    save_journal(journal_path(store, state), record)
