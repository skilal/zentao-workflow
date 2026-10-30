"""在隔离服务中检查已完成会话的续聊、截图和历史交付展示。"""

from base64 import b64encode
from copy import deepcopy
from json import dumps
from pathlib import Path
from re import compile
import sys
from threading import Thread
from urllib.parse import urlencode

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup import FollowupChecks, PNG
from workflow.mcp_server import Tools
from workflow.web import LocalServer


def run():
    output = ROOT / ".local" / "followup-qa"
    output.mkdir(parents=True, exist_ok=True)
    fixture = FollowupChecks()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    app.default_session_id = state["id"]
    app.items = [state["item"]]
    other = deepcopy(state)
    other["id"] = app.sessions.store.new({"kind": "story", "id": 8, "title": "另一会话"})["id"]
    other["item"] = {"kind": "story", "id": 8, "title": "另一会话"}
    other["items"] = [other["item"]]
    app.sessions.add(other).engine.save()
    server = LocalServer(("127.0.0.1", 0), app)
    server.handle_error = lambda *_: None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            context = browser.new_context(viewport={"width": 1500, "height": 1050})
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("if (!sessionStorage.getItem('workflow-page')) sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            send = page.get_by_role("button", name=compile(r"^发\s*送$"))
            reply = page.get_by_placeholder("输入问题、需要获取的信息或修改要求，可粘贴图片…")
            review_switch = page.get_by_role("switch", name="修改后自动 Review", exact=True)
            plan_switch = page.get_by_role("switch", name="计划模式", exact=True)
            expect(plan_switch).not_to_be_checked()
            expect(review_switch).not_to_be_checked()
            review_switch.click()
            expect(send).to_be_enabled()
            expect(reply).to_be_enabled()
            expect(page.locator("#review-1 .review-actions")).to_have_count(0)
            reply.fill("实际结果：保存失败；预期结果：保存成功。")

            # 失败图片须由用户明确移除；不能带着失败队列发送。
            page.locator("input[type=file]").set_input_files({"name": "错误.svg", "mimeType": "image/svg+xml", "buffer": b"<svg/>"})
            failed = page.locator(".attachment").filter(has_text="错误.svg")
            expect(failed.get_by_text("上传失败", exact=True)).to_be_visible()
            expect(send).to_be_disabled()
            failed.get_by_role("button", name="移除", exact=True).click()
            expect(send).to_be_enabled()

            # 正常上传、预览和移除走实际本地 HTTP 接口。
            page.locator("input[type=file]").set_input_files({"name": "上传截图.png", "mimeType": "image/png", "buffer": PNG})
            uploaded = page.locator(".attachment").filter(has_text="上传截图.png")
            expect(uploaded.locator("img")).to_be_visible()
            uploaded.get_by_role("button", name="移除", exact=True).click()
            expect(uploaded).to_have_count(0)

            # 粘贴图片同时保留剪贴板文字。
            reply.evaluate("""(input, data) => {
                const transfer = new DataTransfer();
                transfer.items.add(new File([Uint8Array.from(atob(data), c => c.charCodeAt(0))], '粘贴截图.png', { type: 'image/png' }));
                transfer.setData('text/plain', '补充：点击保存后报错。');
                input.dispatchEvent(new ClipboardEvent('paste', { clipboardData: transfer, bubbles: true, cancelable: true }));
            }""", b64encode(PNG).decode())
            expect(reply).to_have_value("实际结果：保存失败；预期结果：保存成功。补充：点击保存后报错。")
            expect(page.get_by_alt_text("粘贴截图.png")).to_be_visible()
            expect(send).to_be_enabled()
            image_id = state["followup_images"][-1]["id"]
            page.reload()
            expect(review_switch).to_be_checked()
            expect(reply).to_have_value("实际结果：保存失败；预期结果：保存成功。补充：点击保存后报错。")
            expect(page.get_by_alt_text("粘贴截图.png")).to_be_visible()

            # 图片不能从另一会话读取。
            response = page.request.get(server.origin + "/api/supplement-images?" + urlencode({
                "session_id": other["id"], "image_id": image_id, "purpose": "followup"}))
            assert response.status == 400

            # 发送失败保留当前开关和消息。
            page.route("**/api/actions", lambda route: route.fulfill(status=400, json={"error": "模拟发送失败"}), times=1)
            send.click()
            expect(page.get_by_text("模拟发送失败", exact=True)).to_be_visible()
            expect(review_switch).to_be_checked()
            expect(reply).not_to_have_value("")

            page.screenshot(path=str(output / "完成后可继续发送.png"), full_page=True)
            send.click()
            expect(page.get_by_text("续聊处理中", exact=True).first).to_be_visible()
            expect(page.get_by_role("status").filter(has_text="模型正在处理中")).to_be_visible()
            fixture.drain()
            expect(page.get_by_placeholder("输入回复、需要调整的计划内容或补充业务规则…")).to_be_disabled()
            expect(page.locator(".conversation-feed").get_by_alt_text("粘贴截图.png")).to_be_visible()
            expect(page.get_by_text("历史交付记录", exact=True)).to_be_visible()
            assert state["thread_id"] == "原需求线程"
            assert state["delivery_round"] == 2 and state["fix_round"] == 0
            assert state["review_enabled"] is True
            Tools(state["id"]).call("write_file", {"repository": "前端", "path": "业务.txt", "content": "开启 Review 后的修改\n"})
            fixture.finish()
            expect(send).to_be_enabled()
            expect(reply).to_have_value("")
            expect(review_switch).not_to_be_checked()
            assert state["stage"] == "已完成（静态审查）" and len(state["reviews"]) == 2
            expect(page.locator(".conversation-feed").get_by_alt_text("粘贴截图.png")).to_be_visible()
            expect(page.get_by_role("button", name="移除", exact=True)).to_have_count(0)
            expect(page.locator("#review-1 .review-actions, #review-2 .review-actions")).to_have_count(0)

            # 纯图片反馈也可重复发起新一轮。
            page.locator("input[type=file]").set_input_files({"name": "第二次截图.png", "mimeType": "image/png", "buffer": PNG})
            expect(page.get_by_alt_text("第二次截图.png")).to_be_visible()
            expect(send).to_be_enabled()
            send.click()
            expect(page.get_by_text("续聊处理中", exact=True).first).to_be_visible()
            fixture.drain()
            assert state["delivery_round"] == 3 and state["followup"]["text"] == ""
            assert state["review_enabled"] is False
            Tools(state["id"]).call("write_file", {"repository": "前端", "path": "业务.txt", "content": "关闭 Review 后的修改\n"})
            fixture.finish()
            expect(send).to_be_enabled()
            assert state["stage"] == "已完成（未审查）" and state["commits"] == {}
            assert len(state["reviews"]) == 2
            page.reload()
            expect(review_switch).not_to_be_checked()
            expect(page.locator(".conversation-feed img")).to_have_count(2)

            # 已完成（未审查）仍可续聊，开启但无代码修改时不追加 Review。
            reply.fill("请解释当前保存流程，不修改代码")
            review_switch.click()
            send.click()
            expect(page.get_by_text("续聊处理中", exact=True).first).to_be_visible()
            fixture.drain()
            fixture.complete("保存流程会检查输入并持久化数据。")
            expect(send).to_be_enabled()
            expect(review_switch).not_to_be_checked()
            assert state["delivery_round"] == 4 and len(state["reviews"]) == 2
            assert state["stage"] == "已完成（未审查）"
            expect(page.get_by_text("Review（跳过）", exact=True)).to_be_visible()
            expect(page.get_by_text("本地提交（跳过）", exact=True)).to_be_visible()

            # 后续计划开关独立于 Review；刷新和失败保留草稿，成功后恢复关闭。
            plan_switch.click()
            reply.fill("在原功能基础上增加校验，先给出计划")
            page.reload()
            expect(plan_switch).to_be_checked()
            expect(review_switch).not_to_be_checked()
            page.locator(".session-entry").filter(has_text="另一会话").click()
            expect(plan_switch).not_to_be_checked()
            expect(reply).to_have_value("")
            page.locator(".session-entry").filter(has_text="测试反馈续聊").click()
            expect(plan_switch).to_be_checked()
            expect(reply).to_have_value("在原功能基础上增加校验，先给出计划")
            page.route("**/api/actions", lambda route: route.fulfill(status=400, json={"error": "计划消息发送失败"}), times=1)
            send.click()
            expect(page.get_by_text("计划消息发送失败", exact=True)).to_be_visible()
            expect(plan_switch).to_be_checked()
            expect(reply).to_have_value("在原功能基础上增加校验，先给出计划")
            send.click()
            expect(page.get_by_text("计划中", exact=True).first).to_be_visible()
            fixture.drain()
            assert state["phase"] == "plan" and state["followup"]["plan_mode"] is True
            assert state["delivery_round"] == 5
            fixture.complete("<proposed_plan>增加业务校验</proposed_plan>")
            expect(page.get_by_text("实施中", exact=True).first).to_be_visible()

            # 未完成时先暂停，再合并补充需求，保持同一交付轮次和工作区。
            engine.turn_running = True
            page.get_by_role("button", name="暂停当前阶段", exact=True).click()
            expect(page.get_by_text("已暂停", exact=True).first).to_be_visible()
            fixture.drain()
            expect(send).to_be_disabled()
            fixture.complete("")
            expect(send).to_be_enabled()
            expect(plan_switch).not_to_be_checked()
            plan_switch.click()
            review_switch.click()
            page.locator("input[type=file]").set_input_files({"name": "暂停补充.png", "mimeType": "image/png", "buffer": PNG})
            expect(page.get_by_alt_text("暂停补充.png")).to_be_visible()
            reply.fill("补充边界条件，与原功能一起完成")
            repos = deepcopy(state["repos"])
            send.click()
            expect(page.get_by_text("计划中", exact=True).first).to_be_visible()
            fixture.drain()
            assert state["delivery_round"] == 5 and state["repos"] == repos
            expect(page.locator("#review-1 .review-actions, #review-2 .review-actions")).to_have_count(0)
            fixture.complete("请补充具体的边界条件")
            supplement = page.get_by_placeholder("输入回复、需要调整的计划内容或补充业务规则…")
            supplement.fill("边界条件为不能为空")
            page.get_by_role("button", name="提交补充说明", exact=True).click()
            expect(page.get_by_text("计划中", exact=True).first).to_be_visible()
            fixture.drain()
            fixture.complete("<proposed_plan>合并原功能和非空校验</proposed_plan>")
            expect(page.get_by_text("等待计划确认", exact=True).first).to_be_visible()
            expect(page.get_by_role("button", name="暂停当前阶段", exact=True)).to_be_enabled()
            page.get_by_role("button", name="确认当前计划并实施", exact=True).click()
            expect(page.get_by_text("实施中", exact=True).first).to_be_visible()
            fixture.drain()
            Tools(state["id"]).call("write_file", {"repository": "前端", "path": "校验.txt", "content": "不能为空\n"})
            fixture.finish()
            expect(send).to_be_enabled()
            expect(plan_switch).not_to_be_checked()
            expect(review_switch).not_to_be_checked()

            # 独立检查重新生成原生计划后的自动实施、Review、完成提示与刷新恢复。
            delivery_number = state["delivery_round"] + 1
            review_number = len(state["reviews"]) + 1
            reply.fill("重新生成计划后完成业务规则调整")
            review_switch.click()
            send.click()
            expect(page.get_by_text("续聊处理中", exact=True).first).to_be_visible()
            fixture.drain()
            engine.pause()
            fixture.complete("")
            engine._new_plan_round()
            engine.resume()
            expect(page.get_by_text("计划中", exact=True).first).to_be_visible()
            engine.completed_plan = "本轮完整计划：仅调整补充业务规则"
            fixture.complete("我先核对当前代码，再整理实施计划。")
            expect(page.get_by_text("实施中", exact=True).first).to_be_visible()
            expect(page.locator(".conversation-feed").get_by_text("本轮完整计划：仅调整补充业务规则", exact=True)).to_be_visible()
            expect(page.locator(".conversation-feed").get_by_text("本轮计划未提问且一次成稿，已自动授权，开始实施。", exact=True).last).to_be_visible()
            Tools(state["id"]).call("write_file", {"repository": "前端", "path": "计划校验.txt", "content": "补充规则\n"})
            fixture.complete("已完成业务规则修改。")
            expect(page.get_by_text("审查中", exact=True).first).to_be_visible()
            expect(page.locator(".conversation-feed").get_by_text("本轮计划实施已完成，继续处理后续检查。", exact=True).last).to_be_visible()
            expect(page.locator(".conversation-feed").get_by_text("已进入 Review，正在审查本轮代码；无待处理问题后自动本地提交。", exact=True).last).to_be_visible()
            fixture.complete(dumps({"summary": "本轮无问题", "findings": [], "resolved_previous": []}))
            expect(send).to_be_enabled()
            expect(review_switch).not_to_be_checked()
            expect(page.get_by_label("本轮执行结果")).to_contain_text("Review 已完成，本地提交已完成")
            expect(page.locator("#implementation-plan").get_by_text("计划已完成", exact=True)).to_be_visible()
            page.reload()
            expect(page.get_by_label("本轮执行结果")).to_contain_text(f"第 {delivery_number} 轮已完成")
            expect(page.locator(f"#review-{review_number}")).to_be_visible()
            for width in (1500, 720, 390):
                page.set_viewport_size({"width": width, "height": 1050})
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), f"页面横向溢出：{width}"
                assert page.locator(".chat-meta").evaluate_all("nodes => nodes.every(node => node.scrollWidth <= node.clientWidth)"), f"消息标签溢出：{width}"
            page.screenshot(path=str(output / "续聊历史窄屏.png"), full_page=True)
            assert not errors, errors
            (output / "result.json").write_text(dumps({"结果": "通过", "页面错误": errors,
                "覆盖": ["已完成会话发送", "失败图片移除", "上传预览移除", "文字与图片粘贴", "刷新草稿与截图", "跨会话图片隔离",
                         "原线程多轮续聊", "纯图片反馈", "历史 Review 只读", "历史提交保留", "发送中禁用", "宽窄屏布局",
                         "Review 默认关闭", "开关草稿恢复", "发送失败保留选择", "发送成功重置", "开启后修改审查提交", "关闭后修改保留", "纯问答跳过审查",
                         "手动计划开关", "计划开关刷新与失败保留", "暂停后文字图片补充", "未完成轮次不增加", "补充计划人工确认", "计划与审查开关独立"]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
