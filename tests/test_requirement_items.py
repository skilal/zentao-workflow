"""三类禅道需求的分页、筛选、资料和工作流入口；不连接外部服务。"""

from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import test_previews as previews
from workflow.commit_messages import prepare_commit_messages
from workflow.settings import ROOT, WorkflowError
from workflow.store import session_title
from workflow.zentao import KINDS, REQUIREMENT_KINDS, ZentaoClient


class RequirementItemsChecks(TestCase):
    def setUp(self):
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        settings["zentao"]["page_size"] = 2
        self.client = ZentaoClient(settings)
        self.client.account, self.client.token = "测试账号", "模拟登录状态"

    def item(self, kind, number=7, **changes):
        return {"id": number, "type": kind, "title": f"{KINDS[kind][0]}示例",
                "assignedTo": {"account": "测试账号"}, "status": "active", "deleted": False,
                "pri": 2, **changes}

    def test_product_and_requirement_pagination_filter_and_deduplicate(self):
        calls = []

        def fetch(path):
            parsed = urlparse(path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            calls.append((parsed.path, query))
            self.assertEqual(query["limit"], "2")
            self.assertEqual(query["status"], "all")
            page = int(query["page"])
            if parsed.path == "api.php/v1/products":
                return {"total": 2, "products": [{"id": page}]}
            self.assertEqual(parsed.path, "api.php/v1/stories")
            self.assertEqual(query["branch"], "all")
            kind = query["type"]
            items = [self.item(kind, project={"id": 41, "name": "示例项目"}),
                     self.item(kind, 8, assignedTo="其他账号"),
                     self.item(kind, 9, status="closed"),
                     self.item(kind, 10, deleted=True),
                     self.item(kind, 11, assignedTo="测试账号")]
            return {"total": len(items), "stories": items[(page - 1) * 2:page * 2]}

        with patch.object(self.client, "_fetch", side_effect=fetch):
            items = self.client.list_requirements()
        self.assertEqual([(item["kind"], item["id"]) for item in items],
                         [(kind, number) for kind in REQUIREMENT_KINDS for number in (7, 11)])
        self.assertEqual(len(calls), 20)
        self.assertEqual([item["project_id"] for item in items[:2]], [41, 0])
        self.assertEqual(items[0]["project_candidates"], [{"id": 41, "name": "示例项目"}])

    def test_each_requirement_kind_uses_product_api(self):
        for kind in REQUIREMENT_KINDS:
            with self.subTest(kind=kind), patch.object(self.client, "_fetch", side_effect=[
                    {"total": 1, "products": [{"id": 1}]},
                    {"total": 1, "stories": [self.item(kind, product=1)]}]) as fetch:
                items = self.client.list_items(kind)
                self.assertEqual(items[0]["kind"], kind)
                self.assertEqual(items[0]["project_id"], 0)
                self.assertEqual(parse_qs(urlparse(fetch.call_args.args[0]).query)["type"], [kind])

    def test_empty_products_are_successful_empty_list(self):
        with patch.object(self.client, "_fetch", return_value={"total": 0, "products": []}) as fetch:
            self.assertEqual(self.client.list_requirements(), [])
        self.assertEqual(fetch.call_count, 1)

    def test_incomplete_or_invalid_product_pages_stop(self):
        responses = ([{"total": 2, "products": [{"id": 1}]}, {"total": 2, "products": [{"id": 1}]}],
                     [{"total": 1, "products": []}], [{"total": 1, "products": {}}],
                     [{"total": 1, "products": [{"id": 0}]}], [{"products": []}])
        for response in responses:
            with self.subTest(response=response), patch.object(self.client, "_fetch", side_effect=response):
                with self.assertRaises(WorkflowError):
                    list(self.client._pages("api.php/v1/products", "products", {}))

    def test_incomplete_requirement_pages_and_type_mismatch_stop(self):
        for pages in ([{"total": 2, "stories": [self.item("epic")]},
                       {"total": 2, "stories": [self.item("epic")]}],
                      [{"total": 1, "stories": [self.item("story")]}],
                      [{"total": 1, "stories": {}}]):
            with self.subTest(pages=pages), patch.object(self.client, "_fetch", side_effect=[
                    {"total": 1, "products": [{"id": 1}]}, *pages]):
                with self.assertRaises(WorkflowError):
                    self.client.list_items("epic")

    def test_api_error_stops_without_retry_or_type_switch(self):
        with patch.object(self.client, "_fetch", side_effect=[
                {"total": 1, "products": [{"id": 1}]}, WorkflowError("权限不足")]) as fetch:
            with self.assertRaisesRegex(WorkflowError, "权限不足"):
                self.client.list_requirements()
        self.assertEqual(fetch.call_count, 2)

    def test_details_preserve_kind_body_verification_and_materials(self):
        for kind in REQUIREMENT_KINDS:
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                detail = self.item(kind, spec='<p>需求正文</p><img src="/index.php?fileID=21">',
                                   verify="验收标准", files=[{"id": 21, "extension": "png", "title": "业务图"}])
                with patch.object(self.client, "_fetch", side_effect=[detail, (b"image", "image/png")]) as fetch:
                    snapshot = self.client.collect({"kind": kind, "id": 7}, Path(directory))
                path = "api.php/v1/stories/7" + (f"?type={kind}" if kind != "story" else "")
                self.assertEqual(fetch.call_args_list[0].args[0], path)
                source, = snapshot["sources"]
                self.assertEqual(source["kind"], kind)
                self.assertTrue(source["selected"])
                self.assertIn("需求正文", source["content"])
                self.assertIn("验收标准", source["content"])
                self.assertEqual(len(snapshot["materials"]), 1)
                self.assertEqual(snapshot["blockers"], [])

    def test_detail_reassignment_and_type_mismatch_stop(self):
        for detail in (self.item("requirement", assignedTo="其他账号"), self.item("story")):
            with self.subTest(detail=detail), patch.object(self.client, "_fetch", return_value=detail):
                with self.assertRaises(WorkflowError):
                    self.client.detail("requirement", 7)

    def test_session_title_and_commit_reference_support_new_kinds(self):
        for kind in ("epic", "requirement"):
            state = {"item": {"kind": kind, "id": 7, "title": "查询规则"},
                     "repos": [{"name": "示例仓库"}], "review_manifest": {"示例仓库/query.py": "示例差异"}}
            self.assertIn(KINDS[kind][0], session_title(state))
            messages = prepare_commit_messages(state, [{"repository": "示例仓库", "type": "fix",
                                                        "scope": "查询", "summary": "修正查询规则"}])
            self.assertIn(f"关联工作项: {KINDS[kind][0]} #7", messages["示例仓库"])


class RequirementWorkflowChecks(TestCase):
    def setUp(self):
        self.fixture = previews.PreviewChecks()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.application = self.fixture.application

    def test_refresh_publishes_all_kinds_together(self):
        flow = self.application.sessions.connection
        requirements = [{"kind": kind, "id": 7, "title": KINDS[kind][0]} for kind in REQUIREMENT_KINDS]
        client = self.application.sessions.zentao
        with patch.object(client, "list_requirements", return_value=requirements) as listed, \
                patch.object(client, "list_items", side_effect=lambda kind: [{"kind": kind, "id": 7}]) as other:
            flow.refresh()
        listed.assert_called_once_with()
        self.assertEqual([call.args[0] for call in other.call_args_list], ["task", "bug"])
        self.assertEqual([item["kind"] for item in self.application.items], list(KINDS))

    def test_new_kinds_can_be_selected_as_preview_and_reject_multiple_selection(self):
        for kind in ("epic", "requirement"):
            item = {"kind": kind, "id": 7, "title": KINDS[kind][0]}
            self.application.items = [item]
            runtime = self.fixture.collect(items=[item])
            self.assertEqual(runtime.engine.state["item"], item)
            self.assertEqual(self.application.sessions.runtimes, {})
        self.application.items = [{"kind": kind, "id": 7} for kind in ("epic", "requirement")]
        with self.assertRaisesRegex(WorkflowError, "仅任务支持多选"):
            self.application.dispatch(self.fixture.request("collect", data={"items": self.application.items}))
