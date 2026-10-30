"""用隔离服务检查控制器提问、回合结束及逐项答复，不访问业务系统。"""

from pathlib import Path
from subprocess import CREATE_NO_WINDOW, PIPE, Popen
import sys
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_followup import FollowupChecks
from test_workflow_performance import QUESTIONS
from workflow.mcp_server import Tools
from workflow.web import LocalServer


def run():
    fixture = FollowupChecks()
    fixture.setUp()
    app, state, engine = fixture.application, fixture.state, fixture.engine
    server = None
    try:
        fixture.send("说明文件内容变化，等待确认后再处理", review_enabled=False)
        app.default_session_id = state["id"]
        app.items = state["items"]
        Tools(state["id"]).call("ask_user", {"questions": QUESTIONS})
        engine._event({"method": "item/completed", "params": {"threadId": state["thread_id"],
            "turnId": state["turn_id"], "item": {"type": "mcpToolCall", "server": "workflow", "tool": "ask_user"}}},
            engine.connection_epoch)
        engine.control_executor.submit(lambda: None).result(timeout=5)
        server = LocalServer(("127.0.0.1", 0), app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # 与项目现有浏览器检查相同，复用本地 Node 浏览器库，不下载依赖。
        process = Popen(["node", str(ROOT / "tests" / "check_performance_browser.cjs"), server.origin,
                         str(ROOT / "work" / "browser-check" / "node_modules" / "playwright-core")],
                        stdin=PIPE, stdout=PIPE, stderr=PIPE, text=True, encoding="utf-8",
                        creationflags=CREATE_NO_WINDOW)
        try:
            if process.stdout.readline().strip() != "waiting":
                output, error = process.communicate(timeout=10)
                raise AssertionError("浏览器未进入问题等待状态：" + output + error)
            assert state["phase"] == "followup" and not state["review_requested"]
            engine._event({"method": "turn/completed", "params": {"threadId": state["thread_id"],
                "turn": {"id": state["turn_id"], "status": "interrupted"}}}, engine.connection_epoch)
            fixture.drain()
            output, error = process.communicate("stopped\n", timeout=60)
            assert process.returncode == 0, output + error
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        fixture.drain()
        assert state["thread_id"] == "原需求线程" and state["phase"] == "followup"
        assert "回答：纳入" in state["history"][-2]["text"]
        return {"question_persisted": True, "no_default_answer": True,
                "blocked_until_turn_stops": True, "explicit_answer_resumes_original_phase": True}
    finally:
        app.close()
        if server:
            server.shutdown()
            server.server_close()
            thread.join(3)
        fixture.doCleanups()


if __name__ == "__main__":
    run()
