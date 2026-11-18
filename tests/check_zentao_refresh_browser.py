"""隔离检查启动全量读取、分批展示、分类筛选及手动刷新。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from re import compile as regex
import sys
from threading import Event, Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_zentao_refresh import ZentaoRefreshFixture
from workflow.settings import WorkflowError
from workflow.web import LocalServer


def run():
    fixture = ZentaoRefreshFixture()
    fixture.setUp()
    app, flow, client = fixture.application, fixture.flow, fixture.client
    app.connected = True
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assigned_gate, requirement_gate = Event(), Event()
    fail_kinds, calls, errors = set(), [], []
    assigned = [{"kind": kind, "id": 1, "title": f"快速待办{kind}", "status": "active", "project_id": 41, "project_name": "示例项目"}
                for kind in ("story", "task", "bug")]
    def read_assigned(kinds, on_progress):
        calls.append(kinds)
        on_progress("正在读取研发需求、任务、Bug：第 1 页")
        assigned_gate.wait(30)
        return [item for item in assigned if item["kind"] in kinds]
    def read_requirements(kinds, on_progress):
        calls.append(kinds)
        on_progress("正在读取需求：产品 1/2，第 1 页")
        requirement_gate.wait(30)
        if fail_kinds.intersection(kinds):
            raise WorkflowError("禅道需求读取超时")
        return [{"kind": "requirement", "id": 7, "title": "自动加载的用户需求", "status": "active", "project_id": 41,
                 "project_name": "示例项目"}] if "requirement" in kinds else []
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(client, "list_assigned_items", side_effect=read_assigned), \
                patch.object(client, "list_requirements", side_effect=read_requirements), \
                patch.object(client, "login", return_value={"account": client.account}), \
                patch("workflow.engine.timestamp", return_value=now.isoformat()), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.clock.install(time=now - timedelta(minutes=1))
            page.clock.pause_at(now)
            page.add_init_script("sessionStorage.setItem('workflow-page', 'items')")
            page.goto(server.origin)
            card = page.locator(".app-card").filter(has=page.get_by_text("待处理工作项", exact=True))
            rows, sync = card.locator("tbody .item-row"), card.locator(".items-sync")
            refresh = card.get_by_role("button", name=regex("刷新列表"))
            tabs = card.locator(".table-tools .ant-radio-group")
            expect(rows).to_have_count(2)
            app.dispatch(fixture.fixture.request("connect"))
            expect(sync).to_contain_text("正在读取研发需求、任务、Bug")
            expect(tabs.get_by_text("用户需求 读取中", exact=True)).to_be_visible()
            expect(refresh).to_be_disabled()
            page.clock.fast_forward(3000)
            expect(sync).to_contain_text("已用 3 秒")
            page.reload()
            expect(refresh).to_be_disabled()
            assigned_gate.set()
            expect(sync).to_contain_text("正在读取需求：产品 1/2，第 1 页")
            expect(rows).to_have_count(3)
            expect(tabs.get_by_text("任务 1", exact=True)).to_be_visible()
            assert calls == [("story", "task", "bug"), ("epic", "requirement")]
            requirement_gate.set()
            expect(sync).to_contain_text("已更新 4 项工作项")
            expect(rows).to_have_count(4)
            expect(tabs.get_by_text("业务需求 0", exact=True)).to_be_visible()
            tabs.get_by_text("用户需求 1", exact=True).click()
            expect(rows).to_have_count(1)
            expect(rows.first).to_contain_text("自动加载的用户需求")
            page.reload()
            expect(rows).to_have_count(4)
            tabs.get_by_text("用户需求 1", exact=True).click()
            expect(rows).to_have_count(1)
            assert calls.count(("epic", "requirement")) == 1 and len(calls) == 2

            fail_kinds.add("requirement")
            refresh.click()
            expect(sync).to_contain_text("禅道需求读取超时")
            expect(rows).to_have_count(1)
            expect(refresh).to_be_enabled()
            tabs.get_by_text("全部", exact=True).click()
            tabs.get_by_text("用户需求 1", exact=True).click()
            expect(sync).to_contain_text("禅道需求读取超时")
            assert calls.count(("requirement",)) == 1
            fail_kinds.clear()
            refresh.click()
            expect(sync).to_have_class(regex(r"\bant-alert-success\b"))
            expect(refresh).to_be_enabled()
            assert calls.count(("requirement",)) == 2

            tabs.get_by_text("业务需求 0", exact=True).click()
            expect(rows).to_have_count(0)

            tabs.get_by_text("全部", exact=True).click()
            requirement_gate.clear()
            refresh.click()
            expect(sync).to_contain_text("正在读取需求：产品 1/2，第 1 页")
            requirement_gate.set()
            expect(sync).to_contain_text("已更新 4 项工作项")
            expect(refresh).to_be_enabled()
            expect(rows).to_have_count(4)
            assert calls.count(("requirement",)) == 2 and calls.count(("epic", "requirement")) == 2
            assert calls.count(("story", "task", "bug")) == 2
            for width in (1500, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"工作项列表横向溢出：{width}"
            assert not errors, errors
            browser.close()
    finally:
        assigned_gate.set()
        requirement_gate.set()
        flow.executor.submit(lambda: None).result(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
