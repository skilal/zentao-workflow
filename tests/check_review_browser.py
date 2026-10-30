"""使用隔离服务和模拟 Codex 输出检查会话内审查，不访问真实业务系统。"""

from copy import deepcopy
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workflow.web import LocalServer, WebApplication
from test_review_messages import review_result


PLAN = """<proposed_plan>
# 管理库五字段范围检索统一实施计划

## 交付目标与范围

统一交付任务 **4227（前端）**、**4228（后端）**，保留已有检索方式。

| 字段 | 比较规则 |
| --- | --- |
| 全宗号 | 按纯数字数值比较，02 与 2 等值 |
| 题名、责任者 | 按完整文本字典序比较 |

- [x] 明确业务规则
- [ ] 完成人工验收
  - 保留组合条件

> 仅检查本次修改涉及的业务链路。

访问[业务说明](https://example.com)，使用 `AND` 和 ~~旧条件~~。

```json
{"字段": "全宗号", "很长的示例": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
```

<script>window.unexpectedScript = true</script>
<img src="x" onerror="window.unexpectedScript = true">
[危险链接](javascript:alert(1))
<input type="text" value="不应出现的输入框">
</proposed_plan>"""


def run():
    output = ROOT / ".local" / "review-markdown-qa"
    output.mkdir(parents=True, exist_ok=True)
    errors, actions = [], []
    with TemporaryDirectory() as temporary, patch("workflow.store.LOCAL", Path(temporary)), patch("workflow.web.LOCAL", Path(temporary)), patch("workflow.engine.manifest", return_value={}):
        app = WebApplication(loads((ROOT / "settings.example.json").read_text(encoding="utf-8")))
        app.connected = True
        for number in (7, 8):
            item = {"kind": "task", "id": number, "title": f"范围检索示例{number}", "status": "wait", "project_id": 41, "project_name": "示例项目"}
            app.items.append(item)
            state = app.sessions.store.new(item)
            state.update(project={"id": 41, "name": "示例项目", "candidates": []},
                         snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                         repos=[{"name": "前端", "role": "frontend", "path": str(Path(temporary) / "frontend"), "baseline": "0" * 40, "branch": "示例分支", "workspace_owned": False}],
                         selections=[], phase="review", stage="审查中", review_manifest={}, review_prompt_hash="版本",
                         plan=PLAN, plan_hash=sha256(PLAN.encode()).hexdigest(), max_fix_rounds=3,
                         codex={"model": "模拟模型", "reasoning_effort": "high"})
            runtime = app.sessions.add(state)
            if number == 7:
                first = runtime
                source = review_result()
                runtime.engine.last_message = dumps(source, ensure_ascii=False)
                runtime.engine._completed({"status": "completed"}, state["id"], runtime.engine.connection_epoch)
                # 历史轮次没有关联字段，必须通过旧记录的内容恢复。
                state["reviews"][0].pop("history_index")
                source["findings"][0]["previous_id"] = "R01-01"
                another = deepcopy(source["findings"][0])
                another.update(title="边界条件遗漏", previous_id="", severity="P1", origin="本次引入", line=510)
                source["findings"].append(another)
                state.update(stage="审查中", fix_round=1)
                runtime.engine.last_message = "```json\n" + dumps(source, ensure_ascii=False) + "\n```"
                runtime.engine._completed({"status": "completed"}, state["id"], runtime.engine.connection_epoch)
                app.default_session_id = state["id"]
            else:
                second = runtime
                state.update(phase="plan", stage="等待计划确认")
                runtime.engine.note("Codex", "# 普通计划消息\n\n**中文内容**\n\n```js\nconst unfinished = 1;")
            runtime.engine.save()

        def start_turn(engine, prompt, output_schema=None):
            actions.append(prompt)
            engine.state["stage"] = "修复中"
            engine.save()

        def begin_review(engine):
            engine.state.update(phase="review", stage="审查中")
            engine.save()

        server = LocalServer(("127.0.0.1", 0), app)
        server.handle_error = lambda *_: None
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("workflow.engine.Workflow._start_turn", start_turn), patch("workflow.engine.Workflow._begin_review", begin_review), sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, channel="msedge")
                context = browser.new_context(viewport={"width": 1600, "height": 1050})
                page = context.new_page()
                page.set_default_timeout(10000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("if (!sessionStorage.getItem('workflow-page')) sessionStorage.setItem('workflow-page', 'review')")
                page.goto(server.origin)
                expect(page.locator("#review-2")).to_be_visible()
                expect(page.locator(".navigation .ant-menu-item")).to_have_count(3)
                assert page.evaluate("sessionStorage.getItem('workflow-page')") == "plan"
                expect(page.locator(".plan-panel h1")).to_have_text("管理库五字段范围检索统一实施计划")
                expect(page.locator(".plan-panel table")).to_have_count(1)
                expect(page.locator(".plan-panel input[type=checkbox]:disabled")).to_have_count(2)
                expect(page.locator(".plan-panel input:not([type=checkbox]), .plan-panel script, .plan-panel img, .plan-panel [onerror], .plan-panel a[href^='javascript:']")).to_have_count(0)
                assert not page.evaluate("window.unexpectedScript")
                assert "<proposed_plan>" not in page.locator(".plan-panel").inner_text()
                assert first.engine.state["plan"] == PLAN
                expect(page.locator("#review-1 input[type=checkbox]")).to_be_disabled()
                expect(page.locator("#review-1 .review-actions")).to_have_count(0)
                latest = page.locator("#review-2")
                expect(latest.locator(".finding-card")).to_have_count(2)
                latest.locator(".finding-card").first.get_by_text("判断依据、处理建议与业务影响", exact=True).click()
                expect(latest.get_by_text("分组结果缺少当前条件", exact=True)).to_be_visible()
                latest.get_by_text("输出原文", exact=True).click()
                expect(latest.locator(".review-original")).to_contain_text('"findings"')
                latest.get_by_text("输出原文", exact=True).click()
                latest.locator(".finding-card input[type=checkbox]").first.check()
                latest.locator("textarea").fill("只修复选中问题")
                expect(latest.get_by_role("button", name="接受剩余问题并本地提交", exact=True)).to_be_disabled()
                page.reload()
                expect(page.locator("#review-2 input[type=checkbox]").first).to_be_checked()
                expect(page.locator("#review-2 textarea")).to_have_value("只修复选中问题")
                # 会话间选择和说明草稿隔离，普通消息正确展示未闭合代码块。
                page.locator(".session-entry").filter(has_text="范围检索示例8").click()
                expect(page.locator(".conversation-feed h1")).to_have_text("普通计划消息")
                expect(page.locator(".conversation-feed pre code")).to_contain_text("unfinished")
                page.locator(".session-entry").filter(has_text="范围检索示例7").click()
                latest = page.locator("#review-2")
                expect(latest.locator("input[type=checkbox]").first).to_be_checked()
                page.get_by_role("button", name="定位最新 Review", exact=True).click()
                page.screenshot(path=str(output / "desktop.png"), full_page=True)
                latest.locator(".review-actions").screenshot(path=str(output / "review-actions.png"))
                page.locator(".plan-panel").screenshot(path=str(output / "markdown-plan.png"))
                for width in (1000, 720, 390):
                    page.set_viewport_size({"width": width, "height": 1050})
                    page.wait_for_function("() => !document.getAnimations().some(animation => animation.playState === 'running')")
                    if page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
                        page.screenshot(path=str(output / "overflow.png"), full_page=True)
                        nodes = page.evaluate("""() => [...document.querySelectorAll('body *')].filter(node => node.getBoundingClientRect().right > innerWidth + 1).slice(0, 24).map(node => [node.tagName, node.className, Math.round(node.getBoundingClientRect().width)])""")
                        raise AssertionError(f"页面横向溢出：{width}；{nodes}")
                page.screenshot(path=str(output / "narrow.png"), full_page=True)
                page.set_viewport_size({"width": 1600, "height": 1050})
                latest.get_by_role("button", name="修复选中 1 项并复审", exact=True).click()
                page.wait_for_function("document.querySelector('#review-2 textarea')?.disabled")
                assert first.engine.state["decisions"][-1]["fix_ids"] == ["R01-01"]
                assert "边界条件遗漏" not in actions[-1]
                expect(page.locator("#review-2 input[type=checkbox]").first).to_be_disabled()
                # 完整结果到达前不展示原始 JSON，也不允许选择新问题。
                begin_review(first.engine)
                app.emit(first.engine.state["id"], "delta", '{"findings": [')
                expect(page.get_by_role("status")).to_contain_text("正在审查")
                expect(page.locator(".conversation-feed")).not_to_contain_text('{"findings": [')
                source = review_result()
                source["findings"][0]["previous_id"] = "R01-01"
                first.engine.last_message = dumps(source, ensure_ascii=False)
                first.engine._completed({"status": "completed"}, first.engine.state["id"], first.engine.connection_epoch)
                expect(page.locator("#review-3")).to_be_visible()
                latest = page.locator("#review-3")
                expect(latest.locator("input[type=checkbox]:checked")).to_have_count(0)
                expect(latest.locator("textarea")).to_have_value("")
                expect(latest.locator(".finding-card")).to_have_count(2)
                expect(page.locator("#review-2 input[type=checkbox]").first).to_be_disabled()
                # 达到上限只能在明确追加一轮后再修复；接受全部问题仍要求原因。
                first.engine.state["fix_round"] = first.engine.state["max_fix_rounds"]
                first.engine.save()
                latest.locator("input[type=checkbox]").first.check()
                latest.locator("input[type=checkbox]").nth(1).check()
                expect(latest.get_by_role("button", name="修复选中 2 项并复审", exact=True)).to_be_disabled()
                expect(latest.get_by_role("button", name="接受剩余问题并本地提交", exact=True)).to_be_disabled()
                latest.locator("textarea").fill("人工接受原因")
                expect(latest.get_by_role("button", name="接受剩余问题并本地提交", exact=True)).to_be_enabled()
                latest.get_by_role("button", name="追加一轮", exact=True).click()
                expect(latest.get_by_role("button", name="修复选中 2 项并复审", exact=True)).to_be_enabled()
                latest.get_by_role("button", name="重新 Review", exact=True).click()
                expect(page.get_by_role("status")).to_contain_text("正在审查")
                page.get_by_role("button", name="暂停当前阶段", exact=True).click()
                expect(page.get_by_role("button", name="从暂停点继续", exact=True)).to_be_enabled()
                page.get_by_role("button", name="从暂停点继续", exact=True).click()
                expect(page.get_by_role("status")).to_contain_text("正在审查")
                first.engine.state.update(stage="已完成（静态审查）", phase="complete", commits={"前端": "a" * 40})
                first.engine.save()
                expect(page.get_by_text("本地提交记录", exact=True)).to_be_visible()
                expect(latest.locator("input[type=checkbox]").first).to_be_disabled()
                # 新交付尚无审查结果时，旧交付卡片继续只读且不影响会话展示。
                first.engine.state.update(delivery_round=2, phase="followup", stage="实施中")
                first.engine.save()
                expect(page.locator("#review-3 .review-actions")).to_have_count(0)
                expect(page.locator("#review-3 input[type=checkbox]").first).to_be_disabled()
                expect(page.get_by_role("button", name="定位最新 Review", exact=True)).to_be_disabled()
                # 跨交付引用旧编号的失败结果复用问题卡片，但不能触发修复或提交。
                first.engine.state.update(phase="review", stage="已停止", error="Review 引用了本轮交付中不存在的旧问题 R01-01，请重新 Review。")
                failed = review_result()
                failed["findings"][0]["previous_id"] = "R01-01"
                first.engine.note("Codex", dumps(failed, ensure_ascii=False))
                invalid = page.get_by_role("region", name="未通过校验的 Review", exact=True)
                expect(invalid).to_be_visible()
                expect(invalid.locator(".finding-card")).to_have_count(1)
                expect(invalid.locator(".ant-alert")).to_contain_text("本轮交付中不存在的旧问题 R01-01")
                expect(invalid.locator("input[type=checkbox]")).to_be_disabled()
                expect(invalid.locator(".review-actions")).to_have_count(0)
                expect(invalid.locator(".review-original")).to_have_count(0)
                invalid.get_by_text("输出原文", exact=True).click()
                expect(invalid.locator(".review-original")).to_contain_text('"previous_id": "R01-01"')
                page.reload()
                expect(page.get_by_role("region", name="未通过校验的 Review", exact=True)).to_be_visible()
                page.get_by_role("region", name="未通过校验的 Review", exact=True).screenshot(path=str(output / "invalid-review.png"))
                page.set_viewport_size({"width": 390, "height": 1050})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not errors, errors
                browser.close()
                (output / "result.json").write_text(dumps({"结果": "通过", "页面错误": errors,
                    "覆盖": ["Markdown 与 HTML 清理", "历史 JSON 关联", "只读旧轮次", "原文折叠", "旧导航迁移", "草稿刷新与会话隔离", "仅修复选中项", "新轮次重置", "流式审查", "修复上限", "接受原因", "暂停恢复", "提交记录", "宽窄屏布局"]}, ensure_ascii=False, indent=2), encoding="utf-8")
                print("浏览器交互检查通过")
        finally:
            app.close()
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    run()
