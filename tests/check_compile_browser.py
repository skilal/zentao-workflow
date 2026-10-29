"""隔离服务与现有 Playwright 运行时进行页面检查，不连接业务系统。"""

from copy import deepcopy
from pathlib import Path
from subprocess import PIPE, run
import sys
from threading import Thread
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_compile_checks import CompileChecks, MISSING, PASSED
from test_codex_models import CATALOG
from workflow.compile_checks import CompileRunner
from workflow.web import LocalServer, WebApplication


def check(node, modules):
    fixture = CompileChecks()
    fixture.setUp()
    server = None
    try:
        fixture.start([MISSING])
        state = fixture.state
        state["plan_started_at"] = state["updated_at"]
        app = WebApplication(fixture.settings)
        fixture.addCleanup(app.close)
        runtime = app.sessions.get(state["id"])
        app.default_session_id = state["id"]
        app.codex_catalog = CATALOG
        app.connected = True
        def start_turn(*_):
            runtime.engine.state["stage"] = "审查中"
            runtime.engine.save()
        runtime.engine._start_turn = Mock(side_effect=start_turn)
        app.items = [state["item"]]
        ready = app.sessions.store.new({"kind": "task", "id": 2, "title": "开始前勾选编译检查"})
        ready.update(snapshot={"sources": [], "materials": [], "blockers": [], "feishu_links": []},
                     codex=deepcopy(state["codex"]))
        app.sessions.add(ready).engine.save()
        server = LocalServer(("127.0.0.1", 0), app)
        server.handle_error = lambda *_: None
        Thread(target=server.serve_forever, daemon=True).start()
        output = ROOT / ".local" / "compile-check-qa.png"
        output.parent.mkdir(exist_ok=True)
        with patch.object(CompileRunner, "check", return_value=deepcopy(PASSED)):
            result = run([node, str(ROOT / "tests/check_compile_browser.cjs"), server.origin, modules,
                          str(output)], stdout=PIPE, stderr=PIPE, timeout=90)
        if result.returncode:
            raise AssertionError("页面检查未通过：" + result.stderr.decode("utf-8", errors="replace")[-1200:])
        return output
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == "__main__":
    check(sys.argv[1], sys.argv[2])
