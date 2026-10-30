"""本机网页接口：复用工作流引擎，浏览器断开不结束后台会话。"""

from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from difflib import unified_diff
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps, loads
from mimetypes import guess_type
from os import getpid
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from shutil import copytree, rmtree
from threading import Condition, RLock, Thread, Timer
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4
from webbrowser import open as open_browser

from workflow.projects import ProjectPartitions, assign_project, project_move_error, session_project
from workflow.context import normalized_sources
from workflow.store import session_repositories, session_title, timestamp
from workflow.engine import ACTIVE, COMPLETE, parse_review
from workflow.gitops import branches, cleanup_workspaces, differences, safe_file
from workflow.sessions import SessionManager
from workflow.rpc import AppServer, validate_selection
from workflow.settings import LOCAL, ROOT, WorkflowError, load_settings, save_password, save_settings
from workflow.zentao import KINDS, ZentaoClient, identifier


ASSETS = ROOT / "web" / "dist"
COOKIE_NAME = "zentao_workflow_session"


def public_state(state):
    if state is None:
        return None
    result = deepcopy(state)
    result.setdefault("supplement_images", [])
    result.setdefault("followup_images", [])
    result.setdefault("delivery_round", 1)
    result.setdefault("deliveries", [])
    result.setdefault("compile_checks", [])
    result.setdefault("compile_check", {"enabled": False})
    # 旧记录只在响应副本中关联原文，不修改磁盘上的会话数据。
    cursor = 0
    for review in result.get("reviews", []):
        if "history_index" in review:
            cursor = review["history_index"] + 1
            continue
        saved = review["result"]
        for index in range(cursor, len(result.get("history", []))):
            entry = result["history"][index]
            if entry["role"] != "Codex" or not entry["text"].lstrip().startswith(("{", "```")):
                continue
            try:
                raw = parse_review(entry["text"])
            except WorkflowError:
                continue
            if (raw["summary"] == saved.get("summary")
                    and raw["resolved_previous"] == saved.get("resolved_previous")
                    and len(raw["findings"]) <= len(saved["findings"])
                    and all(all(finding.get(key) == value for key, value in source.items())
                            for source, finding in zip(raw["findings"], saved["findings"]))):
                review["history_index"] = index
                cursor = index + 1
                break
    if "snapshot" in result:
        result["snapshot"]["sources"] = normalized_sources(result["snapshot"].get("sources", []))
    return result


def history_summary(state):
    return {**{key: state.get(key) for key in ("id", "item", "items", "stage", "updated_at", "fix_round", "commits")},
            "project": session_project(state), "title": session_title(state)}


