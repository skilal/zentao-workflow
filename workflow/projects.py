"""本地项目分区与会话执行项目；不修改禅道工作项。"""

from copy import deepcopy
from json import dumps, loads


def session_project(state):
    project = state.get("project") if "project" in state else state.get("snapshot", {}).get("project")
    return deepcopy(project) if project else {"id": 0, "name": "", "candidates": []}


def project_locked(state):
    return bool(state.get("plan_started_at") or state["stage"] not in {"资料已读取", "资料读取失败"}
                or "branch_resolution" in state or any(state.get(key) for key in
                    ("repos", "selections", "thread_id", "plan", "plan_hash", "plan_round", "reviews", "commits")))


def project_move_error(runtime):
    state = runtime.engine.state
    if project_locked(state):
        return "已开始需求分析与计划，不能更换项目"
    if runtime.deleting or runtime.engine.closed or state.get("cleanup"):
        return "会话正在清理，不能更换项目"
    if runtime.busy or runtime.engine.working() or getattr(runtime, "uploads", 0):
        return "当前操作尚未结束，不能更换项目"
    return ""


def assign_project(state, project):
    chosen = {"id": project["id"], "name": project["name"],
              "candidates": [{"id": project["id"], "name": project["name"]}] if project["id"] else [],
              "confirmed": bool(project["id"])}
    state["project_override"] = deepcopy(chosen)
    state["project"] = deepcopy(chosen)
    if "snapshot" in state:
        state["snapshot"]["project"] = deepcopy(chosen)


class ProjectPartitions:
    def __init__(self, path):
        self.path = path
        self.sites = loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def add(self, site, project):
        sites = deepcopy(self.sites)
        sites.setdefault(site, {})[str(project["id"])] = project
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(dumps(sites, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.sites = sites

    def list(self, site, states, items):
        projects = {key: {**project, "manual": True} for key, project in self.sites.get(site, {}).items()}
        for state in sorted(states, key=lambda value: value["updated_at"], reverse=True):
            project = session_project(state)
            if project["id"] > 0:
                projects.setdefault(str(project["id"]), {"id": project["id"], "name": project["name"], "manual": False})
        for item in items:
            if item.get("project_id", 0) > 0:
                projects.setdefault(str(item["project_id"]), {"id": item["project_id"],
                    "name": item.get("project_name", ""), "manual": False})
        return list(projects.values())
