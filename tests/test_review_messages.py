"""审查输出协议、消息关联与历史兼容性检查，使用模拟结果。"""

from copy import deepcopy
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.engine import Workflow, parse_review
from workflow.settings import ROOT, WorkflowError
from workflow.web import public_state


def review_result():
    return {"summary": "需要修复范围检索", "findings": [{
        "title": "组合条件遗漏", "category": "业务链路问题", "origin": "原有问题",
        "severity": "P2", "previous_id": "", "repository": "后端", "path": "目录/检索.java", "line": 491,
        "evidence": "分组结果缺少当前条件", "suggestion": "补齐当前条件", "impact": "查询范围不准确"
    }], "resolved_previous": []}


class ReviewMessageChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patched = patch("workflow.store.LOCAL", Path(temporary.name))
        patched.start()
        self.addCleanup(patched.stop)
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.flow = Workflow(settings, Mock())
        self.addCleanup(self.flow.close)
        self.state = self.flow.store.new({"kind": "task", "id": 7, "title": "审查消息"})
        self.flow.state = self.state
        self.state.update(phase="review", stage="审查中", review_manifest={}, review_prompt_hash="版本", max_fix_rounds=3)

    def complete(self, text):
        self.flow.last_message = text
        with patch("workflow.engine.manifest", return_value={}), patch.object(self.flow, "finalize") as finalize:
            self.flow._completed({"status": "completed"}, self.state["id"], self.flow.connection_epoch)
        return finalize

    def test_json_and_single_fenced_block_share_the_same_protocol(self):
        source = review_result()
        text = dumps(source, ensure_ascii=False)
        for value in (text, f"```json\n{text}\n```", f"```\r\n{text}\r\n```"):
            with self.subTest(value=value):
                self.assertEqual(parse_review(value), source)

    def test_malformed_and_non_schema_results_are_rejected(self):
        invalid = ["说明\n" + dumps(review_result()), "```json\n{}\n```\n附加说明", "{", "[]", "null"]
        for field, value in (("line", True), ("line", "491"), ("origin", "未知"), ("severity", "P9"),
                             ("evidence", ""), ("path", []), ("previous_id", None)):
            source = review_result()
            source["findings"][0][field] = value
            invalid.append(dumps(source))
        source = review_result()
        del source["findings"][0]["suggestion"]
        invalid.append(dumps(source))
        source = review_result()
        source["resolved_previous"] = ["R01-01"]
        invalid.append(dumps(source))
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(WorkflowError):
                parse_review(text)

    def test_valid_review_keeps_original_text_and_message_index(self):
        self.flow.note("用户", "进行审查")
        text = "```json\n" + dumps(review_result(), ensure_ascii=False) + "\n```"
        finalize = self.complete(text)
        self.assertEqual(self.state["stage"], "等待修复选择")
        review = self.state["reviews"][-1]
        self.assertEqual(review["history_index"], 1)
        self.assertEqual(self.state["history"][1]["text"], text)
        self.assertEqual(self.state["history"][1]["phase"], "review")
        self.assertEqual(review["result"]["findings"][0]["id"], "R01-01")
        finalize.assert_not_called()
        self.assertEqual(self.flow.store.load(self.state["id"])["reviews"], self.state["reviews"])

    def test_invalid_review_never_creates_selectable_results(self):
        with self.assertRaises(WorkflowError):
            self.complete("```json\n{}\n```")
        self.assertEqual(self.state["reviews"], [])
        self.assertEqual(len(self.state["history"]), 1)
        self.assertNotIn("review_output", public_state(self.state)["history"][0])

    def test_cross_delivery_reference_stops_but_displays_readonly_result(self):
        old = review_result()
        old["findings"][0]["id"] = "R01-01"
        self.state.update(delivery_round=4, reviews=[{"number": 1, "delivery_round": 1, "result": old}])
        source = review_result()
        source["findings"][0]["previous_id"] = "R01-01"
        with self.assertRaisesRegex(WorkflowError, "本轮交付中不存在的旧问题 R01-01") as raised:
            self.complete(dumps(source, ensure_ascii=False))
        self.state.update(stage="已停止", error=str(raised.exception))
        original = deepcopy(self.state)
        payload = public_state(self.state)
        self.assertEqual(len(payload["reviews"]), 1)
        self.assertEqual(self.flow.current_reviews(), [])
        self.assertEqual(payload["history"][-1]["review_output"], source)
        self.assertEqual(payload["history"][-1]["review_error"], self.state["error"])
        self.assertNotIn("id", payload["history"][-1]["review_output"]["findings"][0])
        self.assertEqual(self.state, original)

    def test_valid_review_is_not_duplicated_as_an_unvalidated_output(self):
        self.complete(dumps(review_result()))
        payload = public_state(self.state)
        self.assertEqual(payload["reviews"][0]["history_index"], 0)
        self.assertNotIn("review_output", payload["history"][0])

    def test_failed_fenced_review_is_preserved_when_later_reviews_succeed(self):
        invalid = review_result()
        invalid["findings"][0]["previous_id"] = "R99-01"
        with self.assertRaises(WorkflowError):
            self.complete("```json\n" + dumps(invalid) + "\n```")
        self.complete(dumps(review_result()))
        payload = public_state(self.state)
        self.assertEqual(payload["history"][0]["review_output"], invalid)
        self.assertIn("不能用于修复或提交", payload["history"][0]["review_error"])
        self.assertNotIn("review_output", payload["history"][1])

    def test_empty_review_still_uses_existing_auto_finalize(self):
        source = review_result()
        source["findings"] = []
        finalize = self.complete(dumps(source))
        finalize.assert_called_once_with([], "", automatic=True)
        self.assertEqual(self.state["reviews"][0]["history_index"], 0)

    def test_legacy_association_uses_saved_result_and_keeps_merged_old_findings(self):
        source = review_result()
        wrong = deepcopy(source)
        wrong["summary"] = "另一段尚未入库的输出"
        self.flow.note("Codex", dumps(wrong))
        self.complete(dumps(source))
        first = self.state["reviews"][0]
        first.pop("history_index")
        self.state["stage"] = "审查中"
        next_result = deepcopy(source)
        next_result["summary"] = "继续保留旧问题"
        next_result["findings"] = []
        self.complete(dumps(next_result))
        self.state["reviews"][1].pop("history_index")
        original = deepcopy(self.state)
        payload = public_state(self.state)
        self.assertEqual([entry["history_index"] for entry in payload["reviews"]], [1, 2])
        self.assertEqual(len(payload["reviews"][1]["result"]["findings"]), 1)
        self.assertEqual(self.state, original)

    def test_unmatched_legacy_text_cannot_become_a_review_card(self):
        self.complete(dumps(review_result()))
        self.state["reviews"][0].pop("history_index")
        self.state["history"][0]["text"] = "这是一条普通说明"
        self.assertNotIn("history_index", public_state(self.state)["reviews"][0])
