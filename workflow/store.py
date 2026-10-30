"""每个工作项的持久状态，原子写入且不保存登录 Token。"""

from datetime import datetime, timezone
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from os import walk
from shutil import rmtree
from tempfile import TemporaryDirectory
from threading import RLock
from uuid import uuid4

from workflow.settings import LOCAL, WorkflowError


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_title(state):
    if state.get("custom_title"):
        return state["custom_title"]
    if state["item"] is None:
        return state.get("conversation_title", "新会话")
    names = {"story": "需求", "task": "任务", "bug": "Bug"}
    return "；".join(f"{names[item['kind']]} #{item['id']} · {item['title']}"
                    for item in state.get("items", [state["item"]]))


def write_authorized(state):
    if state.get("approval_source") == "followup":
        feedback = state["followup"]
        scope = dumps([feedback["text"], [image["id"] for image in feedback["images"]]], ensure_ascii=False)
        return sha256(scope.encode("utf-8")).hexdigest() == feedback.get("authorization_hash")
    plan = state.get("approved_plan", "")
    return bool(plan and sha256(plan.encode("utf-8")).hexdigest() == state.get("approved_hash"))


def followup_available(state):
    phase, stage = state.get("phase"), state.get("stage")
    completed = phase == "complete" and stage in {"已完成（静态审查）", "已完成（未审查）"}
    paused = (stage == "已暂停" and state.get("prepare_complete") and not state.get("commits")
              and phase in {"plan", "implement", "review", "fix", "followup", "compile", "compile_fix"})
    return bool((completed or paused) and state.get("thread_id") and state.get("repos") and not state.get("pending"))


def session_repositories(state):
    return [*state["repos"], *(repo for delivery in state.get("deliveries", []) for repo in delivery["repos"])]


def initial_state(item, record_id):
    state = {
        "id": record_id, "item": item, "items": [item] if item is not None else [], "custom_title": "", "stage": "资料已读取", "phase": "plan",
        "updated_at": timestamp(), "repos": [],
        "thread_id": None, "turn_id": None, "plan": "", "plan_hash": "",
        "approved_plan": "", "approved_hash": "", "supplement": "", "supplement_images": [],
        "plan_round": None, "approval_source": "",
        "pending": None, "history": [], "reviews": [], "decisions": [], "compile_checks": [],
        "fix_round": 0, "commits": {}, "error": "", "rpc_connected": False,
        "delivery_round": 1, "deliveries": [], "followup_images": [],
    }
    if item is None:
        state["project"] = {"id": 0, "name": "", "candidates": []}
        state["snapshot"] = {"items": [], "sources": [], "materials": [], "blockers": [], "feishu_links": [],
                             "project": dict(state["project"])}
    return state


class SessionStore:
    def __init__(self):
        self.root = LOCAL / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()

    def new(self, item, *, save=True):
        prefix = f"{item['kind']}-{item['id']}" if item is not None else "conversation"
        session_id = f"{prefix}-{uuid4().hex[:10]}"
        state = initial_state(item, session_id)
        state["created_at"] = timestamp()
        if save:
            self.save(state)
        return state

    def directory(self, session_id):
        if not isinstance(session_id, str) or not session_id or Path(session_id).name != session_id or session_id in (".", ".."):
            raise WorkflowError("会话标识不合法。")
        candidate = self.root / session_id
        if candidate.exists() and (candidate.is_symlink() or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400):
            raise WorkflowError("会话目录不能是链接。")
        path = candidate.resolve()
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
        with self.lock:
            paths = sorted(self.root.glob("*/state.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            return [self.load(path.parent.name) for path in paths]

    def remove(self, session_id):
        with self.lock:
            directory = self.directory(session_id)
            for current, directories, files in walk(directory, followlinks=False):
                for name in directories + files:
                    path = Path(current) / name
                    if name == ".git" or path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                        raise WorkflowError(f"会话目录仍包含 Git 工作区或链接，已停止删除：{path}")
            rmtree(directory)

    def append(self, state, role, text, **metadata):
        with self.lock:
            state["history"].append({"time": timestamp(), "role": role, "text": text, "phase": state["phase"],
                                     "delivery_round": state.get("delivery_round", 1), **metadata})
            self.save(state)


class PreviewStore(SessionStore):
    """查看资料只写临时附件，不生成任何会话状态文件。"""

    def __init__(self):
        self.temporary = TemporaryDirectory(prefix="zentao-preview-")
        self.root = Path(self.temporary.name)
        self.lock = RLock()

    def new(self, item):
        return initial_state(item, f"preview-{uuid4().hex}")

    def save(self, state):
        state["updated_at"] = timestamp()

    def close(self):
        self.temporary.cleanup()
