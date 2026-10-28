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

    def test_task_source_bug_still_loads_bug_detail(self):
        self.task["fromBug"] = 7
        bug = {"id": 7, "title": "来源 Bug", "steps": "示例步骤"}
        with patch.object(self.client, "_fetch", side_effect=[self.task, bug]) as request:
            snapshot = self.client.collect(self.item, self.directory)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["api.php/v1/tasks/4228", "api.php/v1/bugs/7"])
        self.assertEqual(snapshot["sources"][-1]["kind"], "bug")

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


if __name__ == "__main__":
    main()
