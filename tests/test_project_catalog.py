"""验证可选禅道项目的分页、权限及本地读取接口。"""

from http.client import HTTPConnection
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from workflow.settings import ROOT, WorkflowError
from workflow.web import LocalServer, WebApplication
from workflow.zentao import ZentaoClient


class ProjectCatalogChecks(TestCase):
    def setUp(self):
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.client = ZentaoClient(self.settings)
        self.client.account, self.client.token = "示例账号", "模拟登录"

    def test_catalog_reads_all_pages_and_preserves_same_name_projects(self):
        pages = [{"projects": [{"id": 41, "name": "同名项目"}], "total": 2},
                 {"projects": [{"id": 42, "name": "同名项目"}], "total": 2}]
        with patch.object(self.client, "_fetch", side_effect=pages) as fetch:
            projects = self.client.list_projects()
        self.assertEqual(projects, [{"id": 41, "name": "同名项目"}, {"id": 42, "name": "同名项目"}])
        queries = [parse_qs(urlparse(call.args[0]).query) for call in fetch.call_args_list]
        self.assertEqual([query["page"] for query in queries], [["1"], ["2"]])
        self.assertTrue(all(query["status"] == ["all"] for query in queries))

    def test_empty_catalog_and_incomplete_pagination_are_distinct(self):
        with patch.object(self.client, "_fetch", return_value={"projects": [], "total": 0}):
            self.assertEqual(self.client.list_projects(), [])
        with patch.object(self.client, "_fetch", side_effect=[
                {"projects": [{"id": 41, "name": "示例项目"}], "total": 2},
                {"projects": [{"id": 41, "name": "示例项目"}], "total": 2}]) as fetch:
            with self.assertRaisesRegex(WorkflowError, "分页提前结束"):
                self.client.list_projects()
            self.assertEqual(fetch.call_count, 2)

    def test_login_or_permission_failure_does_not_retry_or_save_a_partition(self):
        self.client.token = None
        with patch.object(self.client, "_fetch") as fetch:
            with self.assertRaisesRegex(WorkflowError, "请先连接禅道"):
                self.client.list_projects()
            fetch.assert_not_called()
        self.client.token = "模拟登录"
        with patch.object(self.client, "_fetch", side_effect=WorkflowError("没有项目权限")) as fetch:
            with self.assertRaisesRegex(WorkflowError, "没有项目权限"):
                self.client.list_projects()
            fetch.assert_called_once()

    def test_malformed_catalog_is_rejected(self):
        for data in ({"projects": {}, "total": 0}, {"projects": [], "total": "2"},
                     {"projects": [{"id": 0, "name": "示例项目"}], "total": 1},
                     {"projects": [{"id": 41, "name": " "}], "total": 1}):
            with self.subTest(data=data), patch.object(self.client, "_fetch", return_value=data):
                with self.assertRaises(WorkflowError):
                    self.client.list_projects()

    def test_http_catalog_requires_cookie_and_only_reads_remote_projects(self):
        with TemporaryDirectory() as directory, patch("workflow.store.LOCAL", Path(directory)), \
                patch("workflow.web.LOCAL", Path(directory)):
            application = WebApplication(self.settings)
            self.addCleanup(application.close)
            server = LocalServer(("127.0.0.1", 0), application)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            Thread(target=server.serve_forever, daemon=True).start()
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            self.addCleanup(client.close)
            projects = [{"id": 41, "name": "可选项目"}]
            with patch.object(application.sessions.zentao, "list_projects", return_value=projects) as fetch:
                client.request("GET", "/api/zentao-projects")
                response = client.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                fetch.assert_not_called()
                client.request("GET", "/")
                response = client.getresponse()
                cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                response.read()
                client.request("GET", "/api/zentao-projects", headers={"Cookie": cookie})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(loads(response.read()), {"projects": projects})
                fetch.assert_called_once()
            self.assertFalse(application.project_partitions.path.exists())
