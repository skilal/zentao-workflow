"""使用脱敏响应检查任务关联资料，不连接禅道或 Codex。"""

from copy import deepcopy
from io import BytesIO
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch
from urllib.error import HTTPError

from workflow.settings import WorkflowError
from workflow.web import public_state
from workflow.zentao import ZentaoClient


ROOT = Path(__file__).resolve().parent.parent


class TaskCollectionChecks(TestCase):
    def setUp(self):
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.client = ZentaoClient(settings)
        self.client.account = "测试账号"
        self.client.token = "模拟登录状态"
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.item = {"kind": "task", "id": 4228, "title": "示例任务"}
        self.task = {"id": 4228, "name": "示例任务", "assignedTo": {"account": "测试账号"},
                     "desc": "完成示例查询功能", "story": 2872, "storyTitle": "示例关联需求",
                     "storyVersion": 1, "latestStoryVersion": 2, "storySpec": "<p>支持范围查询</p>",
                     "storyVerify": "查询结果符合条件", "storyFiles": [], "files": [], "fromBug": 0}

    def test_task_uses_embedded_version_and_downloads_story_image(self):
        self.task["storySpec"] += '<img src="/index.php?m=file&amp;f=read&amp;t=png&amp;fileID=27881">'
        self.task["storyVerify"] += '<a href="https://example.feishu.cn/docx/demo">业务规则</a>'
        def fetch(path, payload=None, binary=False):
            if path == "api.php/v1/tasks/4228":
                return self.task
            self.assertEqual(path, "api.php/v1/files/27881")
            self.assertTrue(binary)
            return b"simulated image", "image/png"
        with patch.object(self.client, "_fetch", side_effect=fetch) as request:
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual(request.call_count, 2)
        task, story = snapshot["sources"]
        self.assertEqual((story["id"], story["title"], story["version"]), (2872, "示例关联需求", 1))
        self.assertEqual(story["origin"], "task")
        self.assertEqual(story["note"], "来自任务详情的关联需求资料")
        self.assertIn("支持范围查询", story["content"])
        self.assertIn("查询结果符合条件", story["content"])
        self.assertNotIn(self.task["desc"], story["content"])
        self.assertNotIn("version", task)
        self.assertEqual(snapshot["blockers"], [])
        self.assertEqual(snapshot["feishu_links"], ["https://example.feishu.cn/docx/demo"])
        self.assertEqual(snapshot["materials"][0]["file_id"], 27881)
        self.assertTrue(Path(snapshot["materials"][0]["path"]).is_file())

    def test_missing_story_content_keeps_task_description(self):
        for description in ("", "<p><br></p>", "   "):
            with self.subTest(description=description):
                self.task.update(storySpec=description, storyVerify="")
                with patch.object(self.client, "_fetch", return_value=self.task) as request:
                    snapshot = self.client.collect(self.item, self.directory)
                request.assert_called_once_with("api.php/v1/tasks/4228")
                self.assertEqual(snapshot["sources"][1]["content"], "")
                self.assertEqual(snapshot["blockers"], [])

    def test_story_description_can_supply_task_with_no_description(self):
        self.task["desc"] = ""
        with patch.object(self.client, "_fetch", return_value=self.task):
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual(snapshot["sources"][0]["content"], "")
        self.assertEqual(snapshot["blockers"], [])

    def test_all_empty_descriptions_require_supplement(self):
        self.task.update(desc="<p><br></p>", storySpec="<p> </p>", storyVerify="")
        with patch.object(self.client, "_fetch", return_value=self.task):
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual(len(snapshot["blockers"]), 1)
        self.assertIn("没有有效描述", snapshot["blockers"][0])

    def test_task_without_story_does_not_create_related_source(self):
        self.task["story"] = 0
        with patch.object(self.client, "_fetch", return_value=self.task) as request:
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual(len(snapshot["sources"]), 1)
        request.assert_called_once_with("api.php/v1/tasks/4228")

    def test_embedded_files_support_list_and_dictionary(self):
        file = {"id": 99, "extension": "txt", "title": "验收说明"}
        for files in ([file], {"99": file}):
            with self.subTest(files=files):
                self.task["storyFiles"] = files
                with patch.object(self.client, "_fetch", side_effect=[self.task, ("脱敏附件内容".encode(), "text/plain")]) as request:
                    snapshot = self.client.collect(self.item, self.directory)
                self.assertEqual(request.call_args_list[1].args, ("api.php/v1/files/99",))
                self.assertEqual(snapshot["materials"][0]["text"], "脱敏附件内容")
                self.assertEqual(snapshot["blockers"], [])

    def test_attachment_failure_remains_a_blocker(self):
        self.task["storyFiles"] = [{"id": 99, "extension": "txt", "title": "验收说明"}]
        with patch.object(self.client, "_fetch", side_effect=[self.task, WorkflowError("模拟附件不可读")]) as request:
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual(request.call_count, 2)
        self.assertIn("请人工补充", snapshot["blockers"][0])

    def test_primary_http_failures_stop_without_retry(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                self.client.token = "模拟登录状态"
                error = HTTPError("http://127.0.0.1/api.php/v1/tasks/4228", status, "模拟错误", {}, BytesIO(b"{}"))
                with patch.object(self.client.opener, "open", side_effect=error) as request:
                    with self.assertRaises(WorkflowError):
                        self.client.collect(self.item, self.directory)
                self.assertEqual(request.call_count, 1)
                if status in (401, 403):
                    self.assertIsNone(self.client.token)

    def test_task_assignment_check_is_preserved(self):
        self.task["assignedTo"] = {"account": "其他账号"}
        with patch.object(self.client, "_fetch", return_value=self.task) as request:
            with self.assertRaisesRegex(WorkflowError, "不再指派"):
                self.client.collect(self.item, self.directory)
        request.assert_called_once()

    def test_standalone_story_and_bug_collection_is_preserved(self):
        for kind, endpoint in (("story", "stories"), ("bug", "bugs")):
            with self.subTest(kind=kind):
                data = {"id": 1, "title": "示例工作项", "assignedTo": "测试账号", "spec": "示例内容"}
                with patch.object(self.client, "_fetch", return_value=data) as request:
                    snapshot = self.client.collect({"kind": kind, "id": 1}, self.directory)
                request.assert_called_once_with(f"api.php/v1/{endpoint}/1")
                self.assertEqual(snapshot["sources"][0]["kind"], kind)
                self.assertNotIn("origin", snapshot["sources"][0])

    def test_bug_related_story_still_uses_story_endpoint(self):
        bug = {"id": 1, "assignedTo": "测试账号", "steps": "示例步骤", "story": 2}
        story = {"id": 2, "title": "示例需求", "spec": "示例规则"}
        with patch.object(self.client, "_fetch", side_effect=[bug, story]) as request:
            snapshot = self.client.collect({"kind": "bug", "id": 1}, self.directory)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["api.php/v1/bugs/1", "api.php/v1/stories/2"])
        self.assertEqual(len(snapshot["sources"]), 2)

    def test_task_source_bug_preserves_related_story_and_materials(self):
        self.task["fromBug"] = 7
        self.task["bugSteps"] = "示例步骤"
        for key in ("storySpec", "storyVerify", "storyFiles"):
            self.task.pop(key)
        story = {"id": 2872, "title": "需求接口标题", "version": 2,
                 "spec": '范围包含边界值<img src="/index.php?m=file&amp;f=read&amp;t=png&amp;fileID=100">',
                 "verify": "空范围不增加筛选条件",
                 "files": {"99": {"id": 99, "extension": "txt", "title": "验收说明"}}}
        bug = {"id": 7, "title": "来源 Bug", "steps": "示例步骤"}
        responses = [self.task, story, bug, ("脱敏验收说明".encode(), "text/plain"), (b"simulated image", "image/png")]
        with patch.object(self.client, "_fetch", side_effect=responses) as request:
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ["api.php/v1/tasks/4228", "api.php/v1/stories/2872", "api.php/v1/bugs/7",
                          "api.php/v1/files/99", "api.php/v1/files/100"])
        related = snapshot["sources"][1]
        self.assertEqual(related["title"], "需求接口标题")
        self.assertIn("范围包含边界值", related["content"])
        self.assertIn("空范围不增加筛选条件", related["content"])
        self.assertNotIn("origin", related)
        self.assertNotIn("version", related)
        self.assertNotIn("note", related)
        self.assertEqual(snapshot["sources"][-1]["kind"], "bug")
        self.assertEqual(snapshot["blockers"], [])
        self.assertEqual(snapshot["materials"][0]["text"], "脱敏验收说明")
        self.assertTrue(snapshot["materials"][1]["image"])
        self.assertTrue(all(Path(material["path"]).is_file() for material in snapshot["materials"]))

    def test_task_source_bug_without_story_only_loads_bug(self):
        self.task.update(fromBug=7, story=0)
        for key in ("storySpec", "storyVerify", "storyFiles"):
            self.task.pop(key)
        bug = {"id": 7, "title": "来源 Bug", "steps": "示例步骤"}
        with patch.object(self.client, "_fetch", side_effect=[self.task, bug]) as request:
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["api.php/v1/tasks/4228", "api.php/v1/bugs/7"])
        self.assertEqual([source["kind"] for source in snapshot["sources"]], ["task", "bug"])

    def test_task_source_bug_story_failure_stops_collection(self):
        self.task["fromBug"] = 7
        for key in ("storySpec", "storyVerify", "storyFiles"):
            self.task.pop(key)
        with patch.object(self.client, "_fetch", side_effect=[self.task, WorkflowError("模拟关联需求不可读")]) as request:
            with self.assertRaisesRegex(WorkflowError, "模拟关联需求不可读"):
                self.client.collect(self.item, self.directory)
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ["api.php/v1/tasks/4228", "api.php/v1/stories/2872"])

    def test_public_snapshot_preserves_metadata_and_old_snapshots(self):
        with patch.object(self.client, "_fetch", return_value=self.task):
            snapshot = self.client.collect(self.item, self.directory)
        current = public_state({"snapshot": snapshot})
        self.assertEqual(current["snapshot"]["sources"][1]["version"], 1)
        self.assertEqual(current["snapshot"]["sources"][1]["origin"], "task")
        self.assertNotIn("raw", current["snapshot"]["sources"][1])
        old = deepcopy(snapshot)
        for key in ("origin", "version", "note"):
            old["sources"][1].pop(key)
        self.assertEqual(len(public_state({"snapshot": old})["snapshot"]["sources"]), 2)

    def test_multiple_tasks_share_story_and_download_each_attachment_once(self):
        second = {**self.task, "id": 4229, "name": "示例前端任务"}
        file = {"id": 99, "extension": "txt", "title": "共同验收标准"}
        for task in (self.task, second):
            task.update(project={"id": 41, "name": "示例项目"}, storyFiles=[file])
        items = [self.item, {"kind": "task", "id": 4229, "title": second["name"]}]
        def fetch(path, payload=None, binary=False):
            if path == "api.php/v1/files/99":
                return "共同验收标准".encode(), "text/plain"
            return {"api.php/v1/tasks/4228": self.task, "api.php/v1/tasks/4229": second}[path]
        with patch.object(self.client, "_fetch", side_effect=fetch) as request:
            snapshot = self.client.collect_many(items, self.directory)
        self.assertEqual(snapshot["items"], items)
        self.assertEqual([source["id"] for source in snapshot["sources"] if source["selected"]], [4228, 4229])
        self.assertEqual(len([source for source in snapshot["sources"] if source["kind"] == "story"]), 1)
        self.assertEqual(len(snapshot["materials"]), 1)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(snapshot["project"]["id"], 41)

    def test_multiple_tasks_keep_different_story_versions_and_contents(self):
        second = {**self.task, "id": 4229, "storyVersion": 2, "storySpec": "新增前端交互规则"}
        items = [self.item, {"kind": "task", "id": 4229, "title": "前端任务"}]
        with patch.object(self.client, "_fetch", side_effect=[self.task, second]):
            snapshot = self.client.collect_many(items, self.directory)
        stories = [source for source in snapshot["sources"] if source["kind"] == "story"]
        self.assertEqual([source["version"] for source in stories], [1, 2])
        self.assertIn("新增前端交互规则", stories[1]["content"])
        second["storyVersion"] = 1
        with patch.object(self.client, "_fetch", side_effect=[self.task, second]):
            snapshot = self.client.collect_many(items, self.directory)
        self.assertEqual(len([source for source in snapshot["sources"] if source["kind"] == "story"]), 2)

    def test_multiple_tasks_require_common_project_and_check_each_assignee(self):
        self.task["project"] = {"id": 41, "name": "甲项目"}
        second = {**self.task, "id": 4229, "project": {"id": 42, "name": "乙项目"}}
        items = [self.item, {"kind": "task", "id": 4229, "title": "前端任务"}]
        with patch.object(self.client, "_fetch", side_effect=[self.task, second]):
            with self.assertRaisesRegex(WorkflowError, "共同的所属项目"):
                self.client.collect_many(items, self.directory)
        second.update(project=self.task["project"], assignedTo="其他账号")
        with patch.object(self.client, "_fetch", side_effect=[self.task, second]):
            with self.assertRaisesRegex(WorkflowError, "不再指派"):
                self.client.collect_many(items, self.directory)

    def test_multiple_stories_keep_images_with_their_own_source(self):
        self.task["storySpec"] += '<img src="/index.php?m=file&amp;fileID=100&amp;t=png">'
        second = {**self.task, "id": 4229, "story": 2873, "storyTitle": "另一个需求",
                  "storySpec": '<p>另一个需求的正文</p><img src="/index.php?m=file&amp;fileID=101&amp;t=png">'}
        items = [self.item, {"kind": "task", "id": 4229, "title": "另一个任务"}]
        def fetch(path, payload=None, binary=False):
            if binary:
                return b"simulated image", "image/png"
            return {"api.php/v1/tasks/4228": self.task, "api.php/v1/tasks/4229": second}[path]
        with patch.object(self.client, "_fetch", side_effect=fetch):
            snapshot = self.client.collect_many(items, self.directory)
        sources = {(source["kind"], source["id"]): source for source in snapshot["sources"]}
        self.assertEqual(sources[("task", 4228)]["material_ids"], [])
        self.assertEqual(sources[("task", 4229)]["material_ids"], [])
        self.assertEqual(sources[("story", 2872)]["material_ids"], [100])
        self.assertEqual(sources[("story", 2873)]["material_ids"], [101])

    def test_same_story_text_with_different_images_is_not_merged(self):
        self.task["storySpec"] += '<img src="/index.php?fileID=100&amp;t=png">'
        second = {**self.task, "id": 4229, "storySpec": self.task["storySpec"].replace("100", "101")}
        items = [self.item, {"kind": "task", "id": 4229, "title": "另一个任务"}]
        with patch.object(self.client, "_fetch", side_effect=[self.task, second,
                (b"first image", "image/png"), (b"second image", "image/png")]):
            snapshot = self.client.collect_many(items, self.directory)
        stories = [source for source in snapshot["sources"] if source["kind"] == "story"]
        self.assertEqual([source["material_ids"] for source in stories], [[100], [101]])

    def test_old_snapshots_restore_source_materials_without_changing_saved_data(self):
        state = {"snapshot": {"sources": [
            {"kind": "story", "id": 1, "raw": {"spec": '<img src="/index.php?fileID=100&amp;t=png">',
                "files": {"99": {"id": 99}}, "verify": '<img src="/index.php?fileID=100&amp;t=png">'}},
            {"kind": "story", "id": 2, "raw": {"spec": '<img src="/index.php?fileID=101&amp;t=png">'}},
        ]}}
        result = public_state(state)
        self.assertEqual([source["material_ids"] for source in result["snapshot"]["sources"]], [[99, 100], [101]])
        self.assertTrue(all("raw" not in source for source in result["snapshot"]["sources"]))
        self.assertTrue(all("material_ids" not in source for source in state["snapshot"]["sources"]))


if __name__ == "__main__":
    main()
