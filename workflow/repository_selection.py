"""检测候选仓库的当前分支，按目标分支匹配会话目录。"""

from pathlib import Path

from workflow.branching import configured_repositories
from workflow.gitops import branch_catalog, checkout_snapshot, git
from workflow.workspace_idle import IDLE_SCHEMA, idle_evidence, validate_idle
from workflow.settings import WorkflowError
from workflow.store import timestamp


def candidate_paths(repository, extra=()):
    values = repository.get("candidate_paths", [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise WorkflowError("candidate_paths 必须是仓库绝对目录组成的数组。")
    result = {}
    for value in [repository["path"], *values, *extra]:
        if not isinstance(value, str) or not value.strip() or not Path(value.strip()).is_absolute():
            raise WorkflowError("请填写有效的仓库绝对目录。")
        path = str(Path(value.strip()).resolve())
        result.setdefault(path.casefold(), path)
    return list(result.values())


def repository_catalog(repository, reader, *, tolerate_errors=False):
    """合并真实分支名称，同时逐目录保留引用及提交元数据。"""
    catalog = {}
    errors = []
    for path in candidate_paths(repository):
        try:
            entries = reader({**repository, "path": path})
        except WorkflowError as error:
            if not tolerate_errors:
                raise
            errors.append({"path": path, "error": str(error)})
            continue
        for entry in entries:
            source = {**entry, "path": path}
            current = catalog.get(entry["name"])
            if current is None:
                catalog[entry["name"]] = {**entry, "sources": [source]}
            else:
                sources = [*current["sources"], source]
                local, remote = current["local"] or entry["local"], current["remote"] or entry["remote"]
                if entry["local"] and not current["local"]:
                    current.update(entry)
                current.update(local=local, remote=remote, sources=sources)
    if errors and not catalog:
        raise WorkflowError(errors[0]["error"])
    return sorted(catalog.values(), key=lambda entry: entry["name"])


def inspect_repository(path):
    result = {"path": path, "branch": "", "head": "", "date": "", "commit_time": None, "error": ""}
    try:
        root = Path(path)
        if not root.is_dir() or Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            raise WorkflowError("请选择已存在的 Git 仓库根目录。")
        # 一次读取 HEAD 的分支、提交及时间，避免分别读取时混入不同提交。
        output = git(root, "log", "-1", "--format=%H%x00%ct%x00%cI", "HEAD")
        head, commit_time, date = output.split("\0")
        branch = git(root, "branch", "--show-current")
        if git(root, "rev-parse", "HEAD") != head:
            raise WorkflowError("检测期间 HEAD 已变化，请重新检测。")
        result.update(branch=branch, head=head, date=date, commit_time=int(commit_time))
        if not branch:
            result["error"] = "目录处于游离 HEAD，请先检出分支。"
    except WorkflowError as error:
        result["error"] = str(error)
    return result


def match_repository(repository, branch, extra=()):
    candidates = [inspect_repository(path) for path in candidate_paths(repository, extra)]
    matches = [entry for entry in candidates if not entry["error"] and entry["branch"] == branch]
    selected = None
    if any(entry["error"] for entry in candidates):
        reason = "候选目录检测未全部成功，请核对后明确选择目录。"
    elif not matches:
        reason = "没有目录当前检出目标分支，请手动指定目录或切换分支后重新检测。"
    else:
        latest = max(entry["commit_time"] for entry in matches)
        newest = [entry for entry in matches if entry["commit_time"] == latest]
        if len(newest) == 1:
            selected = newest[0]
            reason = "唯一匹配目标分支。" if len(matches) == 1 else "多个目录匹配目标分支，采用 HEAD 提交者时间最新的目录。"
        else:
            reason = "匹配目录的 HEAD 提交者时间相同，请明确选择目录。"
    return {"name": repository["name"], "role": repository["role"], "branch": branch,
            "candidates": candidates, "status": "selected" if selected else "needs_user",
            "path": selected["path"] if selected else "", "head": selected["head"] if selected else "",
            "reason": reason}


class RepositorySelectionFlow:
    def detect_repositories(self, purpose="prepare", paths=None):
        state = self.state
        if purpose == "baseline":
            self.require_baseline_change()
            targets = state["repos"]
        elif purpose == "prepare":
            self.require_state("等待选择目录", "准备工作区失败", "已暂停", "已停止")
            targets = self._repository_targets()
        else:
            raise WorkflowError("不支持该目录检测用途。")
        roles = {repo["role"] for repo in targets}
        if paths is not None and (not isinstance(paths, dict) or not set(paths).issubset(roles)
                                 or any(not isinstance(path, str) or not path.strip() or not Path(path.strip()).is_absolute()
                                        for path in paths.values())):
            raise WorkflowError("请按仓库角色填写有效的本机绝对目录。")
        if purpose == "prepare":
            state["pause_requested"] = False
        self._detect_repositories(purpose, targets, paths or {})

    def _repository_targets(self):
        state = self.state
        targets = (state.get("branch_resolution") or {}).get("repositories", [])
        if (state.get("prepare_complete") or state.get("followup") or state.get("pending")
                or len(targets) != 2 or any(repo["status"] != "resolved" for repo in targets)):
            raise WorkflowError("请先确定前后端目标分支，再检测首次准备目录。")
        return targets

    def _detect_repositories(self, purpose, targets, paths):
        configured = {repo["role"]: repo for repo in configured_repositories(self.settings)}
        previous = self.state.get("repository_detection") or {}
        entries = []
        prepared = {repo["role"]: repo for repo in self.state["repos"]} if purpose == "prepare" else {}
        for target in targets:
            role = target["role"]
            extras = [target["path"]]
            if purpose == previous.get("purpose"):
                old = next((repo for repo in previous["repositories"] if repo["role"] == role), None)
                if old:
                    extras.extend(entry["path"] for entry in old["candidates"])
            if role in paths:
                extras.append(paths[role])
            entry = match_repository({**configured[role], "name": target["name"]}, target["branch"], extras)
            if (purpose == "prepare" and entry["status"] == "needs_user"
                    and not any(candidate["error"] or candidate["branch"] == entry["branch"] for candidate in entry["candidates"])):
                source = next((candidate for candidate in entry["candidates"] if any(
                    branch["name"] == entry["branch"] for branch in branch_catalog({**configured[role], "path": candidate["path"]}))), None)
                if source:
                    entry.update(status="selected", path=source["path"], head="", action="allocate",
                                 reason="将按顺序切换空闲目录；没有空闲目录时创建独立工作区。")
            if role in prepared:
                repo = prepared[role]
                entry.update(status="bound", path=repo["path"], head=repo["baseline"], reason="已绑定当前会话目录。")
            entries.append(entry)
        self.state["repository_detection"] = {"purpose": purpose, "repositories": entries, "time": timestamp()}
        if purpose == "prepare":
            self.state.update(stage="等待选择目录", error="")
        self.save()

    def _analyze_idle(self, request):
        state = self.state
        self._close_rpc()
        state["workspace_analysis"] = {**request, "return_phase": state["phase"],
                                       "return_thread": state.get("thread_id"), "offset": 0, "files": []}
        state.update(phase="branch", thread_id=state.get("branch_thread_id"), turn_id=None)
        self.save()
        self._start_turn("逐文件判断未提交修改是否影响功能；无法判断返回 unknown。", IDLE_SCHEMA)

    def _idle_context(self):
        request = self.state["workspace_analysis"]
        current = checkout_snapshot(request["path"])
        if (current != request["snapshot"] or git(request["path"], "rev-parse", "HEAD") != request["head"]
                or git(request["path"], "branch", "--show-current") != request["branch"]):
            raise WorkflowError("空闲判定期间目录发生变化，请重新准备工作区。")
        paths = request["snapshot"]["paths"][request["offset"]:request["offset"] + 10]
        return {"step": "idle", "path": request["path"], "branch": request["branch"],
                "project": self.state.get("project"), "supplement": self.state["supplement"],
                "files": idle_evidence(request["path"], {"paths": paths})}

    def _complete_idle(self, text):
        state = self.state
        request = state["workspace_analysis"]
        paths = request["snapshot"]["paths"][request["offset"]:request["offset"] + 10]
        request["files"].extend(validate_idle(text, paths))
        request["offset"] += len(paths)
        if request["offset"] < len(request["snapshot"]["paths"]):
            self._start_turn("继续逐文件判断功能影响。", IDLE_SCHEMA)
            return
        state.setdefault("idle_assessments", {})[request["path"].casefold()] = {
            key: request[key] for key in ("path", "snapshot", "head", "branch", "files")}
        self._close_rpc()
        state["branch_thread_id"] = state["thread_id"]
        state.update(phase=request["return_phase"], thread_id=request["return_thread"], turn_id=None,
                     stage="准备工作区失败")
        state.pop("workspace_analysis")
        self.save()
        if state.get("followup"):
            self._prepare_followup()
        else:
            self.prepare_and_plan(state["selections"], state["supplement"])

    def choose_repositories(self, paths):
        self.require_state("等待选择目录")
        self._repository_targets()
        detection = self.state.get("repository_detection")
        if not detection or detection["purpose"] != "prepare":
            raise WorkflowError("请先检测当前目标分支对应的仓库目录。")
        entries = detection["repositories"]
        expected = {entry["role"] for entry in entries if entry["status"] != "bound"}
        if not isinstance(paths, dict) or set(paths) != expected or any(not isinstance(path, str) for path in paths.values()):
            raise WorkflowError("请明确选择所有尚未绑定的前后端目录。")
        selected = []
        for entry in entries:
            if entry["status"] == "bound":
                continue
            candidate = next((value for value in entry["candidates"] if value["path"] == paths[entry["role"]]), None)
            allocation = entry.get("action") == "allocate" and paths[entry["role"]] == entry["path"]
            if not candidate or candidate["error"] or (candidate["branch"] != entry["branch"] and not allocation):
                raise WorkflowError("所选目录须先检测成功，并已检出目标分支。")
            current = inspect_repository(candidate["path"])
            if current != candidate:
                raise WorkflowError("所选目录分支或 HEAD 在检测后发生变化，请重新检测并选择。")
            selected.append((entry, candidate))
        for entry, candidate in selected:
            entry.update(status="selected", path=candidate["path"], head=candidate["head"], reason="人工明确选择目录。")
        self._prepare_matched_repositories()

    def _prepare_matched_repositories(self):
        if self.closed or self.state.get("pause_requested"):
            self.state["stage"] = "已暂停"
            self.save()
            return
        detection = self.state["repository_detection"]
        if any(entry["status"] == "needs_user" for entry in detection["repositories"]):
            return
        if len({entry["path"].casefold() for entry in detection["repositories"]}) != len(detection["repositories"]):
            raise WorkflowError("前后端不能使用同一个仓库目录。")
        selections = []
        configured = {repo["role"]: repo for repo in configured_repositories(self.settings)}
        for entry in detection["repositories"]:
            selection = {"name": entry["name"], "role": entry["role"], "path": entry["path"],
                         "branch": entry["branch"], "base": "refs/heads/" + entry["branch"],
                         "create": False, "track": False, "remote": configured[entry["role"]]["remote"]}
            if entry.get("action") == "allocate":
                selection.update(allocate=True, candidate_paths=[candidate["path"] for candidate in entry["candidates"]])
            else:
                selection["expected_head"] = entry["head"]
            selections.append(selection)
        self._close_rpc()
        self.state["branch_thread_id"] = self.state.get("thread_id")
        self.state.update(thread_id=None, turn_id=None, phase="plan", stage="资料已读取")
        self.prepare_and_plan(selections, self.state["supplement"])
