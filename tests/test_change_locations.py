"""隔离工作区检查位置报告、回合推进与中断恢复。"""

from copy import deepcopy
from hashlib import sha256
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.change_locations import (LOCATION_INSTRUCTIONS, begin_location_turn, complete_locations,
                                       journal_path, load_journal)
from workflow.engine import Workflow
from workflow.gitops import git
from workflow.mcp_server import TOOLS, Tools
from workflow.rpc import thread_parameters
from workflow.settings import ROOT, WorkflowError
from workflow.store import SessionStore
from workflow.web import public_state


class ChangeLocationsChecks(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patched = patch("workflow.store.LOCAL", self.root / "local")
        patched.start()
        self.addCleanup(patched.stop)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.flow = Workflow(settings, Mock())
        self.addCleanup(self.flow.close)
        self.state = self.flow.store.new({"kind": "task", "id": 7, "title": "位置报告"})
        self.flow.state = self.state
        self.path = self.root / "frontend"
        self.path.mkdir()
        git(self.path, "init", "-b", "main")
        git(self.path, "config", "user.name", "位置检查")
        git(self.path, "config", "user.email", "locations@example.invalid")
        (self.path / "页面.js").write_text("原始内容\n", encoding="utf-8")
        git(self.path, "add", ".")
        git(self.path, "commit", "-m", "初始化")
        plan = "修改保存校验"
        self.state.update(approved_plan=plan, approved_hash=sha256(plan.encode()).hexdigest(),
                          approval_source="manual", thread_id="线程", prepare_complete=True,
                          snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                          codex={"model": "模拟模型", "reasoning_effort": "high"},
                          repos=[{"name": "前端", "role": "frontend", "path": str(self.path),
                                  "source": str(self.path), "branch": "main", "preserved_paths": [],
                                  "baseline": git(self.path, "rev-parse", "HEAD")}])
        self.flow._before_review = Mock()
        self.flow._compile_repaired = Mock()
        self.flow._apply_review_request = Mock()
        self.new_turn("回合一")

    def new_turn(self, turn, phase="implement"):
        stages = {"implement": "实施中", "followup": "续聊处理中", "fix": "修复中", "compile_fix": "编译修复中"}
        self.state.update(turn_id=turn, phase=phase, stage=stages[phase], pause_requested=False, pending=None)
        begin_location_turn(self.flow.store, self.state)
        self.flow.save()

    def write(self, path="页面.js", text="新增必填校验\n", repository="前端"):
        tool = Tools(self.state["id"])
        repo = next(repo for repo in self.state["repos"] if repo["name"] == repository)
        file = Path(repo["path"]) / path
        version = sha256(file.read_bytes()).hexdigest() if file.exists() else "missing"
        return tool.call("write_file", {"repository": repository, "path": path, "content": text, "expected_version": version})

    def item(self, path="页面.js", status="located", repository="前端"):
        return {"status": status, "location_path": ["系统管理", "用户管理", "编辑弹窗", "保存按钮"],
                "route": "/system/users", "change": "新增必填校验", "steps": ["打开用户管理，点击编辑", "查看保存按钮校验"],
                "reason": "" if status == "located" else "未找到菜单配置" if status == "unresolved" else "仅后台定时任务使用",
                "files": [{"repository": repository, "path": path}],
                "evidence": [{"repository": "前端", "path": "页面.js", "line": 1, "description": "保存逻辑"}]}

    def report(self, items=None):
        return Tools(self.state["id"]).call("report_change_locations", {"items": items or [self.item()]})

    def finish(self, status="completed", text="修改完成"):
        self.flow.last_message = text
        self.flow._completed({"id": self.state["turn_id"], "status": status}, self.state["id"], self.flow.connection_epoch)

    def test_registration_is_not_published_until_normal_completion_and_survives_reload(self):
        self.write()
        self.report()
        self.assertNotIn("change_locations", self.state)
        self.finish()
        self.flow._before_review.assert_called_once()
        reports = SessionStore().load(self.state["id"])["change_locations"]
        self.assertEqual(reports[0]["items"][0]["location_path"][-1], "保存按钮")
        self.assertEqual(public_state(self.state)["change_locations"], reports)
        self.assertEqual(len(reports), 1)
        complete_locations(self.flow.store, self.state)
        self.assertEqual(len(self.state["change_locations"]), 1)

    def test_missing_and_outdated_report_stop_before_review(self):
        self.write()
        self.finish()
        self.assertEqual(self.state["stage"], "已停止")
        self.assertIn("报告缺失或已过期", self.state["error"])
        self.flow._before_review.assert_not_called()
        self.new_turn("人工继续")
        self.report()
        self.write(text="再次修改\n")
        self.finish()
        self.assertEqual(self.state["stage"], "已停止")
        self.assertNotIn("change_locations", self.state)
        self.new_turn("再次继续")
        self.assertEqual(Tools(self.state["id"]).call("task_context", {})["change_locations"]["files"],
                         [{"repository": "前端", "path": "页面.js"}])
        self.report()
        self.finish()
        self.assertEqual(len(self.state["change_locations"]), 1)
        self.flow._before_review.assert_called_once()

    def test_multifile_multi_entry_and_backend_mapping(self):
        self.write()
        self.write("接口.js", "接口变更\n")
        second = self.root / "backend"
        second.mkdir()
        git(second, "init", "-b", "main")
        self.state["repos"].append({**self.state["repos"][0], "name": "后端", "role": "backend", "path": str(second)})
        self.flow.save()
        self.write("接口.java", "接口返回值变化\n", "后端")
        item = self.item()
        with self.assertRaisesRegex(WorkflowError, "未覆盖全部"):
            self.report([item])
        item["files"].extend([{"repository": "前端", "path": "接口.js"}, {"repository": "后端", "path": "接口.java"}])
        another = deepcopy(item)
        another["location_path"] = ["人员查询", "详情页面"]
        self.report([item, another])
        self.finish()
        self.assertEqual(len(self.state["change_locations"][0]["items"]), 2)

    def test_no_ui_and_unresolved_continue_without_inventing_entries(self):
        for number, status in enumerate(("no_ui", "unresolved")):
            with self.subTest(status=status):
                self.new_turn(f"状态{number}")
                self.write(text=f"变更{number}\n")
                item = self.item(status=status)
                item.update(location_path=[], steps=[])
                if status == "unresolved":
                    item["evidence"] = []
                self.report([item])
                self.finish()
                self.assertEqual(self.state["change_locations"][-1]["items"][0]["status"], status)

    def test_delete_new_file_noop_and_reverted_changes(self):
        self.write(text="原始内容\n")
        self.finish()
        self.assertNotIn("change_locations", self.state)
        self.new_turn("恢复原样")
        self.write()
        self.write(text="原始内容\n")
        self.finish()
        self.assertNotIn("change_locations", self.state)
        self.new_turn("新增删除")
        self.write("新增.js")
        self.write("短暂.js")
        tool = Tools(self.state["id"])
        for name in ("页面.js", "短暂.js"):
            version = sha256((self.path / name).read_bytes()).hexdigest()
            tool.call("delete_file", {"repository": "前端", "path": name, "expected_version": version})
        self.report([self.item(), self.item("新增.js")])
        self.finish()
        files = [file["path"] for item in self.state["change_locations"][0]["items"] for file in item["files"]]
        self.assertEqual(files, ["页面.js", "新增.js"])

    def test_every_edit_phase_records_and_readonly_phase_rejects_reporting(self):
        for number, phase in enumerate(("implement", "followup", "fix", "compile_fix")):
            self.new_turn(f"阶段{number}", phase)
            if phase == "compile_fix":
                # 编译修复工具的既有授权约束保持不变。
                self.state["repos"][0]["role"] = "backend"
                self.state["compile_check"] = {"enabled": True}
                self.state["compile_checks"] = [{"status": "repairing", "repair_repository": "前端", "repair_paths": ["页面.js"]}]
                self.flow.save()
            self.write(text=f"阶段{number}\n")
            self.report()
            self.finish()
        self.assertEqual([report["phase"] for report in self.state["change_locations"]],
                         ["implement", "followup", "fix", "compile_fix"])
        self.flow._compile_repaired.assert_called_once()
        self.state.update(phase="review", stage="审查中")
        self.flow.save()
        with self.assertRaisesRegex(WorkflowError, "已授权"):
            self.report()

    def test_interrupted_report_is_not_completed_and_next_turn_carries_files(self):
        self.write()
        self.report()
        self.finish("interrupted")
        self.assertNotIn("change_locations", self.state)
        self.new_turn("恢复回合")
        record = load_journal(self.flow.store, self.state)
        self.assertEqual(record["carried_from"], ["回合一"])
        self.assertIsNone(record["report_revision"])
        self.report()
        self.finish()
        self.assertEqual(self.state["change_locations"][0]["turn_id"], "恢复回合")

    def test_repeated_registration_replaces_report_and_sessions_are_isolated(self):
        self.write()
        self.report()
        item = self.item()
        item["change"] = "最终说明"
        self.report([item])
        other = self.flow.store.new({"kind": "task", "id": 8, "title": "其他会话"})
        other.update(turn_id=self.state["turn_id"], phase="implement")
        self.assertNotEqual(journal_path(self.flow.store, self.state), journal_path(self.flow.store, other))
        self.assertEqual(load_journal(self.flow.store, other)["files"], [])
        self.finish()
        self.assertEqual(self.state["change_locations"][0]["items"][0]["change"], "最终说明")

    def test_invalid_fields_or_unrelated_files_do_not_replace_valid_report(self):
        self.write()
        self.report()
        for field, value in (("files", [{"repository": "前端", "path": "其他.js"}]),
                             ("status", "猜测"), ("location_path", []), ("evidence", []), ("steps", "打开页面")):
            item = self.item()
            item[field] = value
            with self.subTest(field=field), self.assertRaises(WorkflowError):
                self.report([item])
        self.finish()
        self.assertEqual(self.state["change_locations"][0]["items"][0]["change"], "新增必填校验")

    def test_edit_and_restore_turn_prompts_require_report(self):
        self.flow.rpc = Mock()
        self.flow.rpc.model = "模拟模型"
        self.flow.rpc.request.return_value = {"turn": {"id": "新回合"}}
        self.flow.ensure_rpc = Mock()
        self.flow._start_turn("按原阶段继续")
        prompt = self.flow.rpc.request.call_args.args[1]["input"][0]["text"]
        self.assertIn(LOCATION_INSTRUCTIONS, prompt)
        self.assertTrue(journal_path(self.flow.store, self.state).exists())
        self.assertIn("report_change_locations", {name for name, _, _ in TOOLS})

    def test_every_edit_phase_exposes_required_reporting_tool(self):
        for phase in ("implement", "followup", "fix", "compile_fix"):
            with self.subTest(phase=phase):
                self.state["phase"] = phase
                params = thread_parameters(self.state, "模拟模型", {})
                server = params["config"]["mcp_servers"]["workflow"]
                self.assertTrue(server["enabled"])
                self.assertIn("report_change_locations", server["enabled_tools"])
