"""图片在禅道正文中的位置、历史恢复及模型输入关联；不连接外部服务。"""

from copy import deepcopy
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from workflow.context import requirement_context
from workflow.settings import ROOT
from workflow.web import public_state
from workflow.zentao import ZentaoClient, description_content, parse_description
from test_workflow_performance import PerformanceFixture


STEPS = ('<p>[步骤]</p><p>打开编辑窗口<img src="/index.php?fileID=200&amp;t=png">继续编辑</p>'
         '<p>[结果]</p><p>当前显示错误<img src="/index.php?fileID=100&amp;t=png"></p>'
         '<p>[期望]</p><p>应当正确显示<img src="/index.php?fileID=200&amp;t=png">保存成功</p>')


class ImageLocationChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = ZentaoClient(loads((ROOT / "settings.example.json").read_text(encoding="utf-8")))
        self.client.account = "测试账号"
        self.client.token = "模拟登录状态"

    def collect(self, data):
        with patch.object(self.client, "_fetch", side_effect=lambda path, **kwargs:
                          (b"simulated image", "image/png") if kwargs.get("binary") else data) as request:
            snapshot = self.client.collect({"kind": "bug", "id": data["id"]}, self.directory)
        return snapshot, request

    def test_bug_sections_keep_original_order_and_each_occurrence(self):
        snapshot, request = self.collect({"id": 7, "assignedTo": "测试账号", "steps": STEPS})
        source = snapshot["sources"][0]
        locations = source["image_locations"]
        self.assertEqual([entry["file_id"] for entry in locations], [200, 100, 200])
        self.assertEqual(request.call_count, 3)  # 详情一次、两张图片各一次。
        self.assertEqual(len(snapshot["materials"]), 2)
        content = source["content"]
        self.assertLess(content.index("[步骤]"), content.index(locations[0]["marker"]))
        self.assertLess(content.index(locations[0]["marker"]), content.index("继续编辑"))
        self.assertLess(content.index("[结果]"), content.index(locations[1]["marker"]))
        self.assertLess(content.index(locations[1]["marker"]), content.index("[期望]"))
        self.assertLess(content.index("[期望]"), content.index(locations[2]["marker"]))
        self.assertLess(content.index(locations[2]["marker"]), content.index("保存成功"))
        self.assertTrue(locations[0]["context_before"].endswith("打开编辑窗口"))
        self.assertTrue(locations[2]["context_after"].startswith("保存成功"))

    def test_spec_and_acceptance_use_distinct_markers_for_the_same_file(self):
        content, locations, _ = description_content({"spec": '<img src="?fileID=8">',
                                                    "verify": '<img src="?fileID=8">'})
        self.assertEqual([entry["field"] for entry in locations], ["spec", "verify"])
        self.assertNotEqual(locations[0]["marker"], locations[1]["marker"])
        for location in locations:
            self.assertIn(location["marker"], content)

    def test_adjacent_images_and_image_only_description_keep_all_positions(self):
        snapshot, _ = self.collect({"id": 7, "assignedTo": "测试账号",
                                    "steps": '<img src="?fileID=200"><img src="?fileID=100">'})
        source = snapshot["sources"][0]
        first, second = source["image_locations"]
        self.assertLess(source["content"].index(first["marker"]), source["content"].index(second["marker"]))
        self.assertEqual(snapshot["blockers"], [])

    def test_different_story_image_positions_are_not_merged(self):
        task = {"id": 1, "assignedTo": "测试账号", "story": 8, "storyVersion": 1,
                "storySpec": '<p>复现<img src="?fileID=200"></p><p>期望<img src="?fileID=100"></p>'}
        other = {**task, "id": 2, "storySpec":
                 '<p>复现<img src="?fileID=100"></p><p>期望<img src="?fileID=200"></p>'}
        with patch.object(self.client, "_fetch", side_effect=lambda path, **kwargs:
                          (b"simulated image", "image/png") if kwargs.get("binary") else
                          {"api.php/v1/tasks/1": task, "api.php/v1/tasks/2": other}[path]):
            snapshot = self.client.collect_many([{"kind": "task", "id": 1}, {"kind": "task", "id": 2}], self.directory)
        stories = [source for source in snapshot["sources"] if source["kind"] == "story"]
        self.assertEqual(len(stories), 2)
        self.assertEqual([[entry["file_id"] for entry in source["image_locations"]] for source in stories],
                         [[200, 100], [100, 200]])

    def test_hidden_images_are_not_treated_as_requirement_screenshots(self):
        content, locations, _ = description_content({"desc": '<script><img src="?fileID=9"></script>'
                                                    '<p>有效正文<img src="?fileID=8"></p>'})
        self.assertEqual([entry["file_id"] for entry in locations], [8])
        self.assertIn("有效正文", content)
        self.assertEqual(parse_description('<p>普通文字<img src="?fileID=8"></p>').text(), "普通文字")

    def test_historical_source_recovers_positions_without_mutating_snapshot(self):
        state = {"snapshot": {"sources": [{"kind": "bug", "id": 7, "content": "旧的纯文字",
                  "material_ids": [100, 200], "raw": {"steps": STEPS}}],
                  "materials": [{"file_id": 200, "image": True, "path": "200.png"},
                                {"file_id": 100, "image": True, "path": "100.png"}]}}
        before = deepcopy(state)
        model = requirement_context(state)["sources"][0]
        visible = public_state(state)["snapshot"]["sources"][0]
        self.assertEqual([entry["path"] for entry in model["image_locations"]],
                         ["200.png", "100.png", "200.png"])
        self.assertIn("不要将复现步骤、实际结果和期望结果中的截图混用", model["image_location_note"])
        self.assertEqual(visible["content"], model["content"])
        self.assertNotIn("raw", model)
        self.assertEqual(state, before)

    def test_unavailable_images_keep_their_location_and_blocker(self):
        snapshot, _ = self.collect({"id": 7, "assignedTo": "测试账号", "steps":
                                  '<p>[期望]</p><img src="https://outside.invalid/picture.png">'})
        source = requirement_context({"snapshot": snapshot})["sources"][0]
        self.assertIn("[期望]", source["image_locations"][0]["context_before"])
        self.assertNotIn("path", source["image_locations"][0])
        self.assertTrue(snapshot["blockers"])


