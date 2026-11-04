"""人工确认当前 HEAD 为新审查基线，保留会话与代码。"""

from contextlib import ExitStack
from copy import deepcopy
from hashlib import sha256
from json import dumps
from uuid import uuid4

from workflow.gitops import checkout_snapshot, git, repository_lock, working_paths
from workflow.settings import WorkflowError
from workflow.store import timestamp


BASELINE_STAGES = {"已暂停", "已停止", "等待计划确认", "等待计划补充", "等待修复选择", "等待编译选择"}


class BaselineFlow:
    def change_baseline(self, token=None):
        state = self.require_state(*BASELINE_STAGES)
        if (self.turn_running or not state.get("prepare_complete") or not state.get("repos")
                or state.get("commits") or state.get("phase") == "complete"):
            raise WorkflowError("请先暂停并等待当前操作结束；仅已准备工作区且没有部分提交的未完成会话可变更基线。")
        if state.get("pending"):
            raise WorkflowError("仍有问题等待答复，请先回答；基线变更不会代替人工回答。")
        with ExitStack() as locks:
            for path in sorted({repo["path"] for repo in state["repos"]}, key=str.casefold):
                locks.enter_context(repository_lock(path))
            repositories = []
            for repo in state["repos"]:
                path = repo["path"]
                branch = git(path, "branch", "--show-current")
                if branch != repo["branch"]:
                    raise WorkflowError(f"{repo['name']}当前分支为“{branch or '游离 HEAD'}”，会话要求“{repo['branch']}”。"
                                        f"请在 {path} 手工恢复原分支后重新预览；会话及代码未修改。")
                head = git(path, "rev-parse", "HEAD")
                previous = repo["baseline"]
                common = git(path, "merge-base", previous, head, optional=True)
                relationship = ("未变化" if head == previous else "快进" if common == previous else
                                "回退" if common == head else "历史分叉或无共同祖先")
                repositories.append({"name": repo["name"], "path": path, "branch": branch,
                    "previous": previous, "current": head, "relationship": relationship,
                    "stat": git(path, "diff", "--no-ext-diff", "--no-textconv", "--stat", previous, head),
                    "remaining_paths": working_paths(repo), "preserved_paths": repo.get("preserved_paths", []),
                    "snapshot": checkout_snapshot(path)})
            if not any(repo["previous"] != repo["current"] for repo in repositories):
                raise WorkflowError("当前 HEAD 与保存的审查基线一致，无需变更。")
            preview = {"repositories": repositories, "phase": state["phase"],
                       "delivery_round": state.get("delivery_round", 1)}
            fingerprint = sha256(dumps(preview, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if token is None:
                state["baseline_change_request"] = {**preview, "token": fingerprint}
                self.save()
                return
            saved = state.get("baseline_change_request")
            if not saved or token != saved["token"] or token != fingerprint:
                raise WorkflowError("基线、分支或文件在预览后发生变化，请重新预览并明确确认。")
            # 先落盘完整备份；不切换分支、不改动工作树和暂存区。
            backup = self.store.directory(state["id"]) / f"baseline-backup-{uuid4().hex}.json"
            backup.write_text(dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            record = {**deepcopy(preview), "time": timestamp(), "backup": str(backup),
                      "review_start": len(state["reviews"])}
            self._close_rpc()
            for repo, selected in zip(state["repos"], repositories):
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
                f"{repo['name']}：{repo['previous']} → {repo['current']}（{repo['relationship']}）"
                for repo in repositories if repo["previous"] != repo["current"]))
            self.note("系统", f"完整会话已备份至 {backup}。原线程、聊天、计划、附件与历史记录保留。"
                      "新基线之前的已提交代码不再计入本轮差异；工作区与暂存区未修改，本机保留文件仍排除。"
                      "旧基线下的审查及检查结论已失效，继续后按当前阶段处理。")

    def cancel_baseline_change(self):
        self.require_state(*BASELINE_STAGES)
        self.state["baseline_change_request"] = None
        self.save()
