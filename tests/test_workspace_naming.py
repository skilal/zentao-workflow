"""工作项编号命名、并发名称登记及历史路径兼容。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from workflow.settings import ROOT
from workflow.store import SessionStore
from workflow.workspace_paths import managed_workspace, reserve_workspace_directory, workspace_directory


class WorkspaceNamingChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(dir=ROOT / ".local")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        local = patch("workflow.store.LOCAL", self.root / "local")
        local.start()
        self.addCleanup(local.stop)
        self.store = SessionStore()
        self.workspaces = self.root / "worktrees"

    def new_session(self, kind="task", number=123):
        item = {"kind": kind, "id": number, "title": "工作项"} if kind else None
        state = self.store.new(item)
        state["workspace_root"] = str(self.workspaces)
        self.store.save(state)
        return state

    def test_kinds_with_the_same_number_have_distinct_names_without_random_suffixes(self):
        paths = []
        for kind in ("task", "story", "bug", "epic", "requirement"):
            with self.subTest(kind=kind):
                state = self.new_session(kind)
                path = reserve_workspace_directory(self.store, state)
                self.assertEqual(path, self.workspaces / f"{kind}-123")
                self.assertNotIn(state["id"], str(path))
                paths.append(path)
        self.assertEqual(len(set(paths)), 5)
        self.assertFalse(self.workspaces.exists())

    def test_concurrent_sessions_reserve_sequential_names_before_any_directory_exists(self):
        states = [self.new_session() for _ in range(3)]
        with ThreadPoolExecutor(max_workers=3) as executor:
            paths = list(executor.map(lambda state: reserve_workspace_directory(self.store, state), states))
        self.assertEqual({path.name for path in paths}, {"task-123", "task-123-2", "task-123-3"})
        for state, path in zip(states, paths):
            restored = self.store.load(state["id"])
            self.assertEqual(reserve_workspace_directory(self.store, restored), path)
        self.assertFalse(self.workspaces.exists())

    def test_existing_directory_is_preserved_and_skipped(self):
        existing = self.workspaces / "task-123"
        existing.mkdir(parents=True)
        marker = existing / "本机文件.txt"
        marker.write_text("保留内容", encoding="utf-8")
        state = self.new_session()
        path = reserve_workspace_directory(self.store, state)
        self.assertEqual(path.name, "task-123-2")
        self.assertFalse(path.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "保留内容")

    def test_conversations_use_sequential_names(self):
        paths = [reserve_workspace_directory(self.store, self.new_session(None)) for _ in range(2)]
        self.assertEqual([path.name for path in paths], ["conversation", "conversation-2"])

    def test_primary_item_and_persisted_name_remain_stable_across_delivery_rounds(self):
        state = self.new_session("bug", 789)
        state["items"].append({"kind": "task", "id": 456, "title": "相关任务"})
        path = reserve_workspace_directory(self.store, state)
        state["items"].reverse()
        state["delivery_round"] = 2
        self.assertEqual(reserve_workspace_directory(self.store, state), path)
        self.assertEqual(path.name, "bug-789")

    def test_historical_root_directory_and_ownership_remain_unchanged(self):
        state = self.new_session()
        old = self.workspaces / state["id"]
        repository = old / "repo-1"
        repository.mkdir(parents=True)
        self.assertEqual(reserve_workspace_directory(self.store, state), old)
        self.assertNotIn("workspace_name", state)
        self.assertEqual(managed_workspace(self.store, repository, [state]), state["id"])

    def test_historical_session_layout_remains_unchanged(self):
        state = self.new_session()
        state.pop("workspace_root")
        old = self.store.directory(state["id"]) / "workspace"
        self.assertEqual(reserve_workspace_directory(self.store, state), old)
        self.assertFalse(old.exists())

    def test_owned_repository_records_keep_the_historical_root_even_when_missing(self):
        state = self.new_session()
        old = workspace_directory(self.store, state)
        state["repos"] = [{"path": str(old / "repo-1"), "workspace_owned": True}]
        self.assertEqual(reserve_workspace_directory(self.store, state), old)
        self.assertNotIn("workspace_name", state)
