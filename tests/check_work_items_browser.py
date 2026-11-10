"""隔离检查待处理工作项分页及组合筛选，不连接外部服务。"""

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
        {"kind": "task" if number <= 60 else "bug", "id": number,
         "title": f"待办{number:02d}", "status": "wait", "priority": 2,
         "project_id": 41 if number <= 35 else 42,
         "project_name": "示例项目甲" if number <= 35 else "示例项目乙",
         "project": "示例项目甲" if number <= 35 else "示例项目乙"}
        for number in range(1, 63)
    ]
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, actions = [], []
    output = ROOT / ".local" / "work-items-pagination"
    output.mkdir(parents=True, exist_ok=True)
    try:
        with patch.object(app.sessions.zentao, "_fetch", side_effect=AssertionError("列表操作不得访问禅道")), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: actions.append(request.post_data_json) if request.url.endswith("/api/actions") else None)
            page.add_init_script("sessionStorage.setItem('workflow-page', 'items')")
            page.goto(server.origin)
            card = page.locator(".app-card").filter(has=page.get_by_text("待处理工作项", exact=True))
            rows = card.locator("tbody .item-row")
            pagination = card.locator(".ant-pagination")
            project = page.get_by_role("combobox", name="筛选所属项目")
            search = page.get_by_placeholder("搜索编号或标题")

            expect(rows).to_have_count(10)
            expect(pagination).to_contain_text("共 62 项")
            pagination.get_by_title("7", exact=True).click()
            expect(rows).to_have_count(2)
            pagination.locator(".ant-select-selector").click()
            for label in ("10 条/页", "20 条/页", "50 条/页"):
                expect(page.get_by_role("option", name=label, exact=True)).to_be_attached()
            page.get_by_text("20 条/页", exact=True).last.click()
            expect(rows).to_have_count(20)
            expect(rows.first).to_contain_text("待办01")
            pagination.get_by_title("4", exact=True).click()
            expect(rows).to_have_count(2)
            pagination.locator(".ant-select-selector").click()
            page.get_by_text("50 条/页", exact=True).last.click()
            expect(rows).to_have_count(50)
            expect(rows.first).to_contain_text("待办01")

            card.locator(".table-tools .ant-select-selector").click()
            page.get_by_text("示例项目甲", exact=True).last.click()
            expect(rows).to_have_count(35)
            expect(pagination).to_contain_text("共 35 项")
            pagination.locator(".ant-select-selector").click()
            page.get_by_text("10 条/页", exact=True).last.click()
            pagination.get_by_title("4", exact=True).click()
            expect(rows).to_have_count(5)
            card.locator(".table-tools .ant-select-selector").click()
            project.fill("项目乙")
            page.get_by_text("示例项目乙", exact=True).last.click()
            expect(rows).to_have_count(10)
            expect(rows.first).to_contain_text("待办36")
            expect(pagination).to_contain_text("共 27 项")
            pagination.get_by_title("3", exact=True).click()
            expect(rows).to_have_count(7)
            card.get_by_text("Bug 2", exact=True).click()
            expect(rows).to_have_count(2)
            search.fill("待办62")
            expect(rows).to_have_count(1)
            expect(rows.first).to_contain_text("待办62")
            card.locator(".table-tools .ant-select-selector").click()
            page.get_by_text("示例项目甲", exact=True).last.click()
            expect(rows).to_have_count(0)
            expect(card.get_by_text("暂无符合条件的待处理工作项", exact=True)).to_be_visible()

            card.locator(".table-tools .ant-select-selector").click()
            page.get_by_text("全部项目", exact=True).last.click()
            search.fill("")
            card.get_by_text("全部", exact=True).click()
            pagination.get_by_title("7", exact=True).click()
            search.fill("待办0")
            expect(rows).to_have_count(9)
            expect(rows.first).to_contain_text("待办01")
            search.fill("")
            pagination.get_by_title("7", exact=True).click()
            with app.condition:
                app.items = app.items[:5]
                app._publish("items", app.items)
            expect(rows).to_have_count(5)
            expect(rows.first).to_contain_text("待办01")
            page.screenshot(path=str(output / "desktop.png"), full_page=True)
            assert not errors, errors
            assert not actions, actions
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
