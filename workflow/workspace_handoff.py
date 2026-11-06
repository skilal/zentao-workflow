"""已完成需求的工作目录交接；保留本机文件及可核对的中断进度。"""

from copy import deepcopy
from hashlib import sha256
from json import dumps
from pathlib import Path
from uuid import uuid4

from workflow.gitops import checkout_snapshot, git, reject_hooks, repository_lock, safe_file, workspace_claims, worktrees
from workflow.settings import WorkflowError
from workflow.store import timestamp


def handoff_reason(state):
    if state.get("phase") != "complete" or state.get("stage") not in {"已完成（静态审查）", "已完成（未审查）"}:
        return "仅已完成且需求代码已提交的会话可以交接。"
    if not state.get("prepare_complete") or not state.get("repos"):
        return "请先完成工作区准备。"
    if any(not all(repo.get(key) for key in ("name", "role", "source", "path", "branch", "baseline"))
           for repo in state["repos"]):
        return "历史仓库信息不完整，不能交接；原有会话仍可查看。"
    if (state.get("pending") or state.get("manual_compile") or state.get("workspace_reuse_request")
            or state.get("baseline_change_request") or state.get("cleanup")
            or (state.get("checkout_request") or {}).get("status", "restored") != "restored"):
        return "请先处理完待答问题、目录操作或独立检查。"
    return ""


def handoff_file(root, relative):
    path = safe_file({"path": str(root)}, relative)
    current = Path(root)
    for part in [None, *Path(relative).parts]:
        if part is not None:
            current /= part
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise WorkflowError(f"交接文件路径不能包含链接或重解析点：{relative}")
    return path


