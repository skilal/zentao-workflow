"""使用模拟协议和临时 Git 仓库检查自动实施、本地提交及暂停边界。"""

from hashlib import sha256
from itertools import count
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.approvals import is_controlled_write
from workflow.engine import Workflow
from workflow.gitops import commit, git, manifest
from workflow.mcp_server import Tools, TOOLS
from workflow.settings import ROOT, WorkflowError
from workflow.web import public_state


def finding():
    return {"title": "遗漏业务分支", "category": "业务链路问题", "origin": "本次引入", "severity": "P2",
            "previous_id": "", "repository": "前端", "path": "页面.js", "line": 1,
            "evidence": "需求中的业务分支未处理", "suggestion": "补齐该分支", "impact": "无法完成业务操作"}


class AutoDeliveryChecks(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patched = patch("workflow.store.LOCAL", self.root)
        patched.start()
        self.addCleanup(patched.stop)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.flow = Workflow(settings, Mock())
        self.addCleanup(self.flow.close)
        self.state = self.flow.store.new({"kind": "task", "id": 7, "title": "自动交付"})
        self.flow.state = self.state
        self.state.update(snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                          thread_id="会话", prepare_complete=True, max_fix_rounds=3)
        self.flow.rpc = Mock()
        self.flow.rpc.model = "模拟模型"
        self.flow.ensure_rpc = Mock()
        turns = count(1)
        self.flow.rpc.request.side_effect = lambda *args: {"turn": {"id": f"回合-{next(turns)}"}}

    def start_plan(self):
        self.flow._new_plan_round()
        self.flow._start_turn("开始计划")

    def event(self, method, **params):
        self.flow._event({"method": method, "params": {"threadId": self.state["thread_id"],
                         "turnId": self.state["turn_id"], **params}}, self.flow.connection_epoch)

    def complete(self, text="", plan=None, status="completed"):
        self.event("item/completed", item={"type": "agentMessage", "text": text})
        if plan is not None:
            self.event("item/completed", item={"type": "plan", "text": plan})
        self.flow._completed({"id": self.state["turn_id"], "status": status},
                             self.state["id"], self.flow.connection_epoch)

    def ask(self):
        self.flow._event({"id": 17, "method": "item/tool/requestUserInput", "params": {
            "threadId": self.state["thread_id"], "turnId": self.state["turn_id"],
            "questions": [{"id": "rule", "header": "业务规则", "question": "请选择业务规则"}]}}, self.flow.connection_epoch)

    def review(self, findings=None, resolved=None):
        self.state.update(phase="review", stage="审查中", review_manifest={}, review_prompt_hash="提示词版本")
        self.flow._start_turn("开始审查")
        return dumps({"summary": "静态审查结论", "findings": findings or [],
                      "resolved_previous": resolved or []}, ensure_ascii=False)

    def test_one_turn_without_questions_automatically_authorizes_exact_plan(self):
        self.start_plan()
        self.complete("<proposed_plan>完成最小修改</proposed_plan>")
        self.assertEqual((self.state["phase"], self.state["stage"]), ("implement", "实施中"))
        self.assertEqual(self.state["approval_source"], "auto")
        self.assertEqual(self.state["approved_plan"], "完成最小修改")
        self.assertEqual(self.state["approved_hash"], sha256("完成最小修改".encode()).hexdigest())
        self.assertTrue(any(entry["role"] == "控制器" and "自动授权实施" in entry["text"] for entry in self.state["history"]))
        self.assertFalse(any(entry["role"] == "用户" for entry in self.state["history"]))

    def test_final_plan_item_is_authoritative_over_streamed_text(self):
        self.start_plan()
        self.event("item/plan/delta", delta="尚未完成的计划")
        self.complete(plan="完整最终计划")
        self.assertEqual(self.state["approved_plan"], "完整最终计划")

    def test_stream_only_and_empty_plan_cannot_authorize_implementation(self):
        for text, stream in (("", "流式计划"), ("<proposed_plan> </proposed_plan>", "")):
            with self.subTest(text=text):
                self.start_plan()
                self.event("item/plan/delta", delta=stream)
                self.complete(text)
                self.assertEqual(self.state["stage"], "等待计划补充")
                self.assertFalse(self.state["approved_hash"])

    def test_question_and_answer_in_same_turn_still_require_manual_confirmation(self):
        self.start_plan()
        self.ask()
        self.state["rpc_connected"] = True
        self.flow.answer("明确的业务规则")
        self.complete("<proposed_plan>按回答实施</proposed_plan>")
        self.assertEqual(self.state["stage"], "等待计划确认")
        self.assertEqual(self.state["plan_round"]["turns"], 1)
        self.flow.approve_plan()
        self.assertEqual(self.state["approval_source"], "manual")

    def test_unanswered_question_is_not_cleared_by_turn_completion(self):
        self.start_plan()
        self.ask()
        self.complete("<proposed_plan>不能自动实施</proposed_plan>")
        self.assertEqual(self.state["stage"], "等待人工回答")
        self.assertIsNotNone(self.state["pending"])
        self.assertFalse(self.state["approved_hash"])
        self.state["rpc_connected"] = True
        self.flow.answer("明确回答业务规则")
        self.flow.rpc.respond.assert_not_called()
        self.assertEqual(self.state["plan_round"]["turns"], 2)
        self.complete("<proposed_plan>回答后成稿</proposed_plan>")
        self.assertEqual(self.state["stage"], "等待计划确认")

    def test_missing_materials_gate_is_shared_by_manual_and_automatic_paths(self):
        self.start_plan()
        self.state["snapshot"]["blockers"] = ["缺少验收标准"]
        self.complete("<proposed_plan>仍缺资料</proposed_plan>")
        self.assertEqual(self.state["stage"], "等待计划确认")
        with self.assertRaises(WorkflowError):
            self.flow.approve_plan()
        self.assertFalse(self.state["approved_hash"])

    def test_multiple_turns_for_missing_information_still_require_confirmation(self):
        self.start_plan()
        self.complete("需要补充资料")
        self.flow.answer("补充验收规则")
        self.complete("<proposed_plan>补充后成稿</proposed_plan>")
        self.assertEqual(self.state["plan_round"]["turns"], 2)
        self.assertEqual(self.state["stage"], "等待计划确认")

    def test_explicit_plan_adjustment_starts_independent_round(self):
        self.start_plan()
        self.ask()
        self.state["pending"] = None
        self.complete("<proposed_plan>原计划</proposed_plan>")
        self.flow.answer("请调整为新方案")
        self.assertEqual(self.state["plan_round"], {"number": 2, "turns": 1, "asked": False})
        self.complete("<proposed_plan>调整后的计划</proposed_plan>")
        self.assertEqual(self.state["approval_source"], "auto")

    def test_implementation_replan_starts_a_new_independent_round(self):
        self.start_plan()
        self.complete("<proposed_plan>原计划</proposed_plan>")
        self.complete("[需要重做计划] 请补充新的业务范围")
        self.assertEqual(self.state["plan_round"]["number"], 2)
        self.assertFalse(self.state["approved_hash"])
        self.flow.answer("明确新的范围")
        self.complete("<proposed_plan>新范围计划</proposed_plan>")
        self.assertEqual(self.state["approval_source"], "auto")

    def test_pause_restore_preserves_question_and_turn_count(self):
        self.start_plan()
        self.ask()
        self.flow.pause()
        self.flow.restore(self.state["id"])
        self.state = self.flow.state
        self.flow.rpc = Mock(model="模拟模型")
        self.flow.rpc.request.return_value = {"turn": {"id": "恢复回合"}}
        self.flow.resume()
        self.complete("<proposed_plan>恢复后的计划</proposed_plan>")
        self.assertEqual(self.state["plan_round"]["turns"], 2)
        self.assertTrue(self.state["plan_round"]["asked"])
        self.assertEqual(self.state["stage"], "等待计划确认")

    def test_legacy_sessions_do_not_gain_automatic_authorization(self):
        for legacy in (None, "missing"):
            with self.subTest(legacy=legacy):
                if legacy is None:
                    self.state["plan_round"] = None
                else:
                    self.state.pop("plan_round", None)
                self.state["phase"] = "plan"
                self.flow._start_turn("旧会话继续")
                self.complete("<proposed_plan>旧会话计划</proposed_plan>")
                self.assertEqual(self.state["stage"], "等待计划确认")

    def test_failed_interrupted_paused_and_disconnected_turns_cannot_advance(self):
        for status in ("failed", "interrupted", "paused", "disconnected"):
            with self.subTest(status=status):
                self.state["pause_requested"] = False
                self.start_plan()
                if status == "paused":
                    self.flow.pause()
                elif status == "disconnected":
                    self.event("client/disconnected")
                self.complete("<proposed_plan>不应实施</proposed_plan>",
                              status=status if status in ("failed", "interrupted") else "completed")
                self.assertNotEqual(self.state["phase"], "implement")
                self.assertFalse(self.state["approved_hash"])

    def test_duplicate_and_stale_completion_events_do_not_repeat_implementation(self):
        self.start_plan()
        old_turn = self.state["turn_id"]
        self.event("item/completed", item={"type": "plan", "text": "一次计划"})
        message = {"method": "turn/completed", "params": {"threadId": self.state["thread_id"],
                   "turn": {"id": old_turn, "status": "completed"}}}
        self.flow._event(message, self.flow.connection_epoch)
        self.flow.executor.submit(lambda: None).result()
        call_count = self.flow.rpc.request.call_count
        self.flow._event(message, self.flow.connection_epoch)
        self.flow._event(message, self.flow.connection_epoch - 1)
        self.flow.executor.submit(lambda: None).result()
        self.assertEqual(self.flow.rpc.request.call_count, call_count)
        self.assertEqual(self.state["phase"], "implement")

    def test_plan_hash_mismatch_cannot_enter_implementation(self):
        self.start_plan()
        self.state.update(stage="等待计划确认", plan="计划", plan_hash="错误版本")
        with self.assertRaises(WorkflowError):
            self.flow.approve_plan()

    def test_public_and_mcp_context_expose_persisted_authorization(self):
        self.start_plan()
        self.complete("<proposed_plan>实施计划</proposed_plan>")
        context = Tools(self.state["id"]).call("task_context", {})
        for value in (context, public_state(self.state), self.flow.store.load(self.state["id"])):
            self.assertEqual(value["approval_source"], "auto")
            self.assertEqual(value["plan_round"], self.state["plan_round"])

    def test_automatic_authorization_keeps_mcp_stage_and_hash_checks(self):
        self.start_plan()
        self.complete("<proposed_plan>实施计划</proposed_plan>")
        repository = self.root / "workspace"
        repository.mkdir()
        self.state["repos"] = [{"name": "前端", "path": str(repository)}]
        description = next(description for name, description, definition in TOOLS if name == "write_file")
        params = {"serverName": "workflow", "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call",
                  "tool_description": description, "tool_params": {"repository": "前端", "path": "页面.js", "content": "内容"}}}
        self.assertTrue(is_controlled_write(self.state, params))
        self.state["stage"] = "审查中"
        self.assertFalse(is_controlled_write(self.state, params))
        self.state.update(stage="实施中", approved_hash="错误版本")
        self.assertFalse(is_controlled_write(self.state, params))

    def test_first_review_without_findings_automatically_uses_local_finalize(self):
        text = self.review()
        with patch("workflow.engine.manifest", return_value={}), patch.object(self.flow, "finalize") as finalize:
            self.complete(text)
        finalize.assert_called_once_with([], "", automatic=True)

    def test_omitted_old_finding_prevents_automatic_commit(self):
        old = finding()
        old["id"] = "R01-01"
        self.state["reviews"] = [{"result": {"findings": [old]}}]
        text = self.review()
        with patch("workflow.engine.manifest", return_value={}), patch.object(self.flow, "finalize") as finalize:
            self.complete(text)
        finalize.assert_not_called()
        self.assertEqual(self.state["reviews"][-1]["result"]["findings"], [old])

    def test_repaired_findings_require_resolution_evidence_before_auto_commit(self):
        old = finding()
        old["id"] = "R01-01"
        self.state["reviews"] = [{"result": {"findings": [old]}}]
        self.state["fix_round"] = 1
        text = self.review(resolved=[{"id": "R01-01", "evidence": "已补齐分支并静态核对"}])
        with patch("workflow.engine.manifest", return_value={}), patch.object(self.flow, "finalize") as finalize:
            self.complete(text)
        finalize.assert_called_once_with([], "", automatic=True)

    def test_review_with_findings_requires_explicit_acceptance_and_reason(self):
        text = self.review([finding()])
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.commit") as operation:
            self.complete(text)
            operation.assert_not_called()
            with self.assertRaises(WorkflowError):
                self.flow.finalize([], "")
            with self.assertRaises(WorkflowError):
                self.flow.finalize(["R01-01"], "")
            self.flow.finalize(["R01-01"], "明确接受该问题")
        self.assertEqual(self.state["acceptance"]["source"], "manual")
        self.assertEqual(self.state["phase"], "complete")

    def test_changed_files_and_invalid_review_results_never_commit(self):
        text = self.review()
        with patch("workflow.engine.manifest", return_value={"前端/页面.js": "changed"}), patch.object(self.flow, "finalize") as finalize:
            with self.assertRaises(WorkflowError):
                self.complete(text)
        finalize.assert_not_called()
        text = self.review(resolved=[{"id": "不存在", "evidence": "无依据"}])
        with patch("workflow.engine.manifest", return_value={}), patch.object(self.flow, "finalize") as finalize:
            with self.assertRaises(WorkflowError):
                self.complete(text)
        finalize.assert_not_called()

    def test_paused_review_cannot_commit_and_resume_requires_new_review(self):
        text = self.review()
        self.flow.pause()
        with patch("workflow.engine.manifest", return_value={}), patch.object(self.flow, "finalize") as finalize:
            self.complete(text)
        finalize.assert_not_called()
        self.assertFalse(self.state["reviews"])
        self.state["reviews"] = [{"result": {"findings": []}}]
        with patch.object(self.flow, "_begin_review") as review:
            self.flow.resume()
        review.assert_called_once()

    def test_pause_between_review_save_and_finalize_prevents_commit(self):
        text = self.review()
        original_note = self.flow.note
        def pause_on_commit(role, message):
            original_note(role, message)
            if role == "控制器" and "自动执行本地提交" in message:
                self.state["pause_requested"] = True
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.commit") as operation, patch.object(self.flow, "note", side_effect=pause_on_commit):
            self.complete(text)
        operation.assert_not_called()
        self.assertNotEqual(self.state["phase"], "complete")

    def test_partial_failure_preserves_success_and_only_retries_remaining_repository(self):
        self.state.update(stage="等待修复选择", phase="review", review_manifest={},
                          repos=[{"name": "前端", "path": "frontend"}, {"name": "后端", "path": "backend"}],
                          reviews=[{"result": {"findings": []}}])
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.commit", side_effect=["first-sha", WorkflowError("提交失败")]) as operation:
            with self.assertRaises(WorkflowError):
                self.flow.finalize([], "", automatic=True)
            self.assertEqual(operation.call_count, 2)
        self.assertEqual(self.state["commits"], {"前端": "first-sha"})
        self.assertEqual(self.state["stage"], "部分提交")
        self.flow.pause()
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.git", return_value="first-sha"), patch("workflow.engine.working_paths", return_value=[]), patch("workflow.engine.commit", return_value="second-sha") as operation:
            self.flow.finalize([], "")
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(operation.call_args.args[0]["name"], "后端")
        self.assertEqual(self.state["phase"], "complete")

    def test_first_commit_failure_stops_without_automatic_retry(self):
        self.state.update(stage="等待修复选择", phase="review", review_manifest={},
                          repos=[{"name": "前端", "path": "frontend"}], reviews=[{"result": {"findings": []}}])
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.commit", side_effect=WorkflowError("提交失败")) as operation:
            with self.assertRaises(WorkflowError):
                self.flow.finalize([], "", automatic=True)
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(self.state["stage"], "等待修复选择")
        self.assertFalse(self.state["commits"])


