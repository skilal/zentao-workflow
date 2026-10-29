"""临时仓库和模拟编译进程，绝不执行业务编译或下载业务依赖。"""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from workflow.approvals import is_controlled_write
from workflow.compile_checks import CompileRunner, backend_modules, classify, compile_context, redact, source_snapshot
from workflow.engine import PHASE_STAGE, Workflow
from workflow.gitops import git
from workflow.mcp_server import TOOLS, Tools
from workflow.settings import ROOT, WorkflowError, load_settings
from workflow.web import WebApplication, public_state


POM = '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>{}</project>'
PASSED = {"status": "passed", "summary": "编译检查通过。", "diagnostics": []}
MISSING = {"status": "dependencies", "summary": "本机依赖不足。", "diagnostics": []}
BLOCKED = {"status": "blocked", "summary": "环境异常。", "diagnostics": []}


class CompileChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("workflow.store.LOCAL", "workflow.web.LOCAL"):
            mocked = patch(target, self.root / "local")
            mocked.start()
            self.addCleanup(mocked.stop)
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        self.flow = Workflow(self.settings, Mock())
        self.addCleanup(self.flow.close)
        self.state = self.flow.store.new({"kind": "task", "id": 1, "title": "模拟编译检查"})
        self.flow.state = self.state
        plan = "实现本次需求"
        self.state.update(compile_check={"enabled": True}, stage="实施中", phase="implement", approved_plan=plan,
                          approved_hash=sha256(plan.encode()).hexdigest(), approval_source="manual", max_fix_rounds=3,
                          snapshot={"materials": [], "blockers": [], "feishu_links": []}, prepare_complete=True,
                          codex={"model": "模拟模型", "reasoning_effort": "high"}, thread_id="模拟线程")
        self.repo = self.repository("backend", "后端")
        self.change("档案服务/业务.java", "class 业务 { int 值 = 错误; }\n")
        self.started = []

        def start(text, output_schema=None):
            self.started.append((self.state["phase"], text))
            self.state["stage"] = PHASE_STAGE[self.state["phase"]]
        self.flow._start_turn = Mock(side_effect=start)

    def repository(self, role, name):
        root = self.root / role
        root.mkdir()
        git(root, "init", "-b", "功能分支")
        git(root, "config", "user.name", "模拟检查")
        git(root, "config", "user.email", "compile@example.invalid")
        (root / ".gitignore").write_text("node_modules/\ntarget/\ndist/\n", encoding="utf-8")
        (root / "保留.txt").write_text("原有内容", encoding="utf-8")
        if role == "backend":
            (root / "pom.xml").write_text(POM.format("<modules><module>档案服务</module><module>技术组件</module></modules>"), encoding="utf-8")
            for module in ("档案服务", "技术组件"):
                (root / module).mkdir()
                (root / module / "pom.xml").write_text(POM.format(""), encoding="utf-8")
            (root / "档案服务" / "业务.java").write_text("class 业务 { int 值 = 1; }\n", encoding="utf-8")
        else:
            (root / "package.json").write_text(dumps({"scripts": {"build-local": "max build"}, "dependencies": {"react": "1"}}), encoding="utf-8")
            (root / "页面.ts").write_text("const 值 = 1;\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-m", "模拟基线")
        repository = {"name": name, "role": role, "path": str(root), "source": str(root),
                      "baseline": git(root, "rev-parse", "HEAD"), "branch": "功能分支", "preserved_paths": ["保留.txt"]}
        self.state["repos"].append(repository)
        (root / "保留.txt").write_text("本机原有修改", encoding="utf-8")
        return repository

    def change(self, relative, content):
        (Path(self.repo["path"]) / relative).write_text(content, encoding="utf-8")

    def start(self, results):
        with patch.object(CompileRunner, "check", side_effect=deepcopy(results)) as check:
            self.flow._before_review()
        return check

    def decide(self, decision, result=PASSED):
        batch = self.state["compile_checks"][-1]
        with patch.object(CompileRunner, "check", return_value=deepcopy(result)) as check:
            self.flow.compile_decision(batch["id"], "后端", decision)
        return check

    def code_error(self):
        return {"status": "code_error", "summary": "本次代码错误。", "diagnostics": [
            {"path": "档案服务/业务.java", "line": 1, "message": "找不到符号"}]}

    def test_module_selection_and_global_configuration(self):
        self.assertEqual(backend_modules(self.repo), ["档案服务"])
        self.change("技术组件/pom.xml", POM.format("<name>更新</name>"))
        self.assertEqual(backend_modules(self.repo), ["技术组件", "档案服务"])
        self.change("pom.xml", POM.format("<modules><module>档案服务</module><module>技术组件</module></modules><name>更新</name>"))
        self.assertEqual(backend_modules(self.repo), [])

    def test_unregistered_module_stops(self):
        root = Path(self.repo["path"]) / "未登记"
        root.mkdir()
        (root / "pom.xml").write_text(POM.format(""), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "未登记"):
            backend_modules(self.repo)

    def test_disabled_and_legacy_sessions_do_not_run_commands(self):
        for config in ({"enabled": False}, None):
            if config is None:
                self.state.pop("compile_check", None)
            else:
                self.state["compile_check"] = config
            with patch.object(CompileRunner, "check") as check:
                self.flow._before_review()
            check.assert_not_called()
            self.assertEqual(self.started[-1][0], "review")
        self.assertFalse(public_state(self.state)["compile_check"]["enabled"])

    def test_only_changed_repositories_checked_before_review(self):
        frontend = self.repository("frontend", "前端")
        (Path(frontend["path"]) / "页面.ts").write_text("const 值 = 2;\n", encoding="utf-8")
        check = self.start([PASSED])
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.args[1:], (["档案服务"], False))
        self.assertEqual(self.started[-1][0], "review")
        self.assertIn("编译检查", self.started[-1][1])

    def test_frontend_changes_while_waiting_still_enter_review_without_compiling(self):
        frontend = self.repository("frontend", "前端")
        self.start([MISSING])
        (Path(frontend["path"]) / "页面.ts").write_text("const 值 = 2;\n", encoding="utf-8")
        check = self.decide("skip")
        check.assert_not_called()
        self.assertEqual(len(self.state["compile_checks"][-1]["repositories"]), 1)
        self.assertIn("前端/页面.ts", self.state["review_manifest"])

    def test_missing_dependencies_wait_and_explicit_download_is_scoped(self):
        self.start([MISSING])
        self.assertFalse(self.started)
        self.assertEqual(self.state["stage"], "等待编译选择")
        check = self.decide("download")
        self.assertTrue(check.call_args.args[2])
        self.assertEqual(self.state["phase"], "review")
        self.start([MISSING])
        self.assertEqual(self.state["stage"], "等待编译选择")

    def test_changed_dependencies_reject_download_and_skip(self):
        self.start([MISSING])
        self.change("档案服务/pom.xml", POM.format("<name>变化</name>"))
        for decision in ("download", "skip"):
            with self.assertRaisesRegex(WorkflowError, "已变化"):
                self.decide(decision)
        check = self.decide("retry")
        self.assertFalse(check.call_args.args[2])

    def test_download_failure_never_retries_or_repairs(self):
        self.start([MISSING])
        check = self.decide("download", BLOCKED)
        self.assertEqual(check.call_count, 1)
        self.assertFalse(self.started)
        self.assertEqual(self.state["stage"], "等待编译选择")

    def test_skip_enters_review_and_records_choice(self):
        self.start([MISSING])
        check = self.decide("skip")
        check.assert_not_called()
        batch = self.state["compile_checks"][-1]
        self.assertEqual(batch["repositories"][0]["status"], "skipped")
        self.assertEqual(batch["decisions"][-1]["decision"], "skip")
        self.assertEqual(self.state["phase"], "review")

    def test_code_repairs_have_independent_limit_and_lose_download_permission(self):
        self.state["fix_round"] = 2
        self.state["max_fix_rounds"] = 1
        self.start([self.code_error()])
        self.assertEqual(self.state["phase"], "compile_fix")
        self.assertEqual(self.state["fix_round"], 2)
        with patch.object(CompileRunner, "check", return_value=self.code_error()) as check:
            self.flow._compile_repaired()
        self.assertFalse(check.call_args.args[2])
        self.assertEqual(self.state["stage"], "等待编译选择")
        self.decide("add_round")
        self.assertEqual(self.state["compile_checks"][-1]["fix_round"], 2)
        self.assertEqual(self.state["phase"], "compile_fix")

    def test_environment_errors_and_zero_limit_never_auto_repair(self):
        self.start([BLOCKED])
        self.assertFalse(self.started)
        self.state["max_fix_rounds"] = 0
        self.start([self.code_error()])
        self.assertFalse(self.started)
        self.assertEqual(self.state["stage"], "等待编译选择")

    def test_followup_review_switch_is_respected_after_compile(self):
        self.state.update(followup={"text": "修改代码"}, review_enabled=False)
        check = self.start([PASSED])
        self.assertEqual(check.call_count, 1)
        self.assertEqual(self.state["stage"], "已完成（未审查）")
        self.assertFalse(self.started)

    def test_stale_result_blocks_review_and_commit(self):
        self.start([PASSED])
        self.change("档案服务/业务.java", "再次改变\n")
        with self.assertRaises(WorkflowError):
            self.flow._begin_review()
        self.state["stage"] = "等待修复选择"
        with self.assertRaises(WorkflowError):
            self.flow.finalize([], "", automatic=True)

    def test_pause_and_restart_restore_waiting_without_authorizing(self):
        self.start([MISSING])
        self.decide("pause")
        stored = self.flow.store.load(self.state["id"])
        self.flow.state = stored
        with patch.object(CompileRunner, "check") as check:
            self.flow.resume()
        check.assert_not_called()
        self.assertEqual(stored["stage"], "等待编译选择")

    def test_classification_requires_changed_line_evidence(self):
        output = f"[ERROR] {self.repo['path']}/档案服务/业务.java:[1,12] cannot find symbol\nCompilation failure"
        self.assertEqual(classify(self.repo, 1, output)["status"], "code_error")
        self.assertEqual(classify(self.repo, 1, output.replace("[1,12]", "[8,12]"))["status"], "blocked")
        self.assertEqual(classify(self.repo, 1, output + " JAVA_HOME invalid")["status"], "blocked")
        self.assertEqual(classify(self.repo, 1, "artifact has not been downloaded in offline mode")["status"], "dependencies")
        self.assertEqual(classify(self.repo, 1, "Could not transfer artifact: connection timed out")["status"], "blocked")

    def test_compiler_mutations_are_detected_and_preserved_files_included(self):
        runner = CompileRunner()
        self.assertIn("保留.txt", source_snapshot(self.repo))

        def mutate(*args):
            self.change("保留.txt", "不应出现的变化")
            return 0, ""
        with patch("workflow.compile_checks.which", return_value="mvn.cmd"), patch.object(runner, "execute", side_effect=mutate):
            with self.assertRaisesRegex(WorkflowError, "改变了源码"):
                runner.check(self.repo, ["档案服务"])

    def test_backend_offline_command_and_download_command(self):
        runner = CompileRunner()
        with patch("workflow.compile_checks.which", return_value="mvn.cmd"), patch.object(runner, "execute", return_value=(0, "")) as execute:
            runner.check(self.repo, ["档案服务"])
            command = execute.call_args.args[0]
            self.assertIn("-o", command)
            self.assertEqual(command[-4:], ["-pl", "档案服务", "-am", "compile"])
            self.assertNotIn("install", command)
            runner.check(self.repo, ["档案服务"], True)
            self.assertNotIn("-o", execute.call_args.args[0])

    def test_frontend_adapter_is_removed_without_probing_tools(self):
        repository = self.repository("frontend", "前端")
        runner = CompileRunner()
        with patch("workflow.compile_checks.which") as which, patch.object(runner, "execute") as execute:
            with self.assertRaisesRegex(WorkflowError, "仅支持后端"):
                runner.check(repository, [])
        which.assert_not_called()
        execute.assert_not_called()

    def test_frontend_only_changes_skip_compilation_and_are_reviewed(self):
        self.change("档案服务/业务.java", "class 业务 { int 值 = 1; }\n")
        frontend = self.repository("frontend", "前端")
        (Path(frontend["path"]) / "页面.ts").write_text("const 值 = 2;\n", encoding="utf-8")
        check = self.start([])
        check.assert_not_called()
        self.assertEqual(self.state["phase"], "review")
        self.assertEqual(self.state["compile_checks"][-1]["repositories"], [])
        self.assertIn("前端/页面.ts", self.state["review_manifest"])
        self.assertEqual(self.state["compile_checks"][-1]["manifest"], {})

    def test_old_frontend_wait_can_resume_without_executing_frontend_commands(self):
        self.repository("frontend", "前端")
        self.start([MISSING])
        batch = self.state["compile_checks"][-1]
        batch["repositories"].insert(0, {"name": "前端", "status": "dependencies", "summary": "旧记录", "modules": [], "diagnostics": []})
        self.state["stage"] = "已暂停"
        with patch.object(CompileRunner, "check", return_value=deepcopy(PASSED)) as check:
            self.flow.resume()
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.args[0]["role"], "backend")
        self.assertEqual(batch["repositories"][0]["status"], "excluded")
        self.assertEqual(self.state["phase"], "review")

    def test_model_compilation_context_is_compact_and_current_delivery_only(self):
        self.start([self.code_error()])
        batch = self.state["compile_checks"][-1]
        batch["manifest"] = {f"后端/文件{index}.java": "文件摘要" for index in range(500)}
        summary = compile_context(self.state)
        serialized = dumps(summary, ensure_ascii=False)
        for private in ("manifest", "source_hash", "dependency_hash", "decisions", "created_at", "文件摘要"):
            self.assertNotIn(private, serialized)
        self.assertEqual(summary[0]["repair_paths"], ["档案服务/业务.java"])
        self.assertEqual(summary[0]["repositories"][0]["diagnostics"], self.code_error()["diagnostics"])
        self.flow.save()
        tools = Tools(self.state["id"])
        tools.store = self.flow.store
        self.assertEqual(tools.call("task_context", {})["compile_checks"], summary)
        self.state["delivery_round"] = 2
        self.assertEqual(compile_context(self.state), [])

    def test_review_prompt_includes_compilation_summary_only_once(self):
        self.flow._start_turn = Workflow._start_turn.__get__(self.flow)
        self.flow.rpc = Mock()
        self.flow.rpc.request.return_value = {"turn": {"id": "审查回合"}}
        self.flow.ensure_rpc = Mock()
        self.start([PASSED])
        params = self.flow.rpc.request.call_args.args[1]
        prompt = params["input"][0]["text"]
        self.assertEqual(prompt.count('"compile_checks"'), 1)
        self.assertNotIn('"source_hash"', prompt)
        self.assertNotIn('"dependency_hash"', prompt)
        self.assertNotIn('"manifest"', prompt)
        context = loads(prompt.split("工作流上下文：\n", 1)[1])
        self.assertEqual(context["compile_checks"][0]["repositories"][0]["status"], "passed")

    def test_compilation_fix_tools_are_limited_to_authorized_files(self):
        self.start([self.code_error()])
        self.flow.save()
        tools = Tools(self.state["id"])
        tools.store = self.flow.store
        with self.assertRaises(WorkflowError):
            tools.call("write_file", {"repository": "后端", "path": "pom.xml", "content": ""})
        description = next(description for name, description, _ in TOOLS if name == "write_file")
        params = {"serverName": "workflow", "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call",
                  "tool_description": description, "tool_params": {"repository": "后端", "path": "档案服务/业务.java", "content": "修复"}}}
        self.assertTrue(is_controlled_write(self.state, params))
        tools.call("write_file", params["_meta"]["tool_params"])
        params["_meta"]["tool_params"]["path"] = "保留.txt"
        self.assertFalse(is_controlled_write(self.state, params))

    def test_redaction_and_cancelled_runner(self):
        secret = "password=明文 token=敏感 Bearer abc https://user:pass@example.invalid/?token=密钥"
        redacted = redact(secret)
        for value in ("明文", "敏感", "abc", "pass", "密钥"):
            self.assertNotIn(value, redacted)
        runner = CompileRunner()
        runner.stop()
        with patch("workflow.compile_checks.Popen") as process, self.assertRaises(WorkflowError):
            runner.execute(["mvn.cmd", "compile"], self.repo["path"], False)
        process.assert_not_called()

    def test_process_tree_and_child_environment_are_controlled(self):
        runner = CompileRunner()
        process = Mock(stdout=BytesIO(b""), stderr=BytesIO(b""), returncode=0)
        process.poll.return_value = 0
        with patch("workflow.compile_checks.ProcessTree") as tree, patch("workflow.compile_checks.Popen", return_value=process) as popen:
            runner.execute(["mvn.cmd", "-o", "compile"], self.repo["path"], False)
            self.assertEqual(popen.call_args.kwargs["env"]["CI"], "true")
            tree.return_value.attach.assert_called_once_with(process)
            tree.return_value.close.assert_called_once()
        tree = Mock()
        runner.tree = tree
        runner.stop()
        tree.terminate.assert_called_once()

    def test_completion_of_each_modification_phase_goes_through_compile(self):
        for phase in ("implement", "fix", "followup"):
            self.state.update(phase=phase, stage=PHASE_STAGE[phase], pending=None)
            self.flow.last_message = "修改完成"
            with patch.object(self.flow, "_before_review") as before:
                self.flow._completed({"status": "completed"}, self.state["id"], self.flow.connection_epoch)
            before.assert_called_once()

    def test_enabled_global_setting_is_not_applied_to_existing_session(self):
        self.settings["compile_check"]["enabled"] = True
        self.state.pop("compile_check")
        with patch.object(CompileRunner, "check") as check:
            self.flow._before_review()
        check.assert_not_called()

    def test_old_configuration_defaults_off_and_invalid_switch_is_rejected(self):
        settings = deepcopy(self.settings)
        settings.pop("compile_check")
        with patch("workflow.settings.ROOT", self.root):
            path = self.root / "settings.json"
            path.write_text(dumps(settings), encoding="utf-8")
            self.assertFalse(load_settings()["compile_check"]["enabled"])
            settings["compile_check"] = {"enabled": 1}
            path.write_text(dumps(settings), encoding="utf-8")
            with self.assertRaises(WorkflowError):
                load_settings()

    def test_personal_dependency_configuration_changes_invalidate_authorization(self):
        with patch("workflow.compile_checks.Path.home", return_value=self.root):
            self.start([MISSING])
            (self.root / ".m2").mkdir()
            (self.root / ".m2/settings.xml").write_text("<settings/>", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "已变化"):
                self.decide("download")

    def test_api_rejects_stale_revision_and_duplicate_does_not_repeat_choice(self):
        self.start([MISSING])
        app = WebApplication(self.settings)
        self.addCleanup(app.close)
        runtime = app.sessions.get(self.state["id"])
        runtime.engine.compile_decision = Mock()
        batch = self.state["compile_checks"][-1]
        request = {"action": "compile_decision", "session_id": self.state["id"], "revision": runtime.revision - 1,
                   "request_id": str(uuid4()), "data": {"batch_id": batch["id"], "repository": "后端", "decision": "skip"}}
        with self.assertRaisesRegex(WorkflowError, "过期"):
            app.dispatch(request)
        request["revision"] = runtime.revision
        app.dispatch(request)
        runtime.engine.executor.submit(lambda: None).result(timeout=5)
        self.assertTrue(app.dispatch(request)["duplicate"])
        self.assertEqual(runtime.engine.compile_decision.call_count, 1)

    def test_session_override_is_saved_and_locked_when_plan_starts(self):
        self.settings["compile_check"]["enabled"] = True
        app = WebApplication(self.settings)
        self.addCleanup(app.close)
        app.codex_catalog = {"default_model": "模拟模型", "models": [{"model": "模拟模型", "display_name": "模拟模型",
                             "reasoning_efforts": ["high"], "is_default": True}]}
        for override, expected in ((False, False), (None, True)):
            runtime = app.sessions.add(app.sessions.store.new(self.state["item"]))
            runtime.engine.state["snapshot"] = {"sources": [], "materials": [], "blockers": [], "feishu_links": []}
            data = {"model": "模拟模型", "reasoning_effort": "high", "items": [{"kind": "task", "id": 1}]}
            if override is not None:
                data["compile_check_enabled"] = override
            with patch.object(Workflow, "start_plan"):
                result = app.dispatch({"action": "plan", "session_id": runtime.engine.state["id"], "revision": runtime.revision,
                                       "request_id": str(uuid4()), "data": data})
                runtime = app.sessions.get(result["session_id"])
                runtime.engine.executor.submit(lambda: None).result(timeout=5)
            self.assertIs(runtime.engine.state["compile_check"]["enabled"], expected)
            self.assertIs(app.public_settings()["compile_check_enabled"], True)
            with self.assertRaisesRegex(WorkflowError, "已经固定"):
                app.dispatch({"action": "plan", "session_id": result["session_id"], "revision": runtime.revision,
                              "request_id": str(uuid4()), "data": {**data, "compile_check_enabled": not expected}})
