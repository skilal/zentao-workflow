"""用脱敏资料、模拟模型和临时 Git 仓库检查分支识别，不访问业务仓库。"""

from copy import deepcopy
from json import dumps, loads
from pathlib import Path
from sys import executable as python_executable
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from workflow.branching import BRANCH_SCHEMA, configured_repositories, saved_branches
from workflow.engine import PHASE_STAGE, Workflow
from workflow.gitops import branch_catalog, git, prepare, refresh_branches
from workflow.rpc import AppServer, thread_parameters
from workflow.settings import ROOT, WorkflowError
from workflow.zentao import ZentaoClient, project_context


def decision(role, branch="", status="selected", candidates=None):
    return {"role": role, "status": status, "branch": branch, "reason": "依据所属项目和本次任务确定。",
            "candidates": candidates or [], "question": "请明确选择项目分支。" if status == "needs_user" else ""}


class ProjectChecks(TestCase):
    def test_id_and_name_are_merged_without_using_product_or_execution(self):
        project = project_context({"project": "示例档案馆", "product": "其他产品"},
                                  {"project": 41, "executionName": "其他迭代"})
        self.assertEqual((project["id"], project["name"]), (41, "示例档案馆"))
        missing = project_context({"productName": "示例档案馆", "execution": 41})
        self.assertEqual((missing["id"], missing["name"]), (0, ""))

    def test_multiple_projects_and_conflicting_ids_require_explicit_choice(self):
        project = project_context({"projects": {"41": "示例甲", "42": "示例乙"}})
        self.assertEqual(project["id"], 0)
        self.assertEqual({entry["id"] for entry in project["candidates"]}, {41, 42})
        self.assertEqual(project_context({"project": 41}, {"project": 42})["id"], 0)

    def test_list_preserves_project_identity_and_candidates(self):
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        client = ZentaoClient(settings)
        client.account, client.token = "示例账号", "模拟登录"
        item = {"id": 7, "project": 41, "projectName": "示例档案馆", "title": "示例需求", "assignedTo": "示例账号"}
        with patch.object(client, "_fetch", return_value={"story": {"total": 1, "stories": [item]}}):
            result = client.list_items("story")[0]
        self.assertEqual((result["project_id"], result["project_name"]), (41, "示例档案馆"))
        self.assertEqual(result["project_candidates"], [{"id": 41, "name": "示例档案馆"}])

    def test_detail_resolves_project_name_by_project_api(self):
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        client = ZentaoClient(settings)
        client.account, client.token = "示例账号", "模拟登录"
        primary = {"id": 7, "project": 41, "title": "示例需求", "spec": "需求正文", "assignedTo": "示例账号"}
        def fetch(path):
            return primary if path == "api.php/v1/stories/7" else {"id": 41, "name": "示例档案馆"}
        with TemporaryDirectory() as directory, patch.object(client, "_fetch", side_effect=fetch) as request:
            result = client.collect({"kind": "story", "id": 7}, Path(directory))
        self.assertEqual(result["project"]["name"], "示例档案馆")
        self.assertEqual(request.call_count, 2)


