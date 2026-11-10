"""提示词目录与实际发送内容一致，只读接口不依赖禅道和会话。"""

from copy import deepcopy
from http.client import HTTPConnection
from json import dumps, loads
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch

import test_auto_delivery as delivery
import test_previews as previews
from workflow import prompt_templates, prompts
from workflow.branching import BRANCH_INSTRUCTIONS
from workflow.change_locations import SUMMARY_INSTRUCTIONS
from workflow.commit_messages import COMMIT_INSTRUCTIONS
from workflow.prompt_catalog import prompt_catalog
from workflow.settings import ROOT, WorkflowError
from workflow.web import LocalServer


class PromptCatalogChecks(TestCase):
    def setUp(self):
        self.settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))

    def test_all_stages_and_current_sources_are_present(self):
        catalog = prompt_catalog(self.settings)
        stages = {entry["id"]: entry for entry in catalog["stages"]}
        self.assertEqual(set(stages), {"collect", "branch", "prepare", "plan", "implement", "compile", "compile_fix",
                                       "review", "fix", "commit", "followup", "restore", "answer", "common"})
        self.assertEqual(stages["branch"]["templates"][0]["content"], BRANCH_INSTRUCTIONS)
        self.assertIn((ROOT / self.settings["review"]["prompt_file"]).read_text(encoding="utf-8").strip(),
                      stages["review"]["templates"][0]["content"])
        self.assertEqual(stages["common"]["templates"][0]["content"],
                         (ROOT / "skills/zentao-delivery/SKILL.md").read_text(encoding="utf-8"))
        for name in ("collect", "prepare", "commit"):
            self.assertTrue(stages[name]["controller"])
            self.assertEqual(stages[name]["templates"], [])
        self.assertIn("审查快照", stages["review"]["notes"])
        self.assertIn("结构化答案", stages["answer"]["notes"])

    def test_review_changes_are_read_fresh_and_missing_source_does_not_substitute(self):
        with TemporaryDirectory() as folder, patch("workflow.prompt_catalog.ROOT", Path(folder)):
            root = Path(folder)
            skill = root / "skills/zentao-delivery/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("通用规则", encoding="utf-8")
            review = root / self.settings["review"]["prompt_file"]
            review.parent.mkdir(parents=True)
            for text in ("原 Review", "修改后的 Review"):
                review.write_text(text, encoding="utf-8")
                stages = {entry["id"]: entry for entry in prompt_catalog(self.settings)["stages"]}
                self.assertTrue(stages["review"]["templates"][0]["content"].splitlines()[1].startswith(text))
            review.write_text("", encoding="utf-8")
            with self.assertRaises(WorkflowError):
                prompt_catalog(self.settings)
            review.unlink()
            with self.assertRaises(OSError):
                prompt_catalog(self.settings)

    def test_stage_variants_include_only_applicable_appendages(self):
        stages = {entry["id"]: entry for entry in prompt_catalog(self.settings)["stages"]}
        for stage in stages.values():
            for variant in stage["templates"]:
                text = variant["content"]
                self.assertTrue(variant["condition"])
                if stage["id"] in {"restore", "answer"}:
                    phase = variant["id"]
                    self.assertEqual(SUMMARY_INSTRUCTIONS in text, phase in {"review", "followup"})
                    self.assertEqual(COMMIT_INSTRUCTIONS in text, phase == "review")
        self.assertNotIn(prompts.COMPILE_MANUAL_ANALYSIS, stages["compile"]["templates"][0]["content"])
        self.assertNotIn(prompts.FOLLOWUP_PLAN, stages["plan"]["templates"][0]["content"])

    def test_templates_match_actual_model_inputs(self):
        fixture = delivery.AutoDeliveryChecks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        flow, state = fixture.flow, fixture.state
        original = deepcopy(state)
        stages = {entry["id"]: entry for entry in prompt_catalog(self.settings)["stages"]}
        cases = [("plan", "plan", "plan", prompts.PLAN),
                 ("plan", "followup_plan", "plan", prompts.PLAN + prompts.FOLLOWUP_PLAN),
                 ("implement", "implement", "implement", prompts.IMPLEMENT),
                 ("compile", "automatic", "compile_analysis", prompts.COMPILE_ANALYSIS),
                 ("compile", "manual", "compile_analysis", prompts.COMPILE_MANUAL_ANALYSIS),
                 ("compile_fix", "compile_fix", "compile_fix", prompts.COMPILE_FIX),
                 ("followup", "followup", "followup", prompts.FOLLOWUP),
                 ("review", "review", "review", prompt_templates.REVIEW_INPUT.format(
                     prompt=(ROOT / self.settings["review"]["prompt_file"]).read_text(encoding="utf-8").strip(),
                     review=prompts.REVIEW, previous="【上轮审查结果 JSON；首次为 null】")),
                 ("fix", "fix", "fix", prompt_templates.FIX_INPUT.format(
                     fix=prompts.FIX, chosen="【选中问题 JSON】", note="【人工修复说明】"))]
        cases += [(stage, phase, phase, prompts.RESTORE if stage == "restore" else
                   prompt_templates.ANSWER_INPUT.format(reply="【人工补充回答】"))
                  for stage in ("restore", "answer") for phase in ("plan", "implement", "review", "compile_analysis", "compile_fix")]
        for stage_id, variant_id, phase, text in cases:
            with self.subTest(stage=stage_id, variant=variant_id):
                state.clear()
                state.update(deepcopy(original), phase=phase, title_pending=False)
                flow._start_turn(text)
                sent = flow.rpc.request.call_args.args[1]["input"][0]["text"]
                context = sent.split("\n工作流上下文：\n", 1)[1]
                variant = next(entry for entry in stages[stage_id]["templates"] if entry["id"] == variant_id)
                expected = variant["content"].replace("【需求范围说明：按条件选择一项】", prompt_templates.SCOPE_ITEMS)
                expected = expected.replace("【资料读取指令：按条件选择一项】", prompt_templates.MATERIAL_CHANGED)
                expected = expected.replace("【变化上下文 JSON】", context).replace("【交付轮次】", "1")
                expected = expected.replace("【可引用旧编号 JSON】", "[]").replace("【本轮不修复编号 JSON】", "[]")
                self.assertEqual(sent, expected)

    def test_scope_naming_cached_materials_and_branch_use_actual_conditions(self):
        fixture = delivery.AutoDeliveryChecks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        flow, state = fixture.flow, fixture.state
        initial = deepcopy(state)
        cases = [({}, prompt_templates.SCOPE_ITEMS),
                 ({"item": None, "items": []}, prompt_templates.SCOPE_CONVERSATION),
                 ({"followup": {"in_progress": False}}, prompt_templates.SCOPE_FOLLOWUP),
                 ({"followup": {"in_progress": True}}, prompt_templates.SCOPE_CONTINUATION)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                state.clear()
                state.update(deepcopy(initial), phase="plan", title_pending=False, **changes)
                flow._start_turn(prompts.PLAN)
                sent = flow.rpc.request.call_args.args[1]["input"][0]["text"]
                self.assertTrue(sent.startswith("当前阶段：plan。" + expected))
                self.assertIn(prompt_templates.MATERIAL_CHANGED, sent)
                flow._start_turn(prompts.PLAN)
                cached = flow.rpc.request.call_args.args[1]["input"][0]["text"]
                self.assertIn(prompt_templates.MATERIAL_CACHED, cached)
                self.assertNotIn(prompt_templates.MATERIAL_CHANGED, cached)
        state.clear()
        state.update(deepcopy(initial), phase="plan", title_pending=True)
        flow._start_turn(prompts.PLAN)
        sent = flow.rpc.request.call_args.args[1]["input"][0]["text"]
        self.assertIn(prompt_templates.MATERIAL_NAMING.format(max_length=20), sent)
        self.assertNotIn(prompt_templates.MATERIAL_CHANGED, sent)
        state.clear()
        state.update(deepcopy(initial), phase="branch", project={"id": 41, "name": "示例项目"},
                     branch_resolution={"repositories": [{"status": "pending", "role": "frontend", "refreshed": False, "catalog": []}]})
        flow._start_turn("控制器中的分支记录")
        sent = flow.rpc.request.call_args.args[1]["input"][0]["text"]
        self.assertTrue(sent.startswith("依据线程中的分支规则分析上下文，按 Schema 返回。\n"))
        self.assertNotIn("控制器中的分支记录", sent)
        self.assertNotIn("当前阶段：", sent)
        self.assertEqual(loads(sent.split("\n", 1)[1])["project"]["id"], 41)

    def test_http_is_authenticated_read_only_and_has_no_workflow_side_effects(self):
        fixture = previews.PreviewChecks()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.application
        server = LocalServer(("127.0.0.1", 0), app)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Thread(target=server.serve_forever, daemon=True).start()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(client.close)
        settings = deepcopy(app.settings)
        with patch.object(app.sessions.zentao, "_fetch", side_effect=AssertionError("不得访问禅道")), \
                patch.object(app.sessions.connection, "ensure_rpc", side_effect=AssertionError("不得调用模型")):
            client.request("GET", "/api/prompts")
            response = client.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            client.request("GET", "/")
            response = client.getresponse()
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            response.read()
            headers = {"Cookie": cookie}
            before = {str(path.relative_to(fixture.root)): path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()}
            client.request("GET", "/api/prompts", headers=headers)
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(len(loads(response.read())["stages"]), 14)
            for query in ("path=settings.json", "path="):
                client.request("GET", "/api/prompts?" + query, headers=headers)
                response = client.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            client.request("POST", "/api/prompts", body=dumps({"content": "禁止保存"}),
                           headers={**headers, "X-Workflow-Token": app.csrf_token, "Content-Type": "application/json"})
            response = client.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            after = {str(path.relative_to(fixture.root)): path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(settings, app.settings)
        self.assertEqual(app.sessions.runtimes, {})
        self.assertEqual(app.sessions.previews, {})
