"""隔离浏览器验证成果卡片、内容预览、下载及恢复；不连接真实模型。"""

from pathlib import Path
from re import compile
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_artifacts import ArtifactFixture, BLOCKS, SHEETS
from workflow.settings import load_settings
from workflow.web import LocalServer


def run():
    fixture = ArtifactFixture()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    server = None
    try:
        app.settings["pdf"] = load_settings()["pdf"]
        sql = fixture.create()
        markdown = fixture.create("说明.md", content="# 成果说明\n\n<script>window.artifactInjected=true</script>\n\n**查看内容**")
        text = fixture.create("分页.txt", content="\n".join(f"第{i}行" for i in range(201)))
        word = fixture.create("中文说明.docx", "docx", BLOCKS)
        excel = fixture.create("中文清单.xlsx", "xlsx", SHEETS)
        pdf = fixture.create("验收报告.pdf", "pdf", {"blocks": [*BLOCKS["blocks"],
            *[{"type": "paragraph", "text": f"第 {index} 段：验证中文内容与分页显示。"} for index in range(80)]]})
        engine.note("Codex", "已生成文件，请查看下方成果。", turn_id=state["turn_id"])
        engine.set_progress_status("completed")
        engine.sync_artifacts()
        state.update(stage="已完成（未审查）", phase="complete")
        engine.save()
        app.default_session_id = state["id"]
        server = LocalServer(("127.0.0.1", 0), app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="msedge")
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("sessionStorage.setItem('workflow-page', 'plan')")
            page.goto(server.origin)
            expect(page.locator("[data-artifact-id]")).to_have_count(6)
            def open_preview(entry):
                card = page.locator(f'[data-artifact-id="{entry["id"]}"]')
                try:
                    card.get_by_role("button", name=compile(r"^预\s*览$")).click()
                except Exception:
                    page.screenshot(path=str(ROOT / ".local" / "artifact-browser-failure.png"))
                    (ROOT / ".local" / "artifact-browser-failure.html").write_text(page.content(), encoding="utf-8")
                    raise
                drawer = page.locator(".ant-drawer-content").filter(has_text=f"文件预览 · {entry['name']}")
                expect(drawer).to_be_visible()
                return drawer
            def close_preview(drawer):
                drawer.get_by_role("button", name="Close").click()
                expect(drawer).not_to_be_visible()
            drawer = open_preview(sql)
            expect(drawer.get_by_text("SELECT '中文';", exact=True)).to_be_visible()
            close_preview(drawer)
            with page.expect_download() as download_event:
                page.locator(f'[data-artifact-id="{sql["id"]}"]').get_by_role("link", name=compile(r"^下\s*载$")).click()
            download = download_event.value
            assert download.suggested_filename == "中文成果.sql"
            assert Path(download.path()).read_bytes() == "SELECT '中文';".encode()
            drawer = open_preview(markdown)
            expect(drawer.get_by_role("heading", name="成果说明")).to_be_visible()
            assert page.evaluate("window.artifactInjected") is None
            close_preview(drawer)
            drawer = open_preview(text)
            expect(drawer.get_by_role("button", name="上一页")).to_be_disabled()
            drawer.get_by_role("button", name="下一页").click()
            expect(drawer.locator("pre")).to_contain_text("第100行")
            close_preview(drawer)
            drawer = open_preview(word)
            expect(drawer.get_by_role("heading", name="中文验收说明")).to_be_visible()
            expect(drawer.get_by_role("cell", name="验收通过")).to_be_visible()
            expect(drawer.get_by_text("表格之后的内容", exact=True)).to_be_visible()
            close_preview(drawer)
            drawer = open_preview(excel)
            expect(drawer.get_by_role("cell", name="=SUM(B2:B2)", exact=True)).to_be_visible()
            drawer.locator(".ant-select-selector").click()
            page.locator(".ant-select-item-option").filter(has_text="其他").click()
            expect(drawer.get_by_role("cell", name="第二工作表", exact=True)).to_be_visible()
            close_preview(drawer)
            drawer = open_preview(pdf)
            expect(drawer.get_by_text(compile(r"第 1 / [2-9] 页"))).to_be_visible(timeout=60000)
            drawer.get_by_role("button", name="下一页").click()
            expect(drawer.get_by_text(compile(r"第 2 / [2-9] 页"))).to_be_visible()
            drawer.get_by_role("button", name="上一页").click()
            expect(drawer.get_by_text(compile(r"第 1 / [2-9] 页"))).to_be_visible()
            image = drawer.locator("img").first
            expect(image).to_be_visible()
            page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=image.element_handle())
            page.mouse.move(20, 20)
            image.screenshot(path=str(ROOT / ".local" / "artifact-pdf-check.png"))
            page.screenshot(path=str(ROOT / ".local" / "artifact-preview-check.png"))
            close_preview(drawer)
            # 预览请求失败可见，且下载入口仍可用，不自动重试。
            page.route("**/api/artifacts/preview?**", lambda route: route.fulfill(status=400, json={"error": "模拟预览失败"}), times=1)
            drawer = open_preview(word)
            expect(drawer.get_by_text("模拟预览失败", exact=True)).to_be_visible()
            expect(drawer.get_by_role("link", name="下载原文件", exact=True)).to_be_visible()
            close_preview(drawer)
            page.reload()
            expect(page.locator("[data-artifact-id]")).to_have_count(6)
            # 无最终回复的中断成果同样可见。
            state.update(stage="续聊处理中", phase="followup", turn_id="中断回合")
            engine.start_progress("中断回合")
            engine.save()
            interrupted = fixture.create("中断.txt", content="已保存的文件")
            engine.pause()
            page.reload()
            card = page.locator(f'[data-artifact-id="{interrupted["id"]}"]')
            expect(card.get_by_text("所属回合：已暂停", exact=True)).to_be_visible()
            assert not errors, errors
            browser.close()
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
