"""续聊 ZIP：目录与 XML、权限边界、消息归属和持久化。"""

from copy import deepcopy
from hashlib import sha256
from http.client import HTTPConnection
from io import BytesIO
from json import dumps, loads
from pathlib import Path
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlencode
from zipfile import ZIP_DEFLATED, ZipFile

import test_followup as fixtures
from workflow.followup_attachments import inspect_zip, prepare_attachments
from workflow.mcp_server import Tools
from workflow.settings import WorkflowError
from workflow.store import write_authorized
from workflow.web import COOKIE_NAME, LocalServer, WebApplication, public_state


def zip_bytes(files):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)
    return output.getvalue()


SAMPLE = zip_bytes([("归档包/", b""), ("归档包/空目录/", b""),
                    ("归档包/元数据.xml", '<归档 类型="电子"><标题>测试归档</标题></归档>'),
                    ("归档包/原文.ofd", b"ofd"), ("归档包/原文.pdf", b"pdf"),
                    ("归档包/截图.png", b"png"), ("嵌套.zip", b"not-a-zip")])


class ZipDirectoryChecks(TestCase):
    def test_preserves_paths_empty_directories_and_only_reads_xml(self):
        original = ZipFile.read
        names = []
        def read(archive, member, *args, **kwargs):
            names.append(member.filename)
            return original(archive, member, *args, **kwargs)
        with patch.object(ZipFile, "read", read):
            entries = inspect_zip(SAMPLE)
        self.assertEqual(names, ["归档包/元数据.xml"])
        self.assertEqual(entries[1]["name"], "归档包/空目录/")
        self.assertTrue(entries[1]["directory"])
        self.assertTrue(entries[2]["readable"])
        self.assertTrue(all(not entry["issue"] for entry in entries))
        self.assertEqual([entry["extension"] for entry in entries[3:]], ["ofd", "pdf", "png", "zip"])

    def test_xml_encodings_and_partial_failure(self):
        cases = [("utf-8-sig", '<?xml version="1.0" encoding="UTF-8"?>'),
                 ("utf-16", '<?xml version="1.0" encoding="UTF-16"?>'),
                 ("utf-16-be", '<?xml version="1.0" encoding="UTF-16"?>'),
                 ("utf-32", '<?xml version="1.0" encoding="UTF-32"?>'),
                 ("gbk", '<?xml version="1.0" encoding="GBK"?>'),
                 ("gb2312", '<?xml version="1.0" encoding="GB2312"?>')]
        files = [(f"{index}.xml", (header + '<数据 属性="保留">中文</数据>').encode(encoding))
                 for index, (encoding, header) in enumerate(cases)]
        files += [("未知编码.xml", b'<?xml encoding="missing-codec"?><a/>'), ("损坏编码.xml", b"\xff")]
        entries = inspect_zip(zip_bytes(files))
        self.assertTrue(all(entry["readable"] for entry in entries[:len(cases)]))
        self.assertTrue(all(entry["issue"] for entry in entries[len(cases):]))

    def test_legacy_gbk_names_and_unicode_extension(self):
        placeholder = b"123456.xml"
        name = "元数据.xml"
        data = zip_bytes([(placeholder.decode(), "字段")]).replace(placeholder, name.encode("gbk"))
        self.assertEqual(inspect_zip(data)[0]["name"], name)

    def test_encrypted_and_corrupt_xml_do_not_hide_other_xml(self):
        for encrypted in (True, False):
            data = bytearray(zip_bytes([("bad.xml", "错误"), ("good.xml", "有效")]))
            if encrypted:
                for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
                    data[data.index(signature) + offset] |= 1
            else:
                data[data.index(b"PK\x01\x02") + 16] ^= 1
            entries = inspect_zip(data)
            self.assertTrue(entries[0]["issue"])
            self.assertFalse(entries[0]["readable"])
            self.assertTrue(entries[1]["readable"])

    def test_limits_count_only_xml_bytes_and_all_regular_files(self):
        self.assertFalse(inspect_zip(zip_bytes([("large.ofd", b"x" * (17 * 1024 * 1024))]))[0]["issue"])
        for data, message in [(b"invalid", "ZIP 无法读取"),
                              (zip_bytes([(f"{i}.ofd", b"") for i in range(201)]), "200"),
                              (zip_bytes([("large.xml", b"x" * (16 * 1024 * 1024 + 1))]), "16 MB"),
                              (zip_bytes([("a.xml", "x" * 150000), ("b.xml", "y" * 150001)]), "25 万")]:
            with self.subTest(message=message), self.assertRaisesRegex(WorkflowError, message):
                inspect_zip(data)
        self.assertEqual(inspect_zip(zip_bytes([])), [])


