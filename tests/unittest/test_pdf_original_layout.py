"""Verify public interface, page geometry and portable rendering of PDF primitive block layout."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO

import pytest
from click.testing import CliRunner
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader

from docvortex import load_bundle, parse, render_artifact
from docvortex.cli import main
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.layout import attach_layout_image_rotations, extract_layout_geometry
from docvortex.render import PdfLayout, PdfRenderOptions, RenderFormat, render, render_pdf
from docvortex.render._internal.pdf.formula import FormulaRenderer, PdfFormulaError
from docvortex.schema import MiddleJson, PageInfo, Producer


def _middle(pages: list[dict], *, suffix: str = "pdf", sizes: list[tuple[int, float, float]] | None = None) -> MiddleJson:
    """Constructs a strict document with explicit source page geometry, retaining non-consecutive page numbers to test mapping."""
    return MiddleJson(
        pages=[PageInfo.model_validate(page) for page in pages],
        is_full_document=False,
        metadata={"file_suffix": suffix, "producer": Producer(name="test", version="1")},
        extensions={
            "docvortex_layout": {
                "version": 1,
                "pages": [
                    {"page_idx": idx, "width_pt": width, "height_pt": height}
                    for idx, width, height in (sizes or [(page["page_idx"], 400, 600) for page in pages])
                ],
            }
        },
    )


def _text(text: str, *, index: int = 0, bbox: tuple = (0.1, 0.1, 0.45, 0.3), **kwargs) -> dict:
    """Generate text blocks that can cover different block types and continuation markers."""
    return {"type": "text", "index": index, "bbox": bbox, "content": [{"type": "text", "content": text}], **kwargs}


def _reader(payload: bytes) -> PdfReader:
    """Export from bytes Check the pages and text of the real PDF."""
    return PdfReader(BytesIO(payload))


def _png_uri() -> str:
    """Create an image with the same aspect ratio as the test area to verify absolute positioning."""
    output = BytesIO()
    Image.new("RGB", (100, 100), "red").save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def _source_pdf() -> bytes:
    """Generates true source PDF including rotated, non-A4, blank pages, and landscape pages."""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(400, 600))
    canvas.drawString(40, 500, "FIRST PAGE")
    canvas.showPage()
    canvas.setPageSize((320, 480))
    canvas.showPage()
    canvas.setPageSize((600, 400))
    canvas.drawString(40, 300, "THIRD PAGE")
    canvas.showPage()
    canvas.save()
    writer = PdfWriter(clone_from=BytesIO(output.getvalue()))
    writer.pages[1].rotate(90)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def test_original_preserves_pages_auxiliaries_continuations_and_input() -> None:
    """Auxiliary text and continuation paragraphs remain on the original page, and blank pages and different page sizes are not folded."""
    middle = _middle(
        [
            {"page_idx": 1, "blocks": [_text("HEADER", type="header"), _text("inter-", index=1, bbox=(0.1, 0.4, 0.5, 0.5))]},
            {"page_idx": 2, "blocks": [_text("national", continues_prev=True)]},
            {"page_idx": 6, "blocks": []},
        ],
        sizes=[(1, 400, 600), (2, 320, 480), (6, 600, 400)],
    )
    before = deepcopy(middle)
    payload = render_pdf(middle)
    reader = _reader(payload)
    assert payload == render_pdf(middle, layout=PdfLayout.ORIGINAL)
    assert len(reader.pages) == 3
    assert [tuple(page.mediabox)[2:] for page in reader.pages] == [(400, 600), (320, 480), (600, 400)]
    assert "HEADER" in reader.pages[0].extract_text()
    assert "inter-" in reader.pages[0].extract_text()
    assert "national" not in reader.pages[0].extract_text()
    assert "national" in reader.pages[1].extract_text()
    assert reader.pages[2].extract_text() == ""
    assert middle == before
    assert len(_reader(render_pdf(middle, layout=PdfLayout.REFLOW)).pages) == 1


def test_text_and_image_positions_match_source_boxes() -> None:
    """Use the PDF native object to check the position of the text column and the four sides of the image, with a tolerance not exceeding 0.5 pt."""
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    _text("LEFT"),
                    _text("RIGHT", index=1, bbox=(0.6, 0.1, 0.95, 0.3)),
                    {
                        "type": "image",
                        "index": 2,
                        "bbox": (0.1, 0.5, 0.6, 5 / 6),
                        "content": [
                            {
                                "type": "image_body",
                                "index": 2,
                                "bbox": (0.1, 0.5, 0.6, 5 / 6),
                                "content": "",
                                "image_base64": _png_uri(),
                            },
                        ],
                    },
                ],
            }
        ]
    )
    payload = render_pdf(middle)
    positions = {}

    def visit(text, cm, tm, font, size) -> None:
        """Logging the actual text coordinate transformation reported by the PDF extractor."""
        if text.strip():
            positions[text.strip()] = (tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5])

    _reader(payload).pages[0].extract_text(visitor_text=visit)
    assert positions["LEFT"][0] == pytest.approx(40, abs=0.5)
    assert positions["RIGHT"][0] == pytest.approx(240, abs=0.5)
    assert 420 <= positions["LEFT"][1] <= 540
    with PDFDocument(payload) as document:
        image = document.get_page_image_infos(0)[0]
        assert image.bbox == pytest.approx((40, 300, 240, 500), abs=0.5)


@pytest.mark.parametrize("damage", ["missing", "partial", "version", "duplicate", "negative", "boolean", "bbox"])
def test_auto_falls_back_for_incomplete_geometry_and_original_rejects(damage: str) -> None:
    """Old data and illegal geometries fall back overall at AUTO, explicit ORIGINAL fails before export."""
    middle = _middle([{"page_idx": 3, "blocks": [_text("body")]}])
    extension = middle.extensions["docvortex_layout"]
    if damage == "missing":
        middle.extensions.clear()
    elif damage == "partial":
        extension["pages"] = []
    elif damage == "version":
        extension["version"] = 2
    elif damage == "duplicate":
        extension["pages"] *= 2
    elif damage == "negative":
        extension["pages"][0]["width_pt"] = -1
    elif damage == "bbox":
        middle.pages[0].blocks[0].bbox = None
    else:
        extension["pages"][0]["height_pt"] = True
    artifact = render_artifact(middle, "pdf")
    assert "body" in _reader(artifact.content).pages[0].extract_text()
    assert [item.code for item in artifact.diagnostics] == ["pdf_layout_reflow_fallback"]
    with pytest.raises(ValueError):
        render_pdf(middle, layout=PdfLayout.ORIGINAL)


@pytest.mark.parametrize("suffix", ["ofd", "docx", "html"])
def test_non_pdf_sources_keep_reflow_and_reject_original(suffix: str) -> None:
    """OFD and streaming sources maintain reflow even when carrying geometry extensions and do not enable first-version restore."""
    middle = _middle([{"page_idx": 0, "blocks": [_text("body")]}], suffix=suffix)
    artifact = render_artifact(middle, "pdf")
    assert artifact.content == render_pdf(middle, layout=PdfLayout.REFLOW)
    assert not artifact.diagnostics
    with pytest.raises(ValueError, match="source format"):
        render_pdf(middle, layout=PdfLayout.ORIGINAL)


def test_overflow_preserves_all_text_and_reports_small_type() -> None:
    """The minimal box still contains the final text and outputs low font size and scaling diagnostics."""
    middle = _middle([{"page_idx": 0, "blocks": [_text("word " * 100 + "ENDMARK", bbox=(0.1, 0.1, 0.2, 0.12))]}])
    artifact = render_artifact(middle, "pdf")
    text = _reader(artifact.content).pages[0].extract_text()
    assert "ENDMARK" in "".join(text.split())
    assert text.count("word") == 100
    assert {item.code for item in artifact.diagnostics} >= {"pdf_layout_small_text", "pdf_layout_scaled"}
    assert all(item.page_index == 0 for item in artifact.diagnostics)


def test_structured_table_replaces_region_image_without_duplicate_caption() -> None:
    """The table structure text is given priority, and the entire table image is not superimposed. The table title is still positioned independently and appears only once."""
    bbox = (0.1, 0.1, 0.6, 0.5)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "table",
                        "index": 0,
                        "bbox": bbox,
                        "content": [
                            {
                                "type": "table_body",
                                "index": 0,
                                "bbox": bbox,
                                "content": "<table><tr><td>HIDDEN CELL</td></tr></table>",
                                "image_base64": _png_uri(),
                            },
                            _text("CAPTION", type="table_caption", index=1, bbox=(0.1, 0.55, 0.6, 0.6)),
                        ],
                    },
                ],
            }
        ]
    )
    reader = _reader(render_pdf(middle))
    assert reader.pages[0].extract_text().count("CAPTION") == 1
    assert "HIDDEN CELL" in reader.pages[0].extract_text()
    assert len(reader.pages[0].images) == 0


def test_table_without_image_uses_structured_content() -> None:
    """Normally output structure text when area map is missing, and do not report HTML rendering as degraded."""
    bbox = (0.1, 0.1, 0.9, 0.8)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "table",
                        "index": 0,
                        "bbox": bbox,
                        "content": [
                            {
                                "type": "table_body",
                                "index": 0,
                                "bbox": bbox,
                                "content": "<table><tr><td>CELL</td></tr></table>",
                            },
                        ],
                    },
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    assert "CELL" in _reader(artifact.content).pages[0].extract_text()
    assert "pdf_table_layout" in {item.code for item in artifact.diagnostics}
    assert "pdf_table_fallback" not in {item.code for item in artifact.diagnostics}


def test_missing_child_geometry_uses_parent_group() -> None:
    """The combinations that lack legend coordinates are placed into the parent box as a whole, and the materials and text are retained."""
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "image",
                        "index": 0,
                        "bbox": (0.1, 0.1, 0.9, 0.9),
                        "content": [
                            {"type": "image_body", "index": 0, "content": "", "image_base64": _png_uri()},
                            _text("CAPTION", type="image_caption", index=1, bbox=None),
                        ],
                    },
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    assert "CAPTION" in _reader(artifact.content).pages[0].extract_text()
    assert "pdf_layout_approximate" in {item.code for item in artifact.diagnostics}


def test_formula_image_fallback_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the formula vector fails, the existing area map is used, and the error can be located in the high-level product."""

    def fail_formula(*args, **kwargs):
        """Simulate unconvertible formulas to override picture degradation."""
        raise PdfFormulaError("test unsupported formula")

    monkeypatch.setattr(FormulaRenderer, "render", fail_formula)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "equation",
                        "index": 0,
                        "bbox": (0.1, 0.1, 0.6, 0.5),
                        "content": "bad",
                        "image_base64": _png_uri(),
                    },
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    assert len(_reader(artifact.content).pages[0].images) == 1
    assert "pdf_formula_fallback" in {item.code for item in artifact.diagnostics}


