"""会话成果文件：独立保存、受限发布和按内容预览，不改写业务仓库。"""

from hashlib import sha256
from datetime import datetime, timezone
from importlib import import_module, invalidate_caches
from json import dumps, loads
from mimetypes import guess_type
from pathlib import Path, PureWindowsPath
from re import fullmatch
from shutil import rmtree
import site
import sys
from urllib.parse import urlencode
from uuid import uuid4
from xml.sax.saxutils import escape

from workflow.gitops import safe_file
from workflow.pdf_materials import read_pdf
from workflow.settings import WorkflowError


ARTIFACT_STAGES = {"计划中", "实施中", "修复中", "续聊处理中"}
ARTIFACT_TOOLS = {"create_artifact", "publish_artifact"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".sql", ".json", ".csv", ".tsv", ".xml", ".yaml", ".yml",
                   ".log", ".py", ".js", ".ts", ".java", ".css", ".html", ".sh", ".ps1", ".properties"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MIME_TYPES = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".pdf": "application/pdf"}
PAGE_SIZE = 100


def dependency(module, package):
    # pip 首次安装到用户目录时，已启动的 Python 不会自动把新目录加入 sys.path。
    # 只刷新当前解释器自身的用户依赖目录，不切换解释器或搜索其他环境。
    user_site = site.getusersitepackages()
    if site.ENABLE_USER_SITE and Path(user_site).is_dir() and user_site not in sys.path:
        site.addsitedir(user_site)
        invalidate_caches()
    try:
        return import_module(module)
    except ImportError as error:
        if isinstance(error, ModuleNotFoundError) and error.name == module.split(".")[0]:
            raise WorkflowError(f"成果文件功能缺少依赖 {package}；当前解释器：{sys.executable}。"
                                "请使用该解释器按 README 安装 requirements-artifacts.txt 后重试；未自动安装。") from error
        raise WorkflowError(f"成果文件依赖 {package} 加载失败：{error}；当前解释器：{sys.executable}。"
                            "请处理实际导入错误；未自动重装或切换环境。") from error


def artifact_authorized(state):
    return (state.get("stage") in ARTIFACT_STAGES and bool(state.get("thread_id"))
            and bool(state.get("turn_id")) and not state.get("pending") and not state.get("pause_requested"))


def checked_path(root, relative):
    """在文件入口校验普通路径，包含 Windows junction 等重解析点。"""
    root = Path(root)
    candidate = root / relative
    current = root
    for part in [None, *Path(relative).parts]:
        if part is not None:
            current /= part
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise WorkflowError("成果文件路径不能包含链接或重解析点。")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise WorkflowError("成果文件路径越界。")
    return resolved


def valid_name(name):
    if (not isinstance(name, str) or not name or len(name) > 180 or name in {".", ".."}
            or any(character in name for character in '<>:"/\\|?*')
            or any(ord(character) < 32 for character in name) or name.endswith((".", " "))
            or PureWindowsPath(name).is_reserved()):
        raise WorkflowError("成果文件名必须是有效的单个文件名，不能包含路径或 Windows 特殊名称。")
    return name


def artifact_urls(session_id, identifier):
    query = urlencode({"session_id": session_id, "artifact_id": identifier})
    return {"download_url": "/api/artifacts/download?" + query, "preview_url": "/api/artifacts/preview?" + query}


def manifests(store, state):
    directory = checked_path(store.directory(state["id"]), "artifacts")
    entries = []
    if directory.exists():
        for manifest in sorted(directory.glob("*/manifest.json")):
            if not fullmatch(r"[0-9a-f]{32}", manifest.parent.name):
                raise WorkflowError("成果文件清单编号无效。")
            path = checked_path(directory, f"{manifest.parent.name}/manifest.json")
            entry = loads(path.read_text(encoding="utf-8"))
            if entry["id"] != manifest.parent.name or entry["session_id"] != state["id"]:
                raise WorkflowError("成果文件清单不属于当前会话。")
            valid_name(entry["name"])
            entries.append({**entry, **artifact_urls(state["id"], entry["id"])})
    return sorted(entries, key=lambda entry: (entry["created_at"], entry["id"]))


