"""编译检查的持久暂停点、下载授权及有限次数代码修复。"""

from uuid import uuid4

from workflow.compile_checks import CompileRunner, backend_modules, dependency_hash, redact, snapshot_hash
from workflow.gitops import manifest, repository_lock
from workflow.settings import WorkflowError
from workflow.store import timestamp


class CompileFlow:
    def _compile_manifest(self):
        return manifest([repo for repo in self.state["repos"] if repo["role"] == "backend"])

    def _before_review(self):
        state = self.state
        if (state["phase"] == "implement"
                and any(entry["delivery_round"] == state.get("delivery_round", 1) for entry in state.get("artifacts", []))
                and not manifest(state["repos"])):
            state.update(phase="complete", stage="已完成（未审查）", uncommitted_manifest={})
            self.save()
            self.note("系统", "本轮仅生成成果文件，无业务代码修改，未执行编译检查、自动 Review 或本地提交。")
            return
        if self.state.get("compile_check", {}).get("enabled", False):
            self._begin_compile()
        else:
            self._after_compile()

    def _after_compile(self):
        state = self.state
        if state.get("followup") and "review_enabled" in state:
            changes = manifest(state["repos"])
            if not state.get("review_requested") and (not state["review_enabled"] or not changes):
                state["uncommitted_manifest"] = changes
                state.update(phase="complete", stage="已完成（未审查）")
                self.save()
                self.note("系统", ("本轮有代码修改，已保留在工作区。" if changes else "本轮无代码修改。") +
                          ("Review 开关已关闭。" if not state["review_enabled"] else "无代码修改，跳过 Review。") +
                          "未执行自动 Review 和本地提交，未 push，未修改禅道状态。")
                return
        self._begin_review()

    def _begin_compile(self):
        state = self.state
        changes = self._compile_manifest()
        batch = {"id": uuid4().hex, "delivery_round": state.get("delivery_round", 1),
                 "created_at": timestamp(), "manifest": changes, "fix_round": 0,
                 "limit": state["max_fix_rounds"], "status": "running", "repositories": [], "decisions": []}
        for repository in state["repos"]:
            if repository["role"] == "backend" and any(key.startswith(repository["name"] + "/") for key in changes):
                batch["repositories"].append({"name": repository["name"], "status": "pending", "modules": [],
                                               "summary": "等待检查。", "diagnostics": []})
        state.setdefault("compile_checks", []).append(batch)
        state.update(phase="compile", stage="编译检查中")
        self.save()
        self._run_compile()

    def _compile_batch(self):
        batches = self.state.get("compile_checks", [])
        if not batches or batches[-1]["delivery_round"] != self.state.get("delivery_round", 1):
            raise WorkflowError("当前交付没有可继续的编译检查。")
        return batches[-1]

    def _run_compile(self, download_name=None):
        state = self.state
        batch = self._compile_batch()
        batch["status"] = "running"
        changes = self._compile_manifest()
        recorded = {entry["name"] for entry in batch["repositories"]}
        for repository in state["repos"]:
            if (repository["role"] == "backend" and repository["name"] not in recorded
                    and any(key.startswith(repository["name"] + "/") for key in changes)):
                batch["repositories"].append({"name": repository["name"], "status": "pending", "modules": [],
                                               "summary": "新增任务改动，等待检查。", "diagnostics": []})
        state.update(phase="compile", stage="编译检查中", error="")
        self.compile_runner = CompileRunner()
        self.save()
        for entry in batch["repositories"]:
            repository = next(repo for repo in state["repos"] if repo["name"] == entry["name"])
            if repository["role"] != "backend":
                entry.update(status="excluded", summary="前端不再执行编译检查，继续参与代码 Review。", diagnostics=[])
                continue
            if self.closed or state.get("pause_requested"):
                state["stage"] = "已暂停"
                self.save()
                return
            if entry["status"] in {"passed", "skipped"}:
                continue
            lock = repository_lock(repository["path"])
            while not lock.acquire(timeout=0.2):
                if self.closed or state.get("pause_requested"):
                    state["stage"] = "已暂停"
                    self.save()
                    return
            try:
                if self.closed or state.get("pause_requested"):
                    state["stage"] = "已暂停"
                    self.save()
                    return
                if download_name == entry["name"] and (
                        entry.get("source_hash") != snapshot_hash(repository)
                        or entry.get("dependency_hash") != dependency_hash(repository)):
                    raise WorkflowError("下载开始前代码或依赖配置发生变化，原授权已失效，请重新检查。")
                entry.update(source_hash=snapshot_hash(repository), dependency_hash=dependency_hash(repository),
                             status="running", summary="正在进行编译检查。", diagnostics=[], failure_reasons=[], analysis="")
                self.save()
                entry["modules"] = backend_modules(repository)
                result = self.compile_runner.check(repository, entry["modules"], entry["name"] == download_name)
                entry.update(result)
                if download_name == entry["name"] and entry["status"] == "dependencies":
                    entry.update(status="blocked", summary="允许下载后的检查仍缺少依赖，请人工处理；未自动重试。")
            except WorkflowError as error:
                reason = redact(str(error))[:600]
                entry.update(status="blocked", summary=reason, diagnostics=[], failure_reasons=[reason])
            except (OSError, ValueError, TypeError) as error:
                entry.update(status="blocked", summary="检查工具或项目配置读取失败，请根据下方原因处理。", diagnostics=[],
                             failure_reasons=[redact(str(error))[:600]])
            finally:
                lock.release()
            if self.closed or state.get("pause_requested"):
                state["stage"] = "已暂停"
                self.save()
                return
            self.save()
            if entry["status"] == "code_error" and batch["fix_round"] < batch["limit"]:
                self._repair_compile(entry)
                return
            if entry["status"] != "passed":
                batch["status"] = "waiting"
                state["stage"] = "等待编译选择"
                self.save()
                self.note("系统", f"{entry['name']}：{entry['summary']}" +
                          ("\n失败原因：" + "；".join(entry["failure_reasons"]) if entry.get("failure_reasons") else "") +
                          ("已达到本次编译自动修复上限。" if entry["status"] == "code_error" else ""))
                return
        # 其他仓库等待期间发生变化，也不能沿用之前的通过或跳过结果。
        for entry in batch["repositories"]:
            repository = next(repo for repo in state["repos"] if repo["name"] == entry["name"])
            if repository["role"] != "backend":
                continue
            if (entry["source_hash"] != snapshot_hash(repository)
                    or entry["dependency_hash"] != dependency_hash(repository)):
                entry.update(status="blocked", summary="检查后代码发生变化，请重新检查。", diagnostics=[])
                batch["status"] = "waiting"
                state["stage"] = "等待编译选择"
                self.save()
                return
        batch.update(status="complete", manifest=self._compile_manifest(), completed_at=timestamp())
        self.save()
        results = [f"{entry['name']}（{'通过' if entry['status'] == 'passed' else '人工跳过'}）"
                   for entry in batch["repositories"] if entry["status"] != "excluded"]
        self.note("系统", "本轮后端编译检查已处理完成：" + "；".join(results) if results else
                  "本轮没有后端任务改动，跳过后端编译检查；前端仍按原流程参与 Review。")
        self._after_compile()

    def _repair_compile(self, entry):
        batch = self._compile_batch()
        batch["fix_round"] += 1
        batch["repair_repository"] = entry["name"]
        batch["repair_paths"] = sorted({diagnostic["path"] for diagnostic in entry["diagnostics"]})
        batch["status"] = "repairing"
        self.state["phase"] = "compile_fix"
        self._start_turn("控制器授权修复本轮新增代码引起的编译错误，只修改 compile_checks 最新记录中 "
                         "repair_repository 的 repair_paths，保持需求范围。不得修改依赖、环境或原有问题，不执行命令。"
                         "先对照 baseline_file 和 changes 核实诊断，证据不足或需要扩大范围时调用 workflow.ask_user 后立即结束回合，"
                         "不得猜测修复。脱敏诊断见上下文 compile_checks，完成后由控制器重新检查。")

    def _compile_repaired(self):
        batch = self._compile_batch()
        batch.pop("repair_repository", None)
        batch.pop("repair_paths", None)
        batch["manifest"] = self._compile_manifest()
        # 修复后重新检查有任务改动的后端；下载授权与跳过决定均不跨代码版本。
        for entry in batch["repositories"]:
            entry.update(status="pending", diagnostics=[], summary="修复后等待重新检查。")
        self._run_compile()

    def _analyze_compile(self, entry):
        batch = self._compile_batch()
        batch.update(status="analyzing", analysis_repository=entry["name"])
        self.state["phase"] = "compile_analysis"
        self._start_turn("用户选择分析后端编译失败原因。本回合只读，不修改文件、不执行任何命令、不安装或下载依赖。"
                         "读取 task_context 最新 compile_checks 的 analysis_repository、failure_reasons、diagnostics，"
                         "结合 changes、baseline_file 和当前文件定位原因，说明缺少的具体类、方法、依赖、插件或环境条件及处理办法。"
                         "区分本轮新增错误、原有问题与证据不足，不得猜测或把缺少符号直接当作需要下载依赖。"
                         "历史记录没有具体原因时必须说明无法还原当时失败，建议用户重新检查以取得诊断。"
                         "需要人工提供资料时调用 workflow.ask_user 后立即结束回合；其他情况用简体中文给出分析结论。"
                         "分析结束后仍等待人工选择，不自动进入 Review 或提交。")

    def _compile_analyzed(self, text):
        batch = self._compile_batch()
        entry = next(entry for entry in batch["repositories"] if entry["name"] == batch["analysis_repository"])
        repository = next(repo for repo in self.state["repos"] if repo["name"] == entry["name"])
        if (entry["source_hash"] != snapshot_hash(repository)
                or entry["dependency_hash"] != dependency_hash(repository)):
            batch.pop("analysis_repository")
            batch["status"] = "waiting"
            self.state.update(phase="compile", stage="等待编译选择")
            self.save()
            raise WorkflowError("分析期间代码或依赖配置发生变化，请重新检查后再处理。")
        entry["analysis"] = redact(text).strip()[:6000] or "本次未返回分析结论，请重新分析或重新检查。"
        self.note("Codex", entry["analysis"])
        batch.pop("analysis_repository")
        batch["status"] = "waiting"
        self.state.update(phase="compile", stage="等待编译选择")
        self.save()

    def compile_decision(self, batch_id, name, decision):
        state = self.require_state("等待编译选择", "已暂停", "已停止")
        batch = self._compile_batch()
        if state["phase"] != "compile" or batch["id"] != batch_id or batch["status"] == "complete":
            raise WorkflowError("编译检查操作已过期，请查看最新内容。")
        entry = next((entry for entry in batch["repositories"] if entry["name"] == name), None)
        if not entry or entry["status"] in {"passed", "skipped"}:
            raise WorkflowError("请选择当前待处理的编译仓库。")
        if decision not in {"download", "retry", "skip", "pause", "add_round", "fix", "analyze"}:
            raise WorkflowError("编译检查选择无效。")
        repository = next(repo for repo in state["repos"] if repo["name"] == name)
        if repository["role"] != "backend":
            state["pause_requested"] = False
            self._run_compile()
            return
        if decision in {"download", "skip", "add_round", "fix", "analyze"}:
            if (entry.get("source_hash") != snapshot_hash(repository)
                    or entry.get("dependency_hash") != dependency_hash(repository)):
                raise WorkflowError("代码或依赖配置已变化，请重新检查后再选择。")
        if decision == "download" and entry["status"] != "dependencies":
            raise WorkflowError("当前失败不是缺少依赖，不能授权下载。")
        if decision == "add_round" and (entry["status"] != "code_error" or batch["fix_round"] < batch["limit"]):
            raise WorkflowError("只有代码修复达到上限时才能追加一轮。")
        if decision == "fix" and (entry["status"] != "code_error" or not entry["diagnostics"]
                                  or batch["fix_round"] >= batch["limit"]):
            raise WorkflowError("只有已明确诊断的本轮代码错误且未达修复上限时才能修复。")
        if decision == "analyze" and entry["status"] not in {"blocked", "dependencies", "code_error"}:
            raise WorkflowError("请选择当前失败的编译仓库进行分析。")
        batch["decisions"].append({"repository": name, "decision": decision, "time": timestamp(),
                                    "source_hash": entry.get("source_hash"), "dependency_hash": entry.get("dependency_hash")})
        if decision == "pause":
            self.pause()
            return
        state["pause_requested"] = False
        if decision == "analyze":
            self._analyze_compile(entry)
            return
        if decision == "fix":
            self._repair_compile(entry)
            return
        if decision == "add_round":
            batch["limit"] += 1
            self._repair_compile(entry)
            return
        if decision == "retry":
            # 显式重查时，任何已失效的其他仓库结果也作废。
            for other in batch["repositories"]:
                repo = next(repo for repo in state["repos"] if repo["name"] == other["name"])
                if repo["role"] != "backend":
                    continue
                if (other.get("source_hash") != snapshot_hash(repo)
                        or other.get("dependency_hash") != dependency_hash(repo) or other is entry):
                    other.update(status="pending", summary="等待重新检查。", diagnostics=[])
            batch["manifest"] = self._compile_manifest()
        elif decision == "skip":
            entry.update(status="skipped", summary="人工明确跳过本仓库本轮检查。", diagnostics=[])
        self.save()
        self._run_compile(name if decision == "download" else None)

    def _guard_compile(self):
        if not self.state.get("compile_check", {}).get("enabled", False):
            return
        batch = self._compile_batch()
        names = {repo["name"] for repo in self.state["repos"] if repo["role"] == "backend"}
        expected = {key: value for key, value in batch["manifest"].items()
                    if any(key.startswith(name + "/") for name in names)}
        if batch["status"] != "complete" or expected != self._compile_manifest():
            raise WorkflowError("当前代码尚未完成编译检查或明确跳过，请先处理编译检查。")
        for entry in batch["repositories"]:
            repository = next(repo for repo in self.state["repos"] if repo["name"] == entry["name"])
            if repository["role"] != "backend":
                continue
            if (entry["status"] not in {"passed", "skipped"} or entry["source_hash"] != snapshot_hash(repository)
                    or entry["dependency_hash"] != dependency_hash(repository)):
                raise WorkflowError("编译检查结果已失效，请重新检查。")

    def _resume_compile(self):
        batch = self._compile_batch()
        names = {repo["name"] for repo in self.state["repos"] if repo["role"] == "backend"}
        pending = next((entry for entry in batch["repositories"] if entry["status"] not in {"passed", "skipped", "excluded"}), None)
        # 下载授权、失败、人工问题均不能被普通“恢复”操作隐式接受。
        if batch["status"] == "waiting" and pending and pending["name"] in names:
            self.state.update(phase="compile", stage="等待编译选择")
            self.save()
        else:
            self._run_compile()
