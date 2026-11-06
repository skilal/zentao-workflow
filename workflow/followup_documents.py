"""续聊 PDF、DOCX 和文本资料的准备、分页读取及图片访问。"""

from base64 import b64encode
from json import dumps, loads
from mimetypes import guess_type
from pathlib import Path
from posixpath import join, normpath
from xml.etree.ElementTree import ParseError, fromstring
from zipfile import BadZipFile, ZipFile

from workflow.artifacts import checked_path, content_page, page_number
from workflow.pdf_materials import read_pdf
from workflow.settings import WorkflowError
from workflow.zentao import extract_text


TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".log", ".sql"}
DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def attachment_file(store, state, entry):
    directory = store.directory(state["id"]) / "attachments" / "followup"
    try:
        relative = Path(entry["path"]).relative_to(directory)
    except ValueError as error:
        raise WorkflowError("续聊附件路径越界。") from error
    path = checked_path(directory, relative)
    if not path.is_file():
        raise WorkflowError("续聊附件文件已不存在。")
    return path


def document_view(entry):
    return {**{key: entry[key] for key in ("id", "name", "size", "kind", "readable", "issue", "total_lines")},
            "page_count": len(entry.get("pdf_pages", [])),
            "images": [{key: image[key] for key in ("id", "name", "size")} for image in entry.get("images", [])]}


