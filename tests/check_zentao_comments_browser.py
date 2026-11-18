"""隔离检查按编号读取与已完成会话刷新评论、图片预览。"""

from copy import deepcopy
from pathlib import Path
import sys
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_previews import PreviewChecks, PNG
from workflow.web import LocalServer


def run():
    fixture = PreviewChecks()
    fixture.setUp()
    app = fixture.application
    app.connected = True
    client = app.sessions.zentao
    client.account, client.token = "测试账号", "模拟登录状态"
    detail = {"id": 99, "name": "已完成任务", "assignedTo": "其他账号", "status": "done", "desc": "任务正文", "actions": [
        {"id": 101, "actor": "张三", "date": "2026-11-18 10:30:00", "action": "commented",
         "comment": '<p>关闭后请修改保存逻辑<img src="/file-read-21.png">参照评论截图</p>'}]}
    state = app.sessions.store.new({"kind": "task", "id": 99, "title": "历史完成任务"})
    state.update(phase="complete", stage="已完成（未审查）", thread_id="原线程",
                 codex={"model": "示例模型", "reasoning_effort": "high"},
                 repos=[{"name": "前端", "role": "frontend", "path": str(fixture.root / "frontend"),
                         "baseline": "0" * 40, "branch": "示例分支", "workspace_owned": False}],
                 snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
    runtime = app.sessions.add(state)
    runtime.engine.save()
    app.default_session_id = state["id"]
    server = LocalServer(("127.0.0.1", 0), app)
    Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    output = ROOT / ".local" / "zentao-comments-qa"
    output.mkdir(parents=True, exist_ok=True)
    def fetch(path, payload=None, binary=False):
        if binary:
            assert path == "api.php/v1/files/21"
            return PNG, "image/png"
        assert path == "api.php/v1/tasks/99"
        return deepcopy(detail)
    try:
        with patch.object(client, "_fetch", side_effect=fetch), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            page.get_by_role("button", name="工作项资料", exact=True).click()
            drawer = page.get_by_role("dialog", name="工作项资料", exact=True)
            drawer.get_by_role("button", name="刷新正文与评论").click()
            expect(drawer.locator(".description-text")).to_contain_text("关闭后请修改保存逻辑")
            expect(drawer.locator(".description-text")).to_contain_text("张三")
            expect(drawer.locator(".description-text")).to_contain_text("2026-11-18 10:30:00")
            drawer.locator(".attachment img").evaluate("image => image.decode()")
            drawer.locator(".ant-image").click()
            expect(page.locator(".ant-image-preview-wrap")).to_be_visible()
            page.locator(".ant-image-preview-close").click()
            page.screenshot(path=str(output / "completed-comments.png"))
            assert state["thread_id"] == "原线程"
            assert state["phase"] == "complete"
            drawer.get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("button", name="新建会话", exact=True).click()
            page.get_by_role("menuitem", name="工作项与资料").click()
            page.get_by_role("button", name="按编号读取", exact=True).click()
            dialog = page.locator(".ant-modal-confirm")
            expect(dialog).to_contain_text("按编号读取工作项")
            dialog.get_by_role("spinbutton", name="工作项编号").fill("99")
            dialog.get_by_role("button", name="读取资料", exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.locator(".description-text")).to_contain_text("关闭后请修改保存逻辑")
            expect(page.locator(".attachment img")).to_be_visible()
            expect(page.get_by_role("button", name="开始需求分析与计划", exact=True)).to_be_enabled()
            detail["actions"].append({"id": 102, "actor": "李四", "date": "2026-11-18 11:00:00", "comment": "新增评论也要读取"})
            page.get_by_role("button", name="刷新正文与评论", exact=True).click()
            expect(page.locator(".description-text")).to_contain_text("新增评论也要读取")
            assert len(app.sessions.runtimes) == 1
            page.screenshot(path=str(output / "by-id-comments.png"))
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        fixture.doCleanups()
    print("评论读取、已完成会话刷新及图片预览页面检查通过")


if __name__ == "__main__":
    run()
