"""每个工作项的持久状态，原子写入且不保存登录 Token。"""

from datetime import datetime, timezone
from json import dumps, loads
from pathlib import Path
from threading import RLock
from uuid import uuid4

from workflow.settings import LOCAL, WorkflowError


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    def __init__(self):
        self.root = LOCAL / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def new(self, item):
        session_id = f"{item['kind']}-{item['id']}-{uuid4().hex[:10]}"
        state = {
            "id": session_id, "item": item, "items": [item], "stage": "资料已读取", "phase": "plan",
            "created_at": timestamp(), "updated_at": timestamp(), "repos": [],
            "thread_id": None, "turn_id": None, "plan": "", "plan_hash": "",
            "approved_plan": "", "approved_hash": "", "supplement": "",
            "plan_round": None, "approval_source": "",
            "pending": None, "history": [], "reviews": [], "decisions": [],
            "fix_round": 0, "commits": {}, "error": "", "rpc_connected": False,
        }
        self.save(state)
        return state

    def directory(self, session_id):
        path = (self.root / session_id).resolve()
        if path.parent != self.root.resolve():
            raise WorkflowError("会话路径不合法。")
        return path

    def save(self, state):
        with self.lock:
            state["updated_at"] = timestamp()
            directory = self.directory(state["id"])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "state.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def load(self, session_id):
        with self.lock:
            return loads((self.directory(session_id) / "state.json").read_text(encoding="utf-8"))

    def sessions(self):
        paths = sorted(self.root.glob("*/state.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        return [loads(path.read_text(encoding="utf-8")) for path in paths]

    def append(self, state, role, text):
        with self.lock:
            state["history"].append({"time": timestamp(), "role": role, "text": text})
            self.save(state)
