"""按编号隔离会话执行器；浏览器的查看选择不影响后台执行。"""

from threading import RLock

from workflow.engine import ACTIVE, Workflow
from workflow.settings import WorkflowError
from workflow.store import SessionStore
from workflow.zentao import ZentaoClient


class SessionRuntime:
    def __init__(self, engine):
        self.engine = engine
        self.revision = 0
        self.stream = ""
        self.busy = None
        self.deleting = False
        self.event_cursor = 0


class SessionManager:
    def __init__(self, settings, emit):
        self.settings = settings
        self.emit = emit
        self.store = SessionStore()
        self.settings_lock = RLock()
        self.zentao_lock = RLock()
        self.zentao = ZentaoClient(settings)
        self.runtimes = {}
        self.connection = Workflow(settings, lambda kind, value: emit(None, kind, value),
                                   self.store, self.zentao, self.zentao_lock, self.settings_lock)
        for state in self.store.sessions():
            interrupted = state["stage"] in ACTIVE | {"等待人工回答", "资料读取中"}
            changed = interrupted or state.get("rpc_connected") or state.get("pending")
            state.update(rpc_connected=False, pending=None, turn_id=None)
            if interrupted:
                state.update(stage="资料读取失败" if state["stage"] == "资料读取中" else "已暂停", pause_requested=True)
            if changed:
                self.store.save(state)
            self.add(state)

    def add(self, state):
        session_id = state["id"]
        engine = Workflow(self.settings, lambda kind, value: self.emit(session_id, kind, value),
                          self.store, self.zentao, self.zentao_lock, self.settings_lock)
        engine.state = state
        runtime = SessionRuntime(engine)
        self.runtimes[session_id] = runtime
        return runtime

    def get(self, session_id):
        if not isinstance(session_id, str) or session_id not in self.runtimes:
            raise WorkflowError("会话不存在或已删除，请重新选择。")
        runtime = self.runtimes[session_id]
        if runtime.deleting:
            raise WorkflowError("该会话正在删除，请等待操作结束。")
        return runtime

    def close(self):
        errors = []
        engines = [self.connection, *(runtime.engine for runtime in list(self.runtimes.values()))]
        for engine in engines:
            try:
                engine.close()
            except Exception as error:
                label = engine.state["id"] if engine.state else "禅道连接"
                errors.append(f"{label}：{error}")
        if errors:
            raise WorkflowError("部分会话关闭未完成：" + "；".join(errors))
