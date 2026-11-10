"""隔离检查项目名称、零编号和多项目待确认的展示与筛选。"""

from pathlib import Path
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_previews import PreviewChecks
from workflow.web import LocalServer


def run():
    fixture = PreviewChecks()
    fixture.setUp()
    app = fixture.application
    app.connected = True
    app.items = [
        {"kind": "requirement", "id": 1, "title": "缺少项目关联", "project": 0, "project_id": 0, "project_name": ""},
        {"kind": "requirement", "id": 2, "title": "已补齐项目名称", "project": 0, "project_id": 41, "project_name": "名称正确的项目"},
        {"kind": "bug", "id": 3, "title": "仅编号的旧展示字段", "project": "42", "project_id": 42, "project_name": "问题所属项目"},
        {"kind": "requirement", "id": 4, "title": "存在多个候选项目", "project": 0, "project_id": 0, "project_name": "",
         "project_candidates": [{"id": 41, "name": "名称正确的项目"}, {"id": 42, "name": "问题所属项目"}]},
        {"kind": "story", "id": 5, "title": "字符串零编号", "project": "0", "project_id": 0, "project_name": ""}]
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    output = ROOT / ".local" / "work-item-projects-qa"
    output.mkdir(parents=True, exist_ok=True)
    try:
        with patch.object(app.sessions.zentao, "_fetch", side_effect=AssertionError("列表展示和筛选不应读取禅道")), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.origin)
            card = page.locator(".app-card").filter(has=page.get_by_text("待处理工作项", exact=True))
            rows = card.locator("tbody .item-row")
            expect(rows).to_have_count(5)
            expect(rows.nth(0).get_by_text("待确认项目", exact=True)).to_be_visible()
            expect(rows.nth(1).get_by_text("名称正确的项目", exact=True)).to_be_visible()
            expect(rows.nth(2).get_by_text("问题所属项目", exact=True)).to_be_visible()
            expect(rows.nth(3).get_by_text("待确认项目", exact=True)).to_be_visible()
            expect(rows.nth(4).get_by_text("待确认项目", exact=True)).to_be_visible()
            expect(card.get_by_text("项目 #0", exact=True)).to_have_count(0)
            for width in (1500, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"项目列横向溢出：{width}"
                page.screenshot(path=str(output / f"项目显示-{width}.png"), full_page=True, animations="disabled")
            page.set_viewport_size({"width": 1500, "height": 1050})
            card.locator(".table-tools .ant-select-selector").click()
            page.locator(".ant-select-dropdown:visible").get_by_text("待确认项目", exact=True).click()
            expect(rows).to_have_count(3)
            card.locator(".table-tools .ant-select-selector").click()
            page.locator(".ant-select-dropdown:visible").get_by_text("名称正确的项目", exact=True).click()
            expect(rows).to_have_count(1)
            expect(rows.first).to_contain_text("已补齐项目名称")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
