"""隔离网页检查：默认选择、会话覆盖、失败重试及历史配置确认。"""

from copy import deepcopy
from json import dumps, loads
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workflow.settings import WorkflowError
from workflow.web import LocalServer, WebApplication
from test_codex_models import CATALOG


def run():
    output = ROOT / ".local" / "model-selection-qa"
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as temporary, patch("workflow.store.LOCAL", Path(temporary)), patch("workflow.web.LOCAL", Path(temporary)):
        settings = loads((ROOT / "settings.example.json").read_text(encoding="utf-8"))
        prompt = Path(temporary) / settings["review"]["prompt_file"]
        prompt.parent.mkdir(parents=True)
        prompt.write_text((ROOT / settings["review"]["prompt_file"]).read_text(encoding="utf-8"), encoding="utf-8")
        app = WebApplication(settings)
        app.connected = True
        app.items = [{"kind": "task", "id": number, "title": f"模型选择示例{number}", "project_id": 41,
                      "project_name": "示例项目", "status": "wait"} for number in (7, 8)]
        catalog_mode = {"value": "normal"}

        def catalog(refresh=False):
            if catalog_mode["value"] == "error":
                raise WorkflowError("模拟模型读取失败，请刷新重试")
            if catalog_mode["value"] == "empty":
                return {"default_model": None, "models": []}
            return deepcopy(CATALOG)

        def snapshot(items, directory):
            return {"project": {"id": 41, "name": "示例项目", "candidates": []}, "materials": [],
                    "sources": [{"kind": item["kind"], "id": item["id"], "title": item["title"], "content": "脱敏资料",
                                 "raw": {}, "material_ids": []} for item in items], "blockers": [], "feishu_links": []}

        def start(engine, supplement, manual=False):
            engine.state.update(stage="已暂停", phase="plan")
            engine.save()

        def save_settings(value):
            (Path(temporary) / "settings.json").write_text(dumps(value), encoding="utf-8")

        server = LocalServer(("127.0.0.1", 0), app)
        Thread(target=server.serve_forever, daemon=True).start()
        errors = []
        try:
            with patch.object(app, "codex_models", side_effect=catalog), \
                 patch("workflow.zentao.ZentaoClient.collect_many", side_effect=snapshot), \
                 patch("workflow.engine.Workflow.start_plan", new=start), \
                 patch("workflow.web.ROOT", Path(temporary)), patch("workflow.web.save_settings", side_effect=save_settings), \
                 sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path="C:/Users/86135/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe")
                context = browser.new_context(viewport={"width": 1500, "height": 1050})
                page = context.new_page()
                page.set_default_timeout(10000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(server.origin)
                page.locator('.navigation .ant-menu-item').last.click()
                expect(page.get_by_text("实际模型：model-a", exact=True)).to_be_visible()

                def choose(label, text):
                    page.locator(".codex-selection .ant-select").nth(0 if label == "模型" else 1).click()
                    page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(has_text=text).click()

                choose("模型", "模型乙")
                expect(page.get_by_role("button", name="保存设置", exact=True)).to_be_disabled()
                choose("推理强度", "medium")
                page.get_by_role("button", name="保存设置", exact=True).click()
                expect(page.get_by_text("设置已提交", exact=True)).to_be_visible()
                assert loads((Path(temporary) / "settings.json").read_text())["codex"]["model"] == "model-b"
                page.reload()
                expect(page.get_by_text("实际模型：model-b", exact=True)).to_be_visible()
                page.screenshot(path=str(output / "default-settings.png"), full_page=True)

                # 失败与空列表必须禁止保存，显式刷新后恢复。
                catalog_mode["value"] = "error"
                page.get_by_role("button", name="刷新模型列表", exact=True).click()
                expect(page.get_by_text("读取模型失败", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="保存设置", exact=True)).to_be_disabled()
                catalog_mode["value"] = "empty"
                page.get_by_role("button", name="刷新模型列表", exact=True).click()
                expect(page.get_by_text("本机 Codex 未返回可用模型，请检查账号后刷新列表。", exact=True)).to_be_visible()
                catalog_mode["value"] = "normal"
                page.get_by_role("button", name="刷新模型列表", exact=True).click()
                expect(page.get_by_role("button", name="保存设置", exact=True)).to_be_enabled()

                app.connected = True
                app.items = [{"kind": "task", "id": number, "title": f"模型选择示例{number}", "project_id": 41,
                              "project_name": "示例项目", "status": "wait"} for number in (7, 8)]
                page.locator('.navigation .ant-menu-item').nth(0).click()
                page.reload()
                page.locator("tbody tr").filter(has_text="模型选择示例7").get_by_role("checkbox").check()
                try:
                    expect(page.get_by_text("实际模型：model-b", exact=True)).to_be_visible()
                finally:
                    page.screenshot(path=str(output / "selection-check.png"), full_page=True)
                    (output / "selection-check.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
                choose("模型", "模型甲")
                expect(page.get_by_role("button", name="开始需求分析与计划", exact=True)).to_be_disabled()
                choose("推理强度", "high")
                page.screenshot(path=str(output / "session-selection.png"), full_page=True)
                page.get_by_role("button", name="开始需求分析与计划", exact=True).click()
                expect(page.get_by_text("模型：model-a · 推理强度：高（high）", exact=True)).to_be_visible()
                first = next(iter(app.sessions.runtimes.values()))
                assert first.engine.state["codex"] == {"model": "model-a", "reasoning_effort": "high"}
                assert app.settings["codex"]["model"] == "model-b"
                page.reload()
                expect(page.get_by_text("模型：model-a · 推理强度：高（high）", exact=True)).to_be_visible()
                page.screenshot(path=str(output / "saved-session.png"), full_page=True)

                # 旧会话明确确认一次；保存不会启动真实模型请求。
                item = {"kind": "task", "id": 9, "title": "历史模型待确认", "project_id": 41, "project_name": "示例项目"}
                state = app.sessions.store.new(item)
                state.update(stage="已暂停", phase="plan", snapshot=snapshot([item], None), plan_started_at="2026-10-29T00:00:00+08:00")
                legacy = app.sessions.add(state)
                legacy.engine.save()
                page.reload()
                page.locator(".session-entry").filter(has_text="历史模型待确认").click()
                expect(page.get_by_text("确认历史会话的模型配置", exact=True)).to_be_visible()
                page.get_by_role("button", name="保存会话模型配置", exact=True).click()
                expect(page.get_by_text("模型：model-b · 推理强度：中（medium）", exact=True)).to_be_visible()
                assert legacy.engine.state["codex"] == {"model": "model-b", "reasoning_effort": "medium"}
                for width in (1000, 720, 390):
                    page.set_viewport_size({"width": width, "height": 1050})
                    expect(page.get_by_text("模型：model-b · 推理强度：中（medium）", exact=True)).to_be_visible()
                    page.screenshot(path=str(output / f"session-{width}.png"), full_page=True)
                assert not errors, errors
                context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            app.close()


if __name__ == "__main__":
    run()
