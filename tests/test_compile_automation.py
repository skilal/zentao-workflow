"""自动诊断决策、独立检查及续聊开关；只使用临时仓库和模拟检查结果。"""

from copy import deepcopy
from json import dumps
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

import test_compile_checks as compile_fixtures
from test_compile_checks import BLOCKED, MISSING, PASSED
from workflow.compile_checks import CompileRunner, classify, compile_context, compile_repositories
from workflow.gitops import git
from workflow.mcp_server import Tools
from workflow.settings import WorkflowError
from workflow.store import write_authorized
from workflow.web import WebApplication


class CompileAutomationChecks(TestCase):
    def setUp(self):
        self.fixture = compile_fixtures.CompileChecks()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.flow, self.state, self.repo = self.fixture.flow, self.fixture.state, self.fixture.repo

    def caller_failure(self):
        path = Path(self.repo["path"]) / "技术组件" / "调用方.java"
        path.write_text("class 调用方 { Object 值 = 旧方法(); }\n", encoding="utf-8")
        git(self.repo["path"], "add", "--", "技术组件/调用方.java")
        git(self.repo["path"], "commit", "-m", "记录调用方基线")
        self.repo["baseline"] = git(self.repo["path"], "rev-parse", "HEAD")
        return classify(self.repo, 1, f"[ERROR] {path}:[1,12] cannot find symbol\n[ERROR] symbol: method 旧方法()")

    def analysis(self, category="task_code", **changes):
        result = {"summary": "本轮方法变更影响了原有调用方，应同步修改调用。", "category": category,
                  "repairs": [{"path": "技术组件/调用方.java", "source_excerpt": "Object 值 = 旧方法();",
                               "related_path": "档案服务/业务.java", "reason": "本轮调整方法后，调用方仍使用旧方法。"}]
                  if category == "task_code" else []}
        result.update(changes)
        return dumps(result, ensure_ascii=False)

    def test_automatic_analysis_repairs_affected_caller_then_rechecks(self):
        self.fixture.start([self.caller_failure()])
        self.assertEqual(self.state["phase"], "compile_analysis")
        self.assertTrue(compile_context(self.state)[0]["automatic_analysis"])
        self.assertIsNotNone(self.flow._start_turn.call_args.args[1])
        self.flow._compile_analyzed(self.analysis())
        batch = self.state["compile_checks"][-1]
        self.assertEqual(self.state["phase"], "compile_fix")
        self.assertEqual(batch["repair_paths"], ["技术组件/调用方.java"])
        self.flow.save()
        tools = Tools(self.state["id"])
        tools.store = self.flow.store
        tools.call("replace_text", {"repository": "后端", "path": "技术组件/调用方.java",
                                    "old": "旧方法()", "new": "新方法()"})
        with patch.object(CompileRunner, "check", return_value=deepcopy(PASSED)) as runner:
            self.flow._compile_repaired()
        self.assertEqual(runner.call_count, 1)
        self.assertFalse(runner.call_args.args[2])
        self.assertEqual(self.state["phase"], "review")
        self.assertEqual(batch["fix_round"], 1)

    def test_unrelated_dependencies_and_uncertainty_stop_without_repair(self):
        failure = self.caller_failure()
        for category in ("original_code", "dependencies", "environment", "unknown"):
            self.fixture.start([failure])
            self.flow._compile_analyzed(self.analysis(category))
            self.assertEqual(self.state["stage"], "等待编译选择")
            self.assertEqual(self.state["compile_checks"][-1]["fix_round"], 0)
            self.assertNotIn("repair_paths", self.state["compile_checks"][-1])

    def test_analysis_does_not_expand_scope_or_overwrite_preserved_files(self):
        failure = self.caller_failure()
        variants = [
            {"path": "保留.txt", "source_excerpt": "本机原有修改", "related_path": "档案服务/业务.java", "reason": "不应修改"},
            {"path": "技术组件/调用方.java", "source_excerpt": "不存在的源码", "related_path": "档案服务/业务.java", "reason": "证据无效"},
            {"path": "技术组件/调用方.java", "source_excerpt": "旧方法()", "related_path": "不存在.java", "reason": "没有任务关联"}]
        for repair in variants:
            self.fixture.start([failure])
            with self.assertRaises(WorkflowError):
                self.flow._compile_analyzed(self.analysis(repairs=[repair]))
            self.assertEqual(self.state["stage"], "等待编译选择")
            self.assertEqual(self.state["compile_checks"][-1]["fix_round"], 0)

    def test_invalid_analysis_protocol_stops_instead_of_accepting_plain_text(self):
        self.fixture.start([self.caller_failure()])
        with self.assertRaisesRegex(WorkflowError, "自动编译分析已停止"):
            self.flow._compile_analyzed("看起来可以修复，请继续。")
        self.assertEqual(self.state["stage"], "等待编译选择")

    def test_no_code_change_stops_without_recompiling(self):
        self.fixture.start([self.fixture.code_error()])
        with patch.object(CompileRunner, "check") as runner:
            self.flow._compile_repaired()
        runner.assert_not_called()
        self.assertEqual(self.state["stage"], "等待编译选择")
        self.assertIn("没有产生代码变化", self.state["compile_checks"][-1]["repositories"][0]["summary"])

    def test_direct_environment_and_dependency_failures_wait_for_user(self):
        for output in ("[ERROR] JAVA_HOME invalid", "[ERROR] Could not resolve artifact org.example:missing:1"):
            result = classify(self.repo, 1, output)
            self.flow._start_turn.reset_mock()
            self.fixture.start([result])
            self.flow._start_turn.assert_not_called()
            self.assertEqual(self.state["stage"], "等待编译选择")

    def test_independent_check_does_not_review_or_change_session_compile_default(self):
        self.state.update(stage="已完成（未审查）", phase="complete", compile_check={"enabled": False}, commits={"后端": "无代码修改"})
        with patch.object(CompileRunner, "check", return_value=deepcopy(PASSED)) as runner:
            self.flow.compile_now("独立检查请求")
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(self.state["phase"], "complete")
        self.assertFalse(self.state["compile_check"]["enabled"])
        self.assertNotIn("manual_compile", self.state)
        self.assertFalse(self.fixture.started)

    def test_independent_check_handles_committed_task_and_authorizes_only_diagnostic_file(self):
        git(self.repo["path"], "add", "--", "档案服务/业务.java")
        git(self.repo["path"], "commit", "-m", "上轮任务代码")
        head = git(self.repo["path"], "rev-parse", "HEAD")
        self.state.update(phase="complete", stage="已完成（静态审查）", compile_check={"enabled": False},
                          commits={"后端": head}, review_manifest={"后端/档案服务/业务.java": "已有任务摘要"},
                          approved_hash="", approved_plan="")
        failure = {**BLOCKED, "analysis_required": True,
                   "compiler_diagnostics": [{"path": "档案服务/业务.java", "line": 1, "message": "找不到符号"}]}
        with patch.object(CompileRunner, "check", return_value=failure):
            self.flow.compile_now("检查已提交的任务")
        self.assertEqual(compile_repositories(self.state)[0]["baseline"], head)
        self.assertNotEqual(self.repo["baseline"], head)
        self.assertEqual(self.state["commits"], {})
        self.flow.pause()
        self.assertEqual(self.state["stage"], "已暂停")
        self.flow.resume()
        self.assertEqual(self.state["phase"], "compile_analysis")
        self.flow._compile_analyzed(self.analysis(repairs=[{"path": "档案服务/业务.java", "source_excerpt": "值 = 错误",
                                                           "related_path": "档案服务/业务.java", "reason": "本轮类型不匹配"}]))
        self.assertTrue(write_authorized(self.state))
        self.flow.save()
        tools = Tools(self.state["id"])
        tools.store = self.flow.store
        tools.call("replace_text", {"repository": "后端", "path": "档案服务/业务.java", "old": "值 = 错误", "new": "值 = 1"})
        with patch.object(CompileRunner, "check", return_value=deepcopy(PASSED)):
            self.flow._compile_repaired()
        self.assertEqual(self.state["stage"], "已完成（未审查）")
        self.assertFalse(self.state["compile_check"]["enabled"])
        self.assertEqual(self.state["commits"], {"后端": head})
        self.assertEqual(git(self.repo["path"], "rev-parse", "HEAD"), head)

    def test_manual_check_requires_idle_state_and_unchanged_head(self):
        with self.assertRaises(WorkflowError):
            self.flow.compile_now("执行中")
        self.state.update(phase="complete", stage="已完成（未审查）")
        git(self.repo["path"], "commit", "--allow-empty", "-m", "外部更新")
        with self.assertRaisesRegex(WorkflowError, "HEAD"):
            self.flow.compile_now("已过期")

    def test_independent_failure_and_automatic_analysis_survive_restart(self):
        self.state.update(phase="complete", stage="已完成（未审查）")
        with patch.object(CompileRunner, "check", return_value=deepcopy(MISSING)):
            self.flow.compile_now("等待依赖")
        app = WebApplication(self.fixture.settings)
        self.addCleanup(app.close)
        restored = app.sessions.get(self.state["id"]).engine
        self.assertEqual(restored.state["manual_compile"]["request_id"], "等待依赖")
        with patch.object(CompileRunner, "check", return_value=deepcopy(PASSED)):
            batch = restored.state["compile_checks"][-1]
            restored.compile_decision(batch["id"], "后端", "download")
        self.assertEqual(restored.state["phase"], "complete")
        self.assertNotIn("manual_compile", restored.state)

    def test_followup_switch_and_manual_action_have_revision_and_dedup_guards(self):
        self.state.update(phase="complete", stage="已完成（未审查）")
        app = WebApplication(self.fixture.settings)
        self.addCleanup(app.close)
        runtime = app.sessions.add(self.state)
        runtime.engine.followup = Mock()
        runtime.engine.compile_now = Mock()
        runtime.engine.save()
        for invalid in (None, 1, "true"):
            with self.assertRaisesRegex(WorkflowError, "编译检查开关"):
                app.dispatch({"action": "followup", "session_id": self.state["id"], "revision": runtime.revision,
                              "request_id": uuid4().hex, "data": {"text": "继续修改", "review_enabled": False,
                                                                       "followup_compile_enabled": invalid}})
        for action, data in (("followup", {"text": "继续修改", "review_enabled": False, "followup_compile_enabled": False}),
                             ("compile_now", {})):
            request = {"action": action, "session_id": self.state["id"], "revision": runtime.revision,
                       "request_id": uuid4().hex, "data": data}
            with self.assertRaisesRegex(WorkflowError, "版本"):
                app.dispatch({**request, "revision": runtime.revision - 1})
            app.dispatch(request)
            runtime.engine.executor.submit(lambda: None).result(timeout=5)
            app.dispatch(request)
        self.assertIs(runtime.engine.followup.call_args.args[-1], False)
        self.assertEqual(runtime.engine.compile_now.call_count, 1)

    def test_independent_check_respects_other_session_workspace_ownership(self):
        other = self.flow.store.new({"kind": "task", "id": 99})
        other.update(repos=deepcopy(self.state["repos"]), phase="implement", stage="已暂停", prepare_complete=True)
        self.flow.store.save(other)
        self.state.update(phase="complete", stage="已完成（未审查）")
        with patch.object(CompileRunner, "check") as runner, self.assertRaisesRegex(WorkflowError, "其他未完成会话"):
            self.flow.compile_now("不能占用其他会话")
        runner.assert_not_called()