class WorkspaceHandoffFlow:
    def _require_handoff(self):
        reason = handoff_reason(self.state)
        if reason or self.turn_running or self.closed:
            raise WorkflowError(reason or "请先等待后台操作结束。")
        return self.state

    def _handoff_worktrees(self, source, target):
        entries = worktrees(source)
        for path in (source, target):
            entry = next((entry for entry in entries if Path(entry["worktree"]).resolve() == path), None)
            if not entry or "locked" in entry or "prunable" in entry:
                raise WorkflowError(f"工作目录未登记、被锁定或登记失效，不能交接：{path}")
            if not path.is_dir() or Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
                raise WorkflowError(f"请选择有效的 Git 仓库根目录：{path}")
            reject_hooks(path)
        common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if Path(git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != common:
            raise WorkflowError("目标目录必须属于同一个 Git 仓库。")
        claims = workspace_claims(self.store.sessions(), self.state["id"])
        for path in (source, target):
            if str(path).casefold() in claims:
                raise WorkflowError(f"工作目录正被其他未完成会话或交接使用：{path}")

    def _handoff_ignored(self, target, target_head, source_head):
        # 仅检查目标分支实际新增/改写的路径，避免遍历依赖及构建缓存。
        changed = git(target, "diff", "--no-renames", "--name-only", "-z", target_head, source_head, binary=True)
        incoming = {line.decode() for line in git(target, "ls-tree", "-r", "--name-only", "-z", source_head, binary=True).split(b"\0") if line}
        candidates, directories = set(), []
        for value in changed.split(b"\0"):
            if not value or value.decode() not in incoming:
                continue
            relative = value.decode()
            path = handoff_file(target, relative)
            if path.is_dir():
                directories.append(relative)
            for part in [Path(relative), *Path(relative).parents]:
                if str(part) != "." and (Path(target) / part).exists():
                    candidates.add(part.as_posix())
        ignored = b""
        if candidates:
            ignored = git(target, "check-ignore", "-z", "--stdin", optional=True, binary=True,
                          input_data=b"\0".join(path.encode() for path in sorted(candidates)) + b"\0")
        for directory in directories:
            ignored += git(target, "--literal-pathspecs", "ls-files", "--others", "--ignored", "--exclude-standard",
                           "-z", "--", directory, binary=True)
        risks = sorted({value.decode() for value in ignored.split(b"\0") if value})
        if risks:
            raise WorkflowError("切换可能覆盖被 Git 忽略的文件，请先人工处理：" + "、".join(risks))

    def _handoff_snapshot(self, repository, target_path):
        state = self.state
        repo = next((repo for repo in state["repos"] if repo["name"] == repository), None)
        if repo is None or not isinstance(target_path, str) or not Path(target_path).is_absolute():
            raise WorkflowError("请选择当前会话仓库并填写绝对开发目录。")
        source, target = Path(repo["path"]).resolve(), Path(target_path).resolve()
        if target.is_relative_to(self.store.root.resolve()):
            raise WorkflowError("本地开发目录不能位于会话管理目录中。")
        self._handoff_worktrees(source, target)
        head = git(source, "rev-parse", "HEAD")
        if git(source, "branch", "--show-current") != repo["branch"]:
            raise WorkflowError("源工作区已不在会话目标分支，请先恢复后重新预览。")
        commits = [repo["baseline"]]
        delivered = state.get("commits", {}).get(repository)
        if delivered and delivered != "无代码修改":
            commits.append(delivered)
        if any(git(source, "merge-base", commit, head, optional=True) != commit for commit in commits):
            raise WorkflowError("目标分支未包含会话基线或原交付提交，不能交接。")
        source_snapshot = checkout_snapshot(source)
        preserved = {path.casefold() for path in repo.get("preserved_paths", [])}
        pending = [path for path in source_snapshot["paths"] if path.casefold() not in preserved]
        if pending:
            raise WorkflowError("需求代码仍有未提交修改，请先处理：" + "、".join(sorted(pending)))
        target_head = git(target, "rev-parse", "HEAD")
        target_snapshot = checkout_snapshot(target)
        for root, snapshot in ((source, source_snapshot), (target, target_snapshot)):
            if git(root, "ls-files", "--unmerged", "-z", binary=True):
                raise WorkflowError("工作目录仍有未解决的合并冲突，请先人工处理。")
            for relative in snapshot["paths"]:
                path = handoff_file(root, relative)
                if path.exists() and not path.is_file():
                    raise WorkflowError("本机改动包含非普通文件，不能交接：" + relative)
        task_paths = [path.decode() for path in git(source, "diff", "--no-renames", "--name-only", "-z",
                      repo["baseline"], head, binary=True).split(b"\0") if path]
        switching = [path.decode() for path in git(target, "diff", "--no-renames", "--name-only", "-z",
                     target_head, head, binary=True).split(b"\0") if path]
        conflicts = sorted(path for path in target_snapshot["paths"] if any(
            path.casefold() == other.casefold() or path.casefold().startswith(other.casefold().rstrip("/") + "/")
            or other.casefold().startswith(path.casefold().rstrip("/") + "/") for other in task_paths + switching))
        if source != target and conflicts:
            raise WorkflowError("本机改动与需求或分支切换涉及同一文件，请先人工处理：" + "、".join(conflicts))
        self._handoff_ignored(target, target_head, head)
        return {"repository": repository, "repo": deepcopy(repo), "source": str(source), "target": str(target),
                "branch": repo["branch"], "source_head": head, "target_head": target_head,
                "target_branch": git(target, "branch", "--show-current"), "source_snapshot": source_snapshot,
                "target_snapshot": target_snapshot, "task_paths": task_paths, "already_here": source == target,
                "session": {key: deepcopy(state.get(key)) for key in ("phase", "stage", "delivery_round", "commits", "thread_id")}}

    def preview_workspace_handoff(self, repository, path):
        state = self._require_handoff()
        if (state.get("workspace_handoff_request") or {}).get("started"):
            raise WorkflowError("已有交接尚未完成，请继续交接或恢复原状后取消。")
        repo = next((repo for repo in state["repos"] if repo["name"] == repository), None)
        if repo is None:
            raise WorkflowError("请选择当前会话仓库。")
        with repository_lock(repo["path"]):
            preview = self._handoff_snapshot(repository, path)
            token = sha256(dumps(preview, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            state["workspace_handoff_request"] = {**preview, "token": token, "status": "preview", "step": "preview"}
            state["error"] = ""
            self.save()

    def _handoff_position(self, request):
        source, target = Path(request["source"]), Path(request["target"])
        self._handoff_worktrees(source, target)
        state = self.state
        repo = next((repo for repo in state["repos"] if repo["name"] == request["repository"]), None)
        if repo != request["repo"] or any(state.get(key) != value for key, value in request["session"].items()):
            raise WorkflowError("会话仓库或交付状态已变化，请恢复后重新预览。")
        if git(source, "rev-parse", "HEAD") != request["source_head"]:
            raise WorkflowError("源工作区提交已变化，请恢复后重新核对。")
        if git(source, "rev-parse", "--verify", "refs/heads/" + request["branch"] + "^{commit}") != request["source_head"]:
            raise WorkflowError("目标分支提交已变化，请恢复后重新核对。")
        source_branch = git(source, "branch", "--show-current")
        target_branch, target_head = git(target, "branch", "--show-current"), git(target, "rev-parse", "HEAD")
        detached = source_branch == ""
        switched = target_branch == request["branch"] and target_head == request["source_head"]
        original = target_branch == request["target_branch"] and target_head == request["target_head"]
        if source_branch not in {"", request["branch"]} or not (original or switched) or (switched and not detached):
            raise WorkflowError("实际分支或提交与交接记录不符，请人工恢复后再继续。")
        for root, snapshot in ((source, request["source_snapshot"]), (target, request["target_snapshot"])):
            if checkout_snapshot(root) != snapshot or git(root, "ls-files", "--unmerged", "-z", binary=True):
                raise WorkflowError("本机文件或暂存状态与交接记录不符，请依据备份恢复后再继续。")
        if not switched:
            self._handoff_ignored(target, request["target_head"], request["source_head"])
        return detached, switched

    def _backup_handoff(self, request):
        directory = self.store.directory(self.state["id"]) / "handoffs" / request["id"]
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / "manifest.json"
        if manifest_path.is_file():
            return
        (directory / "session.json").write_text(dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        target, paths = request["target"], request["selected_paths"]
        files = []
        for index, relative in enumerate(paths):
            path = handoff_file(target, relative)
            entry = {"path": relative, "deleted": not path.exists()}
            if path.is_file():
                entry.update(file=f"file-{index}", mode=path.stat().st_mode)
                (directory / entry["file"]).write_bytes(path.read_bytes())
            files.append(entry)
        for name, options in (("index.patch", ["--cached"]), ("worktree.patch", [])):
            patch = git(target, "--literal-pathspecs", "diff", *options, "--binary", "--no-ext-diff", "--no-textconv",
                        "--", *paths, binary=True) if paths else b""
            (directory / name).write_bytes(patch)
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(dumps({"id": request["id"], "source": request["source"], "target": target,
                                    "files": files, "snapshot": request["target_snapshot"]}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(manifest_path)

    def confirm_workspace_handoff(self, token, paths=None):
        state = self._require_handoff()
        if any(entry["token"] == token and entry["status"] == "complete" for entry in state.get("workspace_handoffs", [])):
            return
        request = state.get("workspace_handoff_request")
        if not request or not isinstance(token, str) or token != request["token"]:
            raise WorkflowError("请先预览本地开发目录，再明确确认交接。")
        with repository_lock(request["source"]):
            if not request.get("started"):
                current = self._handoff_snapshot(request["repository"], request["target"])
                if sha256(dumps(current, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != token:
                    raise WorkflowError("目录、分支、HEAD、文件或占用状态已变化，请重新预览。")
                if request["already_here"]:
                    raise WorkflowError("已在该开发目录，无需交接。")
                if (not isinstance(paths, list) or any(not isinstance(path, str) for path in paths)
                        or len(set(paths)) != len(paths) or not set(paths).issubset(request["target_snapshot"]["paths"])):
                    raise WorkflowError("请从预览的本机文件中选择要备份携带的内容。")
                request.update(started=True, id=uuid4().hex, selected_paths=sorted(paths), status="working", step="backup", error="")
                request["backup"] = str(self.store.directory(state["id"]) / "handoffs" / request["id"])
                state["error"] = ""
                self.save()
            elif paths is not None and paths != request["selected_paths"]:
                raise WorkflowError("恢复交接时不能改变原先确认的文件清单。")
            else:
                request.update(status="working", error="")
                state["error"] = ""
                self.save()
            try:
                detached, switched = self._handoff_position(request)
                self._backup_handoff(request)
                self._handoff_position(request)
                if not detached:
                    request.update(status="working", step="detach", error="")
                    self.save()
                    git(request["source"], "switch", "--detach", request["source_head"])
                if not switched:
                    request.update(step="switch")
                    self.save()
                    self._handoff_position(request)
                    # 预检已排除同文件变化，普通切换原样携带本机文件与暂存状态，不进行三方合并。
                    git(request["target"], "switch", request["branch"])
                request.update(step="verify")
                self.save()
                if self._handoff_position(request) != (True, True):
                    raise WorkflowError("分支交接未完成，请核对源目录及目标目录。")
                self._finish_handoff(request)
            except Exception as error:
                actual = {side: {"branch": git(request[side], "rev-parse", "--abbrev-ref", "HEAD", optional=True),
                                 "head": git(request[side], "rev-parse", "--verify", "HEAD", optional=True)}
                          for side in ("source", "target")}
                completed = []
                if (Path(request["backup"]) / "manifest.json").is_file():
                    completed.append("本机文件备份已保存")
                if actual["source"] == {"branch": "HEAD", "head": request["source_head"]}:
                    completed.append("源目录已释放目标分支")
                if actual["target"] == {"branch": request["branch"], "head": request["source_head"]}:
                    completed.append("开发目录已切换到目标分支")
                state["workspace_handoff_request"].update(status="failed", error=str(error), actual=actual,
                                                         completed_steps=completed)
                state["error"] = str(error)
                self.save()
                raise

    def _finish_handoff(self, request):
        state = self.state
        previous = deepcopy(state)
        repo = next(repo for repo in state["repos"] if repo["name"] == request["repository"])
        retired = state.setdefault("retired_repos", [])
        if not any(entry["path"] == repo["path"] for entry in retired):
            retired.append(deepcopy(repo))
        self._close_rpc()
        repo.update(path=request["target"], source=request["target"], workspace_owned=False, workspace_mode="handed_off",
                    create=False, base="refs/heads/" + repo["branch"], handoff_head=request["source_head"],
                    preserved_paths=request["target_snapshot"]["paths"], checkout_backup=request["backup"])
        repo.pop("adopted_paths", None)
        for selection in state.get("selections", []):
            if selection["name"] == repo["name"]:
                selection.update(path=repo["path"], base=repo["base"], create=False)
        record = {**deepcopy(request), "status": "complete", "step": "complete", "time": timestamp()}
        state.setdefault("workspace_handoffs", []).append(record)
        state["workspace_handoff_request"] = None
        state["error"] = ""
        state["history"].append({"time": timestamp(), "role": "用户", "phase": state["phase"],
                                 "delivery_round": state.get("delivery_round", 1),
                                 "text": f"人工确认交接到本地开发目录：{repo['name']} · {request['source']} → {repo['path']}；"
                                         f"目标分支 {repo['branch']}；HEAD {request['source_head']}。本机文件及暂存状态已保留，"
                                         f"备份：{request['backup']}。可在 IDEA 打开目标目录；原会话继续使用此目录。"})
        try:
            self.save()
        except Exception:
            state.clear()
            state.update(previous)
            raise

    def cancel_workspace_handoff(self, token):
        state = self._require_handoff()
        request = state.get("workspace_handoff_request")
        if not request or token != request["token"]:
            raise WorkflowError("交接预览已失效，请查看最新状态。")
        if not request.get("started"):
            state["workspace_handoff_request"] = None
            state["error"] = ""
            self.save()
            return
        with repository_lock(request["source"]):
            if request["step"] == "backup":
                self._handoff_worktrees(Path(request["source"]), Path(request["target"]))
                expected = [(request["source"], request["branch"], request["source_head"]),
                            (request["target"], request["target_branch"], request["target_head"])]
                if any(git(path, "branch", "--show-current") != branch or git(path, "rev-parse", "HEAD") != head
                       for path, branch, head in expected):
                    raise WorkflowError("分支或提交已变化，请完整恢复原状后再取消。")
            else:
                if self._handoff_position(request) != (False, False):
                    raise WorkflowError("交接已经改变分支，请人工完整恢复原分支及文件后再取消。")
            state.setdefault("workspace_handoffs", []).append({**deepcopy(request), "status": "cancelled", "time": timestamp()})
            state["workspace_handoff_request"] = None
            state["error"] = ""
            self.save()
