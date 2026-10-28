"""由 Codex 作为本地 MCP 进程启动。"""

from sys import argv, stdin, stdout

from workflow.mcp_server import serve


if __name__ == "__main__":
    stdin.reconfigure(encoding="utf-8")
    stdout.reconfigure(encoding="utf-8", line_buffering=True)
    serve(argv[1])
