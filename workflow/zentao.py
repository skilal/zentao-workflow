"""只使用明确的禅道 API v1；不自动重登或切换接口。"""

from html import unescape
from hashlib import sha256
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from json import dumps, loads
from pathlib import Path
from re import sub
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
from xml.etree.ElementTree import ParseError, fromstring
from zipfile import BadZipFile, ZipFile

from workflow.settings import WorkflowError, load_password


KINDS = {"story": ("需求", "stories"), "task": ("任务", "tasks"), "bug": ("Bug", "bugs")}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WorkflowError("服务器返回重定向，已停止；请确认完整禅道地址。")


class DescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = []
        self.links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")
        if tag == "img" and values.get("src"):
            self.images.append(values["src"])
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return sub(r"\n[ \t]*\n+", "\n\n", "".join(self.parts)).strip()


def parse_description(value):
    parser = DescriptionParser()
    parser.feed(str(value or ""))
    return parser


def account_of(value):
    return value.get("account", "") if isinstance(value, dict) else str(value or "")


def identifier(value):
    if isinstance(value, dict):
        value = value.get("id", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        raise WorkflowError("禅道关联工作项的编号格式不符合预期。")


class ZentaoClient:
    def __init__(self, settings):
        self.settings = settings["zentao"]
        self.base = self.settings["base_url"].rstrip("/") + "/"
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(CookieJar()))
        self.token = None
        self.account = None

    def _fetch(self, path, payload=None, binary=False):
        url = urljoin(self.base, path)
        base_parts, parts = urlparse(self.base), urlparse(url)
        if (base_parts.scheme, base_parts.netloc) != (parts.scheme, parts.netloc):
            raise WorkflowError("拒绝向禅道站点以外的地址发送登录凭据。")
        headers = {"Accept": "application/json" if not binary else "*/*"}
        if self.token:
            headers["Token"] = self.token
        body = None
        if payload is not None:
            body = dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=self.settings["timeout_seconds"]) as response:
                maximum = self.settings["max_attachment_mb"] * 1024 * 1024 if binary else 16 * 1024 * 1024
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise WorkflowError("服务器返回内容超过配置大小上限，已停止读取。")
                if binary:
                    content_type = response.headers.get_content_type()
                    if content_type in ("text/html", "application/json"):
                        raise WorkflowError("附件接口未返回文件，可能是权限不足。")
                    return data, content_type
                result = loads(data.decode("utf-8-sig"))
                if not isinstance(result, dict):
                    raise WorkflowError("禅道响应必须是 JSON 对象。")
                if "error" in result or result.get("status") == "fail":
                    raise WorkflowError("禅道拒绝了请求，请确认账号权限及接口地址。")
                return result
        except HTTPError as error:
            if error.code in (401, 403):
                self.token = None
                raise WorkflowError("禅道登录已失效或权限不足；已停止，请手动重新连接。") from None
            raise WorkflowError(f"禅道请求失败，HTTP {error.code}；未自动重试。") from None
        except (URLError, TimeoutError, OSError):
            raise WorkflowError("无法连接禅道或请求超时；未自动重试。") from None
        except (ValueError, UnicodeError):
            raise WorkflowError("禅道返回的内容不是预期 JSON；请确认部署版本与入口。") from None

    def login(self):
        self.token = None
        auth = self._fetch("api.php/v1/tokens", {"account": self.settings["account"], "password": load_password()})
        if not isinstance(auth.get("token"), str) or not auth["token"]:
            raise WorkflowError("登录未返回 Token，已停止，未重复尝试。")
        self.token = auth["token"]
        profile = self._fetch("api.php/v1/user").get("profile", {})
        self.account = profile.get("account")
        if self.account != self.settings["account"]:
            self.token = None
            raise WorkflowError("登录后的账号与配置不一致，已停止。")
        return {"account": self.account, "name": profile.get("realname", self.account)}

    def require_login(self):
        if not self.token or not self.account:
            raise WorkflowError("请先连接禅道。")

    def list_items(self, kind):
        self.require_login()
        if kind not in KINDS:
            raise WorkflowError("未知工作项类型。")
        page, seen, records = 1, set(), []
        while True:
            query = urlencode({"fields": kind, "type": "assignedTo", "limit": self.settings["page_size"], "page": page})
            data = self._fetch("api.php/v1/user?" + query)
            bucket = data.get(kind)
            if not isinstance(bucket, dict) or "total" not in bucket:
                raise WorkflowError("禅道未返回该类型列表；请确认“我的工作”访问权限。")
            items = bucket.get(KINDS[kind][1], [])
            if not isinstance(items, list):
                raise WorkflowError("禅道工作项列表格式不符合预期。")
            new_ids = set()
            for item in items:
                item_id = identifier(item.get("id"))
                if item_id in seen:
                    continue
                new_ids.add(item_id)
                if account_of(item.get("assignedTo")) != self.account:
                    continue
                if item.get("status") in self.settings["excluded_statuses"]:
                    continue
                records.append({"kind": kind, "id": item_id,
                                "title": item.get("title") if kind != "task" else item.get("name"),
                                "status": item.get("statusName", item.get("status", "")),
                                "project": item.get("projectName", item.get("project", "")),
                                "priority": item.get("pri", ""), "assigned_to": self.account})
            if not items or not new_ids:
                if len(seen) < int(bucket["total"]):
                    raise WorkflowError("禅道分页提前结束，列表不完整，已停止。")
                break
            seen.update(new_ids)
            if len(seen) >= int(bucket["total"]):
                break
            page += 1
        return records

    def detail(self, kind, item_id, require_assignee=True):
        self.require_login()
        if kind not in KINDS or int(item_id) <= 0:
            raise WorkflowError("工作项类型或编号无效。")
        data = self._fetch(f"api.php/v1/{KINDS[kind][1]}/{int(item_id)}")
        if identifier(data.get("id")) != int(item_id):
            raise WorkflowError("返回的工作项编号不一致。")
        if require_assignee and account_of(data.get("assignedTo")) != self.account:
            raise WorkflowError("该工作项已不再指派给当前账号，已停止。")
        return data

    def collect(self, item, directory):
        primary = self.detail(item["kind"], item["id"])
        related = []
        story_id = identifier(primary.get("story"))
        from_bug = identifier(primary.get("fromBug"))
        if item["kind"] == "task" and not from_bug and story_id:
            related.append({"kind": "story", "origin": "task", "version": primary.get("storyVersion"),
                            "data": {"id": story_id, "title": primary.get("storyTitle", ""),
                                     "spec": primary.get("storySpec", ""), "verify": primary.get("storyVerify", ""),
                                     "files": primary.get("storyFiles", [])}})
        elif item["kind"] != "story" and story_id:
            related.append({"kind": "story", "data": self.detail("story", story_id, require_assignee=False)})
        if item["kind"] == "task" and from_bug:
            related.append({"kind": "bug", "data": self.detail("bug", from_bug, require_assignee=False)})
        materials, sources, blockers, feishu = [], [], [], []
        documents = [{"kind": item["kind"], "data": primary}] + related
        for document in documents:
            data = document["data"]
            content = []
            parsers = []
            for key in ("spec", "verify", "desc", "steps", "bugSteps"):
                if data.get(key):
                    parsed = parse_description(data[key])
                    parsers.append(parsed)
                    if item["kind"] != "task" or parsed.text() or parsed.images:
                        content.append(f"【{key}】\n{parsed.text()}")
            source = {"kind": document["kind"], "id": data["id"],
                      "title": data.get("title", data.get("name", "")),
                      "content": "\n\n".join(content), "raw": data}
            if document.get("origin") == "task":
                source.update(origin="task", version=document["version"], note="来自任务详情的关联需求资料")
            sources.append(source)
            image_urls = {unescape(url) for parser in parsers for url in parser.images}
            links = {unescape(url) for parser in parsers for url in parser.links}
            feishu.extend(sorted(url for url in links if urlparse(url).hostname and
                                 urlparse(url).hostname.endswith((".feishu.cn", ".larksuite.com"))))
            files = data.get("files", [])
            if isinstance(files, dict):
                files = list(files.values())
            for file in files:
                file_id = identifier(file.get("id"))
                extension = str(file.get("extension", "")).lower().lstrip(".")
                materials.append(self._download(file_id, extension, str(file.get("title", file_id)), directory))
            for image_url in sorted(image_urls):
                parsed_url = urlparse(urljoin(self.base, image_url))
                if parsed_url.netloc != urlparse(self.base).netloc:
                    blockers.append("正文包含站外图片，第一版不自动获取：" + image_url)
                    continue
                query = parse_qs(parsed_url.query)
                if "fileID" in query:
                    materials.append(self._download(identifier(query["fileID"][0]), query.get("t", ["png"])[0], "正文图片", directory))
                else:
                    blockers.append("正文图片未包含可识别的禅道 fileID：" + image_url)
        unique = {material["file_id"]: material for material in materials}
        if not any(source["content"].strip() for source in sources):
            blockers.append("工作项和已关联资料没有有效描述，需要补充实现目标和验收标准。")
        blockers.extend(material["issue"] for material in unique.values() if material.get("issue"))
        return {"item": item, "sources": sources, "materials": list(unique.values()),
                "blockers": blockers, "feishu_links": sorted(set(feishu)),
                "feishu_note": "飞书链接未读取；若含必要规则，必须由用户补充。"}

    def _download(self, file_id, extension, title, directory):
        extension = sub(r"[^a-zA-Z0-9]", "", extension.lower())[:12]
        if not extension:
            raise WorkflowError(f"附件 {file_id} 缺少明确格式。")
        path = Path(directory) / "attachments" / f"{file_id}.{extension}"
        path.parent.mkdir(parents=True, exist_ok=True)
        material = {"file_id": file_id, "title": title, "path": str(path), "extension": extension,
                    "content_type": "", "text": "", "issue": ""}
        try:
            raw, content_type = self._fetch(f"api.php/v1/files/{file_id}", binary=True)
        except WorkflowError as error:
            if not self.token:
                raise
            material["issue"] = f"附件 {title}（{file_id}）未能读取：{error} 请人工补充必要内容。"
            return material
        path.write_bytes(raw)
        material["content_type"] = content_type
        material["sha256"] = sha256(raw).hexdigest()
        if extension in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            material["image"] = True
            return material
        try:
            material["text"] = extract_text(path)
        except (ValueError, OSError, KeyError, BadZipFile, ParseError):
            material["issue"] = f"附件 {title} 无法完整提取，请人工补充必要内容。"
        return material


