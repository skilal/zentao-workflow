"""精确比较兼容性、重复行片段编号顺序及扫描量检查。"""

from difflib import SequenceMatcher
from random import Random
from unittest import TestCase
from unittest.mock import Mock, patch

import test_auto_delivery as delivery
from workflow.diff_index import line_opcodes, occurrence_counts
from workflow.engine import ACTIVE


class DiffIndexChecks(TestCase):
    def test_matching_keeps_original_alignment_and_invalidates_changed_content(self):
        random = Random(37)
        for _ in range(150):
            before = [random.choice(("重复\n", "}\n", "\n", "业务\n")) for _ in range(random.randrange(50))]
            after = before[:]
            start = random.randrange(len(after) + 1)
            after[start:start + random.randrange(5)] = ["修改\n"] * random.randrange(5)
            expected = tuple(SequenceMatcher(None, before, after, autojunk=False).get_opcodes())
            self.assertEqual(line_opcodes(before, after), expected)
            after.append("下一次修改\n")
            self.assertEqual(line_opcodes(before, after), tuple(SequenceMatcher(None, before, after, autojunk=False).get_opcodes()))
        self.assertEqual(line_opcodes([], []), ())

    def test_occurrence_ordinals_match_repeated_empty_and_deleted_windows(self):
        random = Random(73)
        for _ in range(100):
            source = [random.choice(("重复\n", "}\n", "\n")) for _ in range(random.randrange(35))]
            windows = []
            for _ in range(20):
                start = random.randrange(len(source) + 1)
                windows.append((start, random.randrange(start, len(source) + 1)))
            expected = []
            for start, end in windows:
                width = end - start
                context = [source[max(0, start - 2):start], source[end:end + 2]]
                expected.append(sum(source[position:position + width] == source[start:end]
                                    and [source[max(0, position - 2):position], source[position + width:position + width + 2]] == context
                                    for position in range(start)))
            self.assertEqual(occurrence_counts(source, windows), expected)
        self.assertEqual(occurrence_counts([], [(0, 0)]), [0])
        self.assertEqual(occurrence_counts([], []), [])

    def test_large_repeated_file_uses_linear_number_of_slice_reads(self):
        class CountedLines(list):
            slice_reads = 0

            def __getitem__(self, key):
                if isinstance(key, slice):
                    self.slice_reads += 1
                return super().__getitem__(key)

        size = 20000
        source = CountedLines(["重复行\n"] * size)
        result = occurrence_counts(source, [(index, index + 1) for index in range(size)])
        self.assertEqual(result[2:7], [0, 1, 2, 3, 4])
        self.assertLessEqual(source.slice_reads, size * 3)


class DifferenceStageChecks(TestCase):
    def setUp(self):
        self.fixture = delivery.AutoDeliveryChecks()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.flow, self.state = self.fixture.flow, self.fixture.state
        self.state.update(phase="implement", stage="实施中", artifacts=[{"delivery_round": 1}])
        self.flow._after_compile = Mock()
        self.flow._begin_compile = Mock()

    def test_stage_is_visible_before_expensive_comparison(self):
        def compare(_):
            self.assertEqual(self.state["stage"], "核对改动中")
            saved = self.flow.store.load(self.state["id"])
            self.assertEqual(saved["stage"], "核对改动中")
            return {"前端/页面.js": "版本"}

        with patch("workflow.compile_flow.manifest", side_effect=compare):
            self.flow._before_review()
        self.flow._after_compile.assert_called_once()
        self.assertIn("核对改动中", ACTIVE)

    def test_pause_during_comparison_does_not_start_checks_or_review(self):
        self.state["compile_check"] = {"enabled": True}

        def compare(_):
            self.state["pause_requested"] = True
            return {"后端/业务.java": "版本"}

        with patch("workflow.compile_flow.manifest", side_effect=compare):
            self.flow._before_review()
        self.assertEqual(self.state["stage"], "已暂停")
        self.flow._after_compile.assert_not_called()
        self.flow._begin_compile.assert_not_called()

    def test_direct_review_shows_comparison_stage_before_review_checks(self):
        self.flow._start_turn = Mock()

        def check():
            self.assertEqual(self.state["stage"], "核对改动中")

        with patch.object(self.flow, "_guard_compile", side_effect=check) as guard:
            self.flow._begin_review()
        guard.assert_called_once()
        self.flow._start_turn.assert_called_once()
