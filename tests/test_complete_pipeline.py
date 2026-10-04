"""Verify the complete closed loop of native parsing, resource lifecycle and offline result packages."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

import docvortex
from docvortex.document.pdf import PDFDocument


def pdf_bytes() -> bytes:
    """Generate text-based PDF that can be independently reused without relying on external corpus of the warehouse."""
    output = BytesIO()
    canvas = Canvas(output)
    for index in range(30):
        canvas.drawString(40, 800 - index * 20, f"Native document line {index}: deterministic text extraction.")
    canvas.save()
    return output.getvalue()


def test_native_parse_never_classifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit native entry does not implicitly call classification or trigger OCR."""

    def forbidden(_document: PDFDocument) -> str:
        """Once native parsing is called, classification fails immediately."""
        raise AssertionError("Native parsing must not classify")

    monkeypatch.setattr(PDFDocument, "classify", forbidden)
    result = docvortex.parse(pdf_bytes(), keep_model_json=True)
    assert result.middle_json.pages
    assert result.model_json is not None


def test_classification_is_cached_in_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller can classify first and then parse, and the same document will not be classified repeatedly."""
    from docvortex.document.pdf import _document as module

    calls: list[bytes] = []

    def classify(_handle: object, payload: bytes) -> str:
        """Log actual classification calls, returning stable text results."""
        calls.append(payload)
        return "txt"

    monkeypatch.setattr(module, "classify", classify)
    with PDFDocument(pdf_bytes()) as document:
        assert document.classify() == document.classify() == "txt"
        docvortex.analyze(document)
        assert document.page_count == 1
    assert len(calls) == 1


def test_bundle_renders_after_source_is_deleted(tmp_path: Path) -> None:
    """After removing the source files, rely on the results package to reuse the same footage in seven targets."""
    image = BytesIO()
    Image.new("RGB", (20, 10), color="blue").save(image, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    source = tmp_path / "source.html"
    source.write_text(f'<h1>Portable</h1><p>One parse, many outputs.</p><img src="{data_uri}" alt="Example">')
    result = docvortex.parse(source, keep_model_json=True)
    assert result.assets
    before = result.to_dict()
    result.save_bundle(tmp_path / "bundle")
    source.unlink()
    restored = docvortex.load_bundle(tmp_path / "bundle")
    for target in ("markdown", "html", "latex", "docx", "epub", "pdf", "structured_content"):
        restored.export(tmp_path / "outputs" / target / "result", output_format=target)
    assert restored.to_dict() == before
    assert result.to_dict() == before
    assert restored.model_json is not None


def test_invalid_bundle_asset_is_rejected(tmp_path: Path) -> None:
    """Corrupted footage will not be silently restored as a valid result package."""
    from docvortex.assets import AssetStore
    from docvortex.result import DocumentResult
    from docvortex.schema import MiddleJson

    result = DocumentResult(
        MiddleJson(
            pages=[],
            metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
            is_full_document=True,
        ),
        AssetStore({"images/a.png": b"image"}),
    )
    result.save_bundle(tmp_path)
    (tmp_path / "images/a.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        docvortex.load_bundle(tmp_path)


def test_asset_paths_cannot_escape() -> None:
    """Material indexing does not allow absolute paths or directory traversals."""
    from docvortex.assets import AssetStore

    with pytest.raises(ValueError):
        AssetStore({"../outside.png": b"data"})


def test_bundle_rejects_unmaterialized_asset_before_writing(tmp_path: Path) -> None:
    """The result package cannot treat missing images as successfully exported to avoid discovering the loss after offline recovery."""
    from docvortex.result import DocumentResult
    from docvortex.schema import ImageBlock, ImageBodyBlock, MiddleJson, PageInfo

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
        metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
        is_full_document=True,
    )
    target = tmp_path / "bundle"
    with pytest.raises(ValueError, match="Missing materialized asset"):
        DocumentResult(middle).save_bundle(target)
    assert not target.exists()


def test_materialized_image_does_not_keep_external_render_dependency(tmp_path: Path) -> None:
    """Existing image bytes are replaced with local materials in the copy, and the original source object is not modified."""
    from docvortex.result import DocumentResult
    from docvortex.schema import ImageBlock, ImageBodyBlock, MiddleJson, PageInfo

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
        metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
        is_full_document=True,
    )
    DocumentResult(middle).save_bundle(tmp_path / "bundle")
    restored = docvortex.load_bundle(tmp_path / "bundle")
    restored_body = restored.middle_json.pages[0].blocks[0].content[0]
    assert restored_body.image_url is None
    assert restored.assets[restored_body.image_path] == image.getvalue()
    assert body.image_url == "https://example.invalid/image.png"