class LocalCommitChecks(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "delivery")
        git(self.source, "config", "user.name", "自动交付检查")
        git(self.source, "config", "user.email", "delivery@example.invalid")
        self.file = self.source / "delivery.txt"
        self.file.write_text("initial\n", encoding="utf-8")
        git(self.source, "add", "delivery.txt")
        git(self.source, "commit", "-m", "初始化检查资料")
        self.baseline = git(self.source, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        git(self.root, "clone", "--bare", str(self.source), str(self.remote))
        git(self.source, "remote", "add", "origin", str(self.remote))
        self.remote_refs = git(self.remote, "show-ref")
        self.repository = {"name": "前端", "path": str(self.source), "branch": "delivery", "baseline": self.baseline}

    def test_commit_changes_only_local_target_branch_and_never_invokes_push(self):
        self.file.write_text("updated\n", encoding="utf-8")
        expected = manifest([self.repository])
        with patch("workflow.gitops.git", wraps=git) as operation:
            result = commit(self.repository, "feat(workflow): 本地交付", expected)
        self.assertNotEqual(result, self.baseline)
        self.assertEqual(git(self.source, "rev-parse", "refs/heads/delivery"), result)
        self.assertEqual(git(self.remote, "show-ref"), self.remote_refs)
        self.assertFalse(any("push" in call.args[1:] for call in operation.call_args_list))
        self.assertEqual(git(self.source, "status", "--porcelain"), "")

    def test_unchanged_repository_creates_no_commit(self):
        self.assertIsNone(commit(self.repository, "feat(workflow): 无修改", {}))
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), self.baseline)

    def test_changed_code_branch_head_and_external_index_block_commit(self):
        self.file.write_text("updated\n", encoding="utf-8")
        expected = manifest([self.repository])
        self.file.write_text("changed after review\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            commit(self.repository, "不应提交", expected)
        expected = manifest([self.repository])
        git(self.source, "add", "delivery.txt")
        with self.assertRaises(WorkflowError):
            commit(self.repository, "不应提交", expected)
        git(self.source, "reset", "--", "delivery.txt")
        git(self.source, "switch", "-c", "other")
        with self.assertRaises(WorkflowError):
            commit(self.repository, "不应提交", expected)
        git(self.source, "switch", "delivery")
        git(self.source, "commit", "--allow-empty", "-m", "外部提交")
        with self.assertRaises(WorkflowError):
            commit(self.repository, "不应提交", expected)

    def test_active_git_hook_blocks_commit(self):
        self.file.write_text("updated\n", encoding="utf-8")
        expected = manifest([self.repository])
        (self.source / ".git" / "hooks" / "pre-commit").write_text("exit 1\n", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            commit(self.repository, "不应提交", expected)
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), self.baseline)

    def test_end_to_end_review_commits_and_records_local_results_once(self):
        with patch("workflow.store.LOCAL", self.root):
            settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
            flow = Workflow(settings, Mock())
        self.addCleanup(flow.close)
        state = flow.store.new({"kind": "task", "id": 7, "title": "自动交付"})
        self.file.write_text("delivered\n", encoding="utf-8")
        state.update(phase="review", stage="审查中", repos=[self.repository], turn_id="review-turn",
                     review_manifest=manifest([self.repository]), review_prompt_hash="固定版本")
        flow.state = state
        flow.last_message = dumps({"summary": "无遗留问题", "findings": [], "resolved_previous": []}, ensure_ascii=False)
        flow._completed({"id": "review-turn", "status": "completed"}, state["id"], flow.connection_epoch)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["commits"]["前端"], git(self.source, "rev-parse", "HEAD"))
        self.assertEqual(state["acceptance"]["source"], "auto")
        self.assertEqual(git(self.remote, "show-ref"), self.remote_refs)
        flow._completed({"id": "review-turn", "status": "completed"}, state["id"], flow.connection_epoch)
        self.assertEqual(len(state["reviews"]), 1)