class BranchWorkflowChecks(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.settings["project_branches"] = {}
        self.catalogs = {
            "frontend": [{"name": "示例档案馆/develop", "local": True, "remote": True, "commit": "a" * 40, "date": "2026-01-01", "subject": "示例前端修改"}],
            "backend": [{"name": "feature/示例", "local": True, "remote": True, "commit": "b" * 40, "date": "2026-01-01", "subject": "示例后端修改"}],
        }
        for target, replacement in (
            ("workflow.store.LOCAL", self.root),
            ("workflow.engine.branch_catalog", lambda repository: deepcopy(self.catalogs[repository["role"]])),
            ("workflow.engine.refresh_branches", lambda repository: deepcopy(self.catalogs[repository["role"]])),
            ("workflow.engine.acquire_workspace", self.prepare),
            ("workflow.engine.save_settings", self.save_settings),
        ):
            patched = patch(target, new=replacement)
            patched.start()
            self.addCleanup(patched.stop)
        self.flow = Workflow(self.settings, lambda *args: None)
        self.addCleanup(self.flow.close)
        state = self.flow.store.new({"kind": "task", "id": 7, "title": "示例后端任务", "project": "示例档案馆"})
        state["project"] = {"id": 41, "name": "示例档案馆", "candidates": [{"id": 41, "name": "示例档案馆"}]}
        state["snapshot"] = {"sources": [{"title": "示例任务", "content": "需求内容", "raw": {"project": 41}}],
                             "materials": [], "blockers": [], "feishu_links": []}
        self.flow.state = state
        self.turns = Mock(side_effect=lambda *args: state.update(stage=PHASE_STAGE[state["phase"]]))
        self.flow._start_turn = self.turns
        self.prepared = []

    def prepare(self, selection, path, store, state):
        self.prepared.append({"branch": selection["branch"], "create": selection["create"], "track": selection.get("track", False)})
        record = {**selection, "source": selection["path"], "path": str(path), "baseline": "a" * 40}
        state["repos"].append(record)
        store.save(state)
        return record

    def save_settings(self, settings):
        (self.root / "settings.json").write_text(dumps(settings, ensure_ascii=False), encoding="utf-8")

    def complete(self, *decisions):
        self.flow._complete_branches(dumps({"repositories": list(decisions)}, ensure_ascii=False))

    def test_both_repositories_are_prepared_even_for_backend_task(self):
        self.flow.start_plan("补充规则")
        self.assertEqual(self.turns.call_args.args[1], BRANCH_SCHEMA)
        self.complete(decision("frontend", "示例档案馆/develop"), decision("backend", "feature/示例"))
        self.assertEqual([entry["branch"] for entry in self.prepared], ["示例档案馆/develop", "feature/示例"])
        self.assertEqual(self.flow.state["phase"], "plan")
        self.assertEqual(self.flow.state["supplement"], "补充规则")
        self.assertEqual(self.settings["project_branches"], {})

    def test_saved_common_branches_skip_ai_and_are_site_scoped(self):
        site = self.settings["zentao"]["base_url"]
        self.settings["project_branches"] = {site: {"41": {"name": "示例档案馆", "branches": {
            "frontend": "示例档案馆/develop", "backend": "feature/示例"}}}}
        self.flow.start_plan("")
        self.assertEqual(self.turns.call_count, 1)
        self.assertEqual(len(self.prepared), 2)
        self.settings["zentao"]["base_url"] = "http://example.invalid"
        self.assertEqual(saved_branches(self.settings, self.flow.state["project"]), {})

    def test_ambiguous_ai_result_waits_and_manual_choices_are_saved(self):
        self.flow.start_plan("")
        self.complete(decision("frontend", status="needs_user", candidates=["示例档案馆/develop"]),
                      decision("backend", "feature/示例"))
        self.assertEqual(self.flow.state["stage"], "等待选择分支")
        self.assertEqual(self.prepared, [])
        self.flow.choose_branches({"frontend": "示例档案馆/develop"})
        saved = loads((self.root / "settings.json").read_text(encoding="utf-8"))
        branches = saved_branches(saved, self.flow.state["project"])
        self.assertEqual(branches, {"frontend": "示例档案馆/develop"})
        self.assertEqual(len(self.prepared), 2)

    def test_no_match_refreshes_once_then_waits_for_user(self):
        self.flow.start_plan("")
        with patch("workflow.engine.refresh_branches", side_effect=lambda repo: deepcopy(self.catalogs[repo["role"]])) as refresh:
            self.complete(decision("frontend", status="not_found"), decision("backend", "feature/示例"))
            self.assertEqual(refresh.call_count, 1)
            self.complete(decision("frontend", status="not_found"))
            self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.flow.state["stage"], "等待选择分支")

    def test_remote_only_choice_refreshes_and_is_rejudged_before_tracking(self):
        self.catalogs["frontend"][0]["local"] = False
        self.flow.start_plan("")
        with patch("workflow.engine.refresh_branches", side_effect=lambda repo: deepcopy(self.catalogs[repo["role"]])) as refresh:
            self.complete(decision("frontend", "示例档案馆/develop"), decision("backend", "feature/示例"))
            self.assertEqual(self.prepared, [])
            self.assertEqual(refresh.call_count, 1)
            self.complete(decision("frontend", "示例档案馆/develop"))
            self.assertEqual(refresh.call_count, 1)
        self.assertTrue(self.prepared[0]["create"] and self.prepared[0]["track"])

    def test_missing_common_branch_refreshes_and_requires_replacement(self):
        site = self.settings["zentao"]["base_url"]
        self.settings["project_branches"] = {site: {"41": {"branches": {"frontend": "已失效分支"}}}}
        self.flow.start_plan("")
        self.complete(decision("backend", "feature/示例"))
        self.assertEqual(self.flow.state["stage"], "等待选择分支")
        self.assertIn("已不可用", self.flow.state["branch_resolution"]["repositories"][0]["reason"])

    def test_refresh_failure_stops_without_preparing_workspaces(self):
        self.flow.start_plan("")
        self.flow.last_message = dumps({"repositories": [decision("frontend", status="not_found"), decision("backend", "feature/示例")]})
        with patch("workflow.engine.refresh_branches", side_effect=WorkflowError("远程鉴权失败")):
            self.flow.submit(self.flow._completed, {"status": "completed"}, self.flow.state["id"], self.flow.connection_epoch).result()
        self.assertEqual(self.flow.state["stage"], "已停止")
        self.assertEqual(self.flow.state["error"], "远程鉴权失败")
        self.assertEqual(self.prepared, [])
        self.assertEqual(self.flow.state["branch_resolution"]["repositories"][0]["refresh_state"], "failed")

    def test_invalid_model_branch_or_role_is_rejected(self):
        self.flow.start_plan("")
        for result in (
            [decision("frontend", "凭空生成的分支"), decision("backend", "feature/示例")],
            [decision("frontend", "示例档案馆/develop"), decision("frontend", "示例档案馆/develop")],
        ):
            with self.assertRaises(WorkflowError):
                self.complete(*result)
        self.assertEqual(self.prepared, [])

    def test_missing_project_waits_without_reading_branches(self):
        self.flow.state["project"] = {"id": 0, "name": "", "candidates": []}
        self.flow.start_plan("")
        self.assertEqual(self.flow.state["stage"], "等待确认项目")
        self.assertEqual(self.turns.call_count, 0)
        self.flow.confirm_project(42, "人工确认项目")
        self.assertEqual(self.flow.state["project"]["id"], 42)
        self.assertEqual(self.turns.call_count, 1)

    def test_multiple_projects_cannot_be_replaced_with_an_unrelated_id(self):
        self.flow.state["project"] = {"id": 0, "name": "", "candidates": [{"id": 41, "name": "示例甲"}, {"id": 42, "name": "示例乙"}]}
        self.flow.start_plan("")
        with self.assertRaises(WorkflowError):
            self.flow.confirm_project(43, "其他项目")
        self.assertEqual(self.flow.state["stage"], "等待确认项目")

    def test_manual_edit_does_not_preselect_or_call_ai(self):
        self.flow.start_plan("", manual=True)
        self.assertEqual(self.flow.state["stage"], "等待选择分支")
        self.assertFalse(self.turns.called)
        with self.assertRaises(WorkflowError):
            self.flow.choose_branches({"frontend": "示例档案馆/develop"})
        self.flow.choose_branches({"frontend": "示例档案馆/develop", "backend": "feature/示例"})
        self.assertEqual(len(saved_branches(self.settings, self.flow.state["project"])), 2)

    def test_pause_and_restore_do_not_start_planning_or_skip_manual_choices(self):
        self.flow.start_plan("")
        self.flow.pause()
        self.assertEqual(self.flow.state["stage"], "已暂停")
        self.flow.restore(self.flow.state["id"])
        self.flow.resume()
        self.assertEqual(self.flow.state["phase"], "branch")
        self.assertEqual(self.turns.call_count, 2)
        self.complete(decision("frontend", status="needs_user"), decision("backend", status="needs_user"))
        session_id = self.flow.state["id"]
        self.flow.restore(session_id)
        self.assertEqual(self.flow.state["stage"], "等待选择分支")
        self.assertEqual(self.prepared, [])

    def test_pause_during_refresh_prevents_workspace_creation(self):
        self.flow.start_plan("")
        def refresh(repository):
            self.flow.pause()
            return deepcopy(self.catalogs[repository["role"]])
        with patch("workflow.engine.refresh_branches", side_effect=refresh):
            self.complete(decision("frontend", status="not_found"), decision("backend", "feature/示例"))
        self.assertEqual(self.flow.state["stage"], "已暂停")
        self.assertEqual(self.prepared, [])

    def test_legacy_workspaces_keep_their_original_selections(self):
        state = self.flow.state
        state["stage"] = "准备工作区失败"
        state["repos"] = [{"name": "前端", "branch": "历史分支", "base": "历史分支", "path": "历史工作区"}]
        state["selections"] = [{"name": "前端", "path": "原仓库", "branch": "历史分支", "base": "历史分支", "create": False}]
        self.flow.start_plan("保留历史")
        self.assertEqual(state["repos"][0]["branch"], "历史分支")
        self.assertNotIn("branch_resolution", state)
        self.assertEqual(self.prepared, [])

    def test_branch_rpc_has_no_business_tools_or_nonexistent_workspace_cwd(self):
        state = self.flow.state
        state["phase"] = "branch"
        disabled_mcp = {"other": {"command": "示例命令", "enabled": False}}
        params = thread_parameters(state, "示例模型", disabled_mcp)
        server = params["config"]["mcp_servers"]["workflow"]
        self.assertFalse(server["enabled"])
        self.assertEqual(server["command"], python_executable)
        self.assertEqual(server["args"], [str(ROOT / "mcp.py"), state["id"]])
        self.assertEqual(params["config"]["mcp_servers"]["other"], disabled_mcp["other"])
        self.assertEqual(Path(params["cwd"]).name, state["id"])
        self.assertNotIn("zentao-delivery", params["developerInstructions"])

    def test_business_phases_enable_workflow_transport(self):
        for phase in ("plan", "implement", "review", "fix"):
            with self.subTest(phase=phase):
                self.flow.state["phase"] = phase
                params = thread_parameters(self.flow.state, "示例模型", {})
                server = params["config"]["mcp_servers"]["workflow"]
                self.assertTrue(server["enabled"])
                self.assertEqual(server["command"], python_executable)
                self.assertEqual(Path(params["cwd"]).name, "workspace")

    def test_disabling_inherited_mcp_preserves_stdio_and_http_transports(self):
        configuration = {"mcp_servers": {
            "local": {"command": "示例命令", "args": ["示例参数"], "enabled": True, "tool_timeout_sec": None},
            "remote": {"url": "https://example.invalid/mcp", "enabled": True, "bearer_token_env_var": None},
            "workflow": {"command": "旧工作流命令"}}}
        original = deepcopy(configuration)
        rpc = AppServer(self.settings, Mock())
        rpc.request = Mock(side_effect=[{}, {"config": configuration},
            {"data": [{"model": "示例模型", "isDefault": True}]},
            {"data": [{"mode": "plan"}, {"mode": "default"}]}])
        self.settings["codex"]["model"] = "示例模型"
        with patch("workflow.rpc.which", return_value="codex.exe"), \
             patch("workflow.rpc.Popen"), patch("workflow.rpc.Thread"), \
             patch.object(rpc, "notify"):
            rpc.start()
        self.assertEqual(rpc.disabled_mcp, {
            "local": {"command": "示例命令", "args": ["示例参数"], "enabled": False},
            "remote": {"url": "https://example.invalid/mcp", "enabled": False}})
        self.assertEqual(configuration, original)

    def test_structured_rpc_completion_enters_a_separate_business_plan(self):
        self.flow.start_plan("业务补充")
        rpc = Mock()
        rpc.model, rpc.disabled_mcp = "示例模型", {}
        rpc.request.side_effect = lambda method, params: {"thread": {"id": "branch-thread"}} if method == "thread/start" else {"turn": {"id": "branch-turn"}}
        with patch("workflow.engine.AppServer", return_value=rpc):
            Workflow._start_turn(self.flow, "识别分支", BRANCH_SCHEMA)
        thread_params = rpc.request.call_args_list[0].args[1]
        turn_params = rpc.request.call_args_list[1].args[1]
        self.assertFalse(thread_params["config"]["mcp_servers"]["workflow"]["enabled"])
        self.assertEqual(Path(turn_params["cwd"]).name, self.flow.state["id"])
        self.assertEqual([entry["type"] for entry in turn_params["input"]], ["text"])
        self.assertEqual(turn_params["outputSchema"], BRANCH_SCHEMA)
        epoch = self.flow.connection_epoch
        text = dumps({"repositories": [decision("frontend", "示例档案馆/develop"), decision("backend", "feature/示例")]})
        self.flow._event({"method": "item/completed", "params": {"threadId": "branch-thread", "item": {"type": "agentMessage", "text": text}}}, epoch)
        self.flow._event({"method": "turn/completed", "params": {"threadId": "branch-thread", "turn": {"id": "branch-turn", "status": "completed"}}}, epoch)
        self.flow.executor.submit(lambda: None).result()
        self.assertEqual(len(self.prepared), 2)
        self.assertEqual(self.flow.state["phase"], "plan")
        self.assertIsNone(self.flow.state["thread_id"])
        rpc.close.assert_called_once()
        self.flow._event({"method": "client/error", "params": {"message": "旧识别连接的迟到事件"}}, epoch)
        self.assertEqual(self.flow.state["error"], "")

    def test_configuration_requires_exactly_one_repository_per_role(self):
        settings = deepcopy(self.settings)
        settings["repositories"][1]["role"] = "frontend"
        with self.assertRaises(WorkflowError):
            configured_repositories(settings)

    def test_branch_and_plan_turns_receive_all_selected_tasks(self):
        state = self.flow.state
        state["items"] = [state["item"], {"kind": "task", "id": 8, "title": "示例前端任务"}]
        self.flow.start_plan("")
        rpc = Mock()
        rpc.model, rpc.disabled_mcp = "示例模型", {}
        rpc.request.side_effect = lambda method, params: {"thread": {"id": "模拟会话"}} if method == "thread/start" else {"turn": {"id": "模拟轮次"}}
        with patch("workflow.engine.AppServer", return_value=rpc):
            for phase in ("branch", "plan"):
                state["phase"] = phase
                Workflow._start_turn(self.flow, "读取全部任务")
                text = rpc.request.call_args.args[1]["input"][0]["text"]
                self.assertIn('"items":', text)
                self.assertIn("示例后端任务", text)
                self.assertIn("示例前端任务", text)

    def test_second_repository_read_failure_cannot_leave_a_one_repository_resolution(self):
        def catalog(repository):
            if repository["role"] == "backend":
                raise WorkflowError("第二个仓库不可访问")
            return deepcopy(self.catalogs[repository["role"]])
        with patch("workflow.engine.branch_catalog", side_effect=catalog):
            self.flow.submit(self.flow.start_plan, "").result()
        self.assertEqual(self.flow.state["stage"], "已停止")
        self.assertEqual(self.flow.state["branch_resolution"]["repositories"], [])
        self.flow.resume()
        self.assertEqual(len(self.flow.state["branch_resolution"]["repositories"]), 2)

    def test_late_turn_completion_cannot_apply_after_resume(self):
        self.flow.start_plan("")
        self.flow.state["turn_id"] = "新的识别轮次"
        self.flow.last_message = dumps({"repositories": [decision("frontend", "示例档案馆/develop"), decision("backend", "feature/示例")]})
        self.flow._completed({"id": "旧识别轮次", "status": "completed"}, self.flow.state["id"], self.flow.connection_epoch)
        self.flow._event({"method": "item/completed", "params": {"turnId": "旧识别轮次", "item": {"type": "agentMessage", "text": "迟到输出"}}}, self.flow.connection_epoch)
        self.assertEqual(self.prepared, [])
        self.assertNotEqual(self.flow.last_message, "迟到输出")


