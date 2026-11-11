"""隔离检查快速刷新、按需读取、同轮复用、空结果缓存及手动重试。"""

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
        if kinds[0] in fail_kinds:
            raise WorkflowError("禅道需求读取超时")
        return [{"kind": "requirement", "id": 7, "title": "按需读取的用户需求", "status": "active", "project_id": 41,
                 "project_name": "示例项目"}] if kinds == ("requirement",) else []
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = ROOT / ".local" / "on-demand-requirements-qa"
    output.mkdir(parents=True, exist_ok=True)
    try:
        with patch.object(client, "list_assigned_items", side_effect=read_assigned), \
                patch.object(client, "list_requirements", side_effect=read_requirements), \
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
            expect(tabs.get_by_text("用户需求 未加载", exact=True)).to_be_visible()
            refresh.click()
            expect(sync).to_contain_text("正在读取研发需求、任务、Bug")
            expect(refresh).to_be_disabled()
            page.clock.fast_forward(3000)
            expect(sync).to_contain_text("已用 3 秒")
            page.reload()
            expect(refresh).to_be_disabled()
            assigned_gate.set()
            expect(sync).to_contain_text("已更新 3 项工作项")
            expect(rows).to_have_count(3)
            assert calls == [("story", "task", "bug")]

            tabs.get_by_text("用户需求 未加载", exact=True).click()
            expect(tabs.get_by_text("用户需求 读取中", exact=True)).to_be_visible()
            expect(sync).to_contain_text("正在读取需求：产品 1/2，第 1 页")
            expect(refresh).to_be_disabled()
            requirement_gate.set()
            expect(rows).to_have_count(1)
            expect(rows.first).to_contain_text("按需读取的用户需求")
            expect(sync).to_contain_text("已更新 1 项工作项")
            expect(refresh).to_be_enabled()
            tabs.get_by_text("全部", exact=True).click()
            expect(rows).to_have_count(4)
            tabs.get_by_text("用户需求 1", exact=True).click()
            expect(rows).to_have_count(1)
            page.reload()
            expect(rows).to_have_count(4)
            tabs.get_by_text("用户需求 1", exact=True).click()
            expect(rows).to_have_count(1)
            assert calls.count(("requirement",)) == 1

            fail_kinds.add("requirement")
            refresh.click()
            expect(sync).to_contain_text("禅道需求读取超时")
            expect(rows).to_have_count(1)
            expect(refresh).to_be_enabled()
            tabs.get_by_text("全部", exact=True).click()
            tabs.get_by_text("用户需求 1", exact=True).click()
            expect(sync).to_contain_text("禅道需求读取超时")
            assert calls.count(("requirement",)) == 2
            fail_kinds.clear()
            refresh.click()
            expect(sync).to_have_class(regex(r"\bant-alert-success\b"))
            expect(refresh).to_be_enabled()
            assert calls.count(("requirement",)) == 3

            # 首次失败不自动重试；手动重试后的空列表也要在本轮复用。
            fail_kinds.add("epic")
            tabs.get_by_text("业务需求 未加载", exact=True).click()
            expect(tabs.get_by_text("业务需求 读取失败", exact=True)).to_be_visible()
            expect(sync).to_contain_text("禅道需求读取超时")
            expect(refresh).to_be_enabled()
            tabs.get_by_text("全部", exact=True).click()
            tabs.get_by_text("业务需求 读取失败", exact=True).click()
            expect(sync).to_contain_text("禅道需求读取超时")
            assert calls.count(("epic",)) == 1
            fail_kinds.clear()
            refresh.click()
            expect(tabs.get_by_text("业务需求 0", exact=True)).to_be_visible()
            expect(sync).to_contain_text("已更新 0 项工作项")
            expect(refresh).to_be_enabled()
            tabs.get_by_text("全部", exact=True).click()
            tabs.get_by_text("业务需求 0", exact=True).click()
            expect(rows).to_have_count(0)
            assert calls.count(("epic",)) == 2

            tabs.get_by_text("全部", exact=True).click()
            refresh.click()
            expect(sync).to_contain_text("已更新 3 项工作项")
            expect(refresh).to_be_enabled()
            expect(rows).to_have_count(4)
            assert calls.count(("requirement",)) == 3 and calls.count(("epic",)) == 2
            assert calls.count(("story", "task", "bug")) == 2
            for width in (1500, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"按需读取提示横向溢出：{width}"
                page.screenshot(path=str(output / f"按需读取-{width}.png"), full_page=True, animations="disabled")
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
