"""隔离检查禅道列表请求合并、服务端筛选、刷新进度和失败边界。"""

from copy import deepcopy
from threading import Event
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import test_previews as previews
from workflow.settings import WorkflowError
from workflow.web import WebApplication
from workflow.zentao import REQUIREMENT_KINDS


class ZentaoRefreshFixture(TestCase):
    def setUp(self):
        self.fixture = previews.PreviewChecks()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.application = self.fixture.application
        self.flow = self.application.sessions.connection
        self.client = self.application.sessions.zentao
        self.client.account, self.client.token = "测试账号", "模拟登录状态"
        self.client.settings["page_size"] = 2

    def item(self, kind, number, **changes):
        return {"id": number, "type": kind, "title": f"示例{kind}{number}", "name": f"示例任务{number}",
                "assignedTo": "测试账号", "status": "active", "deleted": False, "pri": 2,
                "project": 41, "projectName": "示例项目", **changes}


class AssignedListChecks(ZentaoRefreshFixture):
    def test_combines_first_page_and_only_requests_unfinished_kinds_on_later_pages(self):
        pages = [{"story": {"total": 1, "stories": [self.item("story", 1)]},
                  "task": {"total": 3, "tasks": [self.item("task", 1)]}, "bug": {"total": 0, "bugs": []}},
                 {"task": {"total": 3, "tasks": [self.item("task", 2)]}},
                 {"task": {"total": 3, "tasks": [self.item("task", 3)]}}]
        progress = []
        with patch.object(self.client, "_fetch", side_effect=pages) as fetch:
            items = self.client.list_assigned_items(on_progress=progress.append)
        queries = [parse_qs(urlparse(call.args[0]).query) for call in fetch.call_args_list]
        self.assertEqual([query["fields"] for query in queries], [["story,task,bug"], ["task"], ["task"]])
        self.assertEqual([query["page"] for query in queries], [["1"], ["2"], ["3"]])
        self.assertTrue(all(query["type"] == ["assignedTo"] and query["order"] == ["id_asc"] for query in queries))
        self.assertEqual([(item["kind"], item["id"]) for item in items], [("story", 1), ("task", 1), ("task", 2), ("task", 3)])
        self.assertEqual(len(progress), 3)

    def test_filters_status_assignee_and_deletion_without_affecting_pagination(self):
        pages = [{"bug": {"total": 4, "bugs": [self.item("bug", 1, assignedTo="其他账号"),
                                                self.item("bug", 2, status="closed")]}},
                 {"bug": {"total": 4, "bugs": [self.item("bug", 3, deleted=True), self.item("bug", 4)]}}]
        with patch.object(self.client, "_fetch", side_effect=pages) as fetch:
            items = self.client.list_items("bug")
        self.assertEqual([item["id"] for item in items], [4])
        self.assertEqual(fetch.call_count, 2)

    def test_repeated_pages_and_missing_buckets_stop_without_switching_api(self):
        for pages in ([{"task": {"total": 2, "tasks": [self.item("task", 1)]}},
                       {"task": {"total": 2, "tasks": [self.item("task", 1)]}}], [{}]):
            with self.subTest(pages=pages), patch.object(self.client, "_fetch", side_effect=pages) as fetch:
                with self.assertRaises(WorkflowError):
                    self.client.list_items("task")
                self.assertEqual(fetch.call_count, len(pages))
                self.assertTrue(all(call.args[0].startswith("api.php/v1/user?") for call in fetch.call_args_list))

    def test_research_story_type_must_match_and_login_is_required(self):
        with patch.object(self.client, "_fetch", return_value={"story": {"total": 1, "stories": [self.item("requirement", 1)]}}):
            with self.assertRaisesRegex(WorkflowError, "需求类型与请求不一致"):
                self.client.list_assigned_items(("story",))
        self.client.token = None
        with patch.object(self.client, "_fetch") as fetch:
            with self.assertRaisesRegex(WorkflowError, "请先连接禅道"):
                self.client.list_assigned_items()
            fetch.assert_not_called()


