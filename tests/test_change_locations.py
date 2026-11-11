"""整体页面说明只保存一份，且不再阻塞正常修改流程。"""

from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow import prompt_templates, prompts
from workflow.change_locations import SUMMARY_INSTRUCTIONS, SUMMARY_POLICY, collect_change_summary
from workflow.engine import Workflow
from workflow.gitops import git
from workflow.mcp_server import TOOLS, Tools
from workflow.rpc import thread_parameters
from workflow.settings import ROOT, WorkflowError
from workflow.store import SessionStore


class ChangeSummaryChecks(TestCase):
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
        self.state = self.flow.store.new({"kind": "task", "id": 7, "title": "整体页面说明"})
        self.flow.state = self.state
        self.path = self.root / "frontend"
        self.path.mkdir()
        git(self.path, "init", "-b", "main")
        git(self.path, "config", "user.name", "汇总检查")
        git(self.path, "config", "user.email", "summary@example.invalid")
        (self.path / "页面.js").write_text("原始内容\n", encoding="utf-8")
        git(self.path, "add", ".")
        git(self.path, "commit", "-m", "初始化")
        self.state.update(phase="followup", stage="续聊处理中", turn_id="完成回合", review_enabled=False,
                          approved_plan="修改页面", approved_hash=sha256("修改页面".encode()).hexdigest(),
                          approval_source="manual", thread_id="线程", prepare_complete=True,
                          snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                          codex={"model": "模拟模型", "reasoning_effort": "high"},
                          repos=[{"name": "前端", "role": "frontend", "path": str(self.path),
                                  "source": str(self.path), "branch": "main", "preserved_paths": [],
                                  "baseline": git(self.path, "rev-parse", "HEAD")}])
        self.flow._before_review = Mock()
        self.flow._apply_review_request = Mock()
        self.flow._compile_repaired = Mock()
        self.pages = [{"page": "系统管理 → 用户管理", "description": "新增保存时的必填校验"}]
        self.flow.save()

    def set_summary(self, pages=None):
        return Tools(self.state["id"]).call("set_change_summary", {"pages": self.pages if pages is None else pages})

    def finish(self, status="completed"):
        self.flow.last_message = "全部修改完成"
        self.flow._completed({"status": status, "id": self.state["turn_id"]}, self.state["id"], self.flow.connection_epoch)

    def write(self, text="修改页面\n"):
        Tools(self.state["id"]).call("write_file", {"repository": "前端", "path": "页面.js", "content": text,
            "expected_version": sha256((self.path / "页面.js").read_bytes()).hexdigest()})

    def test_single_summary_replaces_previous_and_persists_without_file_history(self):
        self.write()
        self.set_summary()
        updated = [*self.pages, {"page": "档案管理 → 检索页面", "description": "调整查询条件"}]
        self.set_summary(updated)
        self.finish()
        self.flow.save()
        self.assertEqual(SessionStore().load(self.state["id"])["change_summary"], updated)
        directory = self.flow.store.directory(self.state["id"])
        self.assertTrue((directory / "change-summary.json").exists())
        self.assertFalse((directory / "change-locations").exists())
        self.assertNotIn("change_locations", self.state)

    def test_edit_phases_do_not_require_reports_or_track_files(self):
        for number, phase in enumerate(("implement", "followup", "fix", "compile_fix")):
            with self.subTest(phase=phase):
                self.state.update(phase=phase, stage={"implement": "实施中", "followup": "续聊处理中",
                                  "fix": "修复中", "compile_fix": "编译修复中"}[phase], turn_id=f"修改{number}")
                if phase == "compile_fix":
                    self.state["repos"][0]["role"] = "backend"
                    self.state.update(compile_check={"enabled": True}, compile_checks=[{
                        "status": "repairing", "repair_repository": "前端", "repair_paths": ["页面.js"]}])
                self.flow.save()
                self.write(f"修改{number}\n")
                self.finish()
                self.assertNotEqual(self.state["stage"], "已停止")
        self.assertNotIn("change_summary", self.state)
        self.flow._compile_repaired.assert_called_once()

    def test_question_and_interruption_preserve_previous_summary(self):
        self.state["change_summary"] = [{"page": "已有页面", "description": "此前已完成的改动"}]
        self.set_summary()
        self.finish()
        self.assertEqual(self.state["change_summary"][0]["page"], "已有页面")
        self.write()
        self.set_summary()
        self.finish("interrupted")
        self.assertEqual(self.state["change_summary"][0]["page"], "已有页面")

    def test_review_collects_overall_summary_before_commit(self):
        self.state.update(phase="review", stage="审查中", review_manifest={}, review_prompt_hash="提示词", repos=[])
        self.flow.save()
        self.set_summary()
        self.flow.finalize = Mock()
        self.flow.last_message = dumps({"summary": "审查通过", "findings": [], "resolved_previous": [], "commit_messages": []})
        self.flow._completed({"status": "completed", "id": self.state["turn_id"]}, self.state["id"], self.flow.connection_epoch)
        self.assertEqual(self.state["change_summary"], self.pages)
        self.flow.finalize.assert_called_once()

    def test_other_round_or_session_does_not_collect_stale_summary(self):
        self.set_summary()
        self.state["delivery_round"] += 1
        collect_change_summary(self.flow.store, self.state)
        self.assertNotIn("change_summary", self.state)
        other = self.flow.store.new({"kind": "task", "id": 8, "title": "其他会话"})
        other["turn_id"] = self.state["turn_id"]
        collect_change_summary(self.flow.store, other)
        self.assertNotIn("change_summary", other)

    def test_payload_has_only_page_and_description_and_old_tool_is_removed(self):
        for value in ([], [{"page": "页面", "description": ""}], [{**self.pages[0], "files": []}]):
            with self.subTest(value=value), self.assertRaises(WorkflowError):
                self.set_summary(value)
        names = {name for name, _, _ in TOOLS}
        self.assertNotIn("report_change_locations", names)
        self.assertIn("set_change_summary", names)
        self.assertIn("set_change_summary", thread_parameters(self.state, "模拟模型", {})["config"]["mcp_servers"]["workflow"]["enabled_tools"])

    def test_only_summary_stages_receive_summary_instruction(self):
        self.flow.rpc = Mock()
        self.flow.rpc.model = "模拟模型"
        self.flow.ensure_rpc = Mock()
        for phase in ("implement", "fix", "compile_fix", "review", "followup"):
            self.state["phase"] = phase
            self.flow.rpc.request.return_value = {"turn": {"id": phase}}
            self.flow._start_turn("继续原阶段")
            prompt = self.flow.rpc.request.call_args.args[1]["input"][0]["text"]
            self.assertEqual(SUMMARY_INSTRUCTIONS in prompt, phase in {"review", "followup"})
            self.assertIn(SUMMARY_POLICY, prompt)

    def test_old_thread_fix_restore_and_answer_replace_legacy_report_requirement(self):
        self.state["history"].append({"role": "控制器", "text":
            "实际修改后必须调用 workflow.report_change_locations 登记操作路径。"})
        self.state["decisions"] = [{"round": 1, "fix_ids": ["R01-01", "R01-02"], "delivery_round": 1}]
        self.flow.rpc = Mock()
        self.flow.rpc.model = "模拟模型"
        self.flow.ensure_rpc = Mock()
        for text in (prompts.FIX, prompts.RESTORE, prompt_templates.ANSWER_INPUT.format(reply="旧要求已取消，继续修复")):
            with self.subTest(text=text):
                self.state["phase"] = "fix"
                self.flow.rpc.request.return_value = {"turn": {"id": "恢复修复"}}
                self.flow._start_turn(text)
                prompt = self.flow.rpc.request.call_args.args[1]["input"][0]["text"]
                self.assertIn(SUMMARY_POLICY, prompt)
                self.assertNotIn(SUMMARY_INSTRUCTIONS, prompt)
                self.assertEqual(self.state["decisions"][0]["fix_ids"], ["R01-01", "R01-02"])
        instructions = thread_parameters(self.state, "模拟模型", {})["developerInstructions"]
        self.assertIn("此规则替代本线程历史中的", instructions)
        self.assertIn("缺少整体说明不阻塞修复、审查或提交", instructions)
