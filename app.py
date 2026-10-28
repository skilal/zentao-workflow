"""本机网页客户端入口。"""

from sys import argv
from workflow.settings import WorkflowError
from workflow.web import serve


if __name__ == "__main__":
    try:
        if any(value != "--no-browser" for value in argv[1:]):
            raise WorkflowError("不支持该启动参数。可使用 --no-browser 仅启动本地网页服务。")
        serve(open_page="--no-browser" not in argv)
    except (WorkflowError, OSError, ValueError) as error:
        print("网页客户端未能启动：" + str(error))
        raise SystemExit(1)