class WebApplication:
    def __init__(self, settings=None):
        self.settings = settings or load_settings()
        self.lock = RLock()
        self.condition = Condition(self.lock)
        self.events = deque()
        self.event_bytes = 0
        self.sequence = 0
        self.cookie_token = token_urlsafe(32)
        self.csrf_token = token_urlsafe(32)
        self.items = []
        self.connected = False
        self.profile = None
        self.busy = None
        self.requests = OrderedDict()
        self.server = None
        self.closed = False
        self.sessions = SessionManager(self.settings, self.emit)
        self.project_partitions = ProjectPartitions(self.sessions.store.root.parent / "project-partitions.json")
        self.project_lock = RLock()
        self.codex_catalog_lock = RLock()
        self.codex_catalog = None
        self.management_executor = ThreadPoolExecutor(max_workers=1)
        self.pointer = LOCAL / "current-session.json"
        self.default_session_id = None
        if self.pointer.is_file():
            pointer = loads(self.pointer.read_text(encoding="utf-8"))
            if pointer["id"] in self.sessions.runtimes:
                self.default_session_id = pointer["id"]

    def _publish(self, kind, value):
        self.sequence += 1
        if isinstance(value, dict) and value.get("session_id") in self.sessions.runtimes:
            self.sessions.runtimes[value["session_id"]].event_cursor = self.sequence
            value = {**value, "event_cursor": self.sequence}
        if isinstance(value, dict) and value.get("preview_id") in self.sessions.previews:
            self.sessions.previews[value["preview_id"]].event_cursor = self.sequence
            value = {**value, "event_cursor": self.sequence}
        packet = f"id: {self.sequence}\nevent: {kind}\ndata: {dumps(value, ensure_ascii=False)}\n\n".encode("utf-8")
        self.events.append((self.sequence, packet))
        self.event_bytes += len(packet)
        while len(self.events) > 512 or (self.event_bytes > 4 * 1024 * 1024 and len(self.events) > 1):
            self.event_bytes -= len(self.events.popleft()[1])
        self.condition.notify_all()

    def session_payload(self, runtime):
        state = public_state(runtime.engine.state)
        state["title"] = session_title(runtime.engine.state)
        repositories = session_repositories(state)
        state["can_edit"] = runtime.engine.editable() and not runtime.engine.closed and not state.get("cleanup", {}).get("finalizing")
        state["project"] = session_project(state)
        state["project_move_reason"] = project_move_error(runtime)
        state["can_move_project"] = not state["project_move_reason"]
        return {"session_id": state["id"], "state": state, "revision": runtime.revision,
                "event_cursor": runtime.event_cursor, "stream": runtime.stream,
                "busy": runtime.busy, "working": runtime.engine.working(),
                "directory": str(self.sessions.store.directory(state["id"])),
                "reused_workspaces": sorted({entry["path"] for entry in repositories if not entry.get("workspace_owned", True)}),
                "workspaces": sorted({entry["path"] for entry in repositories if entry.get("workspace_owned", True)} |
                    {str(self.sessions.store.directory(state["id"]) / "workspace" /
                         (f"repo-{index + 1}" + (f"-delivery-{state['delivery_round']}" if state.get("followup") else "")))
                     for index, selection in enumerate(state.get("selections", []))
                     if selection["name"] not in {entry["name"] for entry in state["repos"]}})}

    def summary(self, runtime):
        reason = project_move_error(runtime)
        return {**history_summary(runtime.engine.state), "busy": runtime.busy,
                "working": runtime.engine.working(), "deleting": runtime.deleting,
                "can_move_project": not reason, "project_move_reason": reason}

    def preview_payload(self, runtime):
        state = public_state(runtime.engine.state)
        state["project"] = session_project(state)
        state["can_edit"] = not runtime.deleting and runtime.engine.editable()
        return {"preview_id": state["id"], "state": state, "revision": runtime.revision,
                "event_cursor": runtime.event_cursor, "busy": runtime.busy, "working": runtime.engine.working()}

    def preview(self, preview_id):
        with self.lock:
            return self.preview_payload(self.sessions.get_preview(preview_id))

    def discard_preview(self, preview_id):
        with self.condition:
            runtime = self.sessions.get_preview(preview_id)
            runtime.deleting = True
            def release():
                self.sessions.close_preview(preview_id)
                with self.condition:
                    del self.sessions.previews[preview_id]
            return self.management_executor.submit(release)

    def emit(self, session_id, kind, value):
        with self.condition:
            if self.closed:
                return
            if session_id in self.sessions.previews:
                runtime = self.sessions.previews[session_id]
                if runtime.deleting:
                    return
                if kind in {"state", "message", "activity"}:
                    runtime.revision += 1
                    self._publish("preview", self.preview_payload(runtime))
                return
            runtime = self.sessions.runtimes.get(session_id) if session_id else None
            if session_id and (not runtime or runtime.deleting):
                return
            if kind == "state":
                runtime.revision += 1
                if getattr(runtime, "turn_id", None) != value.get("turn_id"):
                    runtime.stream = ""
                runtime.turn_id = value.get("turn_id")
                self._publish("state", self.session_payload(runtime))
            elif kind == "items":
                self.items = deepcopy(value)
                self._publish("items", self.items)
                self._publish("projects", {"project_partitions": self.projects()})
            elif kind == "connected":
                self.connected, self.profile = True, value
                self._publish("connected", value)
            elif kind == "delta":
                runtime.stream += value
                self._publish("delta", {"session_id": session_id, "delta": value})
            elif kind in ("message", "history"):
                if kind == "message" and value["role"] in ("控制器", "Codex"):
                    runtime.stream = ""
                self._publish("conversation", self.session_payload(runtime))
            elif kind == "error":
                if not self.sessions.zentao.token:
                    self.connected = False
                self._publish("operation_error", {"session_id": session_id, "message": value, "connected": self.connected})
            elif kind == "questions":
                self._publish("questions", {"session_id": session_id, "questions": value})
            elif kind == "activity" and runtime:
                self._publish("busy", self.session_payload(runtime))

    def public_settings(self):
        return {"base_url": self.settings["zentao"]["base_url"], "account": self.settings["zentao"]["account"],
                "model": self.settings["codex"]["model"], "reasoning_effort": self.settings["codex"]["reasoning_effort"],
                "max_fix_rounds": self.settings["review"]["max_fix_rounds"],
                "compile_check_enabled": self.settings.get("compile_check", {}).get("enabled", False),
                "max_attachment_mb": self.settings["zentao"]["max_attachment_mb"],
                "review_prompt": (ROOT / self.settings["review"]["prompt_file"]).read_text(encoding="utf-8").strip(),
                "repositories": deepcopy(self.settings["repositories"]), "feishu_enabled": False}

    def codex_models(self, refresh=False):
        with self.codex_catalog_lock:
            if self.codex_catalog is None or refresh:
                self.codex_catalog = None
                rpc = AppServer(self.settings, lambda message: None)
                try:
                    rpc.connect()
                    self.codex_catalog = rpc.catalog()
                except (KeyError, TypeError) as error:
                    raise WorkflowError("Codex 模型列表格式不符合当前协议，已停止读取。") from error
                finally:
                    rpc.close()
            return deepcopy(self.codex_catalog)

    def snapshot(self):
        with self.lock:
            return {"preview_supported": True, "items": deepcopy(self.items), "default_session_id": self.default_session_id,
                    "connected": self.connected, "profile": self.profile, "busy": self.busy,
                    "cursor": self.sequence, "settings": self.public_settings(),
                    "project_partitions": self.projects(),
                    "sessions": {session_id: self.session_payload(runtime) for session_id, runtime in self.sessions.runtimes.items()},
                    "history": sorted((self.summary(runtime) for runtime in self.sessions.runtimes.values()),
                                      key=lambda entry: (entry["updated_at"], entry["id"]), reverse=True)}

    def session(self, session_id):
        with self.lock:
            return self.session_payload(self.sessions.get(session_id))

    def rename_session(self, request):
        session_id, title, request_id = request.get("session_id"), request.get("title"), request.get("request_id")
        if (not isinstance(session_id, str) or not isinstance(title, str) or not title.strip()
                or not isinstance(request_id, str) or not 8 <= len(request_id) <= 100):
            raise WorkflowError("请选择会话并填写非空的会话标题及请求标识。")
        key = ("session_title", session_id, request_id)
        with self.condition:
            if self.closed:
                raise WorkflowError("服务正在关闭。")
            if key in self.requests:
                return {**self.requests[key], "duplicate": True}
            runtime = self.sessions.get(session_id)
            if runtime.engine.closed or runtime.engine.state.get("cleanup"):
                raise WorkflowError("会话已进入清理阶段，不能修改标题。")
            # 与后台保存共用存储锁，保留执行器正在使用的状态对象，只更新标题元数据。
            with runtime.engine.store.lock:
                state = deepcopy(runtime.engine.state)
                state["custom_title"] = title.strip()
                runtime.engine.store.save(state)
                runtime.engine.state.update(custom_title=state["custom_title"], updated_at=state["updated_at"])
            self.emit(session_id, "state", runtime.engine.state)
            result = self.session_payload(runtime)
            self.requests[key] = result
            while len(self.requests) > 512:
                self.requests.popitem(last=False)
            return result

    def projects(self):
        return self.project_partitions.list(self.settings["zentao"]["base_url"].rstrip("/"),
            [runtime.engine.state for runtime in self.sessions.runtimes.values()], self.items)

    def add_project(self, request):
        project_id, request_id = request.get("project_id"), request.get("request_id")
        if (type(project_id) is not int or project_id <= 0 or not isinstance(request_id, str)
                or not 8 <= len(request_id) <= 100):
            raise WorkflowError("请选择有效的禅道项目及请求标识。")
        key = ("project_partition", request_id)
        # 网络读取不持有事件锁，避免阻塞其他会话的状态通知。
        with self.project_lock:
            with self.condition:
                if self.closed or self.busy == "settings":
                    raise WorkflowError("服务正在关闭或修改设置，不能添加项目分区。")
                if key in self.requests:
                    return {**self.requests[key], "duplicate": True}
                site = self.settings["zentao"]["base_url"].rstrip("/")
            with self.sessions.zentao_lock:
                project = self.sessions.zentao._fetch(f"api.php/v1/projects/{project_id}")
            if identifier(project.get("id")) != project_id or not isinstance(project.get("name"), str) or not project["name"].strip():
                raise WorkflowError("所属项目信息不符合预期。")
            project = {"id": project_id, "name": project["name"].strip()}
            with self.condition:
                if self.closed or site != self.settings["zentao"]["base_url"].rstrip("/"):
                    raise WorkflowError("禅道站点已变化或服务正在关闭，未添加项目分区。")
                self.project_partitions.add(site, project)
                result = {"project": project, "project_partitions": self.projects()}
                self.requests[key] = result
                while len(self.requests) > 512:
                    self.requests.popitem(last=False)
                self._publish("projects", {"project_partitions": result["project_partitions"]})
                return result

    def move_project(self, request):
        session_id, preview_id = request.get("session_id"), request.get("preview_id")
        project_id, request_id = request.get("project_id"), request.get("request_id")
        if (bool(session_id) == bool(preview_id) or "project_id" not in request
                or (project_id is not None and (type(project_id) is not int or project_id <= 0))
                or not isinstance(request_id, str) or not 8 <= len(request_id) <= 100):
            raise WorkflowError("请明确指定会话、目标项目及请求标识。")
        key = (session_id or preview_id, request_id)
        with self.condition:
            if self.closed or self.busy == "settings":
                raise WorkflowError("服务正在关闭或修改设置，不能更换项目。")
            if key in self.requests:
                return {**self.requests[key], "duplicate": True}
            runtime = self.sessions.get_preview(preview_id) if preview_id else self.sessions.get(session_id)
            if request.get("revision") != runtime.revision:
                raise WorkflowError("页面显示的流程版本已过期，请查看最新内容后再确认。")
            reason = project_move_error(runtime)
            if reason:
                raise WorkflowError(reason)
            project = {"id": 0, "name": ""} if project_id is None else next(
                (value for value in self.projects() if value["id"] == project_id), None)
            if project is None:
                raise WorkflowError("目标项目分区不存在，请先添加项目分区。")
            state = deepcopy(runtime.engine.state)
            assign_project(state, project)
            runtime.engine.store.save(state)
            runtime.engine.state = state
            self.emit(state["id"], "state", state)
            self._publish("projects", {"project_partitions": self.projects()})
            result = self.preview_payload(runtime) if preview_id else self.session_payload(runtime)
            self.requests[key] = result
            while len(self.requests) > 512:
                self.requests.popitem(last=False)
            return result

    def _supplement_runtime(self, session_id, revision, preview_id=None, purpose="supplement"):
        if not isinstance(purpose, str) or purpose not in {"supplement", "followup"} or (purpose == "followup" and preview_id):
            raise WorkflowError("图片用途无效，后续对话图片必须归属正式会话。")
        if bool(session_id) == bool(preview_id):
            raise WorkflowError("请明确指定一个会话或当前查看资料。")
        runtime = self.sessions.get_preview(preview_id) if preview_id else self.sessions.get(session_id)
        state = runtime.engine.state
        if self.closed or self.busy == "settings" or runtime.busy or runtime.engine.working():
            raise WorkflowError("该会话正在执行操作，请等待完成后修改图片。")
        allowed = (state["stage"] in COMPLETE and state["phase"] == "complete") if purpose == "followup" else state["stage"] in {"资料已读取", "准备工作区失败"}
        if (runtime.engine.closed or not allowed
                or state.get("cleanup", {}).get("removed") or state.get("cleanup", {}).get("finalizing")):
            raise WorkflowError("当前阶段不能修改补充图片。")
        if type(revision) is not int or revision != runtime.revision:
            raise WorkflowError("页面显示的流程版本已过期，请查看最新内容后再操作。")
        return runtime

    def upload_supplement_image(self, session_id, revision, name, data, preview_id=None, purpose="supplement"):
        with self.condition:
            runtime = self._supplement_runtime(session_id, revision, preview_id, purpose)
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
            path = runtime.engine.store.directory(runtime.engine.state["id"]) / "attachments" / purpose / f"{image_id}.{extension}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            image = {"id": image_id, "name": name, "extension": extension, "size": len(data),
                     "path": str(path), "content_type": guess_type(path.name)[0]}
            runtime.engine.state.setdefault(purpose + "_images", []).append(image)
            runtime.engine.save()
            return self.preview_payload(runtime) if preview_id else self.session_payload(runtime)

    def remove_supplement_image(self, session_id, revision, image_id, preview_id=None, purpose="supplement"):
        with self.condition:
            runtime = self._supplement_runtime(session_id, revision, preview_id, purpose)
            images = runtime.engine.state.get(purpose + "_images", [])
            image = next((entry for entry in images if entry["id"] == image_id and "history_index" not in entry), None)
            if image is None:
                raise WorkflowError("图片不在当前会话的补充附件列表中。")
            path = Path(image["path"]).resolve()
            allowed = (runtime.engine.store.directory(runtime.engine.state["id"]) / "attachments" / purpose).resolve()
            if not path.is_relative_to(allowed):
                raise WorkflowError("补充图片路径不合法。")
            path.unlink()
            images.remove(image)
            runtime.engine.save()
            return self.preview_payload(runtime) if preview_id else self.session_payload(runtime)

    def update_settings(self, data):
        if any(runtime.engine.working() or runtime.uploads for runtime in self.sessions.previews.values()):
            raise WorkflowError("工作项资料正在读取或上传，请等待完成后修改设置。")
        if any(runtime.engine.working() or runtime.deleting or runtime.engine.state["stage"] not in
               COMPLETE | {"资料已读取", "资料读取失败"} for runtime in self.sessions.runtimes.values()):
            raise WorkflowError("有会话的流程配置已固定，请结束这些流程后修改默认设置。")
        account = str(data.get("account", "")).strip()
        url = str(data.get("base_url", "")).strip().rstrip("/")
        parsed = urlparse(url)
        maximum = data.get("max_fix_rounds")
        prompt = str(data.get("review_prompt", "")).strip()
        password = data.get("password", "")
        compile_enabled = data.get("compile_check_enabled", self.settings.get("compile_check", {}).get("enabled", False))
        if type(compile_enabled) is not bool:
            raise WorkflowError("编译检查开关必须是布尔值。")
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise WorkflowError("请填写不包含凭据的完整禅道 HTTP 地址。")
        if not account or not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0 or not prompt:
            raise WorkflowError("账号、非负整数轮次及 Review 提示词不能为空。")
        if account != self.settings["zentao"]["account"] and not password:
            raise WorkflowError("更换账号时请同时填写对应密码。")
        model, effort = data.get("model"), data.get("reasoning_effort")
        validate_selection(self.codex_models(), model, effort, allow_default=True)
        if password:
            if not isinstance(password, str):
                raise WorkflowError("密码格式无效。")
            save_password(password)
        with self.sessions.settings_lock:
            self.settings["zentao"].update(base_url=url, account=account)
            self.settings["codex"].update(model=model, reasoning_effort=effort)
            self.settings["review"]["max_fix_rounds"] = maximum
            self.settings["compile_check"] = {"enabled": compile_enabled}
            (ROOT / self.settings["review"]["prompt_file"]).write_text(prompt + "\n", encoding="utf-8")
            save_settings(self.settings)
        with self.sessions.zentao_lock:
            client = ZentaoClient(self.settings)
            self.sessions.zentao = client
            self.sessions.connection.zentao = client
            for runtime in self.sessions.runtimes.values():
                runtime.engine.zentao = client
            for runtime in self.sessions.previews.values():
                runtime.engine.zentao = client
        with self.condition:
            self.connected, self.profile = False, None
            self.items = []
            self._publish("settings", self.public_settings())
            self._publish("items", [])
            self._publish("connection", {"connected": False})
            self._publish("projects", {"project_partitions": self.projects()})

    def promote_preview(self, preview_id, runtime, supplement, items, codex):
        source = runtime.engine.state
        if (source["stage"] != "资料已读取" or not source.get("snapshot")
                or items != [{"kind": item["kind"], "id": item["id"]} for item in source["items"]]):
            raise WorkflowError("当前选择与已读取资料不一致，请完成资料读取后再开始计划。")
        if runtime.uploads:
            raise WorkflowError("补充图片尚未上传完成，请等待后再开始计划。")
        if source["item"] is None and not supplement.strip() and not source["supplement_images"]:
            raise WorkflowError("请填写消息或添加至少一张图片。")
        selection = validate_selection(self.codex_models(), codex.get("model"), codex.get("reasoning_effort"))
        compile_enabled = codex.get("compile_check_enabled", self.settings.get("compile_check", {}).get("enabled", False))
        if type(compile_enabled) is not bool:
            raise WorkflowError("编译检查开关必须是布尔值。")
        state = self.sessions.store.new(source["item"], save=False)
        state.update({key: deepcopy(value) for key, value in source.items() if key not in {"id", "created_at", "updated_at"}})
        state["supplement"] = supplement.strip()
        state["codex"] = selection
        state["compile_check"] = {"enabled": compile_enabled}
        state["plan_started_at"] = timestamp()
        directory = self.sessions.store.directory(state["id"])
        previous = runtime.engine.store.directory(preview_id)
        try:
            if (previous / "attachments").exists():
                copytree(previous / "attachments", directory / "attachments")
            for entry in [*state["snapshot"]["materials"], *state["supplement_images"]]:
                entry["path"] = str(directory / Path(entry["path"]).relative_to(previous))
            if state["item"] is None:
                state["conversation_title"] = " ".join(supplement.split())[:50] or "图片需求"
                state["history"].append({"time": timestamp(), "role": "用户", "text": state["supplement"],
                                         "phase": "plan", "delivery_round": 1, "image_purpose": "supplement",
                                         "images": deepcopy(state["supplement_images"])})
            self.sessions.store.save(state)
        except (OSError, ValueError):
            if directory.exists():
                rmtree(directory)
            raise
        created = self.sessions.add(state)
        self.sessions.promoted_previews[preview_id] = state["id"]
        self.discard_preview(preview_id)
        self.emit(state["id"], "state", state)
        return created

    def dispatch(self, request):
        action = request.get("action")
        data = request.get("data", {})
        request_id = request.get("request_id")
        if not isinstance(data, dict) or not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise WorkflowError("操作参数或请求标识无效。")
        with self.condition:
            if self.closed:
                raise WorkflowError("服务正在关闭。")
            session_id = request.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                raise WorkflowError("会话标识无效。")
            preview_id = request.get("preview_id")
            if preview_id is not None and (not isinstance(preview_id, str) or session_id):
                raise WorkflowError("查看资料标识无效，不能与会话同时指定。")
            request_key = (session_id or preview_id, request_id)
            if request_key in self.requests:
                previous = self.requests[request_key]
                if previous.get("session_id") in self.sessions.runtimes:
                    saved = self.sessions.runtimes[previous["session_id"]].engine.state.get("codex")
                    if saved and any(key in data and data[key] != saved[key] for key in ("model", "reasoning_effort")):
                        raise WorkflowError("会话模型配置已经固定，不能通过重复请求修改。")
                return {**self.requests[request_key], "duplicate": True}
            if action == "restore":
                return {"accepted": True, **self.session(data.get("id"))}
            if preview_id and action == "plan" and preview_id in self.sessions.promoted_previews:
                created_id = self.sessions.promoted_previews[preview_id]
                saved = self.sessions.get(created_id).engine.state["codex"]
                if any(key in data and data[key] != saved[key] for key in ("model", "reasoning_effort")):
                    raise WorkflowError("会话模型配置已经固定，不能通过重复请求修改。")
                return {"accepted": True, "duplicate": True, "session_id": created_id, "request_id": request_id}
            if preview_id and action not in {"collect", "plan"}:
                raise WorkflowError("当前查看资料尚未创建会话。")
            global_action = action in {"connect", "refresh", "settings"}
            runtime = (self.sessions.get_preview(preview_id) if preview_id else
                       None if global_action or action == "new_conversation" or (action == "collect" and not session_id)
                       else self.sessions.get(session_id))
            engine = runtime.engine if runtime else self.sessions.connection
            state = engine.state
            if action == "followup" and any((entry.get("followup") or {}).get("request_id") == request_id
                    for entry in [state, *state.get("deliveries", [])]):
                return {"accepted": True, "duplicate": True, "session_id": session_id, "request_id": request_id}
            if self.busy == "settings" or (global_action and self.busy):
                raise WorkflowError("全局操作尚未完成，请等待。")
            if runtime:
                if runtime.busy and action != "pause":
                    raise WorkflowError("该会话上一个操作尚未完成，请等待。")
                if action not in {"pause", "delete_session", "answer"} and engine.working():
                    raise WorkflowError("该会话仍在执行，请等待或先暂停。")
                if action != "delete_session" and (engine.closed or state.get("cleanup", {}).get("removed") or state.get("cleanup", {}).get("finalizing")):
                    raise WorkflowError("该会话已进入清理阶段，只能继续删除。")
                if action in {"plan", "configure_codex", "confirm_project", "choose_branches", "edit_branches", "approve_plan", "fix", "finalize", "collect", "delete_session", "followup", "rereview", "add_round", "compile_decision"} and request.get("revision") != runtime.revision:
                    raise WorkflowError("页面显示的流程版本已过期，请查看最新内容后再确认。")
                if not preview_id:
                    if ("plan_started_at" in state and "compile_check_enabled" in data
                            and data["compile_check_enabled"] != state.get("compile_check", {}).get("enabled", False)):
                        raise WorkflowError("会话编译检查配置已经固定，不能再次修改。")
                    saved = state.get("codex")
                    if saved and any(key in data and data[key] != saved[key] for key in ("model", "reasoning_effort")):
                        raise WorkflowError("会话模型配置已经固定，请新建会话使用其他配置。")
                    if not saved and action not in {"collect", "pause", "delete_session", "configure_codex", "plan"}:
                        raise WorkflowError("该历史会话尚未保存模型配置，请先选择模型和推理强度。")
            affects, control = True, action == "pause"
            if action == "connect":
                operation, arguments, affects = engine.connect, (), False
            elif action == "refresh":
                operation, arguments, affects = engine.refresh, (), False
            elif action == "new_conversation":
                project_id = data.get("project_id", 0)
                if session_id or preview_id or type(project_id) is not int or project_id < 0:
                    raise WorkflowError("新建会话只接受有效的所属项目编号。")
                project = next((value for value in self.projects() if value["id"] == project_id), None)
                if project_id and project is None:
                    raise WorkflowError("目标项目分区不存在，请先添加项目分区。")
                runtime = self.sessions.new_preview(None)
                engine = runtime.engine
                preview_id = engine.state["id"]
                if project:
                    assign_project(engine.state, project)
                operation, arguments = engine.save, ()
            elif action == "collect":
                choices = data.get("items")
                if not isinstance(choices, list) or not choices:
                    raise WorkflowError("请选择至少一个工作项。")
                available = {(item["kind"], item["id"]): item for item in self.items}
                selected, seen = [], set()
                for choice in choices:
                    if (not isinstance(choice, dict) or not isinstance(choice.get("kind"), str) or choice["kind"] not in KINDS
                            or type(choice.get("id")) is not int):
                        raise WorkflowError("工作项类型或编号无效。")
                    key = (choice["kind"], choice["id"])
                    if key not in available or key in seen:
                        raise WorkflowError("请从当前登录账号的待处理列表中选择不重复的工作项。")
                    selected.append(deepcopy(available[key]))
                    seen.add(key)
                if len(selected) > 1 and any(item["kind"] != "task" for item in selected):
                    raise WorkflowError("仅任务支持多选联合开发，需求和 Bug 请单独选择。")
                if runtime and not engine.editable():
                    raise WorkflowError("开始计划后不能改变工作项，请新建会话。")
                if not runtime:
                    runtime = self.sessions.new_preview(selected[0])
                    engine = runtime.engine
                    preview_id = engine.state["id"]
                    self.emit(preview_id, "state", engine.state)
                operation, arguments = engine.collect, (selected,)
            elif action in {"plan", "edit_branches"}:
                supplement = data.get("supplement", "")
                if not isinstance(supplement, str) or "selections" in data:
                    raise WorkflowError("开始计划只接受补充说明，仓库与分支由服务端确定。")
                if preview_id:
                    runtime = self.promote_preview(preview_id, runtime, supplement, data.get("items"), data)
                    engine = runtime.engine
                    session_id = engine.state["id"]
                elif not state.get("codex"):
                    state["codex"] = validate_selection(self.codex_models(), data.get("model"), data.get("reasoning_effort"))
                    engine.save()
                if "plan_started_at" not in engine.state:
                    enabled = data.get("compile_check_enabled", self.settings.get("compile_check", {}).get("enabled", False))
                    if type(enabled) is not bool:
                        raise WorkflowError("编译检查开关必须是布尔值。")
                    engine.state["compile_check"] = {"enabled": enabled}
                    engine.state["plan_started_at"] = timestamp()
                    engine.store.save(engine.state)
                operation, arguments = engine.start_plan, (supplement, action == "edit_branches")
            elif action == "configure_codex":
                if state.get("codex"):
                    raise WorkflowError("会话模型配置已经固定，不能再次设置。")
                selection = validate_selection(self.codex_models(), data.get("model"), data.get("reasoning_effort"))
                operation, arguments = self.configure_codex, (engine, selection)
            elif action == "confirm_project":
                project_id, name = data.get("project_id"), data.get("name")
                if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0 or not isinstance(name, str) or not name.strip():
                    raise WorkflowError("请明确填写所属项目编号和名称。")
                operation, arguments = engine.confirm_project, (project_id, name.strip())
            elif action == "choose_branches":
                choices = data.get("choices")
                if not isinstance(choices, dict):
                    raise WorkflowError("请明确选择待确定仓库的分支。")
                operation, arguments = engine.choose_branches, (choices,)
            elif action == "followup":
                review_enabled = data.get("review_enabled")
                if not isinstance(review_enabled, bool):
                    raise WorkflowError("请明确设置修改后自动 Review 开关。")
                text = data.get("text", "")
                images = engine.validate_followup(text, data.get("image_ids", []))
                operation, arguments = engine.followup, (text, images, request_id, review_enabled)
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
                operation, arguments = engine.answer, (text, answers)
            elif action == "approve_plan":
                if data.get("plan_hash") != state.get("plan_hash"):
                    raise WorkflowError("你确认的计划版本已经变化，请重新查看。")
                operation, arguments = engine.approve_plan, ()
            elif action == "fix":
                ids = self._ids(data)
                operation, arguments = engine.fix, (ids, str(data.get("note", "")))
            elif action == "add_round":
                operation, arguments = engine.add_round, ()
            elif action == "rereview":
                operation, arguments = engine.rereview, ()
            elif action == "compile_decision":
                if any(not isinstance(data.get(key), str) or not data[key] for key in ("batch_id", "repository", "decision")):
                    raise WorkflowError("请选择当前编译检查及有效操作。")
                operation, arguments = engine.compile_decision, (data["batch_id"], data["repository"], data["decision"])
            elif action == "finalize":
                ids = self._ids(data)
                operation, arguments = engine.finalize, (ids, str(data.get("note", "")))
            elif action == "pause":
                operation, arguments = engine.pause, ()
            elif action == "resume":
                operation, arguments = engine.resume, ()
            elif action == "delete_session":
                if engine.working() or state["stage"] in ACTIVE | {"等待人工回答", "资料读取中"}:
                    raise WorkflowError("该会话仍在执行，请先暂停并等待操作结束后再删除。")
                runtime.deleting = True
                operation, arguments, affects = self.delete_session, (session_id, runtime), False
            elif action == "settings":
                operation, arguments, affects = self.update_settings, (data,), False
            else:
                raise WorkflowError("不支持该操作。")
            viewing = bool(preview_id) and not session_id
            result = {"accepted": True, "request_id": request_id}
            if viewing:
                result["preview_id"] = preview_id
            else:
                result["session_id"] = session_id if runtime else None
            self.requests[request_key] = result
            while len(self.requests) > 512:
                self.requests.popitem(last=False)
            if not control:
                if runtime:
                    runtime.busy = action
                else:
                    self.busy = action
            self._publish("preview" if viewing else "busy", self.preview_payload(runtime) if viewing else
                          self.session_payload(runtime) if runtime else {"session_id": None, "busy": self.busy})
            if action == "delete_session":
                future = self.management_executor.submit(operation, *arguments)
            else:
                future = engine.submit(operation, *arguments, affects_session=affects, control=control)
            def completed(_):
                with self.condition:
                    if self.closed:
                        return
                    if not control:
                        if runtime:
                            runtime.busy = None
                        else:
                            self.busy = None
                    if viewing:
                        if preview_id in self.sessions.previews and not runtime.deleting:
                            self._publish("preview", self.preview_payload(runtime))
                    elif not runtime or session_id in self.sessions.runtimes:
                        self._publish("busy", self.session_payload(runtime) if runtime else {"session_id": None, "busy": self.busy})
            future.add_done_callback(completed)
            return result

    @staticmethod
    def configure_codex(engine, selection):
        engine.state["codex"] = selection
        engine.save()

    def delete_session(self, session_id, runtime):
        engine = runtime.engine
        try:
            engine._close_rpc()
            if not engine.closed:
                engine.executor.submit(lambda: None).result()
                engine.control_executor.submit(lambda: None).result()
            cleanup_workspaces(self.sessions.store, engine.state, self.settings["repositories"])
            engine.state.setdefault("cleanup", {})["finalizing"] = True
            engine.store.save(engine.state)
            engine.close()
            self.sessions.store.remove(session_id)
            with self.condition:
                if self.pointer.is_file() and loads(self.pointer.read_text(encoding="utf-8")).get("id") == session_id:
                    self.pointer.unlink()
                if self.default_session_id == session_id:
                    self.default_session_id = None
                del self.sessions.runtimes[session_id]
                self._publish("projects", {"project_partitions": self.projects()})
                self._publish("session_deleted", {"session_id": session_id})
        except Exception as error:
            with self.condition:
                runtime.deleting = False
                engine.state["error"] = str(error)
                engine.store.save(engine.state)
                self.emit(session_id, "state", engine.state)
                self.emit(session_id, "error", str(error))

    @staticmethod
    def _ids(data):
        ids = data.get("ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
            raise WorkflowError("问题编号清单无效。")
        return ids

    def diff(self, session_id):
        with self.lock:
            state = deepcopy(self.sessions.get(session_id).engine.state)
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
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.management_executor.shutdown(wait=True, cancel_futures=True)
        self.sessions.close()


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
            elif path == "/api/session":
                self._json(200, application.session(query.get("id", [""])[0]))
            elif path == "/api/preview":
                self._json(200, application.preview(query.get("id", [""])[0]))
            elif path == "/api/zentao-projects":
                with application.sessions.zentao_lock:
                    projects = application.sessions.zentao.list_projects()
                self._json(200, {"projects": projects})
            elif path == "/api/codex-models":
                self._json(200, application.codex_models(refresh=query.get("refresh") == ["1"]))
            elif path == "/api/branches":
                name = query.get("repository", [""])[0]
                repository = next((entry for entry in application.settings["repositories"] if entry["name"] == name), None)
                if repository is None:
                    raise WorkflowError("仓库未在配置中登记。")
                self._json(200, branches(repository["path"]))
            elif path == "/api/diff":
                self._json(200, application.diff(query.get("session_id", [""])[0]))
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
        unread_length = 0
        try:
            application = self.server.application
            parsed = urlparse(self.path)
            image_upload = parsed.path == "/api/supplement-images"
            length = int(self.headers.get("Content-Length", "0"))
            maximum = application.settings["zentao"]["max_attachment_mb"] * 1024 * 1024 if image_upload else 1024 * 1024
            if not 0 < length <= maximum:
                self.close_connection = True
                raise WorkflowError("图片为空或超过配置的附件大小上限。" if image_upload else "请求内容为空或超过大小限制。")
            unread_length = length
            self._check(authenticated=True)
            if not compare_digest(self.headers.get("X-Workflow-Token", ""), application.csrf_token):
                raise WorkflowError("操作凭据无效，请刷新页面后重试。")
            if image_upload:
                query = parse_qs(parsed.query)
                preview_id = query.get("preview_id", [None])[0]
                session_id = query.get("session_id", [None])[0]
                revision = int(query.get("revision", ["-1"])[0])
                with application.condition:
                    if preview_id:
                        runtime = application.sessions.get_preview(preview_id)
                        runtime.uploads += 1
                try:
                    unread_length = 0
                    data = self.rfile.read(length)
                    if len(data) != length:
                        raise WorkflowError("图片传输不完整，请重新上传。")
                    self._json(200, application.upload_supplement_image(session_id, revision,
                        query.get("name", [""])[0], data, preview_id, query.get("purpose", ["supplement"])[0]))
                finally:
                    if preview_id:
                        with application.condition:
                            runtime.uploads -= 1
                return
            if self.headers.get_content_type() != "application/json":
                raise WorkflowError("操作必须使用 JSON 请求。")
            unread_length = 0
            data = loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise WorkflowError("请求格式无效。")
            if self.path == "/api/actions":
                self._json(202, application.dispatch(data))
            elif self.path == "/api/supplement-images/remove":
                self._json(200, application.remove_supplement_image(data.get("session_id"), data.get("revision"),
                    data.get("image_id"), data.get("preview_id"), data.get("purpose", "supplement")))
            elif self.path == "/api/projects":
                self._json(200, application.add_project(data))
            elif self.path == "/api/sessions/title":
                self._json(200, application.rename_session(data))
            elif self.path == "/api/sessions/project":
                self._json(200, application.move_project(data))
            elif self.path == "/api/previews/discard":
                application.discard_preview(data.get("preview_id"))
                self._json(200, {"discarded": True})
            elif self.path == "/api/shutdown":
                self._json(200, {"stopping": True})
                Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "接口不存在。"})
        except (WorkflowError, ValueError, OSError, KeyError) as error:
            # 拒绝的请求也消费完限额内的请求体，避免关闭连接时因未读数据触发 TCP 中断。
            if unread_length:
                self.rfile.read(unread_length)
            self._json(400, {"error": str(error)})

    def _material(self, query):
        application = self.server.application
        session_id = query.get("session", [""])[0]
        preview_id = query.get("preview_id", [None])[0]
        file_id = int(query.get("file_id", ["0"])[0])
        runtime = self._material_runtime(session_id, preview_id)
        state = runtime.engine.state
        material = next((item for item in state.get("snapshot", {}).get("materials", []) if item["file_id"] == file_id), None)
        if not material:
            raise WorkflowError("附件不在该工作项的资料清单中。")
        path = Path(material["path"]).resolve()
        allowed = (runtime.engine.store.directory(state["id"]) / "attachments").resolve()
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
        runtime = self._material_runtime(session_id, query.get("preview_id", [None])[0])
        state = runtime.engine.state
        purpose = query.get("purpose", ["supplement"])[0]
        if purpose not in {"supplement", "followup"}:
            raise WorkflowError("图片用途无效。")
        image = next((entry for entry in state.get(purpose + "_images", []) if entry["id"] == query.get("image_id", [""])[0]), None)
        if image is None:
            raise WorkflowError("图片不在当前会话的补充附件列表中。")
        path = Path(image["path"]).resolve()
        allowed = (runtime.engine.store.directory(state["id"]) / "attachments" / purpose).resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise WorkflowError("该补充图片当前不可读取。")
        data = path.read_bytes()
        self._headers(200, image["content_type"], len(data))
        self.wfile.write(data)

    def _material_runtime(self, session_id, preview_id):
        if bool(session_id) == bool(preview_id):
            raise WorkflowError("请明确指定一个会话或当前查看资料。")
        sessions = self.server.application.sessions
        return sessions.get_preview(preview_id) if preview_id else sessions.get(session_id)

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
