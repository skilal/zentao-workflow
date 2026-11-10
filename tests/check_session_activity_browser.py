"""隔离检查会话排序、120 小时边界、实时恢复及桌面和窄屏样式。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from re import compile as regex
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_session_activity import SessionActivityFixture
from workflow.web import LocalServer


def run():
    fixture = SessionActivityFixture()
    fixture.setUp()
    app = fixture.application
    now = datetime(2026, 11, 10, 4, tzinfo=timezone.utc)
    message = lambda role, time: {"role": role, "time": time, "text": "会话时间检查"}
    inactive = fixture.add_session("inactive-session", [message("用户", "2026-11-04T04:00:00+00:00"),
                                                      message("Codex", "2026-11-10T03:00:00+00:00")])
    fixture.add_session("tie-z", [message("用户", "2026-11-09T02:00:00+00:00")])
    tie = fixture.add_session("tie-a", [message("用户", "2026-11-09T10:00:00+08:00")])
    fixture.add_session("no-user", [message("Codex", "2026-11-09T00:00:00+00:00")])
    fixture.add_session("boundary-session", [message("用户", "2026-11-05T04:00:00+00:00")])
    fixture.add_session("empty-session", [message("系统", "2026-11-10T04:00:00+00:00")])
    fixture.add_session("other-project", [message("用户", "2026-11-10T03:30:00+00:00")], 42)
    fixture.add_session("no-project", [message("用户", "2026-11-10T04:00:00+00:00")], 0)
    app.default_session_id = None
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = ROOT / ".local" / "session-activity-qa"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050}, timezone_id="Asia/Shanghai")
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.clock.install(time=now - timedelta(minutes=1))
            page.clock.pause_at(now)
            page.goto(server.origin)
            projects = page.locator(".session-project")
            expect(projects).to_have_count(3)
            assert projects.evaluate_all("nodes => nodes.map(node => node.dataset.projectId)") == ["42", "41", "0"]
            group = page.locator('.session-project[data-project-id="41"]')
            expect(group.locator(".session-entry-custom-title")).to_have_text(
                ["inactive-session", "tie-z", "tie-a", "no-user", "boundary-session", "empty-session"])
            entry = lambda title: group.locator(".session-entry").filter(has=page.locator(".session-entry-custom-title", has_text=title))
            old, boundary, empty, no_user = [entry(title) for title in ("inactive-session", "boundary-session", "empty-session", "no-user")]
            expect(old).to_have_class(regex(r"\binactive\b"))
            expect(old.locator(".session-inactive-tag")).to_have_text("超过5天未对话")
            expect(old.locator('[title="最近对话时间"]')).to_have_text("2026/11/10 11:00:00")
            expect(boundary).not_to_have_class(regex(r"\binactive\b"))
            expect(no_user).not_to_have_class(regex(r"\binactive\b"))
            expect(empty.locator('[title="最近对话时间"]')).to_have_text("尚无对话")

            # 无交互时跨过 120 小时，通过分钟计时自动置灰。
            page.clock.fast_forward(60000)
            expect(boundary).to_have_class(regex(r"\binactive\b"))
            # 前台恢复立即重算，无须等待下一次分钟刷新。
            page.clock.set_system_time(now)
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            expect(boundary).not_to_have_class(regex(r"\binactive\b"))
            page.clock.set_system_time(now + timedelta(hours=1))
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(boundary).to_have_class(regex(r"\binactive\b"))

            # AI 回复更新分区排序和显示时间，仍保持用户过期状态。
            with patch("workflow.store.timestamp", return_value="2026-11-10T05:00:00+00:00"):
                inactive.engine.note("Codex", "新的 AI 回复")
            expect(old.locator('[title="最近对话时间"]')).to_have_text("2026/11/10 13:00:00")
            expect(old).to_have_class(regex(r"\binactive\b"))
            expect(projects.first).to_have_attribute("data-project-id", "41")
            old.click()
            expect(old).to_have_class(regex(r"\bselected inactive\b"))
            expect(old.get_by_role("button", name="修改会话标题", exact=True)).to_be_enabled()
            expect(old.get_by_role("button", name="删除", exact=True)).to_be_enabled()
            colors = old.evaluate("node => ({title: getComputedStyle(node.querySelector('.session-entry-custom-title')).color, shadow: getComputedStyle(node).boxShadow})")
            assert colors["title"] == "rgb(138, 148, 166)"
            assert "inset" in colors["shadow"]
            for width in (1500, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"页面横向溢出：{width}"
                page.screenshot(path=str(output / f"会话置灰-{width}.png"), full_page=True, animations="disabled")

            # 用户发言立即恢复，刷新后时间、排序与置灰状态一致。
            with patch("workflow.store.timestamp", return_value="2026-11-10T05:00:00+00:00"):
                inactive.engine.note("用户", "继续对话")
            expect(old).to_have_class(regex(r"\bselected\b"))
            expect(old).not_to_have_class(regex(r"\binactive\b"))
            expect(old.locator(".session-inactive-tag")).to_have_count(0)
            page.reload()
            expect(old).to_have_class(regex(r"\bselected\b"))
            expect(old).not_to_have_class(regex(r"\binactive\b"))
            expect(old.locator('[title="最近对话时间"]')).to_have_text("2026/11/10 13:00:00")
            expect(projects.first).to_have_attribute("data-project-id", "41")
            expect(empty.locator('[title="最近对话时间"]')).to_have_text("尚无对话")
            with patch("workflow.store.timestamp", return_value="2026-11-10T05:01:00+00:00"):
                tie.engine.note("用户", "新对话提升到分区顶部")
            expect(group.locator(".session-entry-custom-title").first).to_have_text("tie-a")
            page.reload()
            expect(group.locator(".session-entry-custom-title").first).to_have_text("tie-a")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
