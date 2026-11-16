"""检测候选仓库的当前分支，按目标分支匹配会话目录。"""

from pathlib import Path

from workflow.branching import configured_repositories
from workflow.gitops import git
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
            if role in prepared:
                repo = prepared[role]
                entry.update(status="bound", path=repo["path"], head=repo["baseline"], reason="已绑定当前会话目录。")
            entries.append(entry)
        self.state["repository_detection"] = {"purpose": purpose, "repositories": entries, "time": timestamp()}
        if purpose == "prepare":
            self.state.update(stage="等待选择目录", error="")
        self.save()

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
            if not candidate or candidate["error"] or candidate["branch"] != entry["branch"]:
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
        for entry in detection["repositories"]:
            selections.append({"name": entry["name"], "role": entry["role"], "path": entry["path"],
                               "branch": entry["branch"], "base": "refs/heads/" + entry["branch"],
                               "create": False, "track": False, "expected_head": entry["head"]})
        self._close_rpc()
        self.state.update(thread_id=None, turn_id=None, phase="plan", stage="资料已读取")
        self.prepare_and_plan(selections, self.state["supplement"])
