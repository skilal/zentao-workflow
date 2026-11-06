"""隔离检查实施计划详情弹窗的阅读、适配、状态更新和会话隔离。"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup import FollowupChecks
from workflow.web import LocalServer


PLAN = """<proposed_plan>
# 档案范围检索实施计划

## 实施内容

- 保留已有检索条件。
- 增加全宗号范围检索。

| 条件 | 预期结果 |
| --- | --- |
| 全宗号大于 3 | 返回匹配档案 |

```sql
SELECT 档案编号 FROM 档案条目 WHERE 全宗号 > 3;
```

""" + "\n\n".join(f"### 验收步骤 {number}\n\n核对范围检索与原有条件的组合结果。" for number in range(1, 31)) + "\n</proposed_plan>"


def run():
    fixture = FollowupChecks()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    state.update(custom_title="计划详情检查会话", plan=PLAN, approved_plan=PLAN,
                 plan_hash=sha256(PLAN.encode()).hexdigest(), approved_hash=sha256(PLAN.encode()).hexdigest())
    engine.save()
    app.default_session_id = state["id"]
    app.items = state["items"]
    app.codex_catalog = {"default_model": "模拟模型", "models": [
        {"model": "模拟模型", "display_name": "模拟模型", "reasoning_efforts": ["high"], "is_default": True}]}
    other = deepcopy(state)
    other.update(id="plan-details-other", custom_title="另一个计划会话", plan="# 另一份实施计划")
    app.sessions.add(other).engine.save()
    server = LocalServer(("127.0.0.1", 0), app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = ROOT / ".local" / "plan-details-qa"
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
            card = page.locator("#implementation-plan")
            details = card.get_by_role("button", name="查看详情", exact=True)
            dialog = page.get_by_role("dialog", name="实施计划详情", exact=True)
            expect(details).to_be_enabled()
            before = deepcopy(engine.store.load(state["id"]))
            for width, height in ((1500, 1050), (390, 844), (900, 500)):
                page.set_viewport_size({"width": width, "height": height})
                details.click()
                expect(dialog).to_be_visible()
                expect(dialog.get_by_role("heading", name="档案范围检索实施计划", exact=True)).to_be_visible()
                expect(dialog.locator("table")).to_have_count(1)
                expect(dialog.locator("pre code")).to_contain_text("SELECT 档案编号")
                expect(dialog.locator(".plan-version")).to_have_text(f"计划版本 {state['plan_hash'][:12]}")
                expect(dialog.get_by_role("button", name="确认当前计划并实施")).to_have_count(0)
                page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                bounds = dialog.bounding_box()
                assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width + 1, bounds
                assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= height + 1, bounds
                if width == 1500:
                    assert bounds["width"] > card.bounding_box()["width"] * 2, bounds
                body = dialog.locator(".ant-modal-body")
                assert body.evaluate("element => element.scrollHeight > element.clientHeight")
                body.evaluate("element => { element.scrollTop = element.scrollHeight; }")
                expect(dialog.get_by_role("heading", name="验收步骤 30", exact=True)).to_be_in_viewport()
                body.evaluate("element => { element.scrollTop = 0; }")
                if width in (1500, 390):
                    page.screenshot(path=str(output / f"计划详情-{width}.png"), animations="disabled")
                dialog.locator(".ant-modal-footer").get_by_role("button").click()
                expect(dialog).not_to_be_visible()
            assert engine.store.load(state["id"]) == before
            page.set_viewport_size({"width": 1500, "height": 1050})
            details.click()
            expect(dialog).to_be_visible()
            page.wait_for_function("() => document.activeElement?.closest('.ant-modal-wrap')")
            page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
            details.click()
            state["plan"] = PLAN.replace("档案范围检索实施计划", "更新后的实施计划")
            state["plan_hash"] = sha256(state["plan"].encode()).hexdigest()
            engine.save()
            expect(dialog.get_by_role("heading", name="更新后的实施计划", exact=True)).to_be_visible()
            expect(dialog.locator(".plan-version")).to_have_text(f"计划版本 {state['plan_hash'][:12]}")
            page.locator(".session-entry").filter(has_text="另一个计划会话").evaluate("element => element.click()")
            expect(dialog).not_to_be_visible()
            page.locator(".session-entry").filter(has_text="计划详情检查会话").evaluate("element => element.click()")
            expect(details).to_be_visible()
            expect(dialog).not_to_be_visible()
            state.update(approval_source="followup")
            engine.save()
            details.click()
            original = page.get_by_role("dialog", name="原需求实施计划详情", exact=True)
            expect(original).to_be_visible()
            page.get_by_role("menuitem", name="工作项与资料").evaluate("element => element.click()")
            expect(original).not_to_be_visible()
            page.get_by_role("menuitem", name="计划与会话").click()
            expect(original).not_to_be_visible()
            state.update(plan="", plan_hash="")
            engine.save()
            expect(details).to_be_disabled()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