def test_index_links_keep_source_page_numbers() -> None:
    """The fixed layout table of contents retains the original text page numbers and correctly links to the following text targets."""
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "index",
                        "index": 0,
                        "bbox": (0.1, 0.1, 0.9, 0.2),
                        "content": [
                            _text("Chapter 1 ..... 42", anchor="chapter", bbox=(0.1, 0.1, 0.9, 0.2)),
                        ],
                    },
                    _text("Chapter 1", type="paragraph_title", level=2, index=1, anchor="chapter", bbox=(0.1, 0.4, 0.9, 0.5)),
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    page = _reader(artifact.content).pages[0]
    assert "42" in page.extract_text()
    assert len(page["/Annots"]) == 1
    assert not {item.code for item in artifact.diagnostics} & {"pdf_unmatched_link", "pdf_duplicate_anchor"}


def test_pdf_parse_and_bundle_retain_selected_page_geometry(tmp_path) -> None:
    """After real parsing, post-processing and bundle round-trip, the selected page can still be exported without source files."""
    result = parse(_source_pdf(), file_suffix="pdf", page_range="1,3", keep_model_json=True)
    extension = result.middle_json.extensions["docvortex_layout"]
    assert extension == result.model_json.extensions["docvortex_layout"]
    assert extension["pages"] == [
        {"page_idx": 0, "width_pt": 400, "height_pt": 600},
        {"page_idx": 2, "width_pt": 600, "height_pt": 400},
    ]
    before = result.middle_json.to_dict()
    result.save_bundle(tmp_path / "bundle")
    restored = load_bundle(tmp_path / "bundle")
    payload = render_artifact(restored.middle_json, "pdf", assets=restored.assets).content
    assert len(_reader(payload).pages) == 2
    assert restored.middle_json.to_dict() == before == result.middle_json.to_dict()


