"""仅整理 Codex 已返回的可读过程；不参与模型输入或业务授权。"""

from copy import deepcopy


TOOL_LABELS = {
    "task_context": "读取需求资料", "list_files": "查看文件列表", "read_file": "读取文件",
    "read_files": "批量读取文件", "baseline_file": "读取基线文件", "search_text": "搜索代码",
    "changes": "查看代码差异", "write_file": "写入文件", "replace_text": "修改文件",
    "delete_file": "删除文件", "ask_user": "等待你的回答", "request_review": "登记审查请求",
}
ITEM_LABELS = {"reasoning": "模型正在思考中", "agentMessage": "生成回复", "plan": "生成计划",
               "contextCompaction": "压缩上下文", "fileChange": "修改文件", "commandExecution": "执行命令"}
DELTAS = {"item/reasoning/summaryTextDelta": "reasoning", "item/reasoning/summaryPartAdded": "reasoning",
          "item/agentMessage/delta": "agentMessage", "item/plan/delta": "plan"}


def settle_progress(state, status):
    """恢复磁盘会话时结束上次尚未收尾的活动，不生成历史过程。"""
    changed = False
    turns = state.get("progress_turns", [])
    for index, turn in enumerate(turns):
        if turn["status"] == "running" or (index == len(turns) - 1 and turn["status"] == "waiting"):
            changed = changed or turn["status"] != status
            turn["status"] = status
            for item in turn["items"]:
                if item["status"] in ("running", "waiting"):
                    item_status = "waiting" if status == "waiting" else "interrupted"
                    changed = changed or item["status"] != item_status
                    item["status"] = item_status
    return changed


class ProgressFlow:
    def start_progress(self, turn_id):
        with self.lock:
            turns = self.state.setdefault("progress_turns", [])
            if not turn_id or any(turn["id"] == turn_id for turn in turns):
                return
            turn = {"id": turn_id, "thread_id": self.state.get("thread_id"), "phase": self.state["phase"], "history_index": len(self.state["history"]),
                    "status": "running", "items": []}
            turns.append(turn)
            self.emit("progress", {"op": "turn", "turn": deepcopy(turn)})

    def set_progress_status(self, status):
        with self.lock:
            turns = self.state.get("progress_turns", [])
            if not turns or turns[-1]["id"] != self.state.get("turn_id"):
                return
            turn = turns[-1]
            transition = ((turn["status"] == "waiting" and status in ("running", "interrupted", "disconnected", "failed"))
                          or (turn["status"] == "completed" and status == "waiting"))
            if turn["status"] != "running" and not transition:
                return
            turn["status"] = status
            for item in turn["items"]:
                if item["status"] in ("running", "waiting"):
                    item["status"] = status if status in ("running", "waiting", "completed") else "interrupted"
            self.emit("progress", {"op": "status", "turn_id": turn["id"], "status": status})
            self.store.save(self.state)

    def progress_event(self, method, params):
        if method not in DELTAS and method not in ("item/started", "item/completed"):
            return
        with self.lock:
            turn_id = params.get("turnId") or self.state.get("turn_id")
            if not turn_id:
                return
            self.start_progress(turn_id)
            turn = self.state["progress_turns"][-1]
            if turn["id"] != turn_id or turn["status"] != "running":
                return
            source = params.get("item", {})
            kind = DELTAS.get(method) or source.get("type")
            if kind not in ITEM_LABELS and kind != "mcpToolCall":
                return
            item_id = source.get("id") or params.get("itemId")
            if not item_id:
                return
            item = next((entry for entry in turn["items"] if entry["id"] == item_id), None)
            created = item is None
            if item is None:
                item = {"id": item_id, "type": kind, "label": ITEM_LABELS.get(kind, "调用工具"),
                        "status": "running", "text": "", "summary": [], "paths": []}
                turn["items"].append(item)
                if method in DELTAS:
                    self.emit("progress", {"op": "item", "turn_id": turn_id, "item": deepcopy(item)})
            if method in DELTAS:
                if item["status"] != "running":
                    return
                delta = params.get("delta", "")
                index = params.get("summaryIndex", 0)
                if kind == "reasoning":
                    while len(item["summary"]) <= index:
                        item["summary"].append("")
                    item["summary"][index] += delta
                else:
                    item["text"] += delta
                self.emit("progress", {"op": "delta", "turn_id": turn_id, "item_id": item_id,
                                       "summary_index": index if kind == "reasoning" else None, "delta": delta})
                return
            if method == "item/started" and item["status"] != "running":
                return
            previous = deepcopy(item)
            if kind == "mcpToolCall":
                item["label"] = TOOL_LABELS.get(source.get("tool"), "调用工具")
                arguments = source.get("arguments")
                if isinstance(arguments, dict):
                    files = arguments.get("files", []) if source.get("tool") == "read_files" else [arguments]
                    item["paths"] = list(dict.fromkeys(entry["path"] for entry in files
                                                      if isinstance(entry, dict) and isinstance(entry.get("path"), str)))
            elif kind == "fileChange":
                item["paths"] = [entry["path"] for entry in source.get("changes", [])]
            elif kind == "reasoning" and "summary" in source:
                item["summary"] = list(source["summary"])
            elif kind in ("agentMessage", "plan"):
                item["text"] = source.get("text", item["text"])
                if "phase" in source:
                    item["phase"] = source["phase"]
            if method == "item/completed":
                item["status"] = "failed" if source.get("error") or source.get("status") in ("failed", "declined") else "completed"
            if not created and item == previous:
                return
            self.emit("progress", {"op": "item", "turn_id": turn_id, "item": deepcopy(item)})
            if method == "item/completed":
                self.store.save(self.state)
