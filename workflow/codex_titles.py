"""新建工作流会话通过独立队列同步 Codex 线程名称。"""

from copy import deepcopy

from workflow.settings import WorkflowError
from workflow.store import session_title


class CodexTitleFlow:
    def queue_codex_title_sync(self):
        with self.store.lock:
            sync = self.state.get("codex_title_sync") if self.state else None
            if (sync is None or sync["error"] or self.closed or self.title_sync_pending
                    or self.rpc is None or not self.state.get("rpc_connected")):
                return
            name = "工作流会话 · " + session_title(self.state)
            if all(value == name for value in sync["threads"].values()):
                return
            self.title_sync_pending = True
            future = self.title_executor.submit(self._sync_codex_titles, self.rpc, self.connection_epoch)
        future.add_done_callback(self._codex_title_sync_done)

    def _codex_title_sync_done(self, future):
        with self.store.lock:
            self.title_sync_pending = False
        if future.cancelled():
            return
        error = future.exception()
        if error:
            self.emit("error", "Codex 名称同步未完成：" + str(error))
            return
        # 执行期间发生的改名或重连，使用最新状态继续处理。
        self.queue_codex_title_sync()

    def _sync_codex_titles(self, rpc, epoch):
        while True:
            with self.store.lock:
                sync = self.state["codex_title_sync"]
                if (self.closed or self.rpc is not rpc or epoch != self.connection_epoch
                        or not self.state.get("rpc_connected") or sync["error"]):
                    return
                name = "工作流会话 · " + session_title(self.state)
                thread_id = next((key for key, value in sync["threads"].items() if value != name), None)
                if thread_id is None:
                    return
            try:
                # 不在消息读取回调、会话锁或存储锁内等待服务端响应。
                rpc.request("thread/name/set", {"threadId": thread_id, "name": name})
            except (WorkflowError, OSError) as error:
                with self.store.lock:
                    if self.closed or self.rpc is not rpc or epoch != self.connection_epoch:
                        return
                    sync["error"] = "Codex 名称同步已停止，不自动重试：" + str(error)
                    self.store.save(self.state)
                self.note("控制器", sync["error"])
                with self.store.lock:
                    state = deepcopy(self.state)
                self.emit("state", state)
                self.emit("error", sync["error"])
                return
            with self.store.lock:
                if self.closed or self.rpc is not rpc or epoch != self.connection_epoch:
                    return
                sync["threads"][thread_id] = name
                self.store.save(self.state)

    def codex_title_event(self, params):
        with self.store.lock:
            sync = self.state.get("codex_title_sync")
            if sync is None or params["threadId"] not in sync["threads"]:
                return
            sync["threads"][params["threadId"]] = params.get("threadName") or ""
            self.store.save(self.state)
        self.queue_codex_title_sync()
