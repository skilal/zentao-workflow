"""将 PDF 逐页转换为图片，供模型直接阅读文字版和扫描版资料。"""

from pathlib import Path
from shutil import which
from subprocess import CREATE_NO_WINDOW, DEVNULL, PIPE, TimeoutExpired, run
from tempfile import mkdtemp


def read_pdf(material):
    renderer = which("pdftoppm")
    if renderer is None:
        raise ValueError("读取 PDF 需要 Poppler 的 pdftoppm，请将其加入 PATH 后重启工作流")
    path = Path(material["path"])
    directory = Path(mkdtemp(prefix=path.name + ".pages-", dir=path.parent))
    try:
        result = run([renderer, "-png", "-scale-to", "1800", str(path), str(directory / "page")],
                     stdout=DEVNULL, stderr=PIPE, timeout=60, creationflags=CREATE_NO_WINDOW)
    except TimeoutExpired as error:
        raise ValueError("PDF 页图转换超时，请拆分文档后重新提供") from error
    if result.returncode:
        raise ValueError("PDF 无法转换为页图，请检查文件是否损坏或加密")
    pages = sorted(directory.glob("page-*.png"), key=lambda page: int(page.stem.rsplit("-", 1)[1]))
    if not pages:
        raise ValueError("PDF 未生成可读取的页面")
    material["pdf_pages"] = [{"page": index, "title": f"{material['title']} 第 {index} 页",
                              "path": str(page), "image": True}
                             for index, page in enumerate(pages, 1)]
    material["text"] = f"PDF 共 {len(pages)} 页，已生成页图；请逐页查看随回合附加的图片。"


def material_files(materials):
    documents = [*materials, *(entry for material in materials for entry in material.get("archive_entries", []))]
    return [entry for document in documents for entry in [document, *document.get("pdf_pages", [])] if entry.get("path")]
