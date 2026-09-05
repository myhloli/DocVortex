"""验证原生解析、资源生命周期和离线结果包的完整闭环。"""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest
from reportlab.pdfgen.canvas import Canvas

import docgale
from docgale.document.pdf import PDFDocument


def pdf_bytes() -> bytes:
    """生成可独立重复使用的文本型 PDF，不依赖仓库外部语料。"""
    output = BytesIO()
    canvas = Canvas(output)
    for index in range(30):
        canvas.drawString(40, 800 - index * 20, f"Native document line {index}: deterministic text extraction.")
    canvas.save()
    return output.getvalue()


def test_native_parse_never_classifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式原生入口不会隐式调用分类或触发 OCR。"""

    def forbidden(_document: PDFDocument) -> str:
        """一旦原生解析调用分类就立即失败。"""
        raise AssertionError("Native parsing must not classify")

    monkeypatch.setattr(PDFDocument, "classify", forbidden)
    result = docgale.parse(pdf_bytes(), keep_model_json=True)
    assert result.middle_json.pages
    assert result.model_json is not None


def test_classification_is_cached_in_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用方可以先分类再解析，同一文档不会重复执行分类。"""
    from docgale.document.pdf import document as module

    calls: list[bytes] = []

    def classify(_handle: object, payload: bytes) -> str:
        """记录实际分类调用，返回稳定的文本结果。"""
        calls.append(payload)
        return "txt"

    monkeypatch.setattr(module, "classify", classify)
    with PDFDocument(pdf_bytes()) as document:
        assert document.classify() == document.classify() == "txt"
        docgale.analyze(document)
        assert document.page_count == 1
    assert len(calls) == 1


def test_bundle_renders_after_source_is_deleted(tmp_path: Path) -> None:
    """移除源文件后，依靠结果包在九种目标中复用同一份素材。"""
    image = BytesIO()
    Image.new("RGB", (20, 10), color="blue").save(image, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    source = tmp_path / "source.html"
    source.write_text(f'<h1>Portable</h1><p>One parse, many outputs.</p><img src="{data_uri}" alt="Example">')
    result = docgale.parse(source, keep_model_json=True)
    assert result.assets
    before = result.to_dict()
    result.save_bundle(tmp_path / "bundle")
    source.unlink()
    restored = docgale.load_bundle(tmp_path / "bundle")
    for target in ("markdown", "html", "latex", "docx", "epub", "pdf", "structured_content", "content_list", "content_list_v2"):
        restored.export(tmp_path / "outputs" / target / "result", output_format=target)
    assert restored.to_dict() == before
    assert result.to_dict() == before
    assert restored.model_json is not None


def test_invalid_bundle_asset_is_rejected(tmp_path: Path) -> None:
    """损坏素材不会被当作有效结果包静默恢复。"""
    from docgale.assets import AssetStore
    from docgale.result import DocumentResult
    from docgale.schema import MiddleJson

    result = DocumentResult(
        MiddleJson(pages=[], file_suffix="html", is_full_document=True), AssetStore({"images/a.png": b"image"})
    )
    result.save_bundle(tmp_path)
    (tmp_path / "images/a.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        docgale.load_bundle(tmp_path)


def test_asset_paths_cannot_escape() -> None:
    """素材索引不允许绝对路径或目录穿越。"""
    from docgale.assets import AssetStore

    with pytest.raises(ValueError):
        AssetStore({"../outside.png": b"data"})


def test_bundle_rejects_unmaterialized_asset_before_writing(tmp_path: Path) -> None:
    """结果包不能把缺失图片当作成功导出，避免离线恢复后才发现丢失。"""
    from docgale.result import DocumentResult
    from docgale.schema import ImageBlock, ImageBodyBlock, MiddleJson, PageInfo

    middle = MiddleJson(
        pages=[
            PageInfo(
                page_idx=0,
                blocks=[
                    ImageBlock(
                        type="image",
                        index=0,
                        content=[ImageBodyBlock(type="image_body", index=0, content="", image_path="images/missing.png")],
                    )
                ],
            )
        ],
        file_suffix="html",
        is_full_document=True,
    )
    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="Missing materialized asset"):
        DocumentResult(middle).save_bundle(target)
    assert not target.exists()


def test_materialized_image_does_not_keep_external_render_dependency(tmp_path: Path) -> None:
    """已有图片字节在副本中改用本地素材，原始来源对象不被修改。"""
    from docgale.result import DocumentResult
    from docgale.schema import ImageBlock, ImageBodyBlock, MiddleJson, PageInfo

    image = BytesIO()
    Image.new("RGB", (2, 2), color="green").save(image, format="PNG")
    body = ImageBodyBlock(
        type="image_body",
        index=0,
        content="",
        image_url="https://example.invalid/image.png",
        image_base64="data:image/png;base64," + base64.b64encode(image.getvalue()).decode(),
    )
    middle = MiddleJson(
        pages=[PageInfo(page_idx=0, blocks=[ImageBlock(type="image", index=0, content=[body])])],
        file_suffix="html",
        is_full_document=True,
    )
    DocumentResult(middle).save_bundle(tmp_path / "bundle")
    restored = docgale.load_bundle(tmp_path / "bundle")
    restored_body = restored.middle_json.pages[0].blocks[0].content[0]
    assert restored_body.image_url is None
    assert restored.assets[restored_body.image_path] == image.getvalue()
    assert body.image_url == "https://example.invalid/image.png"
