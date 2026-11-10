"""隔离检查禅道刷新阶段、耗时、重复点击及失败时保留原列表。"""

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
    assigned_gate, requirements_gate = Event(), Event()
    failed = [False]
    assigned = [{"kind": kind, "id": 1, "title": f"新列表{kind}", "status": "active", "project_id": 41}
                for kind in ("story", "task", "bug")]
    requirements = [{"kind": kind, "id": 1, "title": f"新列表{kind}", "status": "active", "project_id": 41}
                    for kind in ("epic", "requirement")]
    def read_assigned(on_progress):
        on_progress("正在读取研发需求、任务、Bug：第 1 页")
        assigned_gate.wait(30)
        return assigned
    def read_requirements(kinds, on_progress):
        assert kinds == ("epic", "requirement")
        on_progress("正在读取用户需求：产品 1/2，第 1 页")
        requirements_gate.wait(30)
        if failed[0]:
            raise WorkflowError("禅道需求读取超时")
        return requirements
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    output = ROOT / ".local" / "zentao-refresh-qa"
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
            expect(rows).to_have_count(2)
            refresh.click()
            expect(sync).to_contain_text("正在读取研发需求、任务、Bug：第 1 页")
            expect(refresh).to_be_disabled()
            expect(rows).to_have_count(2)
            page.clock.fast_forward(3000)
            expect(sync).to_contain_text("已用 3 秒")
            page.reload()
            expect(sync).to_contain_text("正在读取研发需求、任务、Bug：第 1 页")
            expect(refresh).to_be_disabled()
            assigned_gate.set()
            expect(sync).to_contain_text("正在读取用户需求：产品 1/2，第 1 页")
            expect(rows).to_have_count(2)
            for width in (1500, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"刷新提示横向溢出：{width}"
                page.screenshot(path=str(output / f"读取进度-{width}.png"), full_page=True, animations="disabled")
            requirements_gate.set()
            expect(sync).to_contain_text("已更新 5 项工作项")
            expect(sync).to_have_class(regex(r"\bant-alert-success\b"))
            expect(rows).to_have_count(5)
            expect(refresh).to_be_enabled()
            failed[0] = True
            refresh.click()
            expect(sync).to_contain_text("禅道需求读取超时")
            expect(sync).to_have_class(regex(r"\bant-alert-error\b"))
            expect(sync).to_contain_text("本次未更新列表")
            expect(rows).to_have_count(5)
            expect(refresh).to_be_enabled()
            page.reload()
            expect(sync).to_contain_text("禅道需求读取超时")
            expect(rows).to_have_count(5)
            assert not errors, errors
            browser.close()
    finally:
        assigned_gate.set()
        requirements_gate.set()
        flow.executor.submit(lambda: None).result(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
