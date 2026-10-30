"""模型资料视图；原始接口快照只保留在控制器中。"""

from hashlib import sha256
from json import dumps
from urllib.parse import parse_qs, urlparse

from workflow.zentao import identifier, parse_description


def normalized_sources(sources):
    result = []
    for saved in sources:
        source = dict(saved)
        raw = source.pop("raw", {})
        # 旧资料的附件归属只存在原始记录中；先恢复关联再移除原文。
        if "material_ids" not in source:
            files = raw.get("files", [])
            if isinstance(files, dict):
                files = files.values()
            material_ids = [identifier(file.get("id")) for file in files]
            for field in ("spec", "verify", "desc", "steps", "bugSteps"):
                for url in parse_description(raw.get(field)).images:
                    query = parse_qs(urlparse(url).query)
                    if "fileID" in query:
                        material_ids.append(identifier(query["fileID"][0]))
            source["material_ids"] = list(dict.fromkeys(material_ids))
        result.append(source)
    return result


def requirement_context(state):
    snapshot = state["snapshot"]
    return {**snapshot,
            "sources": normalized_sources(snapshot.get("sources", [])),
            "materials": snapshot.get("materials", []), "blockers": snapshot.get("blockers", []),
            "feishu_links": snapshot.get("feishu_links", [])}


def context_hash(value):
    return sha256(dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