class GitBranchChecks(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        git(self.remote, "init", "--bare")
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "主目录分支")
        git(self.source, "config", "user.name", "分支检查")
        git(self.source, "config", "user.email", "branch-check@example.invalid")
        (self.source / "说明.txt").write_text("初始文本", encoding="utf-8")
        git(self.source, "add", "说明.txt")
        git(self.source, "commit", "-m", "初始化脱敏文本")
        git(self.source, "remote", "add", "origin", str(self.remote))
        git(self.source, "push", "origin", "HEAD:refs/heads/项目/远程分支")
        self.repository = {"name": "示例", "path": str(self.source), "role": "frontend", "remote": "origin"}

    def test_metadata_merges_local_and_remote_and_skips_symbolic_refs(self):
        git(self.source, "branch", "项目/远程分支", "refs/remotes/origin/项目/远程分支")
        git(self.source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/项目/远程分支")
        entries = branch_catalog(self.repository)
        entry = next(entry for entry in entries if entry["name"] == "项目/远程分支")
        self.assertTrue(entry["local"] and entry["remote"])
        self.assertEqual(entry["subject"], "初始化脱敏文本")
        self.assertFalse(any(entry["name"] == "HEAD" for entry in entries))

    def test_fetch_and_remote_worktree_track_without_switching_source(self):
        git(self.source, "update-ref", "-d", "refs/remotes/origin/项目/远程分支")
        catalog = refresh_branches(self.repository)
        self.assertTrue(any(entry["name"] == "项目/远程分支" and not entry["local"] for entry in catalog))
        result = prepare(self.source, self.root / "workspace", "项目/远程分支",
                         "refs/remotes/origin/项目/远程分支", True, True)
        self.assertEqual(git(result["path"], "rev-parse", "--abbrev-ref", "@{upstream}"), "origin/项目/远程分支")
        self.assertEqual(git(self.source, "branch", "--show-current"), "主目录分支")

    def test_existing_local_branch_keeps_its_head_and_original_uncommitted_changes(self):
        git(self.source, "branch", "本地项目分支")
        initial = git(self.source, "rev-parse", "本地项目分支")
        (self.source / "说明.txt").write_text("原目录尚未提交的内容", encoding="utf-8")
        result = prepare(self.source, self.root / "workspace", "本地项目分支", "refs/heads/本地项目分支", False)
        self.assertEqual(result["baseline"], initial)
        self.assertEqual((self.source / "说明.txt").read_text(encoding="utf-8"), "原目录尚未提交的内容")
        self.assertEqual((Path(result["path"]) / "说明.txt").read_text(encoding="utf-8"), "初始文本")

    def test_occupied_branch_stops_without_creating_another_branch(self):
        with self.assertRaises(WorkflowError):
            prepare(self.source, self.root / "workspace", "主目录分支", "refs/heads/主目录分支", False)
        self.assertEqual(git(self.source, "for-each-ref", "--format=%(refname:short)", "refs/heads"), "主目录分支")

    def test_active_hook_is_not_bypassed_during_fetch(self):
        hook = self.source / ".git" / "hooks" / "reference-transaction"
        hook.write_text("不执行此钩子", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "Git 钩子"):
            refresh_branches(self.repository)

    def test_deleted_remote_branch_is_not_reused_from_stale_tracking_ref(self):
        commit_id = git(self.source, "rev-parse", "HEAD")
        git(self.source, "push", "origin", ":refs/heads/项目/远程分支")
        git(self.source, "update-ref", "refs/remotes/origin/项目/远程分支", commit_id)
        self.assertTrue(any(entry["name"] == "项目/远程分支" for entry in branch_catalog(self.repository)))
        self.assertFalse(any(entry["name"] == "项目/远程分支" for entry in refresh_branches(self.repository)))
