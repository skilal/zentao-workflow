"""提交消息规范、新旧审查协议及真实临时 Git 交付检查。"""

from copy import deepcopy
from json import dumps
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import test_auto_delivery as auto_delivery
import test_followup as followup
from workflow.commit_messages import prepare_commit_messages
from workflow.engine import parse_review
from workflow.gitops import commit, git
from workflow.mcp_server import Tools
from workflow.settings import WorkflowError
from workflow.web import public_state


def entries():
    return [{"repository": "前端", "type": "feat", "scope": "档案检索", "summary": "支持全宗号范围比较"},
            {"repository": "后端", "type": "perf", "scope": "搜索", "summary": "优化范围条件解析"}]


class CommitMessageValidationChecks(TestCase):
    def setUp(self):
        item = {"kind": "task", "id": 4228, "title": "原始任务的长标题-后端开发"}
        self.state = {"item": item, "items": [item], "repos": [{"name": "前端"}, {"name": "后端"}],
                      "review_manifest": {"前端/页面.js": "页面差异", "后端/服务.py": "服务差异"}}

    def test_repository_titles_describe_actual_changes_and_references_only_appear_in_body(self):
        result = prepare_commit_messages(self.state, entries())
        self.assertEqual(result["前端"], "feat(档案检索): 支持全宗号范围比较\n\n关联工作项: 任务 #4228")
        self.assertEqual(result["后端"], "perf(搜索): 优化范围条件解析\n\n关联工作项: 任务 #4228")

    def test_task_bug_story_and_mixed_items_keep_types_and_order(self):
        self.state["items"] += [{"kind": "bug", "id": 9}, {"kind": "story", "id": 8}, self.state["item"]]
        message = prepare_commit_messages(self.state, entries())["前端"]
        self.assertTrue(message.endswith("关联工作项: 任务 #4228、Bug #9、需求 #8"))

    def test_conversation_has_no_work_item_footer(self):
        self.state.update(item=None, items=[])
        self.assertEqual(prepare_commit_messages(self.state, entries())["前端"], "feat(档案检索): 支持全宗号范围比较")

    def test_followup_and_bug_do_not_override_model_commit_type(self):
        self.state.update(delivery_round=8, item={"kind": "bug", "id": 1})
        self.assertTrue(prepare_commit_messages(self.state, entries())["前端"].startswith("feat("))

    def test_unchanged_repository_is_omitted_and_empty_delivery_has_no_messages(self):
        self.state["review_manifest"].pop("后端/服务.py")
        self.assertEqual(set(prepare_commit_messages(self.state, entries()[:1])), {"前端"})
        self.state["review_manifest"] = {}
        self.assertEqual(prepare_commit_messages(self.state, []), {})

    def test_missing_duplicate_unknown_and_unchanged_repository_titles_stop(self):
        for source in ([], entries()[:1], entries() + entries()[:1], [{**entries()[0], "repository": "陌生仓库"}, entries()[1]]):
            with self.subTest(source=source), self.assertRaisesRegex(WorkflowError, "逐一对应"):
                prepare_commit_messages(self.state, source)
        self.state["review_manifest"].pop("后端/服务.py")
        with self.assertRaises(WorkflowError):
            prepare_commit_messages(self.state, entries())

    def test_custom_summary_is_preserved_exactly_and_complete_title_is_unchanged(self):
        self.state["commit_message"] = "修复保存提示"
        source = [{**entry, "summary": "修复保存提示"} for entry in entries()]
        self.assertTrue(prepare_commit_messages(self.state, source)["前端"].startswith("feat(档案检索): 修复保存提示\n"))
        with self.assertRaisesRegex(WorkflowError, "人工指定"):
            prepare_commit_messages(self.state, entries())
        self.state["commit_message"] = "fix(档案整理): 修复保存提示"
        source = [{**entry, "type": "fix", "scope": "档案整理", "summary": "修复保存提示"} for entry in entries()]
        self.assertTrue(all(message.splitlines()[0] == self.state["commit_message"]
                            for message in prepare_commit_messages(self.state, source).values()))

    def test_length_limit_is_exact_and_never_truncates(self):
        source = entries()
        source[0]["summary"] = "字" * (72 - len("feat(档案检索): "))
        self.assertEqual(len(prepare_commit_messages(self.state, source)["前端"].splitlines()[0]), 72)
        source[0]["summary"] += "字"
        with self.assertRaisesRegex(WorkflowError, "72"):
            prepare_commit_messages(self.state, source)

    def test_technical_module_name_is_not_mistaken_for_a_work_item_prefix(self):
        source = entries()
        source[0]["scope"] = "zentao-api"
        self.assertTrue(prepare_commit_messages(self.state, source)["前端"].startswith("feat(zentao-api):"))

    def test_invalid_scope_suffix_and_multiline_titles_stop(self):
        for field, value in (("scope", ""), ("scope", "前端"), ("scope", "backend"), ("scope", "4228"), ("scope", "4228,4227"),
                             ("scope", "zentao-tasks-4228,4227"), ("scope", "检索)"), ("scope", " 搜索"),
                             ("summary", "任务 #4228 修复查询"), ("summary", "完善检索-前端开发"),
                             ("summary", "修复\n保存"), ("summary", "修复\x00保存"), ("summary", "修复\u2028保存")):
            source = entries()
            source[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(WorkflowError):
                prepare_commit_messages(self.state, source)

    def test_new_protocol_rejects_legacy_and_unknown_types_while_explicit_legacy_remains_readable(self):
        result = {"summary": "审查结论", "findings": [], "resolved_previous": []}
        self.assertEqual(parse_review(dumps(result), protocol_version=1), result)
        with self.assertRaisesRegex(WorkflowError, "重新 Review"):
            parse_review(dumps(result))
        result["commit_messages"] = entries()
        self.assertEqual(parse_review(dumps(result)), result)
        result["commit_messages"][0]["type"] = "update"
        with self.assertRaises(WorkflowError):
            parse_review(dumps(result))


class CommitMessageProtocolChecks(TestCase):
    def setUp(self):
        fixture = auto_delivery.AutoDeliveryChecks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.flow, self.state = fixture, fixture.flow, fixture.state
        self.state["review_manifest"] = {}

    def complete(self, result):
        self.state.update(phase="review", stage="审查中", review_prompt_hash="提示词版本")
        self.flow.last_message = dumps(result, ensure_ascii=False)
        with patch("workflow.engine.manifest", return_value=self.state["review_manifest"]), patch.object(self.flow, "finalize") as finalize:
            self.flow._completed({"status": "completed"}, self.state["id"], self.flow.connection_epoch)
        return finalize

    def test_existing_review_turn_generates_titles_without_extra_model_calls(self):
        self.state.update(phase="review", commit_message="修复保存提示")
        self.flow._start_turn("按固定要求审查")
        params = self.flow.rpc.request.call_args.args[1]
        self.assertEqual(self.flow.rpc.request.call_count, 1)
        self.assertIn("commit_messages", params["outputSchema"]["required"])
        self.assertIn("续聊不等于 fix", params["input"][0]["text"])
        self.assertIn('"commit_message": "修复保存提示"', params["input"][0]["text"])
        self.assertEqual(Tools(self.state["id"]).call("task_context", {})["commit_message"], "修复保存提示")
        self.complete({"summary": "结论", "findings": [], "resolved_previous": [], "commit_messages": []})
        self.assertEqual(self.flow.rpc.request.call_count, 1)

    def test_accepted_titles_are_bound_to_review_and_persisted(self):
        self.state.update(repos=[{"name": "前端"}, {"name": "后端"}],
                          review_manifest={"前端/页面.js": "一", "后端/服务.py": "二"})
        self.complete({"summary": "结论", "findings": [], "resolved_previous": [], "commit_messages": entries()})
        saved = self.flow.store.load(self.state["id"])["reviews"][-1]
        self.assertEqual(saved["protocol_version"], 2)
        self.assertEqual(saved["manifest"], self.state["review_manifest"])
        self.assertEqual(saved["commit_messages"]["前端"].splitlines()[0], "feat(档案检索): 支持全宗号范围比较")

    def test_invalid_titles_do_not_create_commit_ready_results_or_retry(self):
        self.state.update(repos=[{"name": "前端"}], review_manifest={"前端/页面.js": "一"})
        calls = self.flow.rpc.request.call_count
        with self.assertRaises(WorkflowError):
            self.complete({"summary": "结论", "findings": [], "resolved_previous": [], "commit_messages": []})
        self.assertFalse(self.state["reviews"])
        self.assertEqual(self.flow.rpc.request.call_count, calls)
        self.assertIn("review_output", public_state(self.state)["history"][-1])

    def test_titles_cannot_be_reused_for_a_different_review_snapshot(self):
        self.state.update(repos=[{"name": "前端"}], review_manifest={"前端/页面.js": "旧差异"})
        self.complete({"summary": "结论", "findings": [], "resolved_previous": [], "commit_messages": entries()[:1]})
        self.state["review_manifest"] = {"前端/页面.js": "新差异"}
        with patch("workflow.engine.manifest", return_value=self.state["review_manifest"]), patch("workflow.engine.commit") as operation:
            with self.assertRaisesRegex(WorkflowError, "重新 Review"):
                self.flow.finalize([], "")
        operation.assert_not_called()

    def test_legacy_history_remains_readable_but_cannot_be_committed(self):
        result = {"summary": "历史审查结论", "findings": [], "resolved_previous": []}
        self.state.update(phase="review", stage="等待修复选择", reviews=[{"number": 1, "result": result}],
                          history=[{"role": "Codex", "phase": "review", "text": dumps(result)}])
        original = deepcopy(self.state)
        self.assertEqual(public_state(self.state)["reviews"][0]["history_index"], 0)
        self.assertEqual(self.state, original)
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.commit") as operation:
            with self.assertRaisesRegex(WorkflowError, "重新 Review"):
                self.flow.finalize([], "")
        operation.assert_not_called()

    def test_legacy_partial_delivery_stops_without_rewriting_existing_commits(self):
        self.state.update(phase="review", stage="部分提交", repos=[], commits={"前端": "既有提交"},
                          reviews=[{"number": 1, "result": {"findings": []}}])
        with patch("workflow.engine.manifest", return_value={}), patch("workflow.engine.commit") as operation:
            with self.assertRaisesRegex(WorkflowError, "人工处理部分提交"):
                self.flow.finalize([], "")
        operation.assert_not_called()
        self.assertEqual(self.state["commits"], {"前端": "既有提交"})


class CommitMessageGitChecks(TestCase):
    def setUp(self):
        fixture = followup.FollowupChecks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.engine, self.state = fixture, fixture.engine, fixture.state

    def prepare(self, both=False):
        self.fixture.send("增加范围检索", review_enabled=True)
        for repo in self.state["repos"][:2 if both else 1]:
            (Path(repo["path"]) / "业务.txt").write_text("本轮范围检索代码\n", encoding="utf-8")
        self.fixture.complete("已完成修改")
        return {"summary": "无待处理问题", "findings": [], "resolved_previous": [],
                "commit_messages": entries() if both else entries()[:1]}

    def test_real_git_subject_body_and_unchanged_repository(self):
        result = self.prepare()
        other_head = git(self.state["repos"][1]["path"], "rev-parse", "HEAD")
        self.fixture.complete(dumps(result))
        message = git(self.state["repos"][0]["path"], "log", "-1", "--format=%B")
        self.assertEqual(message, "feat(档案检索): 支持全宗号范围比较\n\n关联工作项: 需求 #7")
        self.assertEqual(git(self.state["repos"][1]["path"], "rev-parse", "HEAD"), other_head)
        self.assertEqual(self.state["commits"]["后端"], "无代码修改")

    def test_partial_commit_restart_keeps_frozen_messages(self):
        result = self.prepare(both=True)
        expected = "perf(搜索): 优化范围条件解析\n\n关联工作项: 需求 #7"
        def fail_backend(repo, message, snapshot):
            if repo["name"] == "后端":
                raise WorkflowError("模拟后端提交失败")
            return commit(repo, message, snapshot)
        with patch("workflow.engine.commit", side_effect=fail_backend), self.assertRaisesRegex(WorkflowError, "提交失败"):
            self.fixture.complete(dumps(result))
        first = self.state["commits"]["前端"]
        self.engine.restore(self.state["id"])
        self.state = self.engine.state
        self.state["commit_message"] = "不得覆盖冻结标题"
        self.state["reviews"][-1]["commit_messages"]["后端"] = "不得采用的新消息"
        self.engine.finalize([], "")
        self.assertEqual(self.state["commits"]["前端"], first)
        self.assertEqual(git(self.state["repos"][1]["path"], "log", "-1", "--format=%B"), expected)

    def test_new_review_replaces_old_titles_and_new_followup_clears_frozen_batch(self):
        result = self.prepare()
        with patch.object(self.engine, "finalize"):
            self.fixture.complete(dumps(result))
        self.engine.rereview()
        result["commit_messages"][0].update(type="fix", summary="修正范围检索边界")
        self.fixture.complete(dumps(result))
        self.assertEqual(git(self.state["repos"][0]["path"], "log", "-1", "--format=%s"), "fix(档案检索): 修正范围检索边界")
        self.fixture.send("解释实现", review_enabled=False)
        self.assertIsNone(self.state["commit_batch"])
        self.assertTrue(self.state["deliveries"][-1]["commit_batch"])
