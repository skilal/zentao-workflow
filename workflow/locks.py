"""Windows 进程间可重入短操作锁，支持后台操作可取消地排队。"""

from hashlib import sha256
from threading import RLock, local

from workflow.settings import WorkflowError
from workflow.store import KERNEL32, WinError, get_last_error


class ProcessLock:
    def __init__(self, identity):
        self.name = "Local\\ZentaoWorkflowOperation-" + sha256(identity.encode("utf-8")).hexdigest()
        self.local = RLock()
        self.handles = local()

    def acquire(self, timeout=10):
        if not self.local.acquire(timeout=timeout):
            return False
        handle = KERNEL32.CreateMutexW(None, False, self.name)
        if not handle:
            self.local.release()
            raise WinError(get_last_error())
        result = KERNEL32.WaitForSingleObject(handle, max(0, int(timeout * 1000)))
        if result not in (0, 0x80):
            KERNEL32.CloseHandle(handle)
            self.local.release()
            if result == 0x102:
                return False
            raise WinError(get_last_error())
        stack = getattr(self.handles, "stack", [])
        stack.append(handle)
        self.handles.stack = stack
        return True

    def release(self):
        handle = self.handles.stack.pop()
        KERNEL32.ReleaseMutex(handle)
        KERNEL32.CloseHandle(handle)
        self.local.release()

    def __enter__(self):
        if not self.acquire():
            raise WorkflowError("等待仓库操作锁超时，请等待当前操作结束后继续。")
        return self

    def __exit__(self, *arguments):
        self.release()
