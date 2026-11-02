"""用隔离目录和模拟渲染器检查 PDF 页图及 Codex 输入，不访问业务服务。"""

from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from test_supplement_images import SupplementImageFixture, PNG
from test_zip_materials import zip_bytes
from workflow.pdf_materials import read_pdf
from workflow.zentao import ZentaoClient


class PdfRendererChecks(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "扫描件.pdf"
        self.path.write_bytes(b"%PDF-reference")
        self.material = {"path": str(self.path), "title": "扫描件.pdf", "text": "", "issue": ""}

    def test_renders_every_page_in_numeric_order_without_claiming_extracted_text(self):
        def render(command, **options):
            for index in range(12, 0, -1):
                Path(command[-1] + f"-{index:02}.png").write_bytes(PNG)
            self.assertEqual(options["timeout"], 60)
            self.assertEqual(command[1:4], ["-png", "-scale-to", "1800"])
            return CompletedProcess(command, 0)
        with patch("workflow.pdf_materials.which", return_value="pdftoppm.exe"), patch("workflow.pdf_materials.run", side_effect=render):
            read_pdf(self.material)
        self.assertEqual([page["page"] for page in self.material["pdf_pages"]], list(range(1, 13)))
        self.assertEqual([Path(page["path"]).name for page in self.material["pdf_pages"]], [f"page-{i:02}.png" for i in range(1, 13)])
        self.assertTrue(all(Path(page["path"]).is_relative_to(self.directory) for page in self.material["pdf_pages"]))
        self.assertIn("共 12 页", self.material["text"])
        self.assertEqual(self.material["issue"], "")

    def test_missing_tool_stops_without_using_another_parser(self):
        with patch("workflow.pdf_materials.which", return_value=None), patch("workflow.pdf_materials.run") as render:
            with self.assertRaisesRegex(ValueError, "pdftoppm"):
                read_pdf(self.material)
        render.assert_not_called()

    def test_failed_empty_and_timed_out_conversions_are_explicit(self):
        for result, reason in ((CompletedProcess([], 1), "损坏或加密"), (CompletedProcess([], 0), "未生成"),
                               (TimeoutExpired("pdftoppm", 60), "超时")):
            arguments = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(reason=reason), patch("workflow.pdf_materials.which", return_value="pdftoppm.exe"), \
                    patch("workflow.pdf_materials.run", **arguments), self.assertRaisesRegex(ValueError, reason):
                read_pdf(self.material)
        self.assertNotIn("pdf_pages", self.material)


class PdfMaterialChecks(SupplementImageFixture):
    def render(self, command, **options):
        Path(command[-1] + "-1.png").write_bytes(PNG)
        return CompletedProcess(command, 0)

    def test_direct_and_zip_pdf_retain_page_images_and_no_blocker(self):
        client = ZentaoClient(self.settings)
        documents = (("pdf", b"%PDF-scan"), ("zip", zip_bytes([("扫描件.pdf", b"%PDF-scan"), ("元数据.xml", "有效字段")])))
        for extension, data in documents:
            with self.subTest(extension=extension), patch.object(client, "_fetch", return_value=(data, "application/octet-stream")), \
                    patch("workflow.pdf_materials.which", return_value="pdftoppm.exe"), patch("workflow.pdf_materials.run", side_effect=self.render):
                material = client._download(99, extension, "示例资料", self.root)
            pdf = material if extension == "pdf" else material["archive_entries"][0]
            self.assertEqual(material["issue"], "")
            self.assertEqual(pdf["issue"], "")
            self.assertEqual(Path(pdf["pdf_pages"][0]["path"]).read_bytes(), PNG)
            if extension == "zip":
                self.assertEqual(material["archive_entries"][1]["text"], "有效字段")

    def test_pdf_pages_reach_original_codex_thread_and_are_deduplicated(self):
        pages = []
        directory = self.runtime.engine.store.directory(self.state["id"]) / "attachments"
        directory.mkdir()
        for index in range(2):
            path = directory / f"page-{index}.png"
            path.write_bytes(PNG + bytes([index]))
            pages.append({"page": 1, "path": str(path), "image": True, "title": f"扫描件 {index}"})
        self.state["snapshot"]["materials"] = [{"file_id": 1, "title": "直接附件", "pdf_pages": [pages[0]]},
            {"file_id": 2, "title": "ZIP", "archive_entries": [{"title": "包内 PDF", "pdf_pages": [pages[1]]}]}]
        self.state.update(thread_id="原线程", phase="plan")
        engine = self.runtime.engine
        engine.ensure_rpc = Mock()
        engine.rpc = Mock(model="模拟模型")
        engine.rpc.request.return_value = {"turn": {"id": "模拟回合"}}
        engine._start_turn("读取全部 PDF 页图")
        inputs = engine.rpc.request.call_args.args[1]["input"]
        self.assertEqual([entry["path"] for entry in inputs if entry["type"] == "localImage"], [page["path"] for page in pages])
        engine._start_turn("沿用 PDF 资料")
        self.assertFalse(any(entry["type"] == "localImage" for entry in engine.rpc.request.call_args.args[1]["input"]))

    def test_direct_pdf_reports_missing_renderer(self):
        client = ZentaoClient(self.settings)
        with patch.object(client, "_fetch", return_value=(b"%PDF-reference", "application/pdf")), \
                patch("workflow.pdf_materials.which", return_value=None):
            material = client._download(99, "pdf", "扫描件", self.root)
        self.assertIn("pdftoppm", material["issue"])
        self.assertNotIn("pdf_pages", material)