def collect_artifacts(store, state):
    turns = {turn["id"]: turn["status"] for turn in state.get("progress_turns", [])}
    messages = {entry["turn_id"]: index for index, entry in enumerate(state.get("history", []))
                if entry.get("turn_id") and entry["role"] == "Codex"}
    previous = {entry["id"]: entry for entry in state.get("artifacts", [])}
    entries = manifests(store, state)
    for entry in entries:
        entry["turn_status"] = turns.get(entry["turn_id"], previous.get(entry["id"], {}).get("turn_status", "interrupted"))
        if entry["turn_id"] in messages:
            entry["history_index"] = messages[entry["turn_id"]]
    changed = entries != state.get("artifacts", [])
    state["artifacts"] = entries
    return changed


def artifact_file(store, state, identifier):
    if not isinstance(identifier, str) or not fullmatch(r"[0-9a-f]{32}", identifier):
        raise WorkflowError("成果文件编号无效。")
    root = checked_path(store.directory(state["id"]), "artifacts")
    manifest = checked_path(root, f"{identifier}/manifest.json")
    if not manifest.is_file():
        raise WorkflowError("成果文件不在当前会话中。")
    entry = loads(manifest.read_text(encoding="utf-8"))
    if entry["id"] != identifier or entry["session_id"] != state["id"]:
        raise WorkflowError("成果文件不在当前会话中。")
    path = checked_path(root, f"{identifier}/file{Path(valid_name(entry['name'])).suffix}")
    if not path.is_file() or path.stat().st_size != entry["size"]:
        raise WorkflowError("成果文件已不存在或大小发生变化。")
    if sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise WorkflowError("成果文件内容发生变化，已停止读取。")
    return entry, path


def document_blocks(content):
    if not isinstance(content, dict) or set(content) != {"blocks"} or not isinstance(content["blocks"], list):
        raise WorkflowError("Word/PDF 内容须为包含 blocks 数组的对象。")
    for block in content["blocks"]:
        if not isinstance(block, dict):
            raise WorkflowError("文档块必须为对象。")
        kind = block.get("type")
        if kind in {"heading", "paragraph"}:
            allowed = {"type", "text", "level"} if kind == "heading" else {"type", "text"}
            if set(block) - allowed or not isinstance(block.get("text"), str):
                raise WorkflowError("标题和段落须提供 text 字符串。")
            if kind == "heading" and (type(block.get("level", 1)) is not int or not 1 <= block.get("level", 1) <= 6):
                raise WorkflowError("标题 level 必须为 1 至 6。")
        elif kind == "list":
            if (set(block) - {"type", "items", "ordered"} or not isinstance(block.get("items"), list)
                    or any(not isinstance(item, str) for item in block["items"])
                    or type(block.get("ordered", False)) is not bool):
                raise WorkflowError("列表须提供字符串 items 数组及可选 ordered 布尔值。")
        elif kind == "table":
            rows = block.get("rows")
            if (set(block) != {"type", "rows"} or not isinstance(rows, list) or not rows
                    or any(not isinstance(row, list) or not row or any(not isinstance(cell, str) for cell in row) for row in rows)
                    or len({len(row) for row in rows}) != 1):
                raise WorkflowError("表格 rows 须为列数一致的非空二维字符串数组。")
        else:
            raise WorkflowError("文档仅支持 heading、paragraph、list、table 内容块。")
    return content["blocks"]


def generate_docx(blocks, destination):
    docx = dependency("docx", "python-docx")
    document = docx.Document()
    document.styles["Normal"].font.name = "宋体"
    for block in blocks:
        kind = block["type"]
        if kind == "heading":
            document.add_heading(block["text"], level=block.get("level", 1))
        elif kind == "paragraph":
            document.add_paragraph(block["text"])
        elif kind == "list":
            for item in block["items"]:
                document.add_paragraph(item, style="List Number" if block.get("ordered") else "List Bullet")
        else:
            table = document.add_table(rows=0, cols=len(block["rows"][0]))
            table.style = "Table Grid"
            for row in block["rows"]:
                for cell, text in zip(table.add_row().cells, row):
                    cell.text = text
    document.save(destination)


