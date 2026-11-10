"""隔离浏览器检查文件清单、轮询、折叠及会话切换，不连接业务服务。"""

from copy import deepcopy
from json import dumps
from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_changed_files import ChangedFilesFixture
from workflow.gitops import manifest
from workflow.web import LocalServer


def run():
    fixture = ChangedFilesFixture()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.first.engine
    state["custom_title"] = "文件清单主会话"
    empty = fixture.second.engine.state
    empty.update(custom_title="空白清单会话", phase="complete", stage="已完成（未审查）", followup={"text": "仅获取信息"},
                 snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []})
    fixture.second.engine.save()
    fixture.change()
    fixture.change(1)
    long_path = "src/" + "很长的业务目录名称" * 5 + "/文件名称.vue"
    fixture.change(relative=long_path)
    engine.save()
    app.default_session_id = state["id"]
    server = LocalServer(("127.0.0.1", 0), app)
    # 浏览器取消读取时连接可能已关闭，不输出服务器连接错误。
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            panel = page.locator(".changed-files")
            header = panel.locator(".ant-collapse-header")
            expect(header).to_have_text("改动文件 · 3")
            expect(header).to_have_attribute("aria-expanded", "true")
            expect(panel.locator(".changed-files-repository")).to_have_count(2)
            expect(panel.get_by_text("业务.txt", exact=True)).to_have_count(2)
            expect(panel.get_by_text(long_path, exact=True)).to_be_visible()
            expect(panel).not_to_contain_text("任务文件正文不应返回")
            expect(panel).not_to_contain_text("本机.txt")
            header.click()
            expect(header).to_have_attribute("aria-expanded", "false")

            state["stage"] = "实施中"
            engine.save()
            added = fixture.change(relative="实时新增.txt")
            expect(header).to_have_text("改动文件 · 4")
            expect(header).to_have_attribute("aria-expanded", "false")
            header.click()
            expect(panel.get_by_text("实时新增.txt", exact=True)).to_be_visible()
            fixture.change(relative="实时新增.txt")
            expect(panel.get_by_text("实时新增.txt", exact=True)).to_have_count(1)
            added.unlink()
            expect(header).to_have_text("改动文件 · 3")

            page.route("**/api/changed-files?*", lambda route: route.fulfill(
                status=500, content_type="application/json", body=dumps({"error": "读取失败"})))
            expect(panel.get_by_text("文件清单读取失败", exact=True)).to_be_visible()
            expect(header).to_have_text("改动文件")
            page.unroute("**/api/changed-files?*")
            expect(header).to_have_text("改动文件 · 3")

            for number in range(24):
                fixture.change(relative=f"目录/文件{number:02}.txt")
            expect(header).to_have_text("改动文件 · 27")
            assert panel.locator(".changed-files-list").evaluate(
                "element => element.scrollHeight > element.clientHeight && element.clientHeight <= 180")
            assert panel.evaluate("element => element.scrollWidth <= element.clientWidth")
            page.set_viewport_size({"width": 820, "height": 1000})
            assert panel.evaluate("element => element.scrollWidth <= element.clientWidth")
            page.set_viewport_size({"width": 1440, "height": 1050})

            snapshot = manifest(state["repos"])
            state.update(phase="complete", stage="已完成（未审查）", uncommitted_manifest=snapshot)
            engine.save()
            page.reload()
            expect(header).to_have_text("改动文件 · 27")
            expect(header).to_have_attribute("aria-expanded", "true")
            output = ROOT / "work" / "changed-files-qa.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), full_page=True)

            # 挂起旧会话读取；切换后即使旧结果返回也不得覆盖新清单。
            held = []
            page.route("**/api/changed-files?*", lambda route: held.append(route)
                       if state["id"] in route.request.url else route.continue_())
            page.reload()
            expect(panel.get_by_text("正在读取文件清单…", exact=True)).to_be_visible()
            assert held
            page.locator(".session-entry-custom-title").get_by_text("空白清单会话", exact=True).click()
            expect(header).to_have_text("改动文件 · 0")
            expect(panel.get_by_text("暂无改动文件", exact=True)).to_be_visible()
            for route in held:
                route.fulfill(status=200, content_type="application/json", body=dumps({
                    "session_id": state["id"], "total": 1,
                    "repositories": [{"name": "前端", "paths": ["迟到结果不应显示.txt"]}]}))
            page.unroute("**/api/changed-files?*")
            expect(header).to_have_text("改动文件 · 0")
            expect(panel).not_to_contain_text("迟到结果不应显示")
            page.locator(".session-entry-custom-title").get_by_text("文件清单主会话", exact=True).click()
            expect(header).to_have_text("改动文件 · 27")

            # 续聊准备期间当前仓库尚未登记，仍应展示历轮保存的文件清单。
            state["deliveries"] = [{"number": 1, "completed_at": state["updated_at"], "commits": {},
                                    "repos": deepcopy(state["repos"]), "uncommitted_manifest": snapshot}]
            state.update(repos=[], phase="followup", stage="准备工作区中", delivery_round=2, followup={"text": "继续处理"})
            engine.save()
            expect(header).to_have_text("改动文件 · 27")
            page.reload()
            expect(header).to_have_text("改动文件 · 27")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
