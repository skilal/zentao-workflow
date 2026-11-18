"""模型资料视图；原始接口快照只保留在控制器中。"""

from hashlib import sha256
from json import dumps

from workflow.zentao import description_content, identifier, image_reference


def normalized_sources(sources):
    result = []
    for saved in sources:
        source = dict(saved)
        raw = source.pop("raw", {})
        if "image_locations" not in source:
            content, locations, _ = description_content(raw)
            if locations:
                source.update(content=content, image_locations=locations)
        # 旧资料的附件归属只存在原始记录中；先恢复关联再移除原文。
        if "material_ids" not in source:
            files = raw.get("files", [])
            if isinstance(files, dict):
                files = files.values()
            material_ids = [identifier(file.get("id")) for file in files]
            _, _, parsers = description_content(raw)
            for parser in parsers:
                for url in parser.images:
                    file_id, _ = image_reference(url)
                    if file_id is not None:
                        material_ids.append(file_id)
            source["material_ids"] = list(dict.fromkeys(material_ids))
        result.append(source)
    return result


def requirement_context(state):
    snapshot = state["snapshot"]
    materials = snapshot.get("materials", [])
    by_id = {entry["file_id"]: entry for entry in materials if "file_id" in entry}
    sources = normalized_sources(snapshot.get("sources", []))
    for source in sources:
        if not source.get("image_locations"):
            continue
        source["image_location_note"] = ("正文和评论图片标记保留原文位置；结合所在字段、评论作者、时间、章节和前后文字判断图片含义，"
                                         "不要将复现步骤、实际结果和期望结果中的截图混用。"
                                         "每次出现均为独立引用；按 path 对应 image_references，图片内容可能已去重。"
                                         "没有 path 或附件存在 issue 时，不代表图片已读取。")
        locations = []
        for saved in source["image_locations"]:
            location = dict(saved)
            material = by_id.get(location["file_id"], {})
            if material.get("image") and material.get("path"):
                location["path"] = material["path"]
            if material.get("issue"):
                location["issue"] = material["issue"]
            locations.append(location)
        source["image_locations"] = locations
    return {**snapshot,
            "sources": sources,
            "materials": materials, "blockers": snapshot.get("blockers", []),
            "feishu_links": snapshot.get("feishu_links", [])}


def context_hash(value):
    return sha256(dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
