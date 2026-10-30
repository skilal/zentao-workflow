"""显式暂停点、原生计划模式、修复轮次与本地提交控制。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from json import JSONDecodeError, dumps, loads
from pathlib import Path
from re import IGNORECASE, fullmatch, search
from threading import Lock, RLock

from workflow.approvals import is_controlled_write
from workflow.branching import BRANCH_INSTRUCTIONS, BRANCH_SCHEMA, configured_repositories, saved_branches, validate_result
from workflow.compile_flow import CompileFlow
from workflow.compile_checks import compile_context
from workflow.context import context_hash, requirement_context
from workflow.delivery import validate_review_request
from workflow.gitops import acquire_workspace, branch_catalog, commit, ensure_baseline, git, manifest, refresh_branches, working_paths
from workflow.projects import assign_project, project_locked
from workflow.rpc import AppServer, thread_parameters
from workflow.settings import ROOT, WorkflowError, save_settings
from workflow.store import SessionStore, session_title, timestamp
from workflow.user_input import input_request_path, pending_input
from workflow.zentao import ZentaoClient, project_context


FINDING_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "findings": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "category": {"type": "string", "enum": ["超出最小范围", "业务链路问题"]},
        "origin": {"type": "string", "enum": ["本次引入", "原有问题", "证据不足"]},
        "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
        "previous_id": {"type": "string"},
        "repository": {"type": "string"}, "path": {"type": "string"}, "line": {"type": "integer"},
        "evidence": {"type": "string"}, "suggestion": {"type": "string"}, "impact": {"type": "string"}
    }, "required": ["title", "category", "origin", "severity", "previous_id", "repository", "path", "line", "evidence", "suggestion", "impact"], "additionalProperties": False}},
    "resolved_previous": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "evidence": {"type": "string"}
    }, "required": ["id", "evidence"], "additionalProperties": False}}
}, "required": ["summary", "findings", "resolved_previous"], "additionalProperties": False}

ACTIVE = {"识别分支中", "刷新分支中", "准备工作区中", "计划中", "实施中", "审查中", "修复中", "续聊处理中", "编译检查中", "编译修复中"}
COMPLETE = {"已完成（静态审查）", "已完成（未审查）"}
PHASE_STAGE = {"branch": "识别分支中", "plan": "计划中", "implement": "实施中", "review": "审查中", "fix": "修复中", "followup": "续聊处理中", "compile_fix": "编译修复中"}


def parse_review(text):
    """只接收完整 JSON 或单个代码块，并按固定审查协议校验。"""
    match = fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", text.strip(), IGNORECASE)
    try:
        result = loads(match.group(1) if match else text)
    except JSONDecodeError as error:
        raise WorkflowError("Review 结果不是完整的 JSON，已停止。") from error
    pending = [(result, FINDING_SCHEMA)]
    types = {"object": dict, "array": list, "string": str, "integer": int}
    while pending:
        value, schema = pending.pop()
        kind = schema["type"]
        if not isinstance(value, types[kind]) or (kind == "integer" and isinstance(value, bool)):
            raise WorkflowError("Review 结果字段类型不符合约定结构，已停止。")
        if "enum" in schema and value not in schema["enum"]:
            raise WorkflowError("Review 结果包含不支持的类别、来源或优先级，已停止。")
        if kind == "object":
            if set(value) != set(schema["required"]):
                raise WorkflowError("Review 结果缺少必要字段或包含未知字段，已停止。")
            pending.extend((value[key], child) for key, child in schema["properties"].items())
        elif kind == "array":
            pending.extend((entry, schema["items"]) for entry in value)
    if any(not finding[key].strip() for finding in result["findings"]
           for key in ("title", "evidence", "suggestion", "impact")):
        raise WorkflowError("Review 问题缺少必要证据或字段，已停止。")
    return result


def skipped_review_ids(state, delivery_round):
    reviews = {entry.get("fix_round", 0): entry for entry in state["reviews"]
               if entry.get("delivery_round", 1) == delivery_round}
    skipped = set()
    for decision in state["decisions"]:
        if decision.get("delivery_round", 1) != delivery_round:
            continue
        review = reviews.get(decision["round"] - 1)
        if review:
            skipped.update(finding["id"] for finding in review["result"]["findings"]
                           if finding["id"] not in decision["fix_ids"])
    return skipped


class Workflow(CompileFlow):
    def __init__(self, settings, emit, store=None, zentao=None, zentao_lock=None, settings_lock=None):
        self.settings = settings
        self.emit = emit
        self.store = store or SessionStore()
        self.zentao = zentao or ZentaoClient(settings)
        self.zentao_lock = zentao_lock or RLock()
        self.settings_lock = settings_lock or RLock()
        self.rpc = None
        self.state = None
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.control_executor = ThreadPoolExecutor(max_workers=1)
        self.output = {}
        self.plan_stream = ""
        self.completed_plan = ""
        self.pending_turns = set()
        self.last_message = ""
        self.connection_epoch = 0
        self.closed = False
        self.job_lock = Lock()
        self.jobs = 0
        self.turn_running = False
        self.compile_runner = None

    def working(self):
        with self.job_lock:
            return self.jobs > 0 or self.turn_running

    def submit(self, action, *args, affects_session=True, control=False):
        with self.job_lock:
            if self.closed:
                raise WorkflowError("会话已经关闭。")
            self.jobs += 1
        def work():
            if self.closed:
                return
            was_active = bool(self.state and self.state["stage"] in ACTIVE)
            try:
                action(*args)
            except Exception as error:
                with self.lock:
                    if self.state and affects_session:
                        self.state["error"] = str(error)
                        if self.state["stage"] in ACTIVE and (not was_active or getattr(action, "__name__", "") == "_completed"):
                            self.state["stage"] = "已停止"
                        self.save()
                self.emit("error", str(error))
        try:
            future = (self.control_executor if control else self.executor).submit(work)
        except RuntimeError:
            with self.job_lock:
                self.jobs -= 1
            raise
        def finished(_):
            with self.job_lock:
                self.jobs -= 1
            self.emit("activity", None)
        future.add_done_callback(finished)
        return future

    def save(self):
        if self.state:
            self.store.save(self.state)
            self.emit("state", loads(dumps(self.state, ensure_ascii=False)))

    def note(self, role, text, **metadata):
        self.store.append(self.state, role, text, **metadata)
        self.emit("message", {"role": role, "text": text})

    def current_reviews(self):
        return [entry for entry in self.state["reviews"]
                if entry.get("delivery_round", 1) == self.state.get("delivery_round", 1)]

    def validate_followup(self, text, image_ids):
        state = self.require_state(*COMPLETE)
        if state["phase"] != "complete" or not state.get("thread_id"):
            raise WorkflowError("原 Codex 线程不存在，不能继续此会话。")
        if (not isinstance(text, str) or not isinstance(image_ids, list)
                or any(not isinstance(value, str) for value in image_ids)
                or len(set(image_ids)) != len(image_ids)):
            raise WorkflowError("请提供反馈文字及不重复的截图编号。")
        available = {image["id"]: image for image in state.get("followup_images", []) if "history_index" not in image}
        if any(value not in available for value in image_ids):
            raise WorkflowError("截图不属于当前待发送反馈，不能使用其他会话或已发送图片。")
        if not text.strip() and not image_ids:
            raise WorkflowError("请填写消息或添加至少一张图片。")
        if not state["repos"]:
            raise WorkflowError("原会话没有已确定的工作区，不能继续对话。")
        return [deepcopy(available[value]) for value in image_ids]

    def followup(self, text, images, request_id, review_enabled):
        state = self.state
        number = state.get("delivery_round", 1)
        archived = {key: deepcopy(state.get(key)) for key in
                    ("repos", "selections", "plan", "plan_hash", "approved_plan", "approved_hash",
                     "approval_source", "plan_round", "fix_round", "max_fix_rounds", "commits",
                     "acceptance", "review_manifest", "review_prompt", "review_prompt_hash", "followup", "review_enabled", "stage",
                     "uncommitted_manifest", "review_requested", "commit_message", "delivery_request")}
        archived.update(number=number, completed_at=state["updated_at"], reviews=deepcopy(self.current_reviews()),
                        decisions=deepcopy([entry for entry in state["decisions"] if entry.get("delivery_round", 1) == number]))
        state.setdefault("deliveries", []).append(archived)
        targets = [{"name": repo["name"], "role": repo["role"], "path": repo["source"],
                    "branch": repo["branch"], "base": "refs/heads/" + repo["branch"], "create": False}
                   for repo in state["repos"]]
        history_index = len(state["history"])
        image_ids = {entry["id"] for entry in images}
        for image in state.get("followup_images", []):
            if image["id"] in image_ids:
                image["history_index"] = history_index
        state.update(delivery_round=number + 1, repos=[], selections=targets, phase="followup", stage="准备工作区中",
                     prepare_complete=False, pause_requested=False, turn_id=None, pending=None, commits={},
                     fix_round=0, max_fix_rounds=self.settings["review"]["max_fix_rounds"], error="",
                     review_manifest={}, review_prompt="", review_prompt_hash="", acceptance=None,
                     uncommitted_manifest={}, review_requested=False, commit_message="", delivery_request=None,
                     review_enabled=review_enabled, approval_source="followup", followup={"text": text.strip(), "images": images,
                         "request_id": request_id, "history_index": history_index,
                         "authorization_hash": sha256(dumps([text.strip(), [image["id"] for image in images]],
                                                            ensure_ascii=False).encode("utf-8")).hexdigest()})
        self.note("用户", text.strip(), images=images, request_id=request_id, review_enabled=review_enabled)
        self._prepare_followup()

    def _prepare_followup(self):
        state = self.state
        state.update(stage="准备工作区中", pause_requested=False)
        self.save()
        self._close_rpc()
        workspace = self.store.directory(state["id"]) / "workspace"
        workspace.mkdir(exist_ok=True)
        prepared = {repo["name"] for repo in state["repos"]}
        for index, selection in enumerate(state["selections"]):
            if self.closed or state.get("pause_requested"):
                state["stage"] = "已暂停"
                self.save()
                return
            if selection["name"] not in prepared:
                acquire_workspace(selection, workspace / f"repo-{index + 1}-delivery-{state['delivery_round']}", self.store, state)
        state["prepare_complete"] = True
        self.save()
        self._start_turn("先读取 workflow.task_context 和当前代码，按 followup 中本轮消息的实际意图处理。"
                         "获取信息、解释或查询时只读取资料并回答；用户明确要求修改时，才授权对应范围的修改。"
                         "原计划用于理解业务背景。需要澄清时等待明确回答，扩大范围时先重新计划。"
                         "本轮首次修改不占用 Review 后修复次数。Review 是否执行由控制器根据 review_enabled 和代码差异决定，"
                         "用户明确要求重新 Review、提交或重新提交时，先调用 workflow.request_review 登记请求，"
                         "引用用户原话；需纳入的历史任务文件从 task_context.previous_task_changes 选择，"
                         "先核对当前差异与本次任务范围；范围明确时直接登记，不因 unchanged 为 false 重复要求确认提交。"
                         "提交标题按用户要求概括并传入 commit_message，未指定则传空字符串。"
                         "此显式请求不受修改后自动 Review 开关限制。模型不要自行审查或提交。\n本轮消息：\n" + state["followup"]["text"])

    def _apply_review_request(self):
        state = self.state
        path = self.store.directory(state["id"]) / f"review-request-{state['delivery_round']}.json"
        if not path.exists():
            return
        request = loads(path.read_text(encoding="utf-8"))
        if (request.get("request_id") != state["followup"]["request_id"]
                or request.get("authorization_hash") != state["followup"]["authorization_hash"]):
            raise WorkflowError("审查提交请求不属于本轮用户消息，已停止。")
        if state.get("delivery_request") == request:
            return
        current = validate_review_request(state, request)
        if current["included_manifest"] != request["included_manifest"]:
            raise WorkflowError("审查提交请求登记后文件内容发生变化，请重新核对范围。")
        for repository in state["repos"]:
            included = {entry["path"] for entry in request["include_paths"] if entry["repository"] == repository["name"]}
            repository["preserved_paths"] = [value for value in repository.get("preserved_paths", []) if value not in included]
            repository["adopted_paths"] = sorted(included)
        state.update(review_requested=True, commit_message=request["commit_message"], delivery_request=request)
        self.save()
        self.note("控制器", "已接收本轮明确的审查提交请求，将重新 Review，通过后本地提交。")

    def connect(self):
        with self.zentao_lock:
            profile = self.zentao.login()
        self.emit("connected", profile)
        self.refresh()

    def refresh(self):
        items = []
        with self.zentao_lock:
            for kind in ("story", "task", "bug"):
                items.extend(self.zentao.list_items(kind))
        self.emit("items", items)

    def collect(self, items):
        if self.state and not self.editable():
            raise WorkflowError("开始计划后不能改变工作项，请新建会话。")
        if not self.state:
            self.state = self.store.new(items[0])
        self.state.update(item=items[0], items=items, stage="资料读取中", error="")
        self.state.pop("snapshot", None)
        self.save()
        try:
            with self.zentao_lock:
                self.state["snapshot"] = self.zentao.collect_many(items, self.store.directory(self.state["id"]))
            self.state["project"] = self.state["snapshot"]["project"]
            if "project_override" in self.state:
                assign_project(self.state, self.state["project_override"])
            self.state["stage"] = "资料已读取"
            self.save()
            self.note("系统", f"已读取 {len(items)} 个工作项的资料。补充必要说明后开始计划，将自动确定前后端分支；第一版不读取飞书。")
        except Exception:
            self.state["stage"] = "资料读取失败"
            self.save()
            raise

    def editable(self):
        state = self.state
        return bool(state and not project_locked(state))

    def start_plan(self, supplement, manual=False):
        state = self.require_state("资料已读取", "准备工作区失败", "等待选择分支")
        if not state.get("snapshot"):
            raise WorkflowError("需求资料尚未完整读取。")
        if "plan_started_at" not in state:
            state["plan_started_at"] = timestamp()
            self.save()
        if state["repos"]:
            if manual:
                raise WorkflowError("工作区已经创建，不能改变当前会话分支。")
            self.prepare_and_plan(state["selections"], supplement)
            return
        self._close_rpc()
        state.update(phase="branch", thread_id=None, turn_id=None, supplement=supplement.strip(),
                     pause_requested=False, error="", branch_resolution={"repositories": [], "manual": manual})
        if "project" not in state:
            state["project"] = project_context(state["item"], state["snapshot"]["sources"][0]["raw"])
        self._resolve_branches()

    def confirm_project(self, project_id, name):
        state = self.require_state("等待确认项目")
        candidates = state["project"]["candidates"]
        if candidates and project_id not in {entry["id"] for entry in candidates}:
            raise WorkflowError("请从工作项关联的项目中明确选择。")
        state["project"] = {"id": project_id, "name": name, "candidates": candidates, "confirmed": True}
        self.note("用户", f"确认所属项目：{name}（#{project_id}）")
        self._resolve_branches()

    def _resolve_branches(self):
        state = self.state
        project = state["project"]
        if not project["id"] or not project["name"]:
            state["stage"] = "等待确认项目"
            self.save()
            return
        state["stage"] = "识别分支中"
        resolution = state["branch_resolution"]
        if not resolution["repositories"]:
            with self.settings_lock:
                preferred = dict(saved_branches(self.settings, project))
            resolution["repositories"] = [{**repository, "catalog": branch_catalog(repository),
                "status": "needs_user" if resolution["manual"] else "pending", "branch": "",
                "reason": "请指定项目常用分支。" if resolution["manual"] else "", "question": "",
                "candidates": [], "source": "", "refreshed": False, "refresh_state": "",
                "preferred": preferred.get(repository["role"], "")}
                for repository in configured_repositories(self.settings)]
            self.save()
        for repository in resolution["repositories"]:
            if state.get("pause_requested") or self.closed:
                state["stage"] = "已暂停"
                self.save()
                return
            name = repository["preferred"]
            if repository["status"] != "pending" or not name:
                continue
            if not any(entry["name"] == name for entry in repository["catalog"]) and not repository["refreshed"]:
                self._refresh_branches(repository)
            if any(entry["name"] == name for entry in repository["catalog"]):
                self._select_branch(repository, name, "common", "使用人工保存的项目常用分支。")
            else:
                repository.update(status="needs_user", reason=f"项目常用分支 {name} 已不可用。",
                                  question="请重新选择项目常用分支。")
        self._advance_branches()

    def _refresh_branches(self, repository):
        repository["refresh_state"] = "running"
        self.state["stage"] = "刷新分支中"
        self.save()
        try:
            repository["catalog"] = refresh_branches(repository)
        except WorkflowError:
            repository["refresh_state"] = "failed"
            self.save()
            raise
        repository.update(refreshed=True, refresh_state="done")
        self.state["stage"] = "已暂停" if self.state.get("pause_requested") else "识别分支中"
        self.save()

    def _select_branch(self, repository, name, source, reason):
        entry = next((entry for entry in repository["catalog"] if entry["name"] == name), None)
        if entry and not entry["local"] and not repository["refreshed"]:
            self._refresh_branches(repository)
            entry = next((entry for entry in repository["catalog"] if entry["name"] == name), None)
        if entry is None:
            raise WorkflowError(f"分支 {name} 已不可用，请重新读取并选择。")
        repository.update(status="resolved", branch=name, source=source, reason=reason, question="")

    def _advance_branches(self):
        state = self.state
        if state.get("pause_requested") or self.closed:
            state["stage"] = "已暂停"
            self.save()
            return
        repositories = state["branch_resolution"]["repositories"]
        for repository in repositories:
            if not repository["catalog"] and not repository["refreshed"]:
                self._refresh_branches(repository)
            if not repository["catalog"]:
                repository.update(status="needs_user", reason="远程刷新后仍没有可用分支。",
                                  question="请先在该仓库准备对应项目分支，再重新识别。")
        if state.get("pause_requested"):
            state["stage"] = "已暂停"
            self.save()
        elif any(entry["status"] == "pending" for entry in repositories):
            self._start_turn("请判断上下文中待识别仓库的分支，严格返回结构化结果。", BRANCH_SCHEMA)
        elif any(entry["status"] == "needs_user" for entry in repositories):
            state["stage"] = "等待选择分支"
            self.save()
        else:
            selections = []
            for repository in repositories:
                branch = next(entry for entry in repository["catalog"] if entry["name"] == repository["branch"])
                base = ("refs/heads/" if branch["local"] else f"refs/remotes/{repository['remote']}/") + branch["name"]
                selections.append({"name": repository["name"], "path": repository["path"],
                    "role": repository["role"], "branch": branch["name"], "base": base,
                    "create": not branch["local"], "track": not branch["local"]})
            self._close_rpc()
            state.update(thread_id=None, turn_id=None, phase="plan", stage="资料已读取")
            self.prepare_and_plan(selections, state["supplement"])

    def choose_branches(self, choices):
        state = self.require_state("等待选择分支")
        repositories = state["branch_resolution"]["repositories"]
        expected = {entry["role"] for entry in repositories if entry["status"] == "needs_user"}
        if set(choices) != expected or any(not isinstance(name, str) or not name.strip() for name in choices.values()):
            raise WorkflowError("请明确选择所有待确定仓库的分支。")
        if any(choices[entry["role"]] not in {branch["name"] for branch in entry["catalog"]}
               for entry in repositories if entry["role"] in choices):
            raise WorkflowError("所选分支不在当前仓库候选中，请重新读取。")
        for repository in repositories:
            if repository["role"] in choices:
                self._select_branch(repository, choices[repository["role"]], "human", "人工确认并保存为项目常用分支。")
        project = state["project"]
        site = self.settings["zentao"]["base_url"].rstrip("/")
        with self.settings_lock:
            mapping = self.settings.setdefault("project_branches", {}).setdefault(site, {})
            saved = mapping.setdefault(str(project["id"]), {"name": project["name"], "branches": {}})
            saved["name"] = project["name"]
            saved["branches"].update({entry["role"]: entry["branch"] for entry in repositories if entry["source"] == "human"})
            save_settings(self.settings)
        self.note("用户", "确认项目常用分支：" + "；".join(f"{role}：{name}" for role, name in choices.items()))
        self._advance_branches()

    def _complete_branches(self, text):
        state = self.state
        repositories = state["branch_resolution"]["repositories"]
        results = validate_result(loads(text), repositories)
        for result in results:
            repository = next(entry for entry in repositories if entry["role"] == result["role"])
            repository.update(reason=result["reason"], candidates=result["candidates"], question=result["question"])
            if result["status"] == "selected":
                branch = next(entry for entry in repository["catalog"] if entry["name"] == result["branch"])
                if not branch["local"] and not repository["refreshed"]:
                    self._refresh_branches(repository)
                else:
                    self._select_branch(repository, branch["name"], "ai", result["reason"])
            elif result["status"] == "not_found" and not repository["refreshed"]:
                self._refresh_branches(repository)
            else:
                repository["status"] = "needs_user"
        self.save()
        self._advance_branches()

    def prepare_and_plan(self, selections, supplement):
        state = self.require_state("资料已读取", "准备工作区失败")
        if not selections:
            raise WorkflowError("尚未确定代码仓库及分支。")
        if not state.get("snapshot"):
            raise WorkflowError("需求资料尚未完整读取。")
        state["supplement"] = supplement.strip()
        state["selections"] = selections
        state["prepare_complete"] = False
        state["pause_requested"] = False
        state["stage"] = "准备工作区中"
        self.save()
        state["max_fix_rounds"] = self.settings["review"]["max_fix_rounds"]
        workspace = self.store.directory(state["id"]) / "workspace"
        workspace.mkdir(exist_ok=True)
        existing = {repository["name"]: repository for repository in state["repos"]}
        for index, selection in enumerate(selections):
            if self.closed or state.get("pause_requested"):
                state["stage"] = "已暂停"
                self.save()
                return
            if selection["name"] in existing:
                old = existing[selection["name"]]
                if old["branch"] != selection["branch"] or old["base"] != selection["base"]:
                    raise WorkflowError("已创建的工作区选择发生变化，请新建工作流处理，不会自动删除旧工作区。")
                continue
            try:
                acquire_workspace(selection, workspace / f"repo-{index + 1}", self.store, state)
            except Exception:
                state["stage"] = "准备工作区失败"
                self.save()
                raise
            self.save()
        if set(existing) - {selection["name"] for selection in selections}:
            raise WorkflowError("不能移除已创建的工作区，请保留原选择。")
        state["prepare_complete"] = True
        self._new_plan_round()
        self._start_turn("请以原生计划模式分析本次需求。先调用 workflow.task_context，阅读选定代码。资料不足或问题影响方案时必须提问并等待。"
                         "只生成计划，不修改业务文件。最终使用 <proposed_plan>...</proposed_plan> 输出中文计划；没有该计划块不能进入实施。")

    def _new_plan_round(self):
        state = self.state
        previous = state.get("plan_round") or {}
        state.update(phase="plan", plan="", plan_hash="", approved_plan="", approved_hash="", approval_source="",
                     plan_round={"number": previous.get("number", 0) + 1, "turns": 0, "asked": False})

    def require_state(self, *stages):
        if not self.state or self.state["stage"] not in stages:
            raise WorkflowError("当前阶段不允许该操作。")
        return self.state

    def ensure_rpc(self):
        if not self.state.get("codex"):
            raise WorkflowError("该历史会话尚未保存模型配置，请先选择模型和推理强度。")
        if self.state.get("followup") and not self.state.get("thread_id"):
            raise WorkflowError("原 Codex 线程不存在，不能新建线程替代续聊。")
        if self.closed:
            raise WorkflowError("客户端已经关闭，不能启动新会话。")
        if self.rpc is not None and self.rpc.process.poll() is not None:
            self._close_rpc()
        if self.rpc is None:
            self.connection_epoch += 1
            epoch = self.connection_epoch
            self.rpc = AppServer(self.settings, lambda message: self._event(message, epoch))
            try:
                self.rpc.start(self.state["codex"]["model"])
            except Exception:
                self.rpc.close()
                self.rpc = None
                raise
            if self.state["thread_id"]:
                params = thread_parameters(self.state, self.rpc.model, self.rpc.disabled_mcp)
                params.update(threadId=self.state["thread_id"], excludeTurns=True)
                try:
                    self.rpc.request("thread/resume", params)
                except Exception:
                    self._close_rpc()
                    raise
            else:
                data = self.rpc.request("thread/start", thread_parameters(self.state, self.rpc.model, self.rpc.disabled_mcp))
                self.state["thread_id"] = data["thread"]["id"]
            self.state["rpc_connected"] = True
            self.save()

    def _start_turn(self, text, output_schema=None):
        if self.closed:
            return
        state = self.state
        if pending_input(self.store, state):
            raise WorkflowError("仍有问题等待明确答复，不能启动新的模型回合。")
        phase = state["phase"]
        if phase == "review":
            reviews = self.current_reviews()
            previous_ids = [finding["id"] for finding in reviews[-1]["result"]["findings"]] if reviews else []
            output_schema = deepcopy(FINDING_SCHEMA)
            output_schema["properties"]["findings"]["items"]["properties"]["previous_id"]["enum"] = ["", *previous_ids]
            resolved_schema = output_schema["properties"]["resolved_previous"]
            if previous_ids:
                resolved_schema["items"]["properties"]["id"]["enum"] = previous_ids
            else:
                resolved_schema["maxItems"] = 0
            text += (f"\n本次是第 {state.get('delivery_round', 1)} 轮交付，允许引用的旧问题编号：" + dumps(previous_ids, ensure_ascii=False) +
                     "。previous_id 只能填写上述编号或空字符串；没有可引用编号时，所有问题的 previous_id 必须为空，resolved_previous 必须为空数组。"
                     "此前其他交付轮次的问题及接受记录仅作背景，不能沿用其编号或自动列为本轮遗留问题。"
                     "本轮重新发现的问题按当前差异独立判断，origin 为原有问题不代表可以引用历史编号。")
            text += ("\n本轮已选择不修复的问题编号：" + dumps(sorted(skipped_review_ids(state, state.get("delivery_round", 1)))) +
                     "。这些问题保留原编号和记录，不修改、不要求再次选择或填写原因，也不作为待修复问题；不要另建编号重复报告同一问题。")
        for repository in state["repos"]:
            ensure_baseline(repository)
        if state.get("pause_requested"):
            state["stage"] = "已暂停"
            self.save()
            return
        state["stage"] = PHASE_STAGE[phase]
        self.save()
        self.ensure_rpc()
        if self.closed or state.get("pause_requested"):
            state["stage"] = "已暂停"
            self.save()
            return
        state["error"] = ""
        state["pending"] = None
        state["turn_id"] = None
        self.output = {}
        self.plan_stream = ""
        self.completed_plan = ""
        self.last_message = ""
        if phase == "plan" and state.get("plan_round") is not None:
            state["plan_round"]["turns"] += 1
        self.save()
        self.note("控制器", text)
        mode = "plan" if phase == "plan" else "default"
        session_path = self.store.directory(state["id"])
        turn_cwd = session_path if phase == "branch" else session_path / "workspace"
        context = {"items": state.get("items", [state["item"]]), "supplement": state["supplement"],
                   "supplement_images": state.get("supplement_images", []), "plan": state.get("approved_plan"),
                   "phase": phase, "repositories": state["repos"], "blockers": state["snapshot"]["blockers"],
                   "feishu_links": state["snapshot"]["feishu_links"],
                   "decisions": [entry for entry in state["decisions"] if entry.get("delivery_round", 1) == state.get("delivery_round", 1)],
                   "delivery_round": state.get("delivery_round", 1), "followup": state.get("followup"),
                   "review_enabled": state.get("review_enabled", True),
                   "compile_check": state.get("compile_check", {"enabled": False}),
                   "compile_checks": compile_context(state),
                   "materials_revision": context_hash(requirement_context(state))}
        scope = ("原需求资料与历史计划用于理解业务背景，本轮只处理 followup 中的消息。"
                 "获取信息时只读取并回答；明确要求修改时才处理对应修改。是否自动 Review 由控制器决定。"
                 if state.get("followup") else
                 "本次未关联禅道工作项，以 supplement 中的首条消息和 supplement_images 为需求资料，按当前阶段和控制器授权处理。"
                 if state["item"] is None else
                 "本次选中的所有工作项属于同一次交付，须逐项分析并统一规划前后端实现，不得遗漏任务。")
        previous_input = state.get("thread_input", {})
        if previous_input.get("thread_id") != state["thread_id"]:
            previous_input = {}
        image_hashes = set(previous_input.get("images", []))
        image_inputs = []
        if phase != "branch":
            images = [material for material in state["snapshot"]["materials"] if material.get("image")]
            images += state.get("supplement_images", [])
            images += (state.get("followup") or {}).get("images", [])
            references = []
            for image in images:
                digest = sha256(Path(image["path"]).read_bytes()).hexdigest()
                references.append({"path": image["path"], "content_hash": digest})
                if digest not in image_hashes:
                    image_inputs.append({"type": "localImage", "path": image["path"]})
                    image_hashes.add(digest)
            context["image_references"] = references
        hashes = {key: context_hash(value) for key, value in context.items()}
        changed = {key: value for key, value in context.items()
                   if hashes[key] != previous_input.get("context_hashes", {}).get(key)}
        material_instruction = ("资料首次读取或版本已变更，先调用 workflow.task_context(include_materials=true)。"
                                if "materials_revision" in changed else
                                "先调用 workflow.task_context 获取当前授权；原需求资料沿用本线程，需核对时再设置 include_materials=true。")
        prompt = ("当前阶段：" + phase + "。" + scope + "\n" + text + "\n" + material_instruction +
                  "需要人工选择或确认时调用 workflow.ask_user，立即结束回合，不得 sleep 或轮询。"
                  "以下只包含变化字段，未出现的字段沿用本线程上次值。"
                  "\n工作流上下文：\n" + dumps(changed, ensure_ascii=False))
        if phase == "branch":
            context = {"project": state["project"], "items": state.get("items", [state["item"]]), "supplement": state["supplement"],
                       "sources": [{"title": source["title"], "content": source["content"]}
                                   for source in state["snapshot"]["sources"]],
                       "repositories": [{"role": entry["role"], "refreshed": entry["refreshed"], "branches": entry["catalog"]}
                                        for entry in state["branch_resolution"]["repositories"] if entry["status"] == "pending"]}
            inputs = [{"type": "text", "text": BRANCH_INSTRUCTIONS + "\n" + dumps(context, ensure_ascii=False)}]
        else:
            # 技能已由 thread/start 或 thread/resume 的 developerInstructions 提供。
            inputs = [{"type": "text", "text": prompt}, *image_inputs]
        params = {"threadId": state["thread_id"], "input": inputs,
                  "cwd": str(turn_cwd), "approvalPolicy": "on-request",
                  "sandboxPolicy": {"type": "readOnly"},
                  "collaborationMode": {"mode": mode, "settings": {"model": state["codex"]["model"],
                      "reasoning_effort": state["codex"]["reasoning_effort"], "developer_instructions": None}}}
        if output_schema:
            params["outputSchema"] = output_schema
        self.turn_running = True
        try:
            response = self.rpc.request("turn/start", params)
        except Exception:
            self.turn_running = False
            raise
        with self.lock:
            state["turn_id"] = response["turn"]["id"]
            if phase != "branch":
                state["thread_input"] = {"thread_id": state["thread_id"], "context_hashes": hashes,
                                         "images": sorted(image_hashes)}
            self.save()

    def approve_plan(self):
        self._implement_plan("manual")

    def _implement_plan(self, source):
        state = self.require_state("等待计划确认")
        if not state["plan"] or sha256(state["plan"].encode("utf-8")).hexdigest() != state["plan_hash"]:
            raise WorkflowError("计划内容与待确认版本不一致。")
        if state["snapshot"]["blockers"] and not state["supplement"].strip() and not state.get("supplement_images"):
            if source == "auto":
                return
            raise WorkflowError("存在明确的资料缺失，请先补充必要内容，再确认更新后的计划。")
        if self.closed or state.get("pause_requested"):
            state["stage"] = "已暂停"
            self.save()
            return
        state["approved_plan"] = state["plan"]
        state["approved_hash"] = state["plan_hash"]
        state["approval_source"] = source
        self.note("控制器" if source == "auto" else "用户",
                  ("本轮计划未提问且一次完整生成，自动授权实施，计划版本：" if source == "auto" else "人工确认实施，计划版本：") + state["approved_hash"])
        state["phase"] = "implement"
        self.save()
        self._start_turn("控制器已授权实施上下文中的计划，授权来源见 workflow.task_context。仅实现该计划的最小范围，先阅读已有代码与项目规范。"
                         "先调用 workflow.task_context 确认当前阶段，再使用 workflow.write_file 或 replace_text 修改。"
                         "原生只读沙箱限制原生命令与文件写入，控制器单独授权的 MCP 写入工具在实施阶段可用；不得据此跳过已经授权的 MCP 修改。"
                         "出现需澄清事项必须调用 workflow.ask_user 后立即结束回合，由控制器等待明确答复。"
                         "完成后说明修改与静态检查，不执行任何验证命令，也不要提交。")

    def _capture_user_input(self):
        with self.lock:
            request = pending_input(self.store, self.state)
            if not request:
                return False
            if (self.state.get("pending") or {}).get("id") != request["id"]:
                self.state.update(stage="等待人工回答", pending={"id": request["id"], "method": "workflow/ask_user",
                                  "params": {"questions": request["questions"]}, "turn_completed": False})
                if self.state["phase"] == "plan" and self.state.get("plan_round") is not None:
                    self.state["plan_round"]["asked"] = True
                self.note("Codex", "需要你的明确答复：\n" + "\n".join(question["question"] for question in request["questions"]))
                self.save()
                self.emit("questions", request["questions"])
            return True

    def _wait_for_user_input(self, session_id, epoch):
        with self.lock:
            if self.closed or self.state["id"] != session_id or epoch != self.connection_epoch:
                return
            if self.state.get("pause_requested") or not self._capture_user_input():
                return
            pending = self.state["pending"]
            interrupt = self.turn_running and not pending.get("turn_completed") and not pending.get("interrupt_requested")
            if interrupt:
                pending["interrupt_requested"] = True
                self.save()
                thread_id, turn_id = self.state["thread_id"], self.state["turn_id"]
        if interrupt:
            self.rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def answer(self, text, answers=None):
        state = self.require_state("等待人工回答", "等待计划补充", "等待计划确认")
        if not text.strip() and not answers:
            raise WorkflowError("请明确回答，不能以空内容继续。")
        pending = state.get("pending")
        controlled = pending and pending["method"] == "workflow/ask_user"
        if controlled:
            if not pending.get("turn_completed"):
                raise WorkflowError("控制器正在结束当前回合，请稍后提交回答。")
            questions = pending["params"]["questions"]
            if answers is not None:
                if (not isinstance(answers, dict) or set(answers) != {question["id"] for question in questions}
                        or any(not isinstance(values, list) or not values
                               or any(not isinstance(value, str) or not value.strip() for value in values)
                               for values in answers.values())):
                    raise WorkflowError("请逐项明确回答所有问题，不能使用空答案。")
            elif len(questions) != 1:
                raise WorkflowError("多个问题必须逐项明确回答。")
        reply = text.strip() or dumps(answers, ensure_ascii=False)
        if controlled and answers is not None:
            response = "\n\n".join(question["question"] + "\n回答：" + "\n".join(answers[question["id"]])
                                   for question in questions)
            reply = "\n\n".join(part for part in (response, text.strip()) if part)
        self.note("用户", reply)
        state["supplement"] += "\n\n人工补充：" + reply
        if pending and state["rpc_connected"] and self.rpc and not pending.get("turn_completed"):
            if pending["method"] != "item/tool/requestUserInput":
                raise WorkflowError("不支持对该请求自动补充答案。")
            result = {"answers": {key: {"answers": value} for key, value in (answers or {}).items()}}
            if not result["answers"]:
                questions = pending["params"].get("questions", [])
                if len(questions) != 1:
                    raise WorkflowError("多个问题必须逐项回答。")
                result["answers"] = {questions[0]["id"]: {"answers": [text]}}
            state["pending"] = None
            state["stage"] = PHASE_STAGE[state["phase"]]
            self.save()
            self.rpc.respond(pending["id"], result)
        else:
            if controlled:
                input_request_path(self.store, state).unlink()
            state["pending"] = None
            if state["stage"] == "等待计划确认":
                self._new_plan_round()
            self._start_turn("用户已补充信息：\n" + reply + "\n请从上次暂停点继续；若在计划阶段，输出最终 proposed_plan，由控制器判断是否需要人工确认。",
                             FINDING_SCHEMA if state["phase"] == "review" else None)

    def _begin_review(self):
        state = self.state
        self._guard_compile()
        state["review_manifest"] = manifest(state["repos"])
        state["phase"] = "review"
        prompt = (ROOT / self.settings["review"]["prompt_file"]).read_text(encoding="utf-8").strip()
        if state.get("review_prompt") and state["review_prompt"] != prompt:
            raise WorkflowError("固定 Review 提示词在当前流程中发生变化，已停止，不自动切换版本。")
        if not prompt:
            raise WorkflowError("固定 Review 提示词不能为空。")
        state["review_prompt"] = prompt
        state["review_prompt_hash"] = sha256(prompt.encode("utf-8")).hexdigest()
        reviews = self.current_reviews()
        previous = reviews[-1]["result"] if reviews else None
        self._start_turn(prompt + "\n\n以上固定提示词必须原样遵循。当前只读审查，不得修改。"
                         "先用 workflow.changes 阅读每个仓库的累计差异，再结合 baseline_file 和当前文件确定问题来源。"
                         "审查新文件，不遗漏上轮修复。无法确认则标为证据不足，不声称业务运行已经通过。"
                         "仅输出符合给定 Schema 的 JSON。每个问题必须给出具体需求、代码证据、处理建议和影响。"
                         "已有问题仍存在时填写 previous_id，新问题填空字符串；上轮问题确已解决时在 resolved_previous 中给出原编号及具体代码证据。"
                         "未提及也未明确说明解决依据的旧问题会继续保留，不能以遗漏方式将其视为通过。"
                         "编译检查仅覆盖后端，结果见上下文 compile_checks；前端仍须审查，跳过编译不能视为编译通过。"
                         "\n上轮审查：" + dumps(previous, ensure_ascii=False), FINDING_SCHEMA)

    def fix(self, selected_ids, note):
        state = self.require_state("等待修复选择")
        if state["fix_round"] >= state["max_fix_rounds"]:
            raise WorkflowError("已达到修复轮次上限；请明确追加一轮后再修复。")
        current = self.current_reviews()[-1]["result"]["findings"]
        skipped = skipped_review_ids(state, state.get("delivery_round", 1))
        lookup = {finding["id"]: finding for finding in current if finding["id"] not in skipped}
        if not selected_ids or any(item not in lookup for item in selected_ids):
            raise WorkflowError("请至少选择一个有效问题。")
        if manifest(state["repos"]) != state["review_manifest"]:
            raise WorkflowError("代码已经变化，须先重新 Review。")
        chosen = [lookup[item] for item in selected_ids]
        state["decisions"].append({"round": state["fix_round"] + 1, "fix_ids": selected_ids, "note": note,
                                   "delivery_round": state.get("delivery_round", 1)})
        state["fix_round"] += 1
        state["phase"] = "fix"
        self._start_turn("用户选择修复以下问题，只处理选中项。不要顺手修改未选项或原有问题。"
                         "若超出已确认计划，调用 workflow.ask_user 后立即结束回合，等待用户明确决定。修复完成后由控制器自动再次 Review。\n" +
                         dumps(chosen, ensure_ascii=False) + "\n用户说明：" + note)

    def add_round(self):
        state = self.require_state("等待修复选择")
        state["max_fix_rounds"] += 1
        self.note("用户", "明确追加一轮修复，当前上限：" + str(state["max_fix_rounds"]))
        self.save()

    def rereview(self):
        self.require_state("等待修复选择", "已停止", "已暂停")
        if self.state.get("followup") and not self.state.get("prepare_complete"):
            raise WorkflowError("请先从暂停点继续准备本轮工作区，再进行审查。")
        if self.state["commits"]:
            raise WorkflowError("已经发生部分提交，请先处理提交状态。")
        self.state["pause_requested"] = False
        self.state["review_requested"] = True
        self._before_review()

    def finalize(self, accepted_ids, acceptance_note, automatic=False):
        state = self.require_state("等待修复选择", "部分提交")
        if self.closed or (automatic and state.get("pause_requested")):
            return
        if not state["commits"]:
            self._guard_compile()
        findings = self.current_reviews()[-1]["result"]["findings"]
        skipped = skipped_review_ids(state, state.get("delivery_round", 1))
        if set(accepted_ids) != {finding["id"] for finding in findings if finding["id"] not in skipped}:
            raise WorkflowError("提交范围与当前待处理问题不一致，请刷新后重试。")
        if not automatic:
            state["pause_requested"] = False
        for repository in state["repos"]:
            previous = state["commits"].get(repository["name"])
            if previous == "无代码修改":
                if manifest([repository]):
                    raise WorkflowError("已记录无修改的仓库再次发生变化，已停止，不把新修改混入剩余提交。")
            elif previous:
                if git(repository["path"], "rev-parse", "HEAD") != previous or working_paths(repository):
                    raise WorkflowError("已提交仓库再次发生变化，已停止，不把新修改混入剩余提交。")
        pending = [repo for repo in state["repos"] if repo["name"] not in state["commits"]]
        expected = {key: value for key, value in state["review_manifest"].items()
                    if any(key.startswith(repo["name"] + "/") for repo in pending)}
        if manifest(pending) != expected:
            raise WorkflowError("最后一次 Review 后代码发生改变，不能提交。")
        state["acceptance"] = {"ids": [finding["id"] for finding in findings], "reason": acceptance_note,
                               "source": "auto" if automatic else "manual"}
        self.save()
        title = session_title(state) if state["item"] is None else str(state["item"]["title"])
        title = title.replace("\n", " ").replace("\r", " ")[:100]
        message = (f"feat: {title}" if state["item"] is None else
                   f"feat(zentao-{state['item']['kind']}-{state['item']['id']}): {title}")
        items = state.get("items", [state["item"]])
        if len(items) > 1:
            ids = ",".join(str(item["id"]) for item in items)
            title = "；".join(str(item["title"]).replace("\n", " ").replace("\r", " ") for item in items)[:100]
            message = f"feat(zentao-tasks-{ids}): {title}"
        if (state["item"] is not None and state["item"]["kind"] == "bug") or state.get("delivery_round", 1) > 1:
            message = message.replace("feat", "fix", 1)
        if state.get("commit_message"):
            message = state["commit_message"]
        for repository in pending:
            if self.closed or state.get("pause_requested"):
                state["stage"] = "部分提交" if state["commits"] else "等待修复选择"
                self.save()
                return
            own = {key: value for key, value in expected.items() if key.startswith(repository["name"] + "/")}
            try:
                result = commit(repository, message, own)
            except Exception:
                state["stage"] = "部分提交" if state["commits"] else "等待修复选择"
                self.save()
                raise
            state["commits"][repository["name"]] = result or "无代码修改"
            self.save()
        state["stage"] = "已完成（静态审查）"
        state["phase"] = "complete"
        self.save()
        self.note("系统", "本地提交已完成。未 push，未修改禅道状态，功能运行与验收未验证。")

    def pause(self):
        state = self.state
        if not state:
            return
        was_active = state["stage"] in ACTIVE | {"等待人工回答"}
        state["pause_requested"] = True
        if self.compile_runner:
            self.compile_runner.stop()
        state["stage"] = "部分提交" if state["commits"] else "已暂停"
        state["last_output"] = self.last_message or "\n\n".join(self.output.values())
        self.save()
        if self.rpc and state.get("turn_id") and was_active and state["phase"] != "compile":
            self.rpc.request("turn/interrupt", {"threadId": state["thread_id"], "turnId": state["turn_id"]})
        state["pending"] = None
        self.save()

    def restore(self, session_id):
        if self.state and self.state["stage"] in ACTIVE | {"等待人工回答"}:
            raise WorkflowError("请先暂停当前会话。")
        self._close_rpc()
        self.state = self.store.load(session_id)
        self.state["rpc_connected"] = False
        if self.state["stage"] in ACTIVE:
            self.state["stage"] = "已暂停"
        self.save()
        self.emit("history", self.state["history"])

    def resume(self):
        state = self.require_state("已暂停", "已停止")
        if state["commits"]:
            raise WorkflowError("存在部分提交，只能先完成或处理该提交过程。")
        state["pause_requested"] = False
        if self._capture_user_input():
            state["pending"]["turn_completed"] = True
            self.save()
            return
        if state["phase"] == "compile":
            self._resume_compile()
            return
        if state["phase"] == "compile_fix":
            batch = self._compile_batch()
            repository = next(repo for repo in state["repos"] if repo["name"] == batch["repair_repository"])
            if repository["role"] != "backend":
                self._compile_repaired()
                return
        if state.get("followup") and not state.get("prepare_complete"):
            self._prepare_followup()
            return
        if state["phase"] == "review":
            # 恢复动作只重新审查；上次结果不能在重启后直接触发提交。
            self._begin_review()
            return
        if state["phase"] == "branch":
            self._resolve_branches()
            return
        if not state.get("prepare_complete") and not state.get("thread_id"):
            if not state.get("selections"):
                state["stage"] = "资料已读取"
                self.start_plan(state["supplement"])
                return
            state["stage"] = "准备工作区失败"
            self.prepare_and_plan(state["selections"], state["supplement"])
            return
        self._start_turn("用户选择恢复。先阅读 workflow.task_context 与当前代码，按原阶段继续。"
                         "不要假定中断前的文件操作已经完成；不得跳过尚未回答的问题和计划确认。",
                         FINDING_SCHEMA if state["phase"] == "review" else None)

    def _event(self, message, epoch):
        if epoch != self.connection_epoch or self.closed:
            return
        method, params = message["method"], message.get("params", {})
        if not self.state:
            return
        if params.get("threadId") and self.state.get("thread_id") and params["threadId"] != self.state["thread_id"]:
            return
        if params.get("turnId") and self.state.get("turn_id") and params["turnId"] != self.state["turn_id"]:
            return
        if method == "item/tool/requestUserInput":
            with self.lock:
                if self.state["phase"] == "plan" and self.state.get("plan_round") is not None:
                    self.state["plan_round"]["asked"] = True
                self.state["pending"] = {"id": message["id"], "method": method, "params": params}
                self.state["stage"] = "等待人工回答"
                self.save()
            self.emit("questions", params.get("questions", []))
            return
        if "id" in message:
            # 命令、权限提升及其他外部动作一律拒绝，不以“自动审批”绕过执行约束。
            if "requestApproval" in method and "commandExecution" in method:
                self.rpc.respond(message["id"], {"decision": "decline"})
            elif "permissions" in method:
                self.rpc.respond(message["id"], {"permissions": {}})
            elif method == "mcpServer/elicitation/request":
                allowed = is_controlled_write(self.state, params)
                self.rpc.respond(message["id"], {"action": "accept" if allowed else "decline", "content": {} if allowed else None})
            elif "fileChange" in method:
                self.rpc.respond(message["id"], {"decision": "decline"})
            else:
                self.rpc._write({"id": message["id"], "error": {"code": -32601, "message": "当前工作流不支持该请求。"}})
            return
        if method == "item/agentMessage/delta":
            key = params.get("itemId", "agent")
            self.output[key] = self.output.get(key, "") + params.get("delta", "")
            self.emit("delta", params.get("delta", ""))
        elif method == "item/plan/delta":
            self.plan_stream += params.get("delta", "")
            self.emit("delta", params.get("delta", ""))
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage" and item.get("text"):
                self.output[item.get("id", "agent")] = item["text"]
                self.last_message = item["text"]
            elif item.get("type") == "plan" and item.get("text"):
                self.completed_plan = item["text"].strip()
            elif item.get("type") == "mcpToolCall" and item.get("server") == "workflow" and item.get("tool") == "ask_user":
                self.submit(self._wait_for_user_input, self.state["id"], epoch, control=True)
        elif method == "turn/started":
            self.turn_running = True
            with self.lock:
                self.state["turn_id"] = params["turn"]["id"]
                self.save()
            if self.state.get("pause_requested"):
                self.submit(self.pause, control=True)
        elif method == "turn/completed":
            turn = params.get("turn", {})
            turn_id = turn.get("id")
            if turn_id and self.state.get("turn_id") and turn_id != self.state["turn_id"]:
                return
            self.turn_running = False
            key = (self.state["id"], turn_id)
            if key not in self.pending_turns:
                self.pending_turns.add(key)
                self.submit(self._completed, turn, self.state["id"], epoch)
        elif method in ("client/error", "client/disconnected"):
            self.turn_running = False
            with self.lock:
                if self.state:
                    self.state["rpc_connected"] = False
                    if self._capture_user_input():
                        self.state["pending"]["turn_completed"] = True
                    elif self.state["stage"] in ACTIVE:
                        self.state["stage"] = "已暂停"
                    self.save()
            if method == "client/error":
                self.emit("error", params["message"])

    def _completed(self, turn, session_id, epoch):
        state = self.state
        if state["id"] != session_id or epoch != self.connection_epoch or self.closed:
            return
        if turn.get("id") and state.get("turn_id") and turn["id"] != state["turn_id"]:
            return
        if state.get("pause_requested"):
            state["stage"] = "已暂停"
            self.save()
            return
        if state["stage"] not in ACTIVE | {"等待人工回答"}:
            return
        self._capture_user_input()
        if (state.get("pending") or {}).get("method") == "workflow/ask_user":
            state["pending"]["turn_completed"] = True
            state["stage"] = "等待人工回答"
            self.save()
            return
        if turn.get("status") != "completed":
            state["stage"] = "已暂停" if turn.get("status") == "interrupted" else "已停止"
            state["error"] = str((turn.get("error") or {}).get("message", ""))
            self.save()
            return
        text = self.last_message.strip()
        if state.get("pending"):
            # 未回答的原生问题不能被回合结束事件隐式清除。
            state["pending"]["turn_completed"] = True
            state["stage"] = "等待人工回答"
            self.save()
            return
        if state["phase"] == "branch":
            self._complete_branches(text)
            return
        if self.completed_plan:
            plan = self.completed_plan
        else:
            match = search(r"<proposed_plan>([\s\S]*?)</proposed_plan>", text)
            plan = match.group(1).strip() if match else ""
        history_index = len(state["history"])
        self.note("Codex", text or self.plan_stream)
        if state["phase"] == "plan":
            state["plan"] = plan
            state["plan_hash"] = sha256(plan.encode("utf-8")).hexdigest() if plan else ""
            state["stage"] = "等待计划确认" if plan else "等待计划补充"
            self.save()
            round_state = state.get("plan_round")
            if plan and round_state is not None and round_state["turns"] == 1 and not round_state["asked"]:
                self._implement_plan("auto")
        elif state["phase"] in ("implement", "fix", "followup", "compile_fix"):
            if "[需要澄清]" in text or "[需要重做计划]" in text:
                if "[需要重做计划]" in text:
                    self._new_plan_round()
                state["stage"] = "等待计划补充"
                self.save()
            else:
                if state["phase"] == "compile_fix":
                    self._compile_repaired()
                else:
                    if state["phase"] == "followup":
                        self._apply_review_request()
                    self._before_review()
        elif state["phase"] == "review":
            if manifest(state["repos"]) != state["review_manifest"]:
                raise WorkflowError("Review 期间代码发生变化，本次结果不作为提交依据。")
            result = parse_review(text)
            review_number = len(state["reviews"]) + 1
            reviews = self.current_reviews()
            previous = {finding["id"]: finding for finding in reviews[-1]["result"]["findings"]} if reviews else {}
            resolved = {entry["id"] for entry in result["resolved_previous"] if entry.get("evidence", "").strip()}
            if not resolved.issubset(previous):
                raise WorkflowError("Review 声称解决了不存在的旧问题，已停止。")
            seen = set()
            for number, finding in enumerate(result["findings"], 1):
                old_id = finding["previous_id"]
                if old_id and old_id not in previous:
                    raise WorkflowError(f"Review 引用了本轮交付中不存在的旧问题 {old_id}，请重新 Review。")
                finding["id"] = old_id or f"R{review_number:02d}-{number:02d}"
                if finding["id"] in seen or finding["id"] in resolved:
                    raise WorkflowError("Review 对同一问题给出了冲突状态。")
                seen.add(finding["id"])
            for old_id, finding in previous.items():
                if old_id not in seen and old_id not in resolved:
                    result["findings"].append(finding.copy())
            state["reviews"].append({"number": review_number, "fix_round": state["fix_round"],
                                     "delivery_round": state.get("delivery_round", 1), "history_index": history_index,
                                     "prompt_hash": state["review_prompt_hash"], "result": result,
                                     "manifest": state["review_manifest"].copy()})
            state["stage"] = "等待修复选择"
            self.save()
            skipped = skipped_review_ids(state, state.get("delivery_round", 1))
            if not any(finding["id"] not in skipped for finding in result["findings"]) and not state.get("pause_requested") and not self.closed:
                self.note("控制器", "Review 无待处理问题，已选择不修复的问题保留记录，自动执行本地提交，不 push。")
                self.finalize([], "", automatic=True)

    def _close_rpc(self):
        self.connection_epoch += 1
        if self.rpc:
            self.rpc.close()
            self.rpc = None
        if self.state:
            self.state["rpc_connected"] = False
        self.turn_running = False

    def close(self):
        self.closed = True
        if self.compile_runner:
            self.compile_runner.stop()
        try:
            self._close_rpc()
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.control_executor.shutdown(wait=True, cancel_futures=True)
            if self.state:
                self.state["rpc_connected"] = False
                if self.state["stage"] in ACTIVE | {"等待人工回答", "资料读取中"}:
                    self.state["stage"] = "已暂停"
                    self.state["pending"] = None
                    self.state["pause_requested"] = True
                self.store.save(self.state)