def extract_text(path):
    extension = path.suffix.lower()
    if extension in (".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".log", ".sql"):
        text = path.read_text(encoding="utf-8-sig")
        if len(text) > 250000:
            raise ValueError("文本过长")
        return parse_description(text).text() if extension == ".html" else text
    patterns = {".docx": ("word/document.xml",), ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"),
                ".pptx": ("ppt/slides/slide",)}
    if extension not in patterns:
        raise ValueError("未支持的格式；不自动替换解析器")
    parts = []
    with ZipFile(path) as archive:
        selected = [name for name in archive.namelist() if name.endswith(".xml") and
                    any(name.startswith(prefix) for prefix in patterns[extension])]
        if not selected or sum(archive.getinfo(name).file_size for name in selected) > 16 * 1024 * 1024:
            raise ValueError("压缩包内容超限或为空")
        if extension == ".xlsx":
            # 表格结构、公式及内嵌图不能由纯文本完整表达，必须向用户明示。
            raise ValueError("电子表格需人工提供与需求有关的表格内容")
        for name in sorted(selected):
            root = fromstring(archive.read(name))
            parts.append("\n".join(node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "t" and node.text))
        if any(name.startswith(("word/media/", "ppt/media/")) for name in archive.namelist()):
            raise ValueError("文档包含内嵌媒体，需要人工补充")
    return "\n\n".join(parts)
