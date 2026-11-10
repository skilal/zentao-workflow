"""逐仓库释放分支，人工确认接管本地开发目录；不搬迁文件。"""

from copy import deepcopy
from hashlib import sha256
from json import dumps
from pathlib import Path
from uuid import uuid4

from workflow.baseline import BASELINE_STAGES
from workflow.gitops import checkout_snapshot, git, reject_hooks, repository_lock, workspace_claims, worktrees
from workflow.settings import WorkflowError
from workflow.store import timestamp


RELEASE_ACTIONS = {
    "preview_workspace_release", "confirm_workspace_release", "cancel_workspace_release",
    "preview_workspace_adoption", "confirm_workspace_adoption", "cancel_workspace_adoption",
    "confirm_workspace_adoption_baseline",
}


def settle_workspace_releases(state):
    changed = False
    for repo in state.get("repos", []):
        release = repo.get("workspace_release")
        if release and release["status"] == "detaching":
            release.update(status="interrupted", error="上次释放中断，请预览并核对实际 Git 状态后明确确认；不会自动重试。")
            changed = True
    return changed


class WorkspaceReleaseFlow:
    def _release_repository(self, name):
        state = self.state
        if self.closed or self.turn_running or state.get("stage") in self.active_stages:
            raise WorkflowError("请先手动暂停并等待后台完全停止，再操作分支占用。")
        if (state.get("workspace_handoff_request") or state.get("workspace_reuse_request")
                or state.get("baseline_change_request") or state.get("cleanup")
                or (state.get("checkout_request") or {}).get("status", "restored") != "restored"):
            raise WorkflowError("请先完成或取消当前目录操作，再操作分支占用。")
        request = state.get("workspace_release_request")
        if request and request["repository"] != name:
            raise WorkflowError("请先完成或取消当前仓库的释放或接管预览。")
        repo = next((repo for repo in state.get("repos", []) if repo["name"] == name), None)
        if not repo or not all(repo.get(key) for key in ("path", "source", "branch", "baseline", "role")):
            raise WorkflowError("请选择已准备且信息完整的当前会话仓库。")
        return repo

    def _release_paths(self, repo, target=None):
        source = Path(repo["path"]).resolve()
        paths = [source] if target is None else [source, target]
        entries = worktrees(source)
        claims = workspace_claims(self.store.sessions(), self.state["id"])
        common = git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        for path in paths:
            entry = next((entry for entry in entries if Path(entry["worktree"]).resolve() == path), None)
            if not entry or "locked" in entry or "prunable" in entry:
                raise WorkflowError(f"工作目录未登记、被锁定或登记失效：{path}")
            if not path.is_dir() or Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
                raise WorkflowError(f"请选择有效的 Git 仓库根目录：{path}")
            if Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != Path(common).resolve():
                raise WorkflowError("开发目录必须属于同一个 Git 仓库。")
            if str(path).casefold() in claims:
                raise WorkflowError(f"工作目录正被其他会话占用：{path}")
            reject_hooks(path)

    def _release_snapshot(self, repo):
        self._release_paths(repo)
        path = repo["path"]
        head = git(path, "rev-parse", "HEAD")
        branch = git(path, "branch", "--show-current")
        release = repo.get("workspace_release")
        if release:
            if head != release["head"] or branch not in {"", repo["branch"]}:
                raise WorkflowError("实际分支或提交与释放记录不符，请人工核对恢复后重新预览。")
            if release["status"] == "released":
                raise WorkflowError("该仓库已释放，请在 IDEA 切换目标分支后确认改用本地开发目录。")
        elif branch != repo["branch"]:
            raise WorkflowError("工作区已不在会话目标分支，请先人工恢复后重新预览。")
        snapshot = checkout_snapshot(path)
        if snapshot["paths"]:
            raise WorkflowError("工作区存在未提交修改，请先处理后释放：" + "、".join(snapshot["paths"]))
        if git(path, "ls-files", "--unmerged", "-z", binary=True):
            raise WorkflowError("工作区仍有合并冲突，请先人工处理。")
        return {"kind": "release", "repository": repo["name"], "repo": deepcopy(repo),
                "source": path, "branch": repo["branch"], "head": head,
                "actual_branch": branch, "snapshot": snapshot,
                "session": {key: deepcopy(self.state.get(key)) for key in ("phase", "stage", "delivery_round", "commits", "pending")}}

    def preview_workspace_release(self, repository):
        repo = self._release_repository(repository)
        with repository_lock(repo["path"]):
            preview = self._release_snapshot(repo)
            preview["id"] = uuid4().hex
            preview["token"] = sha256(dumps(preview, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self.state["workspace_release_request"] = preview
            self.state["error"] = ""
            self.save()

    def confirm_workspace_release(self, token):
        if not isinstance(token, str) or not token:
            raise WorkflowError("请先预览并明确确认释放分支占用。")
        if any(repo.get("workspace_release", {}).get("token") == token
               and repo["workspace_release"]["status"] == "released" for repo in self.state["repos"]):
            return
        request = self.state.get("workspace_release_request")
        if not request or request["kind"] != "release" or token != request["token"]:
            raise WorkflowError("请先预览并明确确认释放分支占用。")
        repo = self._release_repository(request["repository"])
        with repository_lock(repo["path"]):
            current = self._release_snapshot(repo)
            current["id"] = request["id"]
            if sha256(dumps(current, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != token:
                raise WorkflowError("分支、HEAD、文件或会话状态已变化，请重新预览。")
            release = {"status": "detaching", "head": request["head"], "branch": request["branch"],
                       "source": request["source"], "token": token, "time": timestamp()}
            repo["workspace_release"] = release
            self._close_rpc()
            self.save()
            try:
                if current["actual_branch"]:
                    git(repo["path"], "switch", "--detach", request["head"])
                if (git(repo["path"], "branch", "--show-current")
                        or git(repo["path"], "rev-parse", "HEAD") != request["head"]
                        or checkout_snapshot(repo["path"]) != request["snapshot"]):
                    raise WorkflowError("释放后的分支、提交或文件状态不符，请人工核对。")
                release["status"] = "released"
                self.state["workspace_release_request"] = None
                self.state.setdefault("workspace_release_history", []).append({**release, "kind": "release", "repository": repo["name"]})
                self.state["error"] = ""
                self.note("用户", f"已释放分支占用：{repo['name']} · {repo['branch']}；目录 {repo['path']}；"
                          f"HEAD {release['head']}。分支占用已释放，可在 IDEA 原项目切换；目录及文件保留。")
            except Exception as error:
                release.update(status="interrupted", error=str(error))
                self.state["workspace_release_request"] = None
                self.save()
                raise

    def cancel_workspace_release(self, token):
        request = self.state.get("workspace_release_request")
        if not request or token != request["token"]:
            raise WorkflowError("预览已失效，请查看最新状态。")
        self._release_repository(request["repository"])
        self.state["workspace_release_request"] = None
        self.state["error"] = ""
        self.save()

    def _adoption_snapshot(self, repo, path):
        release = repo.get("workspace_release")
        if not release or release["status"] != "released":
            raise WorkflowError("请先完成释放并核对实际 Git 状态。")
        if not isinstance(path, str) or not Path(path.strip()).is_absolute():
            raise WorkflowError("请填写本地开发目录的绝对路径。")
        target = Path(path.strip()).resolve()
        if target.is_relative_to(self.store.root.resolve()):
            raise WorkflowError("请选择会话管理目录之外的本地开发目录。")
        if any(other["name"] != repo["name"] and Path(other["path"]).resolve() == target for other in self.state["repos"]):
            raise WorkflowError("前后端不能使用同一个仓库目录。")
        self._release_paths(repo, target)
        source = Path(repo["path"]).resolve()
        if source != target and (git(source, "branch", "--show-current") or git(source, "rev-parse", "HEAD") != release["head"]):
            raise WorkflowError("释放后的源工作区已变化，请先人工核对恢复。")
        if source != target and checkout_snapshot(source)["paths"]:
            raise WorkflowError("原工作区出现未提交修改，请先处理；接管不搬迁文件。")
        if git(target, "branch", "--show-current") != repo["branch"]:
            raise WorkflowError("请先在 IDEA 原项目中切换到目标分支，再预览接管。")
        if git(target, "ls-files", "--unmerged", "-z", binary=True):
            raise WorkflowError("开发目录仍有合并冲突，请先人工处理。")
        head = git(target, "rev-parse", "HEAD")
        changed = head != release["head"]
        state = self.state
        baseline_reason = ""
        if changed:
            if state.get("commits") and state["phase"] != "complete":
                baseline_reason = "存在部分提交，不能变更冻结基线；请人工将目标目录恢复释放时的提交后接管。"
            elif state.get("pending") or state.get("manual_compile"):
                baseline_reason = "仍有待答问题或独立检查，不能变更基线；请先将目标目录恢复释放时的提交后接管。"
            elif state["phase"] != "complete" and state["stage"] not in BASELINE_STAGES:
                baseline_reason = "当前阶段不允许变更基线，请先将目标目录恢复释放时的提交后接管。"
        return {"kind": "adopt", "repository": repo["name"], "repo": deepcopy(repo),
                "source": str(source), "target": str(target), "branch": repo["branch"],
                "release_head": release["head"], "head": head, "baseline": repo["baseline"],
                "baseline_changed": changed, "baseline_reason": baseline_reason,
                "snapshot": checkout_snapshot(target),
                "session": {key: deepcopy(state.get(key)) for key in ("phase", "stage", "delivery_round", "commits", "pending", "manual_compile")}}

    def preview_workspace_adoption(self, repository, path):
        repo = self._release_repository(repository)
        with repository_lock(repo["path"]):
            preview = self._adoption_snapshot(repo, path)
            preview["id"] = uuid4().hex
            preview["token"] = sha256(dumps(preview, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self.state["workspace_release_request"] = preview
            self.state["error"] = ""
            self.save()

    def confirm_workspace_adoption(self, token, change_baseline=False):
        if not isinstance(token, str) or not token:
            raise WorkflowError("请先预览并明确确认改用本地开发目录。")
        if any(entry["token"] == token and entry["kind"] == "adopt" for entry in self.state.get("workspace_release_history", [])):
            return
        request = self.state.get("workspace_release_request")
        if not request or request["kind"] != "adopt" or token != request["token"]:
            raise WorkflowError("请先预览并明确确认改用本地开发目录。")
        repo = self._release_repository(request["repository"])
        with repository_lock(repo["path"]):
            preview = self._adoption_snapshot(repo, request["target"])
            preview["id"] = request["id"]
            if sha256(dumps(preview, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != token:
                raise WorkflowError("目录、HEAD、文件或会话占用已变化，请重新预览。")
            if request["baseline_changed"]:
                if request["baseline_reason"]:
                    raise WorkflowError(request["baseline_reason"])
                if not change_baseline:
                    raise WorkflowError("IDEA 当前 HEAD 已变化，请单独预览并明确确认变更审查基线。")
            elif change_baseline:
                raise WorkflowError("当前无需变更审查基线，请按原任务范围确认接管。")
            backup = self.store.directory(self.state["id"]) / f"workspace-adoption-{uuid4().hex}.json"
            backup.write_text(dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            previous = deepcopy(self.state)
            try:
                self._close_rpc()
                if request["source"] != request["target"]:
                    retired = deepcopy(repo)
                    retired.pop("workspace_release", None)
                    self.state.setdefault("retired_repos", []).append(retired)
                repo.update(path=request["target"], source=request["target"], workspace_owned=False,
                            workspace_mode="release_adopted", create=False, base="refs/heads/" + repo["branch"],
                            handoff_head=request["head"], checkout_head=request["head"], preserved_paths=request["snapshot"]["paths"])
                for key in ("workspace_release", "adopted_paths", "checkout_backup"):
                    repo.pop(key, None)
                for selection in self.state.get("selections", []):
                    if selection["name"] == repo["name"]:
                        selection.update(path=repo["path"], base=repo["base"], create=False)
                if change_baseline:
                    repo["baseline"] = request["head"]
                    self.state.update(review_start=len(self.state["reviews"]), review_manifest={}, acceptance=None,
                                      commit_batch=None, delivery_request=None)
                    for batch in self.state.get("compile_checks", []):
                        if batch.get("delivery_round", 1) == self.state.get("delivery_round", 1):
                            batch["status"] = "invalidated"
                    if self.state["phase"] in {"review", "compile", "compile_fix", "compile_analysis"}:
                        self.state.update(phase="review", stage="已暂停", pause_requested=True,
                                          baseline_review_pending=True, review_requested=True)
                    if self.state["phase"] == "plan" and self.state.get("plan_round"):
                        self.state["plan_round"]["asked"] = True
                    self.state.setdefault("baseline_changes", []).append({**deepcopy(request), "backup": str(backup), "time": timestamp()})
                self.state["workspace_release_request"] = None
                self.state["error"] = ""
                self.state.setdefault("workspace_release_history", []).append({**deepcopy(request), "kind": "adopt", "backup": str(backup), "time": timestamp()})
                self.note("用户", f"人工确认改用本地开发目录：{repo['name']} · {repo['path']}；目标分支 {repo['branch']}。"
                          + ("已另行明确确认变更基线，旧审查及检查结论失效。" if change_baseline else "原任务基线及审查范围保留。")
                          + f"新目录已有改动按本机保留内容排除；未启动任务。会话备份：{backup}。")
            except Exception:
                self.state.clear()
                self.state.update(previous)
                raise
