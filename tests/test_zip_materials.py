"""ZIP 资料读取与原附件归属，不访问禅道或业务仓库。"""

from io import BytesIO
from json import loads
from pathlib import Path
from struct import pack
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from zlib import crc32

from workflow.context import requirement_context
from workflow.zentao import ZentaoClient


ROOT = Path(__file__).resolve().parents[1]


def zip_bytes(files):
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return stream.getvalue()


class ZipMaterialChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = ZentaoClient(loads((ROOT / "settings.example.json").read_text(encoding="utf-8")))
        self.client.account = "测试账号"
        self.client.token = "模拟登录状态"

    def download(self, data):
        with patch.object(self.client, "_fetch", return_value=(data, "application/octet-stream")) as request:
            material = self.client._download(99, "zip", "验收资料.zip", self.directory)
        request.assert_called_once_with("api.php/v1/files/99", binary=True)
        self.assertEqual(Path(material["path"]).read_bytes(), data)
        return material

    def test_reads_nested_text_office_and_images_without_losing_source(self):
        docx = zip_bytes([("word/document.xml", '<document><t>文档验收</t></document>')])
        data = zip_bytes([("子目录/", b""), ("子目录/元数据.xml", '<record>归档字段</record>'),
                          ("说明.docx", docx), ("截图.png", b"image")])
        task = {"id": 7, "name": "归档任务", "desc": "请核对元数据", "story": 0,
                "assignedTo": {"account": "测试账号"},
                "files": [{"id": 99, "extension": "zip", "title": "验收资料.zip"}]}
        with patch.object(self.client, "_fetch", side_effect=[task, (data, "application/octet-stream")]):
            snapshot = self.client.collect({"kind": "task", "id": 7}, self.directory)
        self.assertEqual(snapshot["sources"][0]["material_ids"], [99])
        self.assertEqual(len(snapshot["materials"]), 1)
        self.assertEqual(snapshot["blockers"], [])
        material = requirement_context({"snapshot": snapshot})["materials"][0]
        xml, document, image = material["archive_entries"]
        self.assertEqual(xml["title"], "子目录/元数据.xml")
        self.assertEqual(xml["text"], "<record>归档字段</record>")
        self.assertEqual(document["text"], "文档验收")
        self.assertTrue(image["image"])
        self.assertEqual(Path(image["path"]).read_bytes(), b"image")

    def test_decodes_gbk_names_in_legacy_chinese_zip(self):
        name = "元数据描述信息.xml"
        encoded = name.encode("gbk")
        placeholder = b"a" * (len(encoded) - 4) + b".xml"
        data = zip_bytes([(placeholder.decode(), "归档元数据")]).replace(placeholder, encoded)
        material = self.download(data)
        self.assertEqual(material["issue"], "")
        self.assertEqual(material["archive_entries"][0]["title"], name)
        self.assertEqual(material["archive_entries"][0]["text"], "归档元数据")

    def test_preserves_unicode_path_extra_without_utf8_flag(self):
        name = "元数据描述信息.xml"
        encoded = name.encode("gbk")
        placeholder = b"a" * (len(encoded) - 4) + b".xml"
        info = ZipInfo(placeholder.decode())
        unicode_path = pack("<BL", 1, crc32(encoded)) + name.encode("utf-8")
        info.extra = pack("<HH", 0x7075, len(unicode_path)) + unicode_path
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(info, "归档元数据")
        data = stream.getvalue().replace(placeholder, encoded)
        material = self.download(data)
        self.assertEqual(material["issue"], "")
        self.assertEqual(material["archive_entries"][0]["title"], name)
        self.assertEqual(material["archive_entries"][0]["text"], "归档元数据")

    def test_does_not_reencode_a_name_already_restored_by_zipfile(self):
        name = "00测试目录1/元数据.xml"
        encoded = name.encode("gbk")
        placeholder = b"a" * (len(encoded) - 4) + b".xml"
        data = zip_bytes([(placeholder.decode(), "归档元数据")]).replace(placeholder, encoded)
        with ZipFile(BytesIO(data)) as archive:
            member = archive.infolist()[0]
            self.assertFalse(member.flag_bits & 0x800)
            self.assertNotEqual(member.orig_filename, name)
            # 重现 Python 3.13 按 Unicode 扩展还原 filename、保留 orig_filename 的状态。
            member.filename = name
            with patch("workflow.zentao.ZipFile", return_value=archive):
                material = self.download(data)
        self.assertEqual(material["issue"], "")
        self.assertEqual(material["archive_entries"][0]["title"], name)
        self.assertEqual(material["archive_entries"][0]["text"], "归档元数据")

    def test_partial_failure_keeps_xml_and_original_binary_files(self):
        data = zip_bytes([("元数据.xml", "有效字段"), ("原文.pdf", b"%PDF-1.4"),
                          ("说明.txt", b"\xff"), ("嵌套.zip", zip_bytes([("a.txt", "a")]))])
        with patch("workflow.zentao.read_pdf", side_effect=ValueError("PDF 无法转换为页图")):
            material = self.download(data)
        xml, pdf, text, nested = material["archive_entries"]
        self.assertEqual(xml["text"], "有效字段")
        self.assertEqual(xml["issue"], "")
        self.assertIn("原文.pdf", material["issue"])
        self.assertIn("PDF 无法转换为页图", pdf["issue"])
        self.assertEqual(Path(pdf["path"]).read_bytes(), b"%PDF-1.4")
        self.assertTrue(text["issue"])
        self.assertIn("嵌套 ZIP", nested["issue"])

    def test_archive_paths_cannot_write_outside_generated_directory(self):
        material = self.download(zip_bytes([("../../outside.txt", "内容"),
                                            ("C:\\outside.txt", "内容"), ("/outside.txt", "内容")]))
        self.assertEqual(material["issue"], "")
        allowed = self.directory / "attachments" / "99.zip.contents"
        for entry in material["archive_entries"]:
            self.assertTrue(Path(entry["path"]).is_relative_to(allowed))
        self.assertFalse((self.directory / "outside.txt").exists())

    def test_empty_corrupt_oversized_and_too_many_members_are_explicit(self):
        cases = [(zip_bytes([]), "为空"), (b"broken", "ZIP 附件"),
                 (zip_bytes([("large.txt", b"x" * (16 * 1024 * 1024 + 1))]), "16 MB"),
                 (zip_bytes([(f"{i}.txt", "x") for i in range(201)]), "200 个")]
        for data, reason in cases:
            with self.subTest(reason=reason):
                material = self.download(data)
                self.assertIn(reason, material["issue"])
                self.assertFalse(material["archive_entries"])
        self.assertFalse((self.directory / "attachments" / "99.zip.contents").exists())

    def test_total_text_limit_keeps_first_member(self):
        material = self.download(zip_bytes([("first.txt", "x" * 150000), ("second.txt", "y" * 150000)]))
        first, second = material["archive_entries"]
        self.assertEqual(len(first["text"]), 150000)
        self.assertEqual(second["text"], "")
        self.assertIn("250000", second["issue"])

    def test_encrypted_member_is_reported_without_requesting_password(self):
        data = bytearray(zip_bytes([("secret.txt", "保密"), ("readable.txt", "可读")]))
        # 本地文件头和中央目录的加密标记；实际数据无须加密，读取器应在解密前停止。
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            position = data.index(signature) + offset
            data[position] |= 1
        material = self.download(bytes(data))
        encrypted, readable = material["archive_entries"]
        self.assertIn("已加密", encrypted["issue"])
        self.assertNotIn("path", encrypted)
        self.assertEqual(readable["text"], "可读")

    def test_corrupt_member_does_not_discard_readable_member(self):
        data = bytearray(zip_bytes([("bad.txt", "错误内容"), ("good.txt", "有效内容")]))
        position = data.index(b"PK\x01\x02") + 16
        data[position] ^= 1  # 损坏中央目录 CRC。
        material = self.download(bytes(data))
        bad, good = material["archive_entries"]
        self.assertIn("读取失败", bad["issue"])
        self.assertNotIn("path", bad)
        self.assertEqual(good["text"], "有效内容")