def test_rotation_and_blank_page_geometry_survive_parsing() -> None:
    """The visible width and height of the rotated blank page are swapped only once, and the original paging remains intact."""
    result = parse(_source_pdf(), file_suffix="pdf")
    reader = _reader(render_pdf(result.middle_json))
    assert len(reader.pages) == 3
    assert tuple(reader.pages[1].mediabox)[2:] == (480, 320)
    assert reader.pages[1].extract_text() == ""


def test_geometry_failure_is_nonfatal_during_analysis() -> None:
    """Preserves diagnostics when a page size cannot be obtained and does not forge A4 sizes."""

    class BrokenDocument:
        page_count = 1

        def page_size(self, page_idx):
            """Simulated underlying page size read failure."""
            raise RuntimeError("broken geometry")

    geometry, diagnostics = extract_layout_geometry(BrokenDocument(), [7])
    assert geometry["pages"] == []
    assert diagnostics[0].page_index == 7


def test_layout_options_are_strict_and_unified_render_is_equivalent() -> None:
    """Unified portals use the same layout as direct portals and reject string impersonation of strict enumerations."""
    middle = _middle([{"page_idx": 0, "blocks": [_text("body")]}])
    assert render(middle, RenderFormat.PDF, options=PdfRenderOptions(layout=PdfLayout.ORIGINAL)) == render_pdf(middle)
    with pytest.raises(TypeError, match="PdfLayout"):
        render_pdf(middle, layout="original")
    with pytest.raises(TypeError, match="PdfLayout"):
        PdfRenderOptions(layout="original")


