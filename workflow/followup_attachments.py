"""续聊附件的消息归属；ZIP 保留目录及 XML 只读规则。"""

from codecs import BOM_UTF8, BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE
from io import BytesIO
from pathlib import Path
from re import search
from zipfile import BadZipFile, ZipFile
from zlib import error as CompressionError

from workflow.settings import WorkflowError
from workflow.followup_documents import attachment_file, document_view, read_document


def decode_xml(data):
    if data.startswith((BOM_UTF32_BE, BOM_UTF32_LE)):
        encoding = "utf-32"
    elif data.startswith((BOM_UTF16_BE, BOM_UTF16_LE)):
        encoding = "utf-16"
    elif data.startswith(BOM_UTF8):
        encoding = "utf-8-sig"
    elif data.startswith(b"\x00<\x00?"):
        encoding = "utf-16-be"
    elif data.startswith(b"<\x00?\x00"):
        encoding = "utf-16-le"
    else:
        declaration = search(br"^<\?xml\s+[^?]*encoding\s*=\s*['\"]([^'\"]+)['\"]", data)
        encoding = declaration.group(1).decode("ascii") if declaration else "utf-8"
    return data.decode(encoding)


def inspect_zip(data):
    entries = []
    try:
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if sum(not member.is_dir() for member in members) > 200:
                raise WorkflowError("ZIP 包内文件超过 200 个。")
            xml_members = [member for member in members if not member.is_dir()
                           and Path(member.filename).suffix.lower() == ".xml"]
            if sum(member.file_size for member in xml_members) > 16 * 1024 * 1024:
                raise WorkflowError("ZIP 内 XML 解压总大小超过 16 MB。")
            total_text = 0
            for index, member in enumerate(members):
                name = member.filename
                if not (member.flag_bits & 0x800) and name == member.orig_filename:
                    name = name.encode("cp437").decode("gbk")
                name = name.replace("\\", "/")
                directory = member.is_dir() or name.endswith("/")
                extension = "" if directory else Path(name).suffix.lower().lstrip(".")
                entry = {"member_id": str(index), "name": name, "directory": directory,
                         "extension": extension, "size": member.file_size, "readable": False, "issue": ""}
                entries.append(entry)
                if directory or extension != "xml":
                    continue
                if member.flag_bits & 1:
                    entry["issue"] = "XML 已加密，内容未读取。"
                    continue
                try:
                    text = decode_xml(archive.read(member))
                except (BadZipFile, RuntimeError, NotImplementedError, UnicodeError, LookupError,
                        CompressionError, EOFError) as error:
                    entry["issue"] = f"XML 内容未读取：{error}"
                    continue
                total_text += len(text)
                if total_text > 250000:
                    raise WorkflowError("ZIP 内 XML 文本总量超过 25 万字符。")
                entry.update(readable=True, total_lines=len(text.splitlines()))
    except (BadZipFile, UnicodeError, NotImplementedError) as error:
        raise WorkflowError(f"ZIP 无法读取：{error}") from error
    return entries


def attachment_view(entry):
    if entry.get("kind"):
        return document_view(entry)
    return {key: entry[key] for key in ("id", "name", "size", "entries")}


def submitted_attachments(state):
    return [attachment_view(entry) for entry in state.get("followup_attachments", []) if "history_index" in entry]


def prepare_attachments(state, identifiers):
    if (not isinstance(identifiers, list) or any(not isinstance(value, str) for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise WorkflowError("请提供不重复的续聊附件编号。")
    available = {entry["id"]: entry for entry in state.get("followup_attachments", []) if "history_index" not in entry}
    if any(value not in available for value in identifiers):
        raise WorkflowError("附件不属于当前待发送消息，不能引用其他会话或已发送附件。")
    return [available[value] for value in identifiers]


def read_followup_attachment(store, state, arguments):
    entry = next((entry for entry in state.get("followup_attachments", [])
                  if entry["id"] == arguments.get("attachment_id") and "history_index" in entry), None)
    if entry is None:
        raise WorkflowError("附件不在当前会话已发送的消息中。")
    if entry.get("kind"):
        return read_document(store, state, entry, arguments)
    if arguments.get("page") is not None or arguments.get("image_id") is not None:
        raise WorkflowError("ZIP 使用 member_id 读取成员，不支持文档页面或图片编号。")
    member_id = arguments.get("member_id")
    if member_id is None:
        return {"attachment": attachment_view(entry)}
    member = next((member for member in entry["entries"] if member["member_id"] == member_id), None)
    if member is None:
        raise WorkflowError("成员编号不在当前 ZIP 清单中。")
    if not member["readable"]:
        return {"member": member, "readable": False,
                "issue": member["issue"] or "仅列出目录及文件信息，本次只读取 XML 内容。"}
    start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
    if type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 500:
        raise WorkflowError("start_line 必须大于等于 1，line_count 必须为 1 至 500。")
    path = attachment_file(store, state, entry)
    with ZipFile(path) as archive:
        try:
            text = decode_xml(archive.read(archive.infolist()[int(member_id)]))
        except (BadZipFile, RuntimeError, NotImplementedError, UnicodeError, LookupError,
                CompressionError, EOFError) as error:
            return {"member": member, "readable": False, "issue": f"XML 内容未读取：{error}"}
    lines = text.splitlines()
    end = min(len(lines), start - 1 + count)
    return {"member": member, "readable": True, "total_lines": len(lines), "start_line": start,
            "next_line": end + 1 if end < len(lines) else None,
            "text": "\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end))}
