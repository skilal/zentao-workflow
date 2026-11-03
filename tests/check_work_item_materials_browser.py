"""隔离检查会话资料抽屉及现有附件展示，不连接禅道或模型。"""

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
    output = ROOT / ".local" / "work-item-materials-qa"
    output.mkdir(parents=True, exist_ok=True)
    errors, actions = [], []
    runtimes = []
    for number in (7, 9, 10, 11, None):
        item = {"kind": "task", "id": number, "title": f"资料会话{number}"} if number else None
        state = app.sessions.store.new(item)
        state.update(project={"id": 41, "name": "示例项目", "candidates": []},
                     snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                     repos=[{"name": "前端", "role": "frontend", "path": str(fixture.root / "frontend"),
                             "baseline": "0" * 40, "branch": "示例分支", "workspace_owned": False}],
                     phase="complete", stage="已完成（未审查）", thread_id=f"模拟线程{number}",
                     codex={"model": "示例模型", "reasoning_effort": "high"})
        if number == 7:
            state["items"].append({"kind": "task", "id": 8, "title": "联合开发任务"})
            state["snapshot"].update(sources=[
                {"kind": "task", "id": 7, "title": "资料会话7", "selected": True,
                 "content": "【desc】已保存的业务规则\n【verify】按范围查询", "material_ids": [42, 43, 44]},
                {"kind": "task", "id": 8, "title": "联合开发任务", "selected": True,
                 "content": "第二个任务的说明", "material_ids": []},
                {"kind": "story", "id": 31, "title": "关联需求" * 30, "selected": False,
                 "version": 2, "note": "需求关联说明", "content": "关联需求正文\n" * 30, "material_ids": []}],
                blockers=["示例待补充规则"], feishu_links=["https://example.feishu.cn/docx/reference"])
            folder = app.sessions.store.directory(state["id"]) / "attachments"
            folder.mkdir(parents=True)
            for name, data in (("42.png", PNG), ("43.zip", b"PK-reference"), ("44.txt", "普通附件正文".encode()),
                               ("scan.pdf", b"%PDF-reference"), ("page.png", PNG)):
                (folder / name).write_bytes(data)
            state["snapshot"]["materials"] = [
                {"file_id": 42, "title": "示例图片", "path": str(folder / "42.png"), "extension": "png", "image": True},
                {"file_id": 43, "title": "验收资料.zip", "path": str(folder / "43.zip"), "extension": "zip", "image": False,
                 "archive_entries": [{"title": "扫描件.pdf", "path": str(folder / "scan.pdf"), "image": False,
                                      "pdf_pages": [{"title": "扫描件.pdf 第 1 页", "page": 1, "path": str(folder / "page.png"), "image": True}]}]},
                {"file_id": 44, "title": "说明.txt", "path": str(folder / "44.txt"), "extension": "txt", "image": False}]
            state["history"] = [{"time": state["updated_at"], "role": "用户", "text": f"会话阅读内容{index}\n" * 5}
                                for index in range(20)]
        elif number == 9:
            state["snapshot"]["sources"] = [{"kind": "task", "id": 9, "title": "资料会话9",
                "selected": True, "content": "另一个会话的独立资料", "material_ids": []}]
        elif number == 11:
            del state["snapshot"]
        runtime = app.sessions.add(state)
        runtime.engine.save()
        runtimes.append(runtime)
    first, _, empty, missing, free = runtimes
    app.default_session_id = first.engine.state["id"]
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(app.sessions.zentao, "_fetch", side_effect=AssertionError("查看资料不得访问禅道")), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: actions.append(request.post_data_json) if request.url.endswith("/api/actions") else None)
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            button = page.locator(".workflow-strip-actions").get_by_role("button", name="工作项资料", exact=True)
            drawer = page.get_by_role("dialog", name="工作项资料", exact=True)
            reply = page.get_by_placeholder("输入问题、需要获取的信息或修改要求，可粘贴图片…")
            reply.fill("保留未发送的草稿")
            feed = page.locator(".conversation-feed")
            feed.evaluate("element => { element.scrollTop = 60; element.dispatchEvent(new Event('scroll')); }")
            scroll = feed.evaluate("element => element.scrollTop")
            baseline = deepcopy(first.engine.state)
            button.click()
            expect(drawer).to_be_visible()
            expect(drawer.get_by_text("本会话资料快照", exact=True)).to_be_visible()
            expect(drawer.locator(".source-block")).to_have_count(3)
            expect(drawer.get_by_text("关联版本 2", exact=True)).to_be_visible()
            expect(drawer.get_by_text("示例待补充规则", exact=True)).to_be_visible()
            expect(drawer.locator(".description-text").first).to_contain_text("【验收标准】按范围查询")
            for image in drawer.locator(".attachment img").all():
                expect(image).to_be_visible()
                image.evaluate("image => image.decode()")
                assert f"session={first.engine.state['id']}" in image.get_attribute("src")
            drawer.locator(".ant-image").first.click()
            expect(page.locator(".ant-image-preview-wrap")).to_be_visible()
            page.locator(".ant-image-preview-close").click()
            for title, content in (("说明.txt", "普通附件正文".encode()), ("扫描件.pdf", b"%PDF-reference"), ("验收资料.zip", b"PK-reference")):
                with page.expect_download() as download:
                    drawer.get_by_role("link", name=title, exact=True).click()
                assert Path(download.value.path()).read_bytes() == content
            drawer.locator(".ant-drawer-body").evaluate("element => { element.scrollTop = 0; }")
            page.screenshot(path=str(output / "desktop.png"))
            drawer.get_by_role("button", name="Close", exact=True).click()
            expect(drawer).not_to_be_visible()
            expect(reply).to_have_value("保留未发送的草稿")
            assert feed.evaluate("element => element.scrollTop") == scroll
            assert first.engine.state == baseline

            for phase, stage in (("plan", "计划中"), ("implement", "实施中"), ("review", "审查中"),
                                 ("implement", "已暂停"), ("complete", "已完成（未审查）")):
                first.engine.state.update(phase=phase, stage=stage)
                first.engine.save()
                expect(page.locator(".workflow-strip-actions .ant-tag").first).to_have_text(stage)
                expect(button).to_be_enabled()
                button.click()
                expect(drawer).to_be_visible()
                page.keyboard.press("Escape")
                expect(drawer).not_to_be_visible()
            expect(reply).to_have_value("保留未发送的草稿")

            button.click()
            # 遮罩期间用真实会话入口触发切换，检查切换逻辑主动关闭抽屉。
            page.locator(".session-entry").filter(has_text="资料会话9").evaluate("element => element.click()")
            expect(drawer).not_to_be_visible()
            button.click()
            expect(drawer.get_by_text("另一个会话的独立资料", exact=True)).to_be_visible()
            expect(drawer.get_by_text("已保存的业务规则", exact=False)).to_have_count(0)
            page.keyboard.press("Escape")
            for runtime in (empty, missing):
                page.locator(".session-entry").filter(has_text=runtime.engine.state["item"]["title"]).click()
                button.click()
                expect(drawer.get_by_text("本会话暂无已保存的工作项资料", exact=True)).to_be_visible()
                page.keyboard.press("Escape")

            page.locator(".session-entry").filter(has_text="资料会话7").click()
            button.click()
            page.set_viewport_size({"width": 390, "height": 900})
            expect(drawer).to_be_in_viewport(ratio=1)
            assert drawer.evaluate("element => element.getBoundingClientRect().width") <= 390
            assert drawer.evaluate("element => element.scrollWidth <= element.clientWidth")
            page.screenshot(path=str(output / "mobile.png"))
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 1500, "height": 1050})
            button.click()
            page.locator(".materials-drawer .ant-drawer-mask").click(position={"x": 10, "y": 10})
            expect(drawer).not_to_be_visible()

            button.click()
            page.get_by_role("menuitem", name="工作项与资料", exact=True).evaluate("element => element.click()")
            expect(drawer).not_to_be_visible()
            expect(page.locator(".workspace-main .source-block")).to_have_count(3)
            assert not actions, actions
            page.locator(".session-entry").filter(has_text="新会话").click()
            expect(button).to_have_count(0)
            page.locator(".session-entry").filter(has_text="资料会话7").click()
            button.click()
            page.get_by_role("button", name="新建会话", exact=True).evaluate("element => element.click()")
            expect(drawer).not_to_be_visible()
            expect(button).to_have_count(0)
            assert not errors, errors
            assert all(action["action"] == "new_conversation" for action in actions), actions
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