def image_format(extension, data):
    signatures = {".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                  ".jpg": data.startswith(b"\xff\xd8\xff"), ".jpeg": data.startswith(b"\xff\xd8\xff"),
                  ".gif": data.startswith((b"GIF87a", b"GIF89a")), ".bmp": data.startswith(b"BM"),
                  ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP"}
    return signatures.get(extension, False)


def read_docx(path, maximum):
    blocks, images, issues, image_cache = [], [], [], {}
    image_bytes = 0
    with ZipFile(path) as archive:
        main = archive.getinfo("word/document.xml")
        rel_name = "word/_rels/document.xml.rels"
        rel_info = archive.getinfo(rel_name) if rel_name in archive.namelist() else None
        if main.file_size + (rel_info.file_size if rel_info else 0) > 16 * 1024 * 1024:
            raise ValueError("Word 正文 XML 超过 16 MB 读取上限")
        document = fromstring(archive.read(main))
        relationships = {node.get("Id"): node for node in fromstring(archive.read(rel_info))} if rel_info else {}
        body = document.find(W + "body")
        if body is None:
            raise ValueError("Word 文档缺少正文")

        def embedded(identifier):
            nonlocal image_bytes
            if identifier in image_cache:
                return image_cache[identifier]
            image_id = f"image-{len(image_cache) + 1}"
            image_cache[identifier] = (image_id, False)
            relation = relationships.get(identifier)
            if relation is None or relation.get("TargetMode") == "External":
                issues.append(f"{image_id} 为外链图片或缺少图片关系，未读取。")
                return image_id, False
            target = relation.get("Target", "").replace("\\", "/")
            member_name = normpath(target.lstrip("/") if target.startswith("/") else join("word", target))
            extension = Path(member_name).suffix.lower()
            if member_name.startswith("../") or member_name not in archive.namelist():
                issues.append(f"{image_id} 的图片部件无效，未读取。")
                return image_id, False
            info = archive.getinfo(member_name)
            if image_bytes + info.file_size > maximum:
                raise ValueError("Word 图片解压总大小超过附件大小上限")
            image_bytes += info.file_size
            data = archive.read(info)
            if not image_format(extension, data):
                issues.append(f"{image_id}（{extension or '无扩展名'}）图片格式不支持或内容无效，未读取。")
                return image_id, False
            destination = path.parent / (image_id + extension)
            destination.write_bytes(data)
            images.append({"id": image_id, "name": Path(member_name).name, "size": len(data),
                           "path": str(destination), "image": True, "content_type": guess_type(destination.name)[0]})
            image_cache[identifier] = (image_id, True)
            return image_id, True

        def inline(element):
            pieces, references = [], []
            for node in element.iter():
                local = node.tag.rsplit("}", 1)[-1]
                if node.tag == W + "p" and pieces:
                    pieces.append("\n")
                elif node.tag == W + "t":
                    pieces.append(node.text or "")
                elif node.tag == W + "tab":
                    pieces.append("\t")
                elif node.tag in {W + "br", W + "cr"}:
                    pieces.append("\n")
                elif local in {"blip", "imagedata"}:
                    identifier = node.get(R + "embed") or node.get(R + "id") or node.get(R + "link")
                    image_id, readable = embedded(identifier)
                    pieces.append(f"[图片 {image_id}{'' if readable else '：未读取'}]")
                    if readable:
                        references.append({"type": "image", "image_id": image_id})
            return "".join(pieces).strip(), references

        def append_content(element):
            if element.tag == W + "p":
                text, references = inline(element)
                style = element.find(W + "pPr/" + W + "pStyle")
                value = style.get(W + "val", "") if style is not None else ""
                if value.startswith("Heading") and value[-1:] in "123456":
                    value = "Heading " + value[-1]
                blocks.append({"type": "paragraph", "text": text, "style": value})
                blocks.extend(references)
            elif element.tag == W + "tbl":
                rows, references = [], []
                for row in element.findall(W + "tr"):
                    cells = []
                    for cell in row.findall(W + "tc"):
                        text, pictures = inline(cell)
                        cells.append(text)
                        references.extend(pictures)
                    rows.append(cells)
                # 大表按行分片，避免预览一次返回整张表。
                blocks.extend({"type": "table", "rows": rows[index:index + 20]} for index in range(0, len(rows), 20))
                blocks.extend(references)
            elif element.tag != W + "sectPr":
                for child in element:
                    append_content(child)

        for element in body:
            append_content(element)
        names = archive.namelist()
        if any(name.startswith(("word/charts/", "word/diagrams/", "word/embeddings/")) for name in names):
            issues.append("文档包含图表、SmartArt 或嵌入对象，这些内容未读取。")
        if document.find(".//" + W + "altChunk") is not None:
            issues.append("文档包含其他格式导入的内容块，这些内容未读取。")
        if document.find(".//" + W + "ins") is not None or document.find(".//" + W + "del") is not None:
            issues.append("文档包含修订，仅读取当前正文文字，修订记录未读取。")
        if any(name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes", "word/comments")) for name in names):
            issues.append("页眉页脚、脚注尾注及批注未读取。")
    text = "\n".join(block["text"] if block["type"] == "paragraph" else
                     "\n".join(" | ".join(row) for row in block["rows"]) if block["type"] == "table" else
                     f"[图片 {block['image_id']}]" for block in blocks)
    if len(text) > 250000:
        raise ValueError("Word 正文超过 25 万字符读取上限")
    return text, blocks, images, list(dict.fromkeys(issues))


def prepare_document(entry, settings):
    path = Path(entry["path"])
    extension = path.suffix.lower()
    entry.update(kind="pdf" if extension == ".pdf" else "docx" if extension == ".docx" else "text",
                 readable=False, issue="", total_lines=0, images=[], pdf_pages=[])
    try:
        if extension == ".pdf":
            material = {"path": str(path), "title": entry["name"]}
            read_pdf(material, settings["pdf"]["renderer"])
            entry["pdf_pages"] = material["pdf_pages"]
        else:
            if extension == ".docx":
                text, blocks, images, issues = read_docx(path, settings["zentao"]["max_attachment_mb"] * 1024 * 1024)
                (path.parent / "blocks.json").write_text(dumps(blocks, ensure_ascii=False), encoding="utf-8")
                entry.update(images=images, issue="\n".join(issues))
            else:
                text = extract_text(path)
            (path.parent / "text.txt").write_text(text, encoding="utf-8", newline="")
            entry["total_lines"] = len(text.splitlines())
        entry["readable"] = True
    except (WorkflowError, ValueError, OSError, KeyError, BadZipFile, ParseError, RuntimeError, NotImplementedError) as error:
        entry.update(readable=False, issue=f"原文件已保存，内容未读取：{error}", images=[], pdf_pages=[])
    entry["page_count"] = len(entry["pdf_pages"])


def attachment_image(store, state, entry, arguments):
    path = attachment_file(store, state, entry)
    if entry["kind"] == "pdf":
        page = arguments.get("page", 1)
        if type(page) is not int or not 1 <= page <= len(entry["pdf_pages"]):
            raise WorkflowError("PDF 页码不在当前附件中。")
        image = entry["pdf_pages"][page - 1]
        content_type = "image/png"
    else:
        image = next((image for image in entry.get("images", []) if image["id"] == arguments.get("image_id")), None)
        if image is None:
            raise WorkflowError("图片编号不在当前 Word 附件中。")
        content_type = image["content_type"]
    try:
        relative = Path(image["path"]).relative_to(path.parent)
    except ValueError as error:
        raise WorkflowError("附件图片路径越界。") from error
    return checked_path(path.parent, relative), content_type


def read_document(store, state, entry, arguments):
    metadata = document_view(entry)
    if arguments.get("member_id") is not None:
        raise WorkflowError("只有 ZIP 附件使用 member_id。")
    if arguments.get("page") is not None and arguments.get("image_id") is not None:
        raise WorkflowError("请分别读取 PDF 页面或 Word 图片。")
    if not entry["readable"]:
        return {"attachment": metadata, "readable": False, "issue": entry["issue"]}
    if arguments.get("page") is not None or arguments.get("image_id") is not None:
        if (entry["kind"] == "pdf") != (arguments.get("page") is not None):
            raise WorkflowError("PDF 使用 page，Word 图片使用 image_id。")
        path, content_type = attachment_image(store, state, entry, arguments)
        return {"content": [{"type": "text", "text": dumps(metadata, ensure_ascii=False)},
                            {"type": "image", "mimeType": content_type, "data": b64encode(path.read_bytes()).decode("ascii")}]}
    if entry["kind"] == "pdf":
        return {"attachment": metadata, "readable": True, "issue": entry["issue"],
                "reading_mode": "page_images", "note": "PDF 按页阅读图片；可用 page 从 1 开始读取，不将页图准备成功当作已阅读。"}
    start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
    if type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 500:
        raise WorkflowError("start_line 必须大于等于 1，line_count 必须为 1 至 500。")
    path = attachment_file(store, state, entry)
    lines = checked_path(path.parent, "text.txt").read_text(encoding="utf-8").splitlines()
    end = min(len(lines), start - 1 + count)
    return {"attachment": metadata, "readable": True, "issue": entry["issue"], "total_lines": len(lines),
            "start_line": start, "next_line": end + 1 if end < len(lines) else None,
            "text": "\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end))}


def preview_document(store, state, entry, query):
    if not entry["readable"]:
        return {"kind": "unsupported", "issue": entry["issue"]}
    page = page_number(query)
    path = attachment_file(store, state, entry)
    if entry["kind"] == "pdf":
        if page > len(entry["pdf_pages"]):
            raise WorkflowError("PDF 预览页码超出范围。")
        return {"kind": "pdf", "page": page, "total": len(entry["pdf_pages"]), "has_next": page < len(entry["pdf_pages"])}
    if entry["kind"] == "docx":
        blocks = loads(checked_path(path.parent, "blocks.json").read_text(encoding="utf-8"))
        return {"kind": "docx", "issue": entry["issue"], **content_page(blocks, page)}
    lines = checked_path(path.parent, "text.txt").read_text(encoding="utf-8").splitlines()
    return {"kind": "markdown" if path.suffix.lower() == ".md" else "text", **content_page(lines, page)}


def current_attachment_images(store, state):
    identifiers = {entry["id"] for entry in (state.get("followup") or {}).get("attachments", [])}
    images = []
    for entry in state.get("followup_attachments", []):
        if entry["id"] not in identifiers:
            continue
        for page, image in enumerate(entry.get("pdf_pages", []), 1):
            path, _ = attachment_image(store, state, entry, {"page": page})
            images.append({**image, "path": str(path), "attachment_id": entry["id"]})
        for image in entry.get("images", []):
            path, _ = attachment_image(store, state, entry, {"image_id": image["id"]})
            images.append({**image, "path": str(path), "attachment_id": entry["id"], "image_id": image["id"]})
    return images
