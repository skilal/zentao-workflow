"""隔离浏览器检查过程展示，不连接模型或业务系统。"""

from copy import deepcopy
from pathlib import Path
from subprocess import CREATE_NO_WINDOW, PIPE, Popen
import sys
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup import FollowupChecks
from workflow.web import LocalServer


def run():
    fixture = FollowupChecks()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    server = None
    process = None
    try:
        fixture.send("核对业务资料", review_enabled=False)
        app.default_session_id = state["id"]
        app.items = state["items"]
        second = deepcopy(state)
        second.update(id="conversation-browser-second", custom_title="其他会话", phase="complete", stage="已完成（未审查）",
                      progress_turns=[], reviews=[], decisions=[], deliveries=[],
                      history=[{"time": "2026-01-01T00:00:00+00:00", "role": "用户", "text": "其他会话独立内容"}])
        app.sessions.add(second).engine.save()

        def event(method, **params):
            engine._event({"method": method, "params": {"threadId": state["thread_id"], "turnId": state["turn_id"], **params}},
                          engine.connection_epoch)

        def item(kind, item_id, method="item/started", **fields):
            event(method, item={"id": item_id, "type": kind, **fields})

        item("reasoning", "summary")
        event("item/reasoning/summaryTextDelta", itemId="summary", summaryIndex=0, delta="正在核对业务边界")
        event("item/reasoning/textDelta", itemId="summary", delta="隐藏思考不应显示")
        for number in range(30):
            item("mcpToolCall", f"done-{number}", "item/completed", server="workflow", tool="read_file",
                 arguments={"path": f"资料-{number}.py", "content": "敏感参数不应显示"})
        for number in (1, 2):
            item("mcpToolCall", f"tool-{number}", server="workflow", tool="read_file", arguments={"path": f"并行-{number}.py"})
        server = LocalServer(("127.0.0.1", 0), app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        process = Popen(["node", str(ROOT / "tests" / "check_progress_browser.cjs"), server.origin,
                         str(ROOT / "work" / "browser-check" / "node_modules" / "playwright-core"), state["id"], second["id"]],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)
        assert process.stdout.readline().strip() == "loaded", "浏览器未完成首次展示"
        for _ in range(1000):
            event("item/reasoning/summaryTextDelta", itemId="summary", summaryIndex=0, delta="摘")
        process.stdin.write("streamed\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "scrolled", "浏览器滚动检查未完成"
        item("reasoning", "summary", "item/completed", summary=["已确认业务边界", "可读摘要第二段"])
        item("agentMessage", "comment", "item/completed", phase="commentary", text="已经读取相关文件，继续处理。")
        for number in (1, 2):
            item("mcpToolCall", f"tool-{number}", "item/completed", server="workflow", tool="read_file")
        item("contextCompaction", "compact")
        item("contextCompaction", "compact", "item/completed")
        item("agentMessage", "final", phase="final_answer")
        event("item/agentMessage/delta", itemId="final", delta="最终回复唯一内容")
        item("agentMessage", "final", "item/completed", phase="final_answer", text="最终回复唯一内容")
        event("turn/completed", turn={"id": state["turn_id"], "status": "completed"})
        fixture.application.sessions.runtimes[state["id"]].engine.executor.submit(lambda: None).result(timeout=5)
        output, error = process.communicate("finished\n", timeout=60)
        assert process.returncode == 0, output + error
        return {"browser": True, "isolated_sessions": True, "refresh": True, "scroll_preserved": True,
                "delta_batching": True, "summary_and_tools": True}
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        app.close()
        if server:
            server.shutdown()
            server.server_close()
            thread.join(3)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