class FollowupAttachmentChecks(TestCase):
    def setUp(self):
        self.fixture = fixtures.FollowupChecks()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.application = self.fixture.application
        self.state, self.runtime, self.engine = self.fixture.state, self.fixture.runtime, self.fixture.engine

    def upload(self, data=SAMPLE, name="归档样例.zip"):
        self.application.upload_followup_attachment(self.state["id"], self.runtime.revision, name, data)
        return self.state["followup_attachments"][-1]

    def send_zip(self, attachment, plan_mode=False, review_enabled=False):
        self.application.dispatch(self.fixture.request(data={"text": "", "image_ids": [],
            "attachment_ids": [attachment["id"]], "review_enabled": review_enabled, "plan_mode": plan_mode}))
        self.fixture.drain()

    def test_only_zip_sends_on_same_thread_and_reads_xml_without_prompt_dump(self):
        entry = self.upload()
        self.assertEqual(self.state["stage"], "已完成（静态审查）")
        tools = Tools(self.state["id"])
        with self.assertRaises(WorkflowError):
            tools.call("read_followup_attachment", {"attachment_id": entry["id"]})
        self.send_zip(entry)
        self.assertEqual(self.state["thread_id"], "原需求线程")
        message = self.state["history"][entry["history_index"]]
        self.assertEqual(message["followup_attachments"][0]["id"], entry["id"])
        self.assertEqual(self.state["followup"]["attachments"][0]["id"], entry["id"])
        self.assertTrue(write_authorized(self.state))
        prompt = self.fixture.calls[-1][1]["input"][0]["text"]
        self.assertIn("归档包/元数据.xml", prompt)
        self.assertNotIn('<归档 类型=', prompt)
        result = tools.call("read_followup_attachment", {"attachment_id": entry["id"], "member_id": "2"})
        self.assertIn('<归档 类型="电子"><标题>测试归档</标题></归档>', result["text"])
        self.assertFalse(tools.call("read_followup_attachment", {"attachment_id": entry["id"], "member_id": "3"})["readable"])
        self.fixture.finish()
        self.assertEqual(self.state["stage"], "已完成（未审查）")
        self.fixture.send("继续解释 XML", review_enabled=False)
        self.assertEqual(tools.call("task_context", {})["followup_attachments"][0]["id"], entry["id"])
        self.assertTrue(tools.call("read_followup_attachment", {"attachment_id": entry["id"], "member_id": "2"})["readable"])

    def test_pages_by_member_id_preserve_duplicate_names_and_encoding(self):
        xml = '<?xml version="1.0" encoding="GBK"?>\r\n<数据>\r\n' + "正文\r\n" * 600 + "</数据>"
        entry = self.upload(zip_bytes([("a.xml", xml.encode("gbk")), ("b.xml", b"<b/>")]).replace(b"b.xml", b"a.xml"))
        self.send_zip(entry)
        tools = Tools(self.state["id"])
        result = tools.call("read_followup_attachment", {"attachment_id": entry["id"], "member_id": "0", "line_count": 200})
        self.assertEqual(result["next_line"], 201)
        self.assertIn("2: <数据>", result["text"])
        result = tools.call("read_followup_attachment", {"attachment_id": entry["id"], "member_id": "0", "start_line": 601})
        self.assertIsNone(result["next_line"])
        self.assertIn("603: </数据>", result["text"])
        self.assertEqual(tools.call("read_followup_attachment", {"attachment_id": entry["id"], "member_id": "1"})["text"], "1: <b/>")
        for arguments in ({"member_id": "missing"}, {"member_id": "0", "line_count": 501}, {"member_id": "0", "start_line": False}):
            with self.assertRaises(WorkflowError):
                tools.call("read_followup_attachment", {"attachment_id": entry["id"], **arguments})

    def test_restores_drafts_and_sent_files_and_prevents_removing_sent_file(self):
        entry = self.upload()
        restored = WebApplication(self.fixture.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.session(self.state["id"])["state"]["followup_attachments"][0]["id"], entry["id"])
        self.send_zip(entry)
        self.fixture.finish()
        saved = self.engine.store.load(self.state["id"])
        self.assertIn("history_index", saved["followup_attachments"][0])
        self.assertEqual(saved["history"][entry["history_index"]]["followup_attachments"][0]["id"], entry["id"])
        with self.assertRaises(WorkflowError):
            self.application.remove_followup_attachment(self.state["id"], self.runtime.revision, entry["id"])
        with self.assertRaises(WorkflowError):
            prepare_attachments(self.state, [entry["id"]])

    def test_remove_draft_and_no_extraction_even_for_relative_parent_paths(self):
        entry = self.upload(zip_bytes([("../../example.xml", b"<a/>"), ("../raw.ofd", b"content")]))
        path = Path(entry["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.application.remove_followup_attachment(self.state["id"], self.runtime.revision, entry["id"])
        self.assertFalse(path.exists())
        self.assertEqual(self.state["followup_attachments"], [])

    def test_upload_rejection_does_not_save_bad_packages(self):
        for name, data, revision in [("sample.ofd", SAMPLE, self.runtime.revision),
                                     ("bad.zip", b"bad", self.runtime.revision),
                                     ("sample.zip", SAMPLE, -1), ("empty.zip", b"", self.runtime.revision)]:
            with self.subTest(name=name), self.assertRaises(WorkflowError):
                self.application.upload_followup_attachment(self.state["id"], revision, name, data)
        self.assertEqual(self.state["followup_attachments"], [])
        self.application.settings["zentao"]["max_attachment_mb"] = 1
        with self.assertRaises(WorkflowError):
            self.upload(data=b"x" * (1024 * 1024 + 1))
        self.state["stage"] = "续聊处理中"
        with self.assertRaises(WorkflowError):
            self.upload()

    def test_ids_authorization_and_legacy_compatibility(self):
        entry = self.upload()
        for identifiers in (["foreign"], [entry["id"], entry["id"]], "invalid", [None]):
            with self.assertRaises(WorkflowError):
                prepare_attachments(self.state, identifiers)
        self.send_zip(entry)
        feedback = self.state["followup"]
        feedback["attachments"][0]["id"] = "changed"
        self.assertFalse(write_authorized(self.state))
        legacy = deepcopy(self.state)
        legacy.pop("followup_attachments")
        legacy["followup"] = {"text": "旧消息", "images": []}
        legacy["followup"]["authorization_hash"] = sha256(dumps(["旧消息", []], ensure_ascii=False).encode()).hexdigest()
        self.assertTrue(write_authorized(legacy))
        self.assertEqual(public_state(legacy)["followup_attachments"], [])

    def test_zip_only_plan_mode_and_review_switches(self):
        entry = self.upload()
        self.send_zip(entry, plan_mode=True, review_enabled=True)
        self.assertEqual(self.state["phase"], "plan")
        self.assertTrue(self.state["review_enabled"])
        self.assertTrue(Tools(self.state["id"]).call("read_followup_attachment", {"attachment_id": entry["id"], "member_id": "2"})["readable"])

    def test_text_image_and_zip_share_the_same_message(self):
        entry = self.upload()
        image = self.fixture.upload()
        self.application.dispatch(self.fixture.request(data={"text": "结合截图分析归档包", "image_ids": [image["id"]],
            "attachment_ids": [entry["id"]], "review_enabled": False, "plan_mode": False}))
        self.fixture.drain()
        message = self.state["history"][entry["history_index"]]
        self.assertEqual(message["text"], "结合截图分析归档包")
        self.assertEqual(message["images"][0]["id"], image["id"])
        self.assertEqual(message["followup_attachments"][0]["id"], entry["id"])
        self.assertIn({"type": "localImage", "path": image["path"]}, self.fixture.calls[-1][1]["input"])
        self.assertTrue(write_authorized(self.state))

    def test_http_upload_download_isolation_remove_and_access_checks(self):
        server = LocalServer(("127.0.0.1", 0), self.application)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(stop)
        def request(method, url, body=None, **headers):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(method, url, body, headers={"Cookie": f"{COOKIE_NAME}={self.application.cookie_token}",
                    "X-Workflow-Token": self.application.csrf_token, "Connection": "close", **headers})
                response = connection.getresponse()
                return response.status, response.read(), response.getheader("Content-Disposition")
            finally:
                connection.close()
        url = "/api/followup-attachments?" + urlencode({"session_id": self.state["id"], "revision": self.runtime.revision, "name": "样例.zip"})
        for headers in ({"Cookie": ""}, {"X-Workflow-Token": "invalid"}, {"Origin": "https://other.invalid"}):
            self.assertEqual(request("POST", url, SAMPLE, **headers)[0], 400)
        status, data, _ = request("POST", url, SAMPLE)
        self.assertEqual(status, 200)
        entry = loads(data)["state"]["followup_attachments"][0]
        self.assertEqual(self.runtime.uploads, 0)
        self.assertEqual(request("POST", url, SAMPLE)[0], 400)
        download = "/api/followup-attachments?" + urlencode({"session_id": self.state["id"], "attachment_id": entry["id"]})
        status, data, disposition = request("GET", download)
        self.assertEqual((status, data), (200, SAMPLE))
        self.assertIn("attachment;", disposition)
        other = self.application.sessions.store.new({"kind": "story", "id": 99})
        self.application.sessions.add(other)
        self.assertEqual(request("GET", download.replace(self.state["id"], other["id"]))[0], 400)
        with self.assertRaises(WorkflowError):
            Tools(other["id"]).call("read_followup_attachment", {"attachment_id": entry["id"]})
        body = dumps({"session_id": self.state["id"], "revision": self.runtime.revision, "attachment_id": entry["id"]})
        self.assertEqual(request("POST", "/api/followup-attachments/remove", body, **{"Content-Type": "application/json"})[0], 200)
        self.assertEqual(request("GET", download)[0], 400)