class ImageLocationModelChecks(PerformanceFixture):
    def test_model_can_match_each_occurrence_after_image_content_deduplication(self):
        materials = []
        for file_id in (100, 200):
            path = self.root / f"{file_id}.png"
            path.write_bytes(b"same picture content")
            materials.append({"file_id": file_id, "image": True, "path": str(path)})
        source = self.state["snapshot"]["sources"][0]
        source["raw"] = {"steps": STEPS}
        source["material_ids"] = [100, 200]
        self.state["snapshot"]["materials"] = materials
        self.state["phase"] = "plan"
        self.flow._start_turn("分析复现与期望截图")
        inputs = self.rpc.request.call_args.args[1]["input"]
        self.assertEqual(len([entry for entry in inputs if entry["type"] == "localImage"]), 1)
        references = loads(inputs[0]["text"].split("工作流上下文：\n", 1)[1])["image_references"]
        full = self.tools.call("task_context", {"include_materials": True})
        locations = full["snapshot"]["sources"][0]["image_locations"]
        self.assertEqual(len(locations), 3)
        reference_by_path = {entry["path"]: entry for entry in references}
        self.assertEqual(len({reference_by_path[entry["path"]]["content_hash"] for entry in locations}), 1)
        original_revision = full["materials_revision"]
        source["raw"]["steps"] = STEPS.replace("[期望]", "[验收期望]")
        self.flow.save()
        self.assertNotEqual(original_revision, self.tools.call("task_context", {"include_materials": True})["materials_revision"])
