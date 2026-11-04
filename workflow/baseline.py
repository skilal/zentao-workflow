"""人工确认当前 HEAD 为新审查基线，保留会话与代码。"""

from contextlib import ExitStack
from copy import deepcopy
from hashlib import sha256
from json import dumps
from pathlib import Path
from uuid import uuid4

from workflow.gitops import checkout_snapshot, git, repository_lock, working_paths, workspace_claims
from workflow.settings import WorkflowError
from workflow.store import timestamp


BASELINE_STAGES = {"已暂停", "已停止", "等待计划确认", "等待计划补充", "等待修复选择", "等待编译选择"}


class BaselineFlow:
    def change_baseline(self, token=None, paths=None):
        state = self.require_state(*BASELINE_STAGES)
        if (self.turn_running or not state.get("prepare_complete") or not state.get("repos")
                or state.get("commits") or state.get("phase") == "complete"):
            raise WorkflowError("请先暂停并等待当前操作结束；仅已准备工作区且没有部分提交的未完成会话可变更基线。")
        if state.get("pending"):
            raise WorkflowError("仍有问题等待答复，请先回答；基线变更不会代替人工回答。")
        saved = state.get("baseline_change_request")
        if token is not None:
            if not saved or token != saved["token"]:
                raise WorkflowError("请重新预览仓库目录与审查基线后明确确认。")
            paths = {repo["name"]: repo["path"] for repo in saved["repositories"]}
        names = {repo["name"] for repo in state["repos"]}
        if paths is not None and (not isinstance(paths, dict) or not paths or not set(paths).issubset(names)
                or any(not isinstance(value, str) or not value.strip() or not Path(value.strip()).is_absolute()
                       for value in paths.values())):
            raise WorkflowError("请按当前会话仓库名称填写有效的本机绝对目录。")
        targets = {repo["name"]: str(Path((paths or {}).get(repo["name"], repo["path"]).strip()).resolve())
                   for repo in state["repos"]}
        if len({path.casefold() for path in targets.values()}) != len(targets):
            raise WorkflowError("前后端不能使用同一个仓库目录。")
        with ExitStack() as locks:
            for path in sorted(set(targets.values()), key=str.casefold):
                if not Path(path).is_dir() or Path(git(path, "rev-parse", "--show-toplevel")).resolve() != Path(path):
                    raise WorkflowError(f"请选择已存在的 Git 仓库根目录：{path}")
                locks.enter_context(repository_lock(path))
            claims = workspace_claims(self.store.sessions(), state["id"])
            repositories = []
            for repo in state["repos"]:
                path = targets[repo["name"]]
                moved = Path(path) != Path(repo["path"]).resolve()
                owner = claims.get(path.casefold())
                if owner:
                    raise WorkflowError(f"目标目录正被未完成会话 {owner} 使用：{path}")
                if moved and Path(path).is_relative_to(self.store.root.resolve()):
                    raise WorkflowError("更换目录请选择会话管理目录之外的本机仓库，不接管会话自建工作区。")
                branch = git(path, "branch", "--show-current")
                if branch != repo["branch"]:
                    raise WorkflowError(f"{repo['name']}当前分支为“{branch or '游离 HEAD'}”，会话要求“{repo['branch']}”。"
                                        f"请手工恢复原分支，或填写已检出该分支的新仓库目录后重新预览；会话及代码未修改。")
                head = git(path, "rev-parse", "HEAD")
                previous = repo["baseline"]
                common = git(path, "merge-base", previous, head, optional=True) if not moved else ""
                relationship = ("更换目录，重新建立基线" if moved else "未变化" if head == previous else "快进" if common == previous else
                                "回退" if common == head else "历史分叉或无共同祖先")
                snapshot = checkout_snapshot(path)
                preserved = snapshot["paths"] if moved else repo.get("preserved_paths", [])
                repositories.append({"name": repo["name"], "path": path, "branch": branch,
                    "previous_path": repo["path"], "directory_changed": moved,
                    "previous": previous, "current": head, "relationship": relationship,
                    "stat": "更换目录后以新目录 HEAD 建立基线，不跨目录比较提交差异。" if moved else
                        git(path, "diff", "--no-ext-diff", "--no-textconv", "--stat", previous, head),
                    "remaining_paths": [] if moved else working_paths(repo), "preserved_paths": preserved,
                    "previous_remaining_paths": working_paths(repo) if moved else [],
                    "snapshot": snapshot})
            if not any(repo["directory_changed"] or repo["previous"] != repo["current"] for repo in repositories):
                raise WorkflowError("当前 HEAD 与保存的审查基线一致，无需变更。")
            preview = {"repositories": repositories, "phase": state["phase"],
                       "delivery_round": state.get("delivery_round", 1)}
            fingerprint = sha256(dumps(preview, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if token is None:
                state["baseline_change_request"] = {**preview, "token": fingerprint}
                self.save()
                return
            if token != fingerprint:
                raise WorkflowError("基线、分支或文件在预览后发生变化，请重新预览并明确确认。")
            # 先落盘完整备份；不切换分支、不改动工作树和暂存区。
            backup = self.store.directory(state["id"]) / f"baseline-backup-{uuid4().hex}.json"
            backup.write_text(dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            record = {**deepcopy(preview), "time": timestamp(), "backup": str(backup),
                      "review_start": len(state["reviews"])}
            self._close_rpc()
            for repo, selected in zip(state["repos"], repositories):
                if selected["directory_changed"]:
                    state.setdefault("retired_repos", []).append(deepcopy(repo))
                    repo.update(path=selected["path"], source=selected["path"],
                                workspace_owned=False, workspace_mode="relocated", create=False,
                                base="refs/heads/" + repo["branch"], preserved_paths=selected["preserved_paths"])
                    repo.pop("adopted_paths", None)
                    repo.pop("checkout_backup", None)
                    for selection in state.get("selections", []):
                        if selection["name"] == repo["name"]:
                            selection.update(path=selected["path"], base=repo["base"], create=False)
                repo["baseline"] = selected["current"]
            state.setdefault("baseline_changes", []).append(record)
            state.update(baseline_change_request=None, review_manifest={}, acceptance=None,
                         review_start=len(state["reviews"]), error="", turn_id=None)
            for batch in state.get("compile_checks", []):
                if batch.get("delivery_round", 1) == state.get("delivery_round", 1):
                    batch.setdefault("status_before_baseline_change", batch["status"])
                    batch["status"] = "invalidated"
            if state["phase"] in {"review", "compile", "compile_fix"}:
                state.update(phase="review", baseline_review_pending=True, review_requested=True)
            if state["phase"] == "plan" and state.get("plan_round"):
                state["plan_round"]["asked"] = True
            if state["stage"] not in {"等待计划确认", "等待计划补充"}:
                state.update(stage="已暂停", pause_requested=True)
            self.note("用户", "人工确认变更审查基线：" + "；".join(
                f"{repo['name']}：{repo['previous_path']} → {repo['path']}；{repo['previous']} → {repo['current']}（{repo['relationship']}）"
                for repo in repositories if repo["directory_changed"] or repo["previous"] != repo["current"]))
            self.note("系统", f"完整会话已备份至 {backup}。原线程、聊天、计划、附件与历史记录保留。"
                      "新基线之前的已提交代码不再计入本轮差异；工作区与暂存区未修改，本机保留文件仍排除。"
                      "更换目录时不搬迁旧目录文件，新目录已有改动按本机保留内容登记；更换仅对当前会话生效。"
                      "旧基线下的审查及检查结论已失效，继续后按当前阶段处理。")

    def cancel_baseline_change(self):
        self.require_state(*BASELINE_STAGES)
        self.state["baseline_change_request"] = None
        self.save()