def test_concurrent_diagnostics_do_not_leak() -> None:
    """When exporting both the old PDF and the new PDF, the fallback diagnostics only belong to the corresponding call."""
    middle = _middle([{"page_idx": 0, "blocks": [_text("body")]}])
    legacy = middle.model_copy(deep=True)
    legacy.extensions.clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: render_artifact(value, "pdf"), [middle, legacy]))
    assert not results[0].diagnostics
    assert [item.code for item in results[1].diagnostics] == ["pdf_layout_reflow_fallback"]


def test_cli_pdf_layout_reaches_export(tmp_path) -> None:
    """Verify complete link of explicit layout options to page output via real CLI."""
    source = tmp_path / "source.pdf"
    source.write_bytes(_source_pdf())
    output = tmp_path / "out.pdf"
    runner = CliRunner()
    result = runner.invoke(main, ["convert", str(source), "-o", str(output), "--format", "pdf", "--pdf-layout", "original"])
    assert result.exit_code == 0, result.output
    assert len(_reader(output.read_bytes()).pages) == 3
    rejected = runner.invoke(main, ["convert", str(source), "-o", str(output), "--format", "html", "--pdf-layout", "original"])
    assert rejected.exit_code != 0
    assert "requires --format pdf" in rejected.output


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_rotated_region_images_restore_source_orientation(angle: int) -> None:
    """Verify that the crop direction is correctly undone via the actual rendered pixels of the two-color material."""
    image = Image.new("RGB", (200, 100), "red")
    image.paste("blue", (100, 0, 200, 100))
    data = BytesIO()
    image.save(data, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
    width, height = (100, 200) if angle in (90, 270) else (200, 100)
    bbox = (0.1, 0.1, 0.1 + width / 400, 0.1 + height / 600)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "table",
                        "index": 0,
                        "bbox": bbox,
                        "content": [
                            {"type": "table_body", "index": 0, "bbox": bbox, "content": "", "image_base64": uri},
                        ],
                    },
                ],
            }
        ]
    )
    middle.extensions["docvortex_layout"]["pages"][0]["image_rotations"] = {"0": angle}
    with PDFDocument(render_pdf(middle)) as document:
        rendered = document.render_page(0, scale=1).pil_image.convert("RGB")
        if angle in (90, 270):
            first = rendered.getpixel((40 + width // 2, 60 + height // 4))
            second = rendered.getpixel((40 + width // 2, 60 + height * 3 // 4))
        else:
            first = rendered.getpixel((40 + width // 4, 60 + height // 2))
            second = rendered.getpixel((40 + width * 3 // 4, 60 + height // 2))
    expected = [(255, 0, 0), (0, 0, 255)] if angle == 90 else [(0, 0, 255), (255, 0, 0)]
    assert [first, second] == expected


def test_crop_rotation_metadata_uses_source_page_and_block_indices() -> None:
    """The cropping angle is related to the source page number after page selection and the final raw block index. Missing material does not declare the angle."""
    extension = {"version": 1, "pages": [{"page_idx": 7, "width_pt": 400, "height_pt": 600}]}
    pages = [
        [
            {"type": "text", "angle": 90},
            {"type": "table", "angle": 270, "image_base64": _png_uri()},
            {"type": "equation", "angle": 90},
        ]
    ]
    attach_layout_image_rotations(extension, pages, [7])
    assert extension["pages"][0]["image_rotations"] == {"1": 270}


def test_rotated_cropped_page_keeps_image_position() -> None:
    """A rotated page with CropBox offset is transformed only once, and the actual picture position is consistent with the source page."""
    source_bytes = BytesIO()
    canvas = Canvas(source_bytes, pagesize=(400, 600))
    canvas.drawImage(ImageReader(Image.new("RGB", (100, 100), "red")), 100, 200, width=100, height=100)
    canvas.save()
    writer = PdfWriter(clone_from=BytesIO(source_bytes.getvalue()))
    writer.pages[0].cropbox.lower_left = (20, 30)
    writer.pages[0].cropbox.upper_right = (380, 550)
    writer.pages[0].rotate(90)
    source = BytesIO()
    writer.write(source)
    result = parse(source.getvalue(), file_suffix="pdf")
    artifact = render_artifact(result.middle_json, "pdf", assets=result.assets)
    with PDFDocument(source.getvalue()) as original, PDFDocument(artifact.content) as rebuilt:
        assert original.page_size(0) == rebuilt.page_size(0) == (520, 360)
        assert rebuilt.get_page_image_infos(0)[0].bbox == pytest.approx(original.get_page_image_infos(0)[0].bbox, abs=0.5)


@pytest.mark.parametrize("layout", [PdfLayout.ORIGINAL, PdfLayout.REFLOW])
def test_bullet_text_is_copyable_as_unicode(layout: PdfLayout) -> None:
    """The list symbol is still Unicode bullet after copying to avoid standard fonts from generating DEL control characters."""
    middle = _middle([{"page_idx": 0, "blocks": [_text("• 项目 Bullet item")]}])
    text = _reader(render_pdf(middle, layout=layout)).pages[0].extract_text()
    assert "• 项目 Bullet item" in text
    assert "\x7f" not in text
