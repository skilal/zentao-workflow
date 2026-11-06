"""现有流程检查中模拟模型返回的逐仓库提交标题。"""


def commit_messages(state):
    return [{"repository": repo["name"], "type": "fix", "scope": "业务",
             "summary": state.get("commit_message") or "完善业务处理"}
            for repo in state["repos"] if any(key.startswith(repo["name"] + "/") for key in state["review_manifest"])]
