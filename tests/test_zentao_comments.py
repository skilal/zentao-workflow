"""禅道评论、截图与已完成工作项读取；外部服务全部使用模拟响应。"""

from copy import deepcopy
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import test_previews as previews
from workflow.context import requirement_context
from workflow.settings import ROOT, WorkflowError
from workflow.web import public_state
from workflow.zentao import ZentaoClient, description_content


class CommentCollectionChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = ZentaoClient(loads((ROOT / "settings.example.json").read_text(encoding="utf-8")))
        self.client.account, self.client.token = "测试账号", "模拟登录状态"
        self.comment = {"id": 101, "actor": "张三", "date": "2026-11-18 10:30:00", "action": "commented",
                        "comment": '<p>修改保存逻辑<img src="/index.php?fileID=21&amp;t=png">参照截图</p>'}

    def test_all_kinds_collect_comments_and_images_in_model_context(self):
        for kind in ("task", "bug", "story", "requirement", "epic"):
            with self.subTest(kind=kind):
                data = {"id": 7, "type": kind, "title": "示例工作项", "assignedTo": "测试账号", "status": "done",
                        "desc": "原始描述", "actions": [self.comment, {"id": 102, "action": "closed", "comment": "关闭时的说明"}]}
                with patch.object(self.client, "_fetch", side_effect=[data, (b"image", "image/png")]) as fetch:
                    snapshot = self.client.collect({"kind": kind, "id": 7}, self.directory)
                source = requirement_context({"snapshot": snapshot})["sources"][0]
                for text in ("张三", "2026-11-18 10:30:00", "修改保存逻辑", "参照截图", "关闭时的说明"):
                    self.assertIn(text, source["content"])
                location, = source["image_locations"]
                self.assertEqual(location["field"], "comment:101")
                self.assertIn("评论图片", location["marker"])
                self.assertTrue(Path(location["path"]).is_file())
                self.assertEqual(source["material_ids"], [21])
                self.assertEqual(fetch.call_count, 2)
                self.assertNotIn("raw", public_state({"snapshot": snapshot})["snapshot"]["sources"][0])

    def test_comment_images_share_download_but_keep_each_position(self):
        data = {"id": 7, "assignedTo": "测试账号", "desc": '<img src="?fileID=21&amp;t=png">',
                "actions": {"101": self.comment, "102": {**self.comment, "id": 102,
                    "actor": {"realname": "李四", "account": "lisi"}, "comment": '<img src="/file-read-21.png">'}}}
        with patch.object(self.client, "_fetch", side_effect=[data, (b"image", "image/png")]) as fetch:
            snapshot = self.client.collect({"kind": "bug", "id": 7}, self.directory)
        source = snapshot["sources"][0]
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([entry["field"] for entry in source["image_locations"]], ["desc", "comment:101", "comment:102"])
        self.assertIn("李四", source["content"])
        self.assertEqual(len(snapshot["materials"]), 1)

    def test_related_story_keeps_bound_version_body_and_reads_current_comments_once(self):
        task = {"id": 7, "assignedTo": "测试账号", "story": 9, "storyVersion": 1,
                "storySpec": "任务绑定的旧版需求正文"}
        story = {"id": 9, "spec": "当前新版正文不应替换旧版", "actions": [self.comment]}
        other = {**task, "id": 8}
        with patch.object(self.client, "_fetch", side_effect=[task, story, other, (b"image", "image/png")]) as fetch:
            snapshot = self.client.collect_many([{"kind": "task", "id": 7}, {"kind": "task", "id": 8}], self.directory)
        related, = [source for source in snapshot["sources"] if source["kind"] == "story"]
        self.assertEqual(related["version"], 1)
        self.assertIn("任务绑定的旧版需求正文", related["content"])
        self.assertIn("修改保存逻辑", related["content"])
        self.assertNotIn("当前新版正文", related["content"])
        self.assertEqual([call.args[0] for call in fetch.call_args_list].count("api.php/v1/stories/9"), 1)

    def test_read_by_id_allows_completed_or_reassigned_item_only_when_explicit(self):
        for kind in ("task", "bug", "story", "requirement", "epic"):
            data = {"id": 7, "type": kind, "name": "已完成工作项", "assignedTo": "其他账号", "status": "closed", "actions": [
                {**self.comment, "comment": "后续修改说明"}]}
            with self.subTest(kind=kind), patch.object(self.client, "_fetch", return_value=data):
                with self.assertRaisesRegex(WorkflowError, "不再指派"):
                    self.client.collect({"kind": kind, "id": 7}, self.directory)
                snapshot = self.client.collect_many([{"kind": kind, "id": 7}], self.directory, require_assignee=False)
                self.assertIn("后续修改说明", snapshot["sources"][0]["content"])
                self.assertEqual(snapshot["items"][0]["title"], "已完成工作项")

    def test_related_comment_permission_failure_stops_without_retry(self):
        task = {"id": 7, "assignedTo": "测试账号", "story": 9, "storyVersion": 1, "storySpec": "任务关联正文"}
        with patch.object(self.client, "_fetch", side_effect=[task, WorkflowError("关联需求无权读取")]) as fetch:
            with self.assertRaisesRegex(WorkflowError, "关联需求无权读取"):
                self.client.collect({"kind": "task", "id": 7}, self.directory)
        self.assertEqual(fetch.call_count, 2)

    def test_invalid_comments_stop_and_inaccessible_images_remain_blockers(self):
        for actions in ("错误协议", ["错误记录"], [{"comment": {"text": "错误内容"}}]):
            with self.subTest(actions=actions), self.assertRaises(WorkflowError):
                description_content({"actions": actions})
        for html in ('<img src="https://outside.invalid/image.png">', '<img src="/unknown.png">',
                     '<img src="?fileID=21&amp;t=png">'):
            data = {"id": 7, "assignedTo": "测试账号", "actions": [{**self.comment, "comment": html}]}
            with patch.object(self.client, "_fetch", side_effect=[data, WorkflowError("图片无权限")]):
                snapshot = self.client.collect({"kind": "bug", "id": 7}, self.directory)
            self.assertTrue(snapshot["blockers"])
            self.assertIn("评论图片", snapshot["sources"][0]["content"])

    def test_old_snapshot_recovers_comment_image_positions_and_ownership(self):
        state = {"snapshot": {"sources": [{"kind": "task", "id": 7, "content": "旧的纯文本", "raw": {"actions": [self.comment]}}],
                 "materials": [{"file_id": 21, "path": "21.png", "image": True}]}}
        before = deepcopy(state)
        source = requirement_context(state)["sources"][0]
        self.assertEqual(source["material_ids"], [21])
        self.assertEqual(source["image_locations"][0]["path"], "21.png")
        self.assertEqual(state, before)


