"""隔离检查会话布局、辅助资料折叠和检查状态更新，不连接业务系统。"""

from copy import deepcopy
from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup import FollowupChecks
from workflow.web import LocalServer


def run():
    fixture = FollowupChecks()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    state["custom_title"] = "布局检查会话"
    state["deliveries"] = [{"number": number, "completed_at": "2026-11-04T08:00:00+00:00",
                            "review_enabled": True, "repos": deepcopy(state["repos"]),
                            "commits": dict(state["commits"])} for number in (1, 2)]
    engine.save()
    app.default_session_id = state["id"]
    app.items = state["items"]
    other = deepcopy(state)
    other.update(id="layout-browser-other", custom_title="另一个布局会话")
    app.sessions.add(other).engine.save()
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = ROOT / ".local" / "session-layout-qa"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            expect(page.locator(".conversation-feed")).to_be_visible()
            compile_panel = page.locator(".compile-checks")
            compile_header = compile_panel.locator(":scope > .ant-collapse-item > .ant-collapse-header")
            expect(compile_header.get_by_text("已关闭", exact=True)).to_be_visible()
            expect(compile_header.get_by_text("尚无记录", exact=True)).to_be_visible()
            frontend = page.locator(".repository-details").filter(has_text="前端分支与工作区")
            frontend_header = frontend.locator(":scope > .ant-collapse-item > .ant-collapse-header")

            # 所有宽度均按主操作顺序纵向排列，静态资料默认收起且不占大块空白。
            expected = ["工作流会话", "后续对话", "流程操作", "后端编译检查", "前端分支与工作区",
                        "后端分支与工作区", "本地提交记录", "历史交付记录"]
            for width in (1920, 1500, 1280, 720, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                layout = page.locator(".conversation-layout").evaluate("""element => {
                    const headings = [...element.querySelectorAll('.ant-card-head-title, .detail-section-title')];
                    const cards = headings.map(heading => heading.closest('.app-card'));
                    return {titles: headings.map(heading => heading.textContent),
                        vertical: cards.every((card, index) => !index || card.getBoundingClientRect().top >= cards[index - 1].getBoundingClientRect().bottom),
                        overflow: document.documentElement.scrollWidth > innerWidth,
                        height: element.querySelector('.conversation-feed').clientHeight};
                }""")
                assert layout["titles"] == expected, (width, layout)
                assert layout["vertical"] and not layout["overflow"] and layout["height"] == 640, (width, layout)
                for header in page.locator(".detail-section > .ant-collapse-item > .ant-collapse-header").all():
                    expect(header).to_have_attribute("aria-expanded", "false")
                if width in (1500, 390):
                    page.screenshot(path=str(output / f"默认收起-{width}.png"), full_page=True, animations="disabled")

            page.set_viewport_size({"width": 1500, "height": 1050})
            frontend_header.click()
            expect(frontend.get_by_text("起始提交", exact=True)).to_be_visible()
            compile_header.click()
            expect(compile_panel.get_by_text("本会话不自动执行后端编译检查。", exact=True)).to_be_visible()
            state["compile_check"] = {"enabled": True}
            engine.save()
            expect(compile_header.get_by_text("已开启", exact=True)).to_be_visible()
            expect(frontend_header).to_have_attribute("aria-expanded", "true")
            expect(compile_header).to_have_attribute("aria-expanded", "true")

            # 完成记录默认折叠；进行中自动展开，普通内容更新保留手动选择。
            def batch(number, status="complete"):
                return {"id": f"layout-check-{number}", "delivery_round": state["delivery_round"],
                        "fix_round": 0, "limit": 3, "status": status, "repositories": [{
                            "name": "后端", "status": "passed", "summary": f"第 {number} 次隔离检查通过。",
                            "modules": ["档案服务"], "diagnostics": [], "failure_reasons": []}]}

            state["compile_checks"] = [batch(number) for number in (1, 2, 3)]
            engine.save()
            expect(compile_header.get_by_text("3 次检查", exact=True)).to_be_visible()
            expect(compile_header).to_have_attribute("aria-expanded", "false")
            compile_header.click()
            expect(compile_panel.get_by_text("第 3 次隔离检查通过。", exact=True)).to_be_visible()
            expect(compile_panel.get_by_text("第 2 次隔离检查通过。", exact=True)).not_to_be_visible()
            history = compile_panel.locator(".compile-history")
            history_header = history.locator(":scope > .ant-collapse-item > .ant-collapse-header")
            expect(history_header).to_have_attribute("aria-expanded", "false")
            history_header.click()
            previous_headers = history.locator(".ant-collapse-content-box > .ant-collapse > .ant-collapse-item > .ant-collapse-header")
            expect(previous_headers).to_have_count(2)
            expect(previous_headers.nth(0)).to_contain_text("第 2 次检查")
            expect(previous_headers.nth(1)).to_contain_text("第 1 次检查")
            previous_headers.nth(0).click()
            expect(compile_panel.get_by_text("第 2 次隔离检查通过。", exact=True)).to_be_visible()
            expect(compile_panel.get_by_text("第 1 次隔离检查通过。", exact=True)).not_to_be_visible()

            current = batch(4, "running")
            current["repositories"][0].update(status="running", summary="正在进行隔离检查。")
            state["compile_checks"].append(current)
            state.update(phase="compile", stage="编译检查中", commits={})
            engine.save()
            expect(compile_header.get_by_text("4 次检查", exact=True)).to_be_visible()
            expect(compile_header).to_have_attribute("aria-expanded", "true")
            compile_header.click()
            current["repositories"][0]["summary"] = "隔离检查内容已更新。"
            engine.save()
            expect(compile_panel.get_by_text("隔离检查内容已更新。", exact=True)).not_to_be_visible()
            expect(compile_header).to_have_attribute("aria-expanded", "false")

            # 进入人工处理重新展开；后续诊断更新不改变手动状态。
            current["status"] = "waiting"
            current["repositories"][0].update(status="dependencies", summary="隔离检查需要人工处理。")
            state["stage"] = "等待编译选择"
            engine.save()
            expect(compile_header.get_by_text("等待处理", exact=True)).to_be_visible()
            expect(compile_header).to_have_attribute("aria-expanded", "true")
            expect(compile_panel.get_by_role("button", name="分析失败原因", exact=True)).to_be_enabled()
            for width in (1500, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"待处理检查横向溢出：{width}"
                assert compile_panel.locator(".question button").evaluate_all("""buttons => buttons.every(button => {
                    const rect = button.getBoundingClientRect(), container = button.closest('.question').getBoundingClientRect();
                    return rect.left >= container.left && rect.right <= container.right;
                })"""), f"待处理按钮超出内容区域：{width}"
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(output / f"最新检查待处理-{width}.png"), full_page=True, animations="disabled")
            page.set_viewport_size({"width": 1500, "height": 1050})
            compile_header.click()
            current["repositories"][0]["summary"] = "等待处理的新诊断。"
            engine.save()
            page.get_by_text("另一个布局会话", exact=True).click()
            expect(frontend_header).to_have_attribute("aria-expanded", "false")
            page.get_by_text("布局检查会话", exact=True).click()
            expect(compile_header).to_have_attribute("aria-expanded", "true")
            expect(frontend_header).to_have_attribute("aria-expanded", "false")
            expect(compile_panel.get_by_text("等待处理的新诊断。", exact=True)).to_be_visible()
            expect(history_header).to_have_attribute("aria-expanded", "false")
            for status in ("complete", "invalidated"):
                current["status"] = status
                current["repositories"][0].update(status="passed", summary="隔离检查已结束。")
                state.update(phase="review", stage="审查中")
                engine.save()
                expect(compile_header.get_by_text("通过" if status == "complete" else "结论已失效", exact=True)).to_be_visible()
                expect(compile_header).to_have_attribute("aria-expanded", "false")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
