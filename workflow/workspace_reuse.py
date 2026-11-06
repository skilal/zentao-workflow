"""准备失败后，人工预览并确认复用原目标分支的外部工作区。"""

from copy import deepcopy
from hashlib import sha256
from json import dumps
from pathlib import Path

from workflow.gitops import checkout_snapshot, git, reject_hooks, repository_lock, workspace_claims, worktrees
from workflow.settings import WorkflowError
from workflow.store import timestamp


class WorkspaceReuseFlow:
    def _require_workspace_reuse(self):
        state = self.require_state("准备工作区失败", "已停止", "已暂停")
        checkout = state.get("checkout_request")
        if (self.turn_running or state.get("prepare_complete") or state["phase"] not in {"plan", "followup"}
                or not state.get("selections") or state.get("commits") or state.get("manual_compile")
                or state.get("pending") or (checkout and checkout["status"] not in {"waiting", "restored"})):
            raise WorkflowError("仅没有待答问题、部分提交或未完成切换的工作区准备失败会话可确认复用。")
        return state

    def _workspace_reuse_snapshot(self, selection):
        state = self.state
        source = Path(selection["path"]).resolve()
        entry = next((entry for entry in worktrees(source)
                      if entry.get("branch") == "refs/heads/" + selection["branch"]), None)
        if not entry:
            raise WorkflowError("未找到已检出原目标分支的工作区，请重新准备工作区。")
        path = Path(entry["worktree"]).resolve()
        owner = workspace_claims(self.store.sessions(), state["id"]).get(str(path).casefold())
        if owner:
            raise WorkflowError(f"工作区正被其他未完成会话 {owner} 使用：{path}，不能接管。")
        if "locked" in entry or "prunable" in entry:
            raise WorkflowError(f"工作区被锁定或登记失效，不能复用：{path}")
        if path == source or path.is_relative_to(self.store.root.resolve()):
            raise WorkflowError("该目录属于原仓库或会话管理工作区，请按原工作区分配流程继续。")
        if not path.is_dir() or Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
            raise WorkflowError(f"工作区不是有效的 Git 仓库根目录：{path}")
        common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != common:
            raise WorkflowError("目标工作区与会话来源仓库不匹配，请重新核对。")
        if git(path, "branch", "--show-current") != selection["branch"]:
            raise WorkflowError("工作区已不在原目标分支，请重新预览。")
        reject_hooks(source)
        reject_hooks(path)
        return {"name": selection["name"], "selection": deepcopy(selection), "path": str(path),
                "branch": selection["branch"], "head": git(path, "rev-parse", "HEAD"),
                "subject": git(path, "log", "-1", "--format=%s"), "snapshot": checkout_snapshot(path),
                "occupancy": "未被其他未完成会话占用", "session_id": state["id"],
                "phase": state["phase"], "delivery_round": state.get("delivery_round", 1)}

    def preview_workspace_reuse(self, repository):
        state = self._require_workspace_reuse()
        selection = next((entry for entry in state["selections"] if entry["name"] == repository
                          and not any(repo["name"] == repository for repo in state["repos"])), None)
        if not selection:
            raise WorkflowError("请选择当前会话尚未准备成功的仓库。")
        with repository_lock(selection["path"]):
            preview = self._workspace_reuse_snapshot(selection)
            token = sha256(dumps(preview, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            state["workspace_reuse_request"] = {**preview, "token": token}
            self.save()

    def cancel_workspace_reuse(self):
        self._require_workspace_reuse()
        self.state["workspace_reuse_request"] = None
        self.save()

    def confirm_workspace_reuse(self, token):
        state = self._require_workspace_reuse()
        saved = state.get("workspace_reuse_request")
        if not saved or token != saved["token"]:
            raise WorkflowError("请重新预览外部工作区后明确确认复用。")
        selection = next((entry for entry in state["selections"] if entry == saved["selection"]
                          and not any(repo["name"] == saved["name"] for repo in state["repos"])), None)
        if not selection:
            raise WorkflowError("仓库选择已经变化，请重新预览外部工作区。")
        with repository_lock(selection["path"]):
            preview = self._workspace_reuse_snapshot(selection)
            current = sha256(dumps(preview, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if token != current:
                raise WorkflowError("工作区、HEAD 或文件在预览后发生变化，请重新预览并明确确认。")
            base = "refs/heads/" + preview["branch"]
            record = {"name": selection["name"], "role": selection["role"],
                      "source": str(Path(selection["path"]).resolve()), "path": preview["path"],
                      "branch": preview["branch"], "base": base, "baseline": preview["head"], "create": False,
                      "workspace_owned": False, "workspace_mode": "reused",
                      "preserved_paths": preview["snapshot"]["paths"]}
            selection.update(base=base, create=False, track=False)
            state["repos"].append(record)
            state.setdefault("workspace_reuses", []).append({**deepcopy(saved), "time": timestamp()})
            state.update(workspace_reuse_request=None, error="", stage="已暂停", pause_requested=True)
            checkout = state.get("checkout_request")
            if checkout and checkout["selection"]["name"] == record["name"]:
                state.pop("checkout_request")
            self.note("用户", f"人工确认复用外部工作区：{record['name']} · {record['path']}；"
                      f"原目标分支 {record['branch']}；本轮初始基线 {record['baseline']}。"
                      "该提交及之前的代码不纳入本轮差异；已有未提交修改按本机保留内容排除，删除会话保留此目录。")
        if state.get("followup"):
            self._prepare_followup()
        else:
            state["stage"] = "准备工作区失败"
            self.prepare_and_plan(state["selections"], state["supplement"])
