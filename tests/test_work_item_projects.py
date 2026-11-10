"""检查项目缺项补全、明确关联、同轮复用与不确定项目，不连接业务系统。"""

from unittest.mock import patch

import test_zentao_refresh as refresh_cases
from workflow.settings import WorkflowError
from workflow.zentao import project_context


class WorkItemProjectChecks(refresh_cases.ZentaoRefreshFixture):
    def test_execution_only_contributes_its_explicit_parent_project(self):
        for executions in ([{"id": 501, "name": "示例执行", "project": 41}],
                           {"501": {"id": 501, "name": "示例执行", "project": 41}}):
            with self.subTest(executions=executions):
                result = project_context({"product": 99, "executions": executions})
                self.assertEqual(result, {"id": 41, "name": "", "candidates": [{"id": 41, "name": ""}]})
        result = project_context({"product": 99, "execution": 501, "executions": [{"id": 501, "name": "示例执行"}]})
        self.assertEqual(result, {"id": 0, "name": "", "candidates": []})

    def test_empty_candidate_name_does_not_overwrite_explicit_project_name(self):
        result = project_context({"project": 41, "projectName": "已知项目", "projects": [{"id": 41, "name": ""}]})
        self.assertEqual(result["name"], "已知项目")

    def test_missing_requirement_relation_reuses_known_project_name_and_detail(self):
        items = [{"kind": "requirement", "id": 7, "project_id": 0, "project_name": ""},
                 {"kind": "requirement", "id": 7, "project_id": 0, "project_name": ""},
                 {"kind": "task", "id": 8, "project_id": 41, "project_name": "已知项目"}]
        detail = {"id": 7, "type": "requirement", "assignedTo": "测试账号",
                  "executions": {"501": {"id": 501, "name": "执行名称", "project": 41}}}
        with patch.object(self.client, "_fetch", return_value=detail) as fetch:
            result = self.client.resolve_item_projects(items)
        fetch.assert_called_once_with("api.php/v1/stories/7?type=requirement")
        self.assertTrue(all(item["project_id"] == 41 and item["project_name"] == "已知项目" for item in result))
        self.assertTrue(all(item["project"] == "已知项目" for item in result))

    def test_project_name_is_only_requested_once_for_shared_id(self):
        items = [{"kind": kind, "id": number, "project_id": 42, "project_name": ""}
                 for number, kind in enumerate(("bug", "task", "story"), 1)]
        progress = []
        with patch.object(self.client, "_fetch", return_value={"id": 42, "name": "  补全项目  "}) as fetch:
            result = self.client.resolve_item_projects(items, on_progress=progress.append)
        fetch.assert_called_once_with("api.php/v1/projects/42")
        self.assertTrue(all(item["project_name"] == "补全项目" for item in result))
        self.assertEqual(len(progress), 1)

    def test_multiple_parent_projects_remain_candidates_and_are_not_auto_selected(self):
        detail = {"id": 7, "type": "requirement", "assignedTo": "测试账号", "executions": [
            {"id": 501, "project": 41}, {"id": 502, "project": 42}]}
        items = [{"kind": "requirement", "id": 7, "project_id": 0},
                 {"kind": "task", "id": 8, "project_id": 41, "project_name": "示例甲"},
                 {"kind": "task", "id": 9, "project_id": 42, "project_name": "示例乙"}]
        with patch.object(self.client, "_fetch", return_value=detail) as fetch:
            result = self.client.resolve_item_projects(items)
        fetch.assert_called_once()
        self.assertEqual(result[0]["project_id"], 0)
        self.assertEqual(result[0]["project_name"], "")
        self.assertEqual(result[0]["project_candidates"], [{"id": 41, "name": "示例甲"}, {"id": 42, "name": "示例乙"}])

    def test_missing_relation_is_preserved_without_using_product_or_execution_id(self):
        items = [{"kind": "requirement", "id": 7, "project_id": 0}]
        detail = {"id": 7, "type": "requirement", "assignedTo": "测试账号", "product": 41,
                  "executions": [{"id": 501, "name": "执行名称"}]}
        with patch.object(self.client, "_fetch", return_value=detail) as fetch:
            result = self.client.resolve_item_projects(items)
        fetch.assert_called_once()
        self.assertEqual(result[0]["project_id"], 0)
        self.assertEqual(result[0]["project_candidates"], [])

    def test_project_permission_or_identity_failure_stops_without_retry(self):
        for response in (WorkflowError("项目无权读取"), {"id": 43, "name": "其他项目"}, {"id": 42, "name": ""}):
            items = [{"kind": "bug", "id": 1, "project_id": 42}]
            with self.subTest(response=response), patch.object(self.client, "_fetch", side_effect=[response]) as fetch:
                with self.assertRaises(WorkflowError):
                    self.client.resolve_item_projects(items)
                fetch.assert_called_once()