def generate_pdf(blocks, destination):
    platypus = dependency("reportlab.platypus", "reportlab")
    fonts = dependency("reportlab.pdfbase.pdfmetrics", "reportlab")
    cidfonts = dependency("reportlab.pdfbase.cidfonts", "reportlab")
    styles = dependency("reportlab.lib.styles", "reportlab")
    fonts.registerFont(cidfonts.UnicodeCIDFont("STSong-Light"))
    body = styles.ParagraphStyle("正文", fontName="STSong-Light", fontSize=11, leading=17, wordWrap="CJK", spaceAfter=8)
    story = []
    for block in blocks:
        kind = block["type"]
        if kind in {"heading", "paragraph"}:
            style = styles.ParagraphStyle("标题", parent=body, fontSize=20 - block.get("level", 1), leading=25) if kind == "heading" else body
            story.append(platypus.Paragraph(escape(block["text"]).replace("\n", "<br/>"), style))
        elif kind == "list":
            for index, item in enumerate(block["items"], 1):
                prefix = f"{index}. " if block.get("ordered") else "• "
                story.append(platypus.Paragraph(escape(prefix + item).replace("\n", "<br/>"), body))
        else:
            rows = [[platypus.Paragraph(escape(cell).replace("\n", "<br/>"), body) for cell in row] for row in block["rows"]]
            table = platypus.Table(rows, colWidths=[451 / len(rows[0])] * len(rows[0]), repeatRows=1)
            table.setStyle(platypus.TableStyle([("GRID", (0, 0), (-1, -1), .5, "#d9d9d9"),
                                              ("BACKGROUND", (0, 0), (-1, 0), "#f0f5ff"),
                                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.extend([table, platypus.Spacer(1, 8)])
    # 即使正文为空也生成一页有效的 PDF。
    platypus.SimpleDocTemplate(str(destination), pagesize=(595, 842)).build(story or [platypus.Spacer(1, 1)])


def generate_xlsx(content, destination):
    if not isinstance(content, dict) or set(content) != {"sheets"} or not isinstance(content["sheets"], list) or not content["sheets"]:
        raise WorkflowError("Excel 内容须包含非空 sheets 数组。")
    names = set()
    for sheet in content["sheets"]:
        if (not isinstance(sheet, dict) or set(sheet) - {"name", "rows", "column_widths"}
                or not isinstance(sheet.get("name"), str) or not sheet["name"] or len(sheet["name"]) > 31
                or any(c in sheet["name"] for c in "[]:*?/\\") or sheet["name"].casefold() in names
                or not isinstance(sheet.get("rows"), list) or len(sheet["rows"]) > 1048576):
            raise WorkflowError("工作表须提供唯一有效名称和 rows 数组。")
        names.add(sheet["name"].casefold())
        widths = sheet.get("column_widths", [])
        if not isinstance(widths, list) or len(widths) > 16384 or any(type(width) not in (int, float) or not 0 < width <= 255 for width in widths):
            raise WorkflowError("column_widths 必须为 0 至 255 之间的列宽数组。")
        for row in sheet["rows"]:
            if not isinstance(row, list) or len(row) > 16384:
                raise WorkflowError("每行须为最多 16384 个单元格的数组。")
            for cell in row:
                value = cell
                if isinstance(cell, dict):
                    if (set(cell) - {"value", "formula", "bold", "number_format"}
                            or ("value" in cell) == ("formula" in cell)
                            or type(cell.get("bold", False)) is not bool
                            or not isinstance(cell.get("number_format", "General"), str)):
                        raise WorkflowError("单元格对象须提供 value 或 formula 之一，可选 bold 和 number_format。")
                    if "formula" in cell and (not isinstance(cell["formula"], str) or not cell["formula"].startswith("=")):
                        raise WorkflowError("formula 必须是以等号开始的公式字符串。")
                    value = cell.get("value", cell.get("formula"))
                if value is not None and type(value) not in (str, int, float, bool):
                    raise WorkflowError("单元格值仅支持字符串、数字、布尔值或 null。")
                if isinstance(value, str) and len(value) > 32767:
                    raise WorkflowError("单元格文字超过 Excel 的 32767 字符上限。")
    excel = dependency("openpyxl", "openpyxl")
    styles = dependency("openpyxl.styles", "openpyxl")
    workbook = excel.Workbook()
    workbook.remove(workbook.active)
    for spec in content["sheets"]:
        sheet = workbook.create_sheet(spec["name"])
        for index, width in enumerate(spec.get("column_widths", []), 1):
            sheet.column_dimensions[excel.utils.get_column_letter(index)].width = width
        for row_index, row in enumerate(spec["rows"], 1):
            for column, value in enumerate(row, 1):
                properties = value if isinstance(value, dict) else {"value": value}
                cell = sheet.cell(row_index, column, properties.get("formula", properties.get("value")))
                if isinstance(properties.get("value"), str):
                    cell.data_type = "s"
                if properties.get("bold"):
                    cell.font = styles.Font(bold=True)
                cell.number_format = properties.get("number_format", "General")
        sheet.freeze_panes = "A2"
    workbook.save(destination)


def create_artifact(store, state, arguments, settings, skip_directories):
    if not artifact_authorized(state):
        raise WorkflowError("当前阶段不能生成或返回成果文件。")
    name = valid_name(arguments.get("name"))
    source = arguments.get("repository")
    if source is not None:
        repository = next((repo for repo in state["repos"] if repo["name"] == source), None)
        if repository is None:
            raise WorkflowError("仓库不属于当前会话的工作区。")
        relative = arguments.get("path")
        path = safe_file(repository, relative)
        # 已有输出目录可以显式发布；代码检索工具仍保持原有跳过范围。
        restricted = skip_directories - {"outputs", "output", "dist", "build", "target", "logs"}
        if any(part.casefold() in restricted for part in Path(relative).parts):
            raise WorkflowError("不允许返回依赖、Git 或工作流配置目录中的文件。")
        # safe_file 限制工作区范围；这里同时拒绝 Windows junction。
        path = checked_path(repository["path"], relative)
        if not path.is_file():
            raise WorkflowError("只能返回工作区内已有的普通文件。")
        format_name = "existing"
    else:
        format_name = arguments.get("format")
        if not isinstance(format_name, str) or format_name not in {"text", "docx", "xlsx", "pdf"}:
            raise WorkflowError("生成格式仅支持 text、docx、xlsx、pdf。")
        if format_name != "text" and Path(name).suffix.lower() != "." + format_name:
            raise WorkflowError("文件扩展名与生成格式不一致。")
        if format_name == "text" and Path(name).suffix.lower() not in TEXT_EXTENSIONS:
            raise WorkflowError("文本成果须使用支持的文本扩展名。")
    maximum = settings["zentao"]["max_attachment_mb"] * 1024 * 1024
    if source is not None and path.stat().st_size > maximum:
        raise WorkflowError("成果文件超过配置的附件大小上限。")
    content = arguments.get("content")
    if source is None and len(dumps(content, ensure_ascii=False).encode("utf-8")) > maximum:
        raise WorkflowError("成果文件内容超过配置的附件大小上限。")
    root = checked_path(store.directory(state["id"]), "artifacts")
    identifier = uuid4().hex
    directory = checked_path(root, identifier)
    directory.mkdir(parents=True)
    destination = directory / ("file" + Path(name).suffix)
    try:
        if source is not None:
            with path.open("rb") as stream:
                data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise WorkflowError("成果文件超过配置的附件大小上限。")
            destination.write_bytes(data)
        elif format_name == "text":
            if not isinstance(content, str):
                raise WorkflowError("文本成果 content 必须为字符串。")
            destination.write_text(content, encoding="utf-8", newline="")
        elif format_name == "xlsx":
            generate_xlsx(content, destination)
        else:
            blocks = document_blocks(content)
            if format_name == "docx":
                generate_docx(blocks, destination)
            else:
                generate_pdf(blocks, destination)
        if destination.stat().st_size > maximum:
            raise WorkflowError("成果文件超过配置的附件大小上限。")
        data = destination.read_bytes()
        entry = {"id": identifier, "session_id": state["id"], "name": name, "format": format_name,
                 "size": len(data), "extension": Path(name).suffix.lower(), "sha256": sha256(data).hexdigest(),
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                 "thread_id": state["thread_id"], "turn_id": state["turn_id"], "phase": state["phase"],
                 "delivery_round": state.get("delivery_round", 1)}
        temporary = directory / "manifest.json.tmp"
        temporary.write_text(dumps(entry, ensure_ascii=False), encoding="utf-8")
        temporary.replace(directory / "manifest.json")
    except Exception as error:
        # 未发布的半成品不可出现在清单中，也不保留失败生成文件。
        rmtree(directory)
        if isinstance(error, WorkflowError):
            raise
        raise WorkflowError(f"成果文件生成失败：{error}；未发布半成品，请明确处理后重试。") from error
    return {**entry, **artifact_urls(state["id"], identifier)}


def page_number(query, key="page"):
    value = query.get(key, ["1"])[0]
    if not isinstance(value, str) or not fullmatch(r"[1-9][0-9]{0,7}", value):
        raise WorkflowError("预览页码必须为正整数。")
    return int(value)


def content_page(items, page):
    start = (page - 1) * PAGE_SIZE
    if page > 1 and start >= len(items):
        raise WorkflowError("预览页码超出内容范围。")
    return {"page": page, "total": len(items), "page_size": PAGE_SIZE,
            "has_next": start + PAGE_SIZE < len(items), "items": items[start:start + PAGE_SIZE]}


def preview_artifact(store, state, identifier, query, settings):
    entry, path = artifact_file(store, state, identifier)
    suffix = path.suffix.lower()
    page = page_number(query)
    if suffix in TEXT_EXTENSIONS:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeError as error:
            raise WorkflowError("文本预览仅支持 UTF-8 编码，请下载原文件查看。") from error
        return {"kind": "markdown" if suffix in {".md", ".markdown"} else "text", **content_page(lines, page)}
    if suffix in IMAGE_EXTENSIONS:
        return {"kind": "image"}
    if suffix == ".pdf":
        cache = path.parent / "preview.json"
        if not cache.exists():
            material = {"path": str(path), "title": entry["name"]}
            try:
                read_pdf(material, settings["pdf"]["renderer"])
            except ValueError as error:
                raise WorkflowError(f"PDF 预览失败：{error}。原文件仍可下载。") from error
            checked_path(path.parent, "preview.json.tmp").write_text(dumps(material["pdf_pages"], ensure_ascii=False), encoding="utf-8")
            (path.parent / "preview.json.tmp").replace(cache)
        pages = loads(checked_path(path.parent, "preview.json").read_text(encoding="utf-8"))
        if page > len(pages):
            raise WorkflowError("PDF 预览页码超出范围。")
        return {"kind": "pdf", "page": page, "total": len(pages), "has_next": page < len(pages)}
    if suffix == ".docx":
        docx = dependency("docx", "python-docx")
        document = docx.Document(path)
        paragraphs = dependency("docx.text.paragraph", "python-docx")
        tables = dependency("docx.table", "python-docx")
        blocks = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                paragraph = paragraphs.Paragraph(child, document)
                style = paragraph.style.name
                if style in {"List Number", "List Bullet"}:
                    ordered = style == "List Number"
                    if blocks and blocks[-1]["type"] == "list" and blocks[-1]["ordered"] == ordered:
                        blocks[-1]["items"].append(paragraph.text)
                    else:
                        blocks.append({"type": "list", "ordered": ordered, "items": [paragraph.text]})
                else:
                    blocks.append({"type": "paragraph", "text": paragraph.text, "style": style})
            elif child.tag.endswith("}tbl"):
                table = tables.Table(child, document)
                # 大表格按行切片，避免一次返回整份表格。
                blocks.extend({"type": "table", "rows": [[cell.text for cell in row.cells] for row in table.rows[index:index + 20]]}
                              for index in range(0, len(table.rows), 20))
        return {"kind": "docx", **content_page(blocks, page)}
    if suffix == ".xlsx":
        excel = dependency("openpyxl", "openpyxl")
        workbook = excel.load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            name = query.get("sheet", [workbook.sheetnames[0]])[0]
            if name not in workbook.sheetnames:
                raise WorkflowError("工作表不在当前成果文件中。")
            sheet = workbook[name]
            total = sheet.max_row or 0
            if page > 1 and (page - 1) * PAGE_SIZE >= total:
                raise WorkflowError("预览页码超出工作表范围。")
            rows = [[cell.value.isoformat() if hasattr(cell.value, "isoformat") else cell.value for cell in row]
                    for row in sheet.iter_rows(min_row=(page - 1) * PAGE_SIZE + 1, max_row=min(page * PAGE_SIZE, total))]
            return {"kind": "xlsx", "sheets": workbook.sheetnames, "sheet": name, "page": page,
                    "total": total, "page_size": PAGE_SIZE, "has_next": page * PAGE_SIZE < total, "items": rows}
        finally:
            workbook.close()
    return {"kind": "unsupported", "issue": "此格式暂不支持预览，请下载原文件查看。"}


def preview_image(store, state, identifier, query):
    entry, path = artifact_file(store, state, identifier)
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return path, guess_type(path.name)[0]
    if suffix != ".pdf":
        raise WorkflowError("该成果文件没有图片预览。")
    cache = checked_path(path.parent, "preview.json")
    if not cache.is_file():
        raise WorkflowError("请先打开 PDF 预览。")
    pages = loads(cache.read_text(encoding="utf-8"))
    page = page_number(query)
    if page > len(pages):
        raise WorkflowError("PDF 预览页码超出范围。")
    candidate = Path(pages[page - 1]["path"])
    if not candidate.is_relative_to(path.parent):
        raise WorkflowError("PDF 页图路径越界。")
    return checked_path(path.parent, candidate.relative_to(path.parent)), "image/png"
