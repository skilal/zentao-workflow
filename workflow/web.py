"""本机网页接口：复用工作流引擎，浏览器断开不结束后台会话。"""

from collections import OrderedDict, deque
from copy import deepcopy
from difflib import unified_diff
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps, loads
from mimetypes import guess_type
from os import getpid
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from threading import Condition, RLock, Thread, Timer
from time import monotonic
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4
from webbrowser import open as open_browser

from workflow.engine import ACTIVE, Workflow
from workflow.gitops import branches, differences, safe_file
from workflow.settings import LOCAL, ROOT, WorkflowError, load_settings, save_password, save_settings
from workflow.zentao import KINDS, ZentaoClient


ASSETS = ROOT / "web" / "dist"
COOKIE_NAME = "zentao_workflow_session"


def public_state(state):
    if state is None:
        return None
    result = deepcopy(state)
    result.setdefault("supplement_images", [])
    for source in result.get("snapshot", {}).get("sources", []):
        source.pop("raw", None)
    return result


def history_summary(state):
    return {key: state.get(key) for key in ("id", "item", "stage", "updated_at", "fix_round", "commits")}


class WebApplication:
    def __init__(self, settings=None):
        self.settings = settings or load_settings()
        self.lock = RLock()
        self.condition = Condition(self.lock)
        self.events = deque()
        self.event_bytes = 0
        self.sequence = 0
        self.revision = 0
        self.cookie_token = token_urlsafe(32)
        self.csrf_token = token_urlsafe(32)
        self.items = []
        self.state = None
        self.stream = ""
        self.connected = False
        self.profile = None
        self.busy = None
        self.requests = OrderedDict()
        self.server = None
        self.closed = False
        self.engine = Workflow(self.settings, self.emit)
        self.pointer = LOCAL / "current-session.json"
        if self.pointer.is_file():
            pointer = loads(self.pointer.read_text(encoding="utf-8"))
            self.engine.restore(pointer["id"])

    def _publish(self, kind, value):
        self.sequence += 1
        packet = f"id: {self.sequence}\nevent: {kind}\ndata: {dumps(value, ensure_ascii=False)}\n\n".encode("utf-8")
        self.events.append((self.sequence, packet))
        self.event_bytes += len(packet)
        while len(self.events) > 512 or (self.event_bytes > 4 * 1024 * 1024 and len(self.events) > 1):
            self.event_bytes -= len(self.events.popleft()[1])
        self.condition.notify_all()

    def emit(self, kind, value):
        with self.condition:
            if self.closed:
                return
            if kind == "state":
                old = self.state
                self.state = public_state(value)
                self.revision += 1
                if not old or old["id"] != value["id"] or old.get("turn_id") != value.get("turn_id"):
                    self.stream = ""
                temporary = self.pointer.with_suffix(".json.tmp")
                temporary.write_text(dumps({"id": value["id"]}), encoding="utf-8")
                temporary.replace(self.pointer)
                self._publish("state", {"state": self.state, "revision": self.revision, "stream": self.stream})
            elif kind == "items":
                self.items = deepcopy(value)
                self._publish("items", self.items)
            elif kind == "connected":
                self.connected, self.profile = True, value
                self._publish("connected", value)
            elif kind == "delta":
                self.stream += value
                self._publish("delta", value)
            elif kind in ("message", "history"):
                self.state = public_state(self.engine.state)
                if kind == "message" and value["role"] in ("控制器", "Codex"):
                    self.stream = ""
                self._publish("conversation", {"state": self.state, "stream": self.stream})
            elif kind == "error":
                if not self.engine.zentao.token:
                    self.connected = False
                self._publish("operation_error", {"message": value, "connected": self.connected})
            elif kind == "questions":
                self._publish("questions", value)

    def public_settings(self):
        return {"base_url": self.settings["zentao"]["base_url"], "account": self.settings["zentao"]["account"],
                "model": self.settings["codex"]["model"], "max_fix_rounds": self.settings["review"]["max_fix_rounds"],
                "max_attachment_mb": self.settings["zentao"]["max_attachment_mb"],
                "review_prompt": (ROOT / self.settings["review"]["prompt_file"]).read_text(encoding="utf-8").strip(),
                "repositories": deepcopy(self.settings["repositories"]), "feishu_enabled": False}

    def snapshot(self):
        with self.lock:
            return {"state": deepcopy(self.state), "items": deepcopy(self.items), "stream": self.stream,
                    "connected": self.connected, "profile": self.profile, "busy": self.busy,
                    "revision": self.revision, "cursor": self.sequence, "settings": self.public_settings(),
                    "history": [history_summary(state) for state in self.engine.store.sessions()]}

    def _supplement_engine(self, session_id, revision):
        engine = self.engine
        state = engine.state
        if not state or session_id != state["id"]:
            raise WorkflowError("当前会话已经变化，请重新选择工作项。")
        if self.closed or self.busy:
            raise WorkflowError("该会话正在执行操作，请等待完成后修改图片。")
        if state["stage"] not in {"资料已读取", "准备工作区失败"}:
            raise WorkflowError("当前阶段不能修改补充图片。")
        if type(revision) is not int or revision != self.revision:
            raise WorkflowError("页面显示的流程版本已过期，请查看最新内容后再操作。")
        return engine

    def _supplement_payload(self):
        return {"state": deepcopy(self.state), "revision": self.revision, "stream": self.stream}

    def upload_supplement_image(self, session_id, revision, name, data):
        with self.condition:
            engine = self._supplement_engine(session_id, revision)
            maximum = self.settings["zentao"]["max_attachment_mb"] * 1024 * 1024
            if not isinstance(name, str) or not name.strip() or not 0 < len(data) <= maximum:
                raise WorkflowError("图片名称不能为空，单张大小不能超过配置的附件上限。")
            extension = Path(name).suffix.lower().lstrip(".")
            signatures = {
                "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                "jpg": data.startswith(b"\xff\xd8\xff"),
                "jpeg": data.startswith(b"\xff\xd8\xff"),
                "gif": data.startswith((b"GIF87a", b"GIF89a")),
                "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
                "bmp": data.startswith(b"BM"),
            }
            if not signatures.get(extension):
                raise WorkflowError("请选择格式与内容一致的 PNG、JPEG、GIF、WebP 或 BMP 图片。")
            image_id = uuid4().hex
            path = self.engine.store.directory(session_id) / "attachments" / "supplement" / f"{image_id}.{extension}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            image = {"id": image_id, "name": name, "extension": extension, "size": len(data),
                     "path": str(path), "content_type": guess_type(path.name)[0]}
            engine.state.setdefault("supplement_images", []).append(image)
            engine.save()
            return self._supplement_payload()

    def remove_supplement_image(self, session_id, revision, image_id):
        with self.condition:
            engine = self._supplement_engine(session_id, revision)
            images = engine.state.get("supplement_images", [])
            image = next((entry for entry in images if entry["id"] == image_id), None)
            if image is None:
                raise WorkflowError("图片不在当前会话的补充附件列表中。")
            path = Path(image["path"]).resolve()
            allowed = (self.engine.store.directory(session_id) / "attachments" / "supplement").resolve()
            if not path.is_relative_to(allowed):
                raise WorkflowError("补充图片路径不合法。")
            path.unlink()
            images.remove(image)
            engine.save()
            return self._supplement_payload()

    def update_settings(self, data):
        state = self.engine.state
        if state and state["stage"] not in ("已完成（静态审查）", "资料已读取", "资料读取失败"):
            raise WorkflowError("当前流程已经固定配置，请结束流程后修改默认设置。")
        account = str(data.get("account", "")).strip()
        url = str(data.get("base_url", "")).strip().rstrip("/")
        parsed = urlparse(url)
        maximum = data.get("max_fix_rounds")
        prompt = str(data.get("review_prompt", "")).strip()
        password = data.get("password", "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise WorkflowError("请填写不包含凭据的完整禅道 HTTP 地址。")
        if not account or not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0 or not prompt:
            raise WorkflowError("账号、非负整数轮次及 Review 提示词不能为空。")
        if account != self.settings["zentao"]["account"] and not password:
            raise WorkflowError("更换账号时请同时填写对应密码。")
        if password:
            if not isinstance(password, str):
                raise WorkflowError("密码格式无效。")
            save_password(password)
        self.settings["zentao"].update(base_url=url, account=account)
        self.settings["codex"]["model"] = str(data.get("model", "")).strip()
        self.settings["review"]["max_fix_rounds"] = maximum
        (ROOT / self.settings["review"]["prompt_file"]).write_text(prompt + "\n", encoding="utf-8")
        save_settings(self.settings)
        self.engine.settings = self.settings
        self.engine.zentao = ZentaoClient(self.settings)
        with self.condition:
            self.connected, self.profile = False, None
            self.items = []
            self._publish("settings", self.public_settings())
            self._publish("items", [])
            self._publish("connection", {"connected": False})

    def dispatch(self, request):
        action = request.get("action")
        data = request.get("data", {})
        request_id = request.get("request_id")
        if not isinstance(data, dict) or not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise WorkflowError("操作参数或请求标识无效。")
        with self.condition:
            if request_id in self.requests:
                return {"accepted": True, "request_id": request_id, "duplicate": True}
            if self.busy and action != "pause":
                raise WorkflowError("上一个操作尚未处理完成，请等待。")
            state = self.engine.state
            session_actions = {"plan", "confirm_project", "choose_branches", "edit_branches", "answer", "approve_plan", "fix", "add_round", "rereview", "finalize", "pause", "resume"}
            if action in session_actions:
                if not state or request.get("session_id") != state["id"]:
                    raise WorkflowError("当前会话已经变化，请刷新后操作。")
                if action in {"plan", "confirm_project", "choose_branches", "edit_branches", "approve_plan", "fix", "finalize"} and request.get("revision") != self.revision:
                    raise WorkflowError("页面显示的流程版本已过期，请查看最新内容后再确认。")
            affects, control = True, action == "pause"
            if action == "connect":
                operation, arguments, affects = self.engine.connect, (), False
            elif action == "refresh":
                operation, arguments, affects = self.engine.refresh, (), False
            elif action == "collect":
                kind, item_id = data.get("kind"), data.get("id")
                item = next((item for item in self.items if item["kind"] == kind and item["id"] == item_id), None)
                if not item:
                    raise WorkflowError("请从当前登录账号的待处理列表中选择工作项。")
                operation, arguments = self.engine.collect, (deepcopy(item),)
            elif action in {"plan", "edit_branches"}:
                supplement = data.get("supplement", "")
                if not isinstance(supplement, str) or "selections" in data:
                    raise WorkflowError("开始计划只接受补充说明，仓库与分支由服务端确定。")
                operation, arguments = self.engine.start_plan, (supplement, action == "edit_branches")
            elif action == "confirm_project":
                project_id, name = data.get("project_id"), data.get("name")
                if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0 or not isinstance(name, str) or not name.strip():
                    raise WorkflowError("请明确填写所属项目编号和名称。")
                operation, arguments = self.engine.confirm_project, (project_id, name.strip())
            elif action == "choose_branches":
                choices = data.get("choices")
                if not isinstance(choices, dict):
                    raise WorkflowError("请明确选择待确定仓库的分支。")
                operation, arguments = self.engine.choose_branches, (choices,)
            elif action == "answer":
                text, answers = str(data.get("text", "")).strip(), data.get("answers")
                pending = state.get("pending")
                if pending:
                    questions = pending["params"].get("questions", [])
                    if not isinstance(answers, dict) or set(answers) != {question["id"] for question in questions}:
                        raise WorkflowError("请逐项明确回答当前所有问题。")
                    if any(not isinstance(values, list) or not values or any(not isinstance(value, str) or not value.strip() for value in values) for values in answers.values()):
                        raise WorkflowError("每个问题都需要明确回答，不能自动选择或留空。")
                elif answers is not None and not isinstance(answers, dict):
                    raise WorkflowError("回答参数无效。")
                operation, arguments = self.engine.answer, (text, answers)
            elif action == "approve_plan":
                if data.get("plan_hash") != state.get("plan_hash"):
                    raise WorkflowError("你确认的计划版本已经变化，请重新查看。")
                operation, arguments = self.engine.approve_plan, ()
            elif action == "fix":
                ids = self._ids(data)
                operation, arguments = self.engine.fix, (ids, str(data.get("note", "")))
            elif action == "add_round":
                operation, arguments = self.engine.add_round, ()
            elif action == "rereview":
                operation, arguments = self.engine.rereview, ()
            elif action == "finalize":
                ids = self._ids(data)
                operation, arguments = self.engine.finalize, (ids, str(data.get("note", "")))
            elif action == "pause":
                operation, arguments = self.engine.pause, ()
            elif action == "resume":
                operation, arguments = self.engine.resume, ()
            elif action == "restore":
                session_id = data.get("id")
                if not isinstance(session_id, str):
                    raise WorkflowError("会话标识无效。")
                operation, arguments = self.engine.restore, (session_id,)
            elif action == "settings":
                operation, arguments, affects = self.update_settings, (data,), False
            else:
                raise WorkflowError("不支持该操作。")
            self.requests[request_id] = monotonic()
            while len(self.requests) > 512:
                self.requests.popitem(last=False)
            if not control:
                self.busy = action
            self._publish("busy", self.busy)
            future = self.engine.submit(operation, *arguments, affects_session=affects, control=control)
            def completed(_):
                with self.condition:
                    if not control:
                        self.busy = None
                    self._publish("busy", self.busy)
            future.add_done_callback(completed)
            return {"accepted": True, "request_id": request_id}

    @staticmethod
    def _ids(data):
        ids = data.get("ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
            raise WorkflowError("问题编号清单无效。")
        return ids

    def diff(self):
        state = self.engine.state
        if not state or not state["repos"]:
            raise WorkflowError("当前还没有准备代码工作区。")
        result = []
        for repository in state["repos"]:
            if repository["name"] in state["commits"]:
                result.append({"repository": repository["name"], "commit": state["commits"][repository["name"]]})
                continue
            record = differences(repository)
            new_files = []
            for relative in record["untracked"]:
                path = safe_file(repository, relative)
                if path.stat().st_size > 512 * 1024:
                    new_files.append({"path": relative, "note": "文件较大，请在本地工作区查看。"})
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                    new_files.append({"path": relative, "patch": "".join(unified_diff([], lines, fromfile="/dev/null", tofile=relative))})
                except UnicodeError:
                    new_files.append({"path": relative, "note": "非 UTF-8 文本文件，请在本地工作区查看。"})
            record["new_files"] = new_files
            result.append(record)
        return result

    def close(self):
        self.engine.close()
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, application):
        super().__init__(address, WebHandler)
        self.application = application
        self.origin = f"http://127.0.0.1:{self.server_port}"


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        return

    def _headers(self, status, content_type, length=None, cookie=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE_NAME}={self.server.application.cookie_token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()

    def _json(self, status, value):
        data = dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _check(self, authenticated=False):
        if self.headers.get("Host") != urlparse(self.server.origin).netloc:
            raise WorkflowError("只接受本机固定地址访问。")
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            raise WorkflowError("拒绝其他站点发起的操作。")
        top_navigation = self.command == "GET" and urlparse(self.path).path == "/" and self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document"
        if self.headers.get("Sec-Fetch-Site") == "cross-site" and not top_navigation:
            raise WorkflowError("拒绝跨站访问本地工作流。")
        if authenticated:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(COOKIE_NAME)
            if value is None or not compare_digest(value.value, self.server.application.cookie_token):
                raise WorkflowError("页面连接已过期，请重新打开本地首页。")

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path, query = unquote(parsed.path), parse_qs(parsed.query)
            self._check(authenticated=path.startswith("/api/"))
            application = self.server.application
            if path == "/api/bootstrap":
                self._json(200, {**application.snapshot(), "csrf_token": application.csrf_token})
            elif path == "/api/events":
                self._events(query)
            elif path == "/api/branches":
                name = query.get("repository", [""])[0]
                repository = next((entry for entry in application.settings["repositories"] if entry["name"] == name), None)
                if repository is None:
                    raise WorkflowError("仓库未在配置中登记。")
                self._json(200, branches(repository["path"]))
            elif path == "/api/diff":
                self._json(200, application.diff())
            elif path == "/api/material":
                self._material(query)
            elif path == "/api/supplement-images":
                self._supplement_image(query)
            elif path.startswith("/api/"):
                self._json(404, {"error": "接口不存在。"})
            else:
                relative = "index.html" if path == "/" else path.lstrip("/")
                target = (ASSETS / relative).resolve()
                if not target.is_relative_to(ASSETS.resolve()) or not target.is_file():
                    self._json(404, {"error": "页面或资源不存在。"})
                    return
                data = target.read_bytes()
                content_type = "application/javascript" if target.suffix == ".js" else (guess_type(target.name)[0] or "application/octet-stream")
                self._headers(200, content_type + ("; charset=utf-8" if content_type.startswith("text/") or target.suffix == ".js" else ""), len(data), cookie=path == "/")
                self.wfile.write(data)
        except (WorkflowError, ValueError, OSError, KeyError) as error:
            self._json(400, {"error": str(error)})

    def do_POST(self):
        try:
            self._check(authenticated=True)
            application = self.server.application
            if not compare_digest(self.headers.get("X-Workflow-Token", ""), application.csrf_token):
                raise WorkflowError("操作凭据无效，请刷新页面后重试。")
            parsed = urlparse(self.path)
            if parsed.path == "/api/supplement-images":
                query = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0"))
                maximum = application.settings["zentao"]["max_attachment_mb"] * 1024 * 1024
                if not 0 < length <= maximum:
                    self.close_connection = True
                    raise WorkflowError("图片为空或超过配置的附件大小上限。")
                data = self.rfile.read(length)
                if len(data) != length:
                    raise WorkflowError("图片传输不完整，请重新上传。")
                self._json(200, application.upload_supplement_image(query.get("session_id", [""])[0],
                    int(query.get("revision", ["-1"])[0]), query.get("name", [""])[0], data))
                return
            if self.headers.get_content_type() != "application/json":
                raise WorkflowError("操作必须使用 JSON 请求。")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise WorkflowError("请求内容为空或超过大小限制。")
            data = loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise WorkflowError("请求格式无效。")
            if self.path == "/api/actions":
                self._json(202, application.dispatch(data))
            elif self.path == "/api/supplement-images/remove":
                self._json(200, application.remove_supplement_image(data.get("session_id"), data.get("revision"), data.get("image_id")))
            elif self.path == "/api/shutdown":
                self._json(200, {"stopping": True})
                Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "接口不存在。"})
        except (WorkflowError, ValueError, OSError, KeyError) as error:
            self._json(400, {"error": str(error)})

    def _material(self, query):
        application = self.server.application
        session_id = query.get("session", [""])[0]
        file_id = int(query.get("file_id", ["0"])[0])
        state = application.engine.store.load(session_id)
        material = next((item for item in state.get("snapshot", {}).get("materials", []) if item["file_id"] == file_id), None)
        if not material:
            raise WorkflowError("附件不在该工作项的资料清单中。")
        path = Path(material["path"]).resolve()
        allowed = (application.engine.store.directory(session_id) / "attachments").resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise WorkflowError("该附件当前不可读取。")
        data = path.read_bytes()
        preview = material.get("image") and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        self.send_response(200)
        self.send_header("Content-Type", (guess_type(path.name)[0] or "application/octet-stream") if preview else "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if not preview:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(str(material["title"]) + path.suffix))
        self.end_headers()
        self.wfile.write(data)

    def _supplement_image(self, query):
        application = self.server.application
        session_id = query.get("session_id", [""])[0]
        state = public_state(application.engine.store.load(session_id))
        image = next((entry for entry in state["supplement_images"] if entry["id"] == query.get("image_id", [""])[0]), None)
        if image is None:
            raise WorkflowError("图片不在当前会话的补充附件列表中。")
        path = Path(image["path"]).resolve()
        allowed = (application.engine.store.directory(session_id) / "attachments" / "supplement").resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise WorkflowError("该补充图片当前不可读取。")
        data = path.read_bytes()
        self._headers(200, image["content_type"], len(data))
        self.wfile.write(data)

    def _events(self, query):
        application = self.server.application
        cursor = int(self.headers.get("Last-Event-ID") or query.get("cursor", ["0"])[0])
        self._headers(200, "text/event-stream; charset=utf-8")
        try:
            while not application.closed:
                with application.condition:
                    if application.events and cursor < application.events[0][0] - 1:
                        value = application.snapshot()
                        cursor = application.sequence
                        packets = [f"id: {cursor}\nevent: snapshot\ndata: {dumps(value, ensure_ascii=False)}\n\n".encode("utf-8")]
                    else:
                        pending = [(number, packet) for number, packet in application.events if number > cursor]
                        packets = [packet for _, packet in pending]
                        if pending:
                            cursor = pending[-1][0]
                    if not packets:
                        application.condition.wait(timeout=10)
                        packets = [b": heartbeat\n\n"]
                for packet in packets:
                    self.wfile.write(packet)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def serve(open_page=True):
    settings = load_settings()
    configuration = settings.get("web", {})
    host, port = configuration.get("host", "127.0.0.1"), configuration.get("port", 8765)
    if host != "127.0.0.1" or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise WorkflowError("网页版本只允许绑定 127.0.0.1，端口须为 1024 至 65535 的整数。")
    application = WebApplication(settings)
    try:
        server = LocalServer((host, port), application)
    except OSError:
        application.close()
        raise WorkflowError(f"本机端口 {port} 已被占用；请明确调整 settings.json，不会自动切换端口。") from None
    application.server = server
    runtime = LOCAL / "web-runtime.json"
    runtime.write_text(dumps({"pid": getpid(), "url": server.origin}), encoding="utf-8")
    print("本地网页：" + server.origin, flush=True)
    if open_page:
        Timer(0.4, lambda: open_browser(server.origin)).start()
    application.dispatch({"action": "connect", "data": {}, "request_id": "startup-" + token_urlsafe(16)})
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        application.close()
        server.server_close()
        if runtime.is_file() and loads(runtime.read_text(encoding="utf-8")).get("pid") == getpid():
            runtime.unlink()
