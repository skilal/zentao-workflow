"""隔离检查 Windows 控制器与 MCP 进程的会话状态读写同步。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from json import loads
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from workflow.settings import WorkflowError
from workflow.store import SessionStore, session_lock


def hold_state_reader(root, session_id, entered, release, result):
    from workflow import store as storage

    storage.LOCAL = Path(root)
    store = storage.SessionStore()

    def read(path, encoding=None, errors=None):
        with path.open("r", encoding=encoding, errors=errors) as stream:
            entered.set()
            if not release.wait(10):
                raise AssertionError("测试未释放状态读取句柄")
            return stream.read()

    try:
        with patch.object(Path, "read_text", read):
            result.put({"stage": store.load(session_id)["stage"]})
    except Exception as error:
        result.put({"error": str(error)})


class StoreConcurrencyChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patched = patch("workflow.store.LOCAL", self.root)
        patched.start()
        self.addCleanup(patched.stop)
        self.store = SessionStore()
        self.state = self.store.new({"kind": "bug", "id": 7, "title": "脱敏会话"})

    def test_controller_save_waits_for_mcp_process_to_close_state_reader(self):
        context = get_context("spawn")
        entered, release = context.Event(), context.Event()
        result = context.Queue()
        reader = context.Process(target=hold_state_reader,
            args=(str(self.root), self.state["id"], entered, release, result))
        reader.start()
        try:
            self.assertTrue(entered.wait(10))
            started, finished = Event(), Event()
            self.state["stage"] = "进度已保存"

            def save():
                started.set()
                self.store.save(self.state)
                finished.set()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(save)
                try:
                    self.assertTrue(started.wait(3))
                    self.assertFalse(finished.wait(0.1))
                finally:
                    release.set()
                future.result(timeout=5)
            self.assertEqual(result.get(timeout=5), {"stage": "资料已读取"})
            self.assertEqual(self.store.load(self.state["id"])["stage"], "进度已保存")
            self.assertFalse((self.store.directory(self.state["id"]) / "state.json.tmp").exists())
        finally:
            release.set()
            reader.join(timeout=10)
            if reader.is_alive():
                reader.terminate()
                reader.join(timeout=5)
            result.close()
            result.join_thread()
        self.assertEqual(reader.exitcode, 0)

    def test_separate_store_instances_serialize_writes_to_same_temporary_file(self):
        other = SessionStore()

        def save(store, label):
            state = deepcopy(self.state)
            for index in range(20):
                state.update(writer=label, sequence=index, payload=label * 16000)
                store.save(state)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(save, store, label) for store, label in ((self.store, "甲"), (other, "乙"))]
            for future in futures:
                future.result(timeout=10)
        state = other.load(self.state["id"])
        self.assertEqual(state["sequence"], 19)
        self.assertEqual(state["payload"], state["writer"] * 16000)

    def test_nested_append_and_catalog_reads_reenter_the_same_process_mutex(self):
        self.store.append(self.state, "用户", "确认保留原会话")
        sessions = self.store.sessions()
        self.assertEqual(sessions[0]["history"][-1]["text"], "确认保留原会话")

    def test_different_roots_do_not_share_a_mutex(self):
        with patch("workflow.store.LOCAL", self.root / "another"):
            other = SessionStore()
        with session_lock(self.store.root), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(other.new, {"kind": "task", "id": 8, "title": "另一工作目录"})
            self.assertEqual(future.result(timeout=5)["item"]["id"], 8)

    def test_lock_timeout_stops_without_writing_or_releasing_an_unowned_mutex(self):
        path = self.store.directory(self.state["id"]) / "state.json"
        before = path.read_bytes()
        with patch("workflow.store.KERNEL32.WaitForSingleObject", return_value=0x102), \
             patch("workflow.store.KERNEL32.ReleaseMutex") as release:
            with self.assertRaisesRegex(WorkflowError, "读写锁超时"):
                self.store.save(self.state)
        release.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_file_permission_failure_is_not_retried_and_keeps_original_state(self):
        path = self.store.directory(self.state["id"]) / "state.json"
        original = loads(path.read_text(encoding="utf-8"))
        self.state["stage"] = "不应覆盖原状态"
        with patch.object(Path, "replace", side_effect=PermissionError("模拟永久权限错误")) as replace:
            with self.assertRaises(PermissionError):
                self.store.save(self.state)
        replace.assert_called_once()
        self.assertEqual(self.store.load(self.state["id"]), original)