class CommentWebChecks(TestCase):
    def setUp(self):
        fixture = previews.PreviewChecks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.application = fixture.application
        self.request = fixture.request
        self.drain = fixture.drain
        self.assert_no_sessions = fixture.assert_no_sessions

    def test_explicit_by_id_is_not_limited_to_pending_list(self):
        client = self.application.sessions.zentao
        client.account, client.token = "测试账号", "模拟登录状态"
        data = {"id": 99, "title": "已关闭Bug", "assignedTo": "其他账号", "status": "closed", "actions": [
            {"id": 3, "actor": "张三", "date": "2026-11-18", "comment": "关闭后新增的说明"}]}
        with patch.object(client, "_fetch", return_value=data):
            result = self.application.dispatch(self.request("collect_by_id", data={"items": [{"kind": "bug", "id": 99}]}))
            runtime = self.application.sessions.get_preview(result["preview_id"])
            self.drain(runtime)
        state = runtime.engine.state
        self.assertEqual(state["stage"], "资料已读取")
        self.assertEqual(state["item"]["title"], "已关闭Bug")
        self.assertIn("关闭后新增的说明", state["snapshot"]["sources"][0]["content"])
        self.assert_no_sessions()
        with self.assertRaises(WorkflowError):
            self.application.dispatch(self.request("collect", data={"items": [{"kind": "bug", "id": 99}]}))

    def test_by_id_rejects_invalid_types_ids_and_multiple_items(self):
        for items in ([{"kind": "other", "id": 7}], [{"kind": "bug", "id": 0}], [{"kind": "bug", "id": True}],
                      [{"kind": "bug", "id": 7}, {"kind": "task", "id": 8}]):
            with self.subTest(items=items), self.assertRaises(WorkflowError):
                self.application.dispatch(self.request("collect_by_id", data={"items": items}))

    def test_completed_session_refresh_preserves_thread_repositories_and_failure_snapshot(self):
        state = self.application.sessions.store.new({"kind": "task", "id": 7, "title": "历史任务"})
        state.update(phase="complete", stage="已完成（静态审查）", thread_id="原线程", repos=[{"name": "前端", "baseline": "原基线", "path": "D:/示例仓库", "workspace_owned": False,
                     "workspace_release": {"status": "released"}}],
                     snapshot={"sources": [], "materials": [], "blockers": []})
        runtime = self.application.sessions.add(state)
        runtime.engine.save()
        client = self.application.sessions.zentao
        client.account, client.token = "测试账号", "模拟登录状态"
        detail = {"id": 7, "assignedTo": "其他账号", "desc": "最新正文", "actions": [{"id": 3, "comment": "最新评论"}]}
        request = {"action": "refresh_materials", "request_id": uuid4().hex, "session_id": state["id"], "revision": runtime.revision, "data": {}}
        with patch.object(client, "_fetch", return_value=detail):
            self.application.dispatch(request)
            self.drain(runtime)
        self.assertEqual(state["stage"], "已完成（静态审查）")
        self.assertEqual(state["thread_id"], "原线程")
        self.assertEqual(state["repos"][0]["baseline"], "原基线")
        self.assertIn("最新评论", state["snapshot"]["sources"][0]["content"])
        saved = deepcopy(state["snapshot"])
        with patch.object(client, "_fetch", side_effect=WorkflowError("无权限")), self.assertRaises(WorkflowError):
            runtime.engine.refresh_materials()
        self.assertEqual(state["snapshot"], saved)
        with self.assertRaisesRegex(WorkflowError, "版本已过期"):
            self.application.dispatch({**request, "request_id": uuid4().hex})
        state.update(stage="实施中", phase="implement")
        with self.assertRaises(WorkflowError):
            runtime.engine.refresh_materials()