class RefreshFlowChecks(ZentaoRefreshFixture):
    def test_quick_refresh_does_not_scan_products_and_preserves_loaded_requirements(self):
        calls = []
        def fetch(path):
            parsed = urlparse(path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            calls.append((parsed.path, query))
            page, limit = int(query["page"]), int(query["limit"])
            if parsed.path == "api.php/v1/products":
                return {"total": 2, "products": [{"id": 1}, {"id": 2}]}
            if parsed.path == "api.php/v1/user":
                response = {}
                for kind in query["fields"].split(","):
                    items = [self.item(kind, number) for number in ((101, 201) if kind == "story" else (1,))]
                    response[kind] = {"total": len(items), "stories" if kind == "story" else kind + "s": items[(page - 1) * limit:page * limit]}
                return response
            self.assertEqual(parsed.path, "api.php/v1/stories")
            kind, product_id = query["type"], int(query["product"])
            items = [self.item(kind, product_id * 100 + number, assignedTo="测试账号" if number == 1 else "其他账号")
                     for number in range(1, 21)]
            if query["status"] == "assignedtome":
                items = [item for item in items if item["assignedTo"] == "测试账号"]
            return {"total": len(items), "stories": items[(page - 1) * limit:page * limit]}

        with patch.object(self.client, "_fetch", side_effect=fetch):
            # 原设计：下载每个产品三类需求的全部分页，最后读取任务和 Bug。
            for product in self.client._pages("api.php/v1/products", "products", {"status": "all"}):
                for kind in REQUIREMENT_KINDS:
                    list(self.client._pages("api.php/v1/stories", "stories", {"product": product["id"], "type": kind, "status": "all"}))
            self.client.list_items("task")
            self.client.list_items("bug")
            previous_requests = len(calls)
            calls.clear()
            self.flow.refresh()
        self.assertEqual(previous_requests, 63)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["fields"], "story,task,bug")
        self.assertEqual([item["kind"] for item in self.application.items], ["story", "story", "task", "bug"])
        with patch.object(self.client, "_fetch", side_effect=fetch):
            self.flow.refresh(kind="requirement")
            self.assertEqual(len(calls), 4)
            requirement_calls = [query for path, query in calls if path == "api.php/v1/stories"]
            self.assertTrue(all(query["type"] == "requirement" and query["status"] == "assignedtome" for query in requirement_calls))
            self.flow.refresh()
        self.assertEqual(len(calls), 5)
        self.assertEqual([item["kind"] for item in self.application.items], ["requirement", "requirement", "story", "story", "task", "bug"])
        sync = self.application.snapshot()["items_sync"]
        self.assertEqual(sync["status"], "complete")
        self.assertEqual(sync["list_requests"], 1)
        self.assertTrue(self.application.items_loads["requirement"]["loaded"])
        self.assertNotIn("epic", self.application.items_loads)
        self.assertGreaterEqual(sync["elapsed_seconds"], 0)
        self.assertNotIn("Token", str(sync))

    def test_failure_keeps_previous_complete_list_and_reports_failed_stage(self):
        previous = deepcopy(self.application.items)
        with patch.object(self.client, "_fetch", side_effect=WorkflowError("需求读取超时")) as fetch:
            with self.assertRaisesRegex(WorkflowError, "需求读取超时"):
                self.flow.refresh(kind="requirement")
        self.assertEqual(self.application.items, previous)
        self.assertEqual(self.application.items_sync["status"], "failed")
        self.assertEqual(self.application.items_sync["list_requests"], 1)
        self.assertFalse(self.application.items_loads["requirement"]["loaded"])
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(any(b"event: items\n" in packet for _, packet in self.application.events))

    def test_connect_logs_in_once_and_uses_the_same_optimized_refresh(self):
        with patch.object(self.client, "login", return_value={"account": "测试账号"}) as login, \
                patch.object(self.client, "list_assigned_items", return_value=[]) as assigned, \
                patch.object(self.client, "list_requirements", return_value=[]) as requirements:
            self.flow.connect()
        login.assert_called_once()
        assigned.assert_called_once()
        self.assertEqual(assigned.call_args.args, (("story", "task", "bug"),))
        requirements.assert_not_called()
        self.assertTrue(self.application.connected)
        self.assertEqual(self.application.items_sync["status"], "complete")

    def test_repeated_refresh_is_rejected_while_request_is_running(self):
        entered, release = Event(), Event()
        def assigned(kinds, **kwargs):
            kwargs["on_progress"]("正在读取研发需求、任务、Bug：第 1 页")
            entered.set()
            release.wait(5)
            return []
        with patch.object(self.client, "list_assigned_items", side_effect=assigned) as request, \
                patch.object(self.client, "list_requirements", return_value=[]):
            self.application.dispatch(self.fixture.request("refresh"))
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(WorkflowError, "全局操作尚未完成"):
                    self.application.dispatch(self.fixture.request("refresh"))
                self.assertEqual(self.application.snapshot()["items_sync"]["status"], "loading")
                request.assert_called_once()
            finally:
                release.set()
                self.flow.executor.submit(lambda: None).result(timeout=5)
        self.assertEqual(self.application.items_sync["status"], "complete")

    def test_empty_requirement_result_is_cached_until_explicit_refresh(self):
        with patch.object(self.client, "list_requirements", return_value=[]) as request:
            self.application.dispatch(self.fixture.request("refresh", data={"kind": "requirement", "force": False}))
            self.flow.executor.submit(lambda: None).result(timeout=5)
            cached = self.application.dispatch(self.fixture.request("refresh", data={"kind": "requirement", "force": False}))
            self.assertTrue(cached["cached"])
            request.assert_called_once()
            self.application.dispatch(self.fixture.request("refresh", data={"kind": "requirement"}))
            self.flow.executor.submit(lambda: None).result(timeout=5)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args, (("requirement",),))
        self.assertTrue(self.application.snapshot()["items_loads"]["requirement"]["loaded"])

    def test_failed_manual_refresh_keeps_cached_kind_and_other_kinds(self):
        items = [{"kind": "requirement", "id": 7, "project_id": 41, "project_name": "示例项目"}]
        with patch.object(self.client, "list_requirements", return_value=items):
            self.flow.refresh(kind="requirement")
        previous = deepcopy(self.application.items)
        with patch.object(self.client, "list_requirements", side_effect=WorkflowError("刷新失败")):
            with self.assertRaisesRegex(WorkflowError, "刷新失败"):
                self.flow.refresh(kind="requirement")
        self.assertEqual(self.application.items, previous)
        self.assertTrue(self.application.items_loads["requirement"]["loaded"])
        self.assertEqual(self.application.items_loads["requirement"]["status"], "failed")

    def test_unknown_kind_or_invalid_force_does_not_start_work(self):
        for data in ({"kind": "unknown"}, {"kind": None}, {"kind": []}, {"kind": "epic", "force": "true"}):
            with self.subTest(data=data), patch.object(self.flow, "refresh") as refresh:
                with self.assertRaisesRegex(WorkflowError, "工作项分类"):
                    self.application.dispatch(self.fixture.request("refresh", data=data))
                refresh.assert_not_called()
                self.assertIsNone(self.application.busy)

    def test_same_account_reconnect_preserves_cache_but_new_service_starts_unloaded(self):
        with patch.object(self.client, "list_requirements", return_value=[]):
            self.flow.refresh(kind="requirement")
        with patch.object(self.client, "login", return_value={"account": "测试账号"}), \
                patch.object(self.client, "list_assigned_items", return_value=[]), \
                patch.object(self.client, "list_requirements") as requirements:
            self.flow.connect()
        requirements.assert_not_called()
        self.assertTrue(self.application.snapshot()["items_loads"]["requirement"]["loaded"])
        restarted = WebApplication(self.fixture.settings)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.snapshot()["items_loads"], {})

    def test_changing_site_or_account_clears_all_cached_kinds(self):
        with patch.object(self.client, "list_requirements", return_value=[]):
            self.flow.refresh(kind="requirement")
        settings_root = self.fixture.root / "settings-root"
        (settings_root / "prompts").mkdir(parents=True)
        data = {"base_url": "http://example.invalid/zentao", "account": "新账号", "password": "模拟密码",
                "max_fix_rounds": 3, "review_prompt": "模拟审查指令", "model": "示例模型", "reasoning_effort": "high"}
        with patch("workflow.web.ROOT", settings_root), patch("workflow.web.save_settings"), patch("workflow.web.save_password"):
            self.application.update_settings(data)
        self.assertEqual(self.application.items, [])
        self.assertEqual(self.application.items_loads, {})
        self.assertIsNone(self.application.items_sync)
        self.assertFalse(self.application.connected)
