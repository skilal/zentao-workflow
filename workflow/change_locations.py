"""交付的整体页面改动说明，只保存一份汇总。"""

from json import dumps, loads

from workflow.settings import WorkflowError


SUMMARY_PHASES = {"review", "followup"}
SUMMARY_POLICY = (
    "页面改动说明现行规则（替代本线程历史中的旧要求）："
    "已废止实际修改后逐次登记操作路径、关联文件和强制报告校验，"
    "workflow.report_change_locations 已移除，不调用、不要求恢复，也不将其作为实施或修复的前置条件。"
    "实施、Review 修复及编译修复阶段无需登记；全部修改完成后由 Review 汇总一份页面位置及简短描述，"
    "关闭自动 Review 的续聊在实际修改结束时汇总，纯问答不更新。"
    "仅在汇总阶段使用 workflow.set_change_summary；缺少整体说明不阻塞修复、审查或提交。"
    "此规则更新不改变已有计划、选中修复项及人工授权。"
)
SUMMARY_INSTRUCTIONS = (
    "结合本次需求全部已完成改动，按业务页面合并为整体说明，"
    "调用 workflow.set_change_summary(pages)，每项仅含 page（页面或菜单位置）和 description（一句话改动说明）。"
    "同一页面合并说明，不按文件、实施或修复回合列记录，不附源码位置或操作步骤。"
    "Review 时汇总最终代码，包括此前实施和修复的结果；"
    "续聊仅在关闭自动 Review 且实际修改代码时汇总，开启时交由 Review 汇总；纯问答不调用。"
    "从现有资料与代码核实页面名称；没有直接页面入口或无法确认时简要说明，不猜测、不声称运行验收完成。"
)


def set_change_summary(store, state, pages):
    if (state["phase"] not in SUMMARY_PHASES or state["stage"] not in {"审查中", "续聊处理中"}
            or not state.get("turn_id") or state.get("pause_requested")):
        raise WorkflowError("请在最终审查或续聊修改结束时汇总页面改动。")
    if (not isinstance(pages, list) or not pages
            or any(not isinstance(page, dict) or set(page) != {"page", "description"}
                   or any(not isinstance(value, str) or not value.strip() for value in page.values()) for page in pages)):
        raise WorkflowError("整体改动说明每项只需提供非空的页面位置和简短描述。")
    path = store.directory(state["id"]) / "change-summary.json"
    record = {"session_id": state["id"], "delivery_round": state.get("delivery_round", 1),
              "turn_id": state["turn_id"], "pages": pages}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return "整体页面改动说明已更新。"


def collect_change_summary(store, state):
    path = store.directory(state["id"]) / "change-summary.json"
    if not path.exists():
        return
    record = loads(path.read_text(encoding="utf-8"))
    if (record["session_id"], record["delivery_round"], record["turn_id"]) == (
            state["id"], state.get("delivery_round", 1), state["turn_id"]):
        state["change_summary"] = record["pages"]
