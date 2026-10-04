"""Verify the structure of the original layout table, content column width, rotation and block failure."""

from __future__ import annotations

import base64
from copy import deepcopy
from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.platypus import Table

from docvortex.render import PdfLayout, render_pdf
from docvortex.render._internal.pdf.diagnostics import collect_pdf_diagnostics
from docvortex.render._internal.pdf.font_plan import PreparedBlock
from docvortex.render._internal.pdf.original import OriginalPdfRenderer
from docvortex.render._internal.pdf.table import SpatialTableOptions
from docvortex.render._internal.pdf.table_layout import SpatialTableContent
from docvortex.schema import BlockBase, MiddleJson, PageInfo


def _png() -> str:
    """Generates solid color area plots distinguishable from structural tables."""
    stream = BytesIO()
    Image.new("RGB", (120, 60), "red").save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


def _text(index: int, rect: tuple, text: str, kind: str = "text") -> dict:
    """Use point coordinates to construct a text block with an independent original frame."""
    return {
        "type": kind,
        "index": index,
        "bbox": [rect[0] / 400, rect[1] / 600, rect[2] / 400, rect[3] / 600],
        "content": [{"type": "text", "content": text}],
    }


def _table(index: int, rect: tuple, html: str, *, image: bool = True, caption: bool = False, missing: bool = False) -> dict:
    """Construct strict tables with original images, table titles, and missing subboxes."""
    bbox = [rect[0] / 400, rect[1] / 600, rect[2] / 400, rect[3] / 600]
    body = {"type": "table_body", "index": index, "bbox": None if missing else bbox, "content": html}
    if image:
        body["image_base64"] = _png()
    content = [body]
    if caption:
        label = _text(index + 1, (rect[0], rect[3] + 5, rect[2], rect[3] + 25), "UNIQUE CAPTION", "table_caption")
        if missing:
            label["bbox"] = None
        content.append(label)
    return {"type": "table", "index": index, "bbox": bbox, "content": content}


def _middle(blocks: list[dict], angle: int = 0) -> MiddleJson:
    """Construct a one-page test protocol and rotate the metadata to only bind the table body numbered 10."""
    return MiddleJson(
        pages=[PageInfo.model_validate({"page_idx": 0, "blocks": blocks})],
        is_full_document=True,
        metadata={"file_suffix": "pdf", "producer": {"name": "test", "version": "1"}},
        extensions={
            "docvortex_layout": {
                "version": 1,
                "pages": [
                    {
                        "page_idx": 0,
                        "width_pt": 400,
                        "height_pt": 600,
                        "image_rotations": {"10": angle},
                    }
                ],
            }
        },
    )


class _RecordingRenderer(OriginalPdfRenderer):
    """Save temporary draw plans through the normal rendering link to verify the true final geometry."""

    def __init__(self, middle: MiddleJson) -> None:
        """Create a renderer and draw plan index that belongs only to this test."""
        super().__init__(middle, asset_resolver=None, document_title="test", page_sizes={0: (400, 600)})
        self.items: dict[int, PreparedBlock] = {}

    def _prepare_block(
        self,
        block: BlockBase,
        page_idx: int,
        page_width: float,
        page_height: float,
        *,
        index_entry: bool = False,
    ) -> list[PreparedBlock]:
        """Record the actual preparation results of the parent box or leaf box, without replacing the production layout algorithm."""
        items = super()._prepare_block(block, page_idx, page_width, page_height, index_entry=index_entry)
        self.items.update({item.block.index: item for item in items})
        return items


@pytest.mark.parametrize("layout", [PdfLayout.AUTO, PdfLayout.ORIGINAL])
def test_default_structure_keeps_cells_and_caption_without_region_image(layout: PdfLayout) -> None:
    """The two original format entrances retain merged cells, empty cells, and table titles by default, and do not change the input data."""
    html = "<table><tr><th colspan='2'>HEADER</th></tr><tr><td></td><td>SELECTABLE</td></tr></table>"
    middle = _middle([_table(10, (40, 60, 350, 220), html, caption=True)])
    before = deepcopy(middle)
    with collect_pdf_diagnostics() as diagnostics:
        payload = render_pdf(middle, layout=layout)
    page = PdfReader(BytesIO(payload)).pages[0]
    text = page.extract_text()
    assert "SELECTABLE" in text and "HEADER" in text
    assert text.count("UNIQUE CAPTION") == 1
    assert len(page.images) == 0
    assert not any(d.code == "pdf_table_fallback" for d in diagnostics)
    assert middle == before


def test_content_widths_reserve_name_column_and_respect_colspan() -> None:
    """The long name column is wider than the pure number column, and the cross-column header does not expand each number column separately."""
    html = (
        "<table><tr><th rowspan='2'>Catchment</th><th colspan='2'>Percentile</th></tr>"
        "<tr><th>10</th><th>20</th></tr>"
        "<tr><td>Lambrechtsbos A (South Africa)</td><td>0.8</td><td>0.9</td></tr></table>"
    )
    renderer = _RecordingRenderer(_middle([_table(10, (40, 80, 350, 250), html)]))
    renderer.render()
    content = renderer.items[10].flowables[0]
    widths = content.tables[0]._colWidths
    assert widths[0] > widths[1] * 1.5
    assert widths[1] == pytest.approx(widths[2])
    assert sum(widths) * content.scale == pytest.approx(310)


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_rotated_structured_text_has_correct_direction_and_stays_in_box(angle: int) -> None:
    """Verify structure table orientation from real PDF text coordinates instead of just checking the angle field of Flowable."""
    html = "<table><tr><td>FIRSTROW</td></tr><tr><td>LASTROW</td></tr></table>"
    renderer = _RecordingRenderer(_middle([_table(10, (60, 80, 200, 350), html)], angle))
    payload = renderer.render()
    positions = {}

    def visit(text: str, cm: list, tm: list, font: object, size: float) -> None:
        """Multiply the text local coordinates by the current canvas matrix to get the page coordinates."""
        if text.strip():
            positions[text.strip()] = (tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5])

    page = PdfReader(BytesIO(payload)).pages[0]
    page.extract_text(visitor_text=visit)
    first, last = positions["FIRSTROW"], positions["LASTROW"]
    if angle == 0:
        assert first[1] > last[1]
    elif angle == 180:
        assert first[1] < last[1]
    elif angle == 90:
        assert first[0] > last[0]
    else:
        assert first[0] < last[0]
    for x, y in (first, last):
        assert 60 <= x <= 200 and 250 <= y <= 520
    assert len(page.images) == 0


def test_safe_expansion_respects_frozen_text_and_adjacent_table() -> None:
    """The table only uses safe space, leaving the original frame of the table, and the position of the text will not be moved."""
    html = "<table>" + "<tr><td>ROW</td><td>123</td></tr>" * 8 + "</table>"
    middle = _middle(
        [
            _text(0, (40, 40, 180, 100), "A substantial paragraph above the table."),
            _table(10, (40, 110, 180, 125), html),
            _table(20, (40, 230, 180, 245), html),
            _text(30, (40, 365, 180, 425), "A substantial paragraph below both tables."),
            _text(40, (220, 40, 360, 425), "The other column remains occupied."),
        ]
    )
    renderer = _RecordingRenderer(middle)
    renderer.render()
    first, second = renderer.items[10], renderer.items[20]
    assert first.draw_rect[3] > first.original_rect[3]
    assert first.draw_rect[3] + 2 <= second.draw_rect[1] + 1e-6
    assert first.draw_rect[1] >= 102 and second.draw_rect[3] <= 363
    assert first.draw_rect[2] <= 180 and second.draw_rect[2] <= 180
    assert renderer.items[0].draw_rect is None and renderer.items[30].draw_rect is None


def test_tiny_region_retains_structure_below_six_points_and_full_width() -> None:
    """When there is no available blank space, the structure table will continue to be reduced and a low font size will be reported, without falling back to the original image due to readability limitations."""
    html = "<table>" + "<tr><td>RETAINED</td><td>123</td></tr>" * 16 + "</table>"
    renderer = _RecordingRenderer(
        _middle(
            [
                _text(0, (40, 50, 180, 100), "BEFORE"),
                _table(10, (40, 102, 180, 109), html),
                _text(20, (40, 111, 180, 155), "AFTER"),
            ]
        )
    )
    with collect_pdf_diagnostics() as diagnostics:
        payload = renderer.render()
    page = PdfReader(BytesIO(payload)).pages[0]
    flow = renderer.items[10].flowables[0]
    assert flow.effective_font_size < 6
    assert flow.width == pytest.approx(140)
    assert flow.height <= 7 + 1e-6
    assert page.extract_text().count("RETAINED") == 16
    assert len(page.images) == 0
    assert any(d.code == "pdf_layout_small_text" and "Structured table" in d.message for d in diagnostics)
    assert not any(d.code == "pdf_table_fallback" for d in diagnostics)


def test_missing_child_boxes_reserve_caption_inside_parent() -> None:
    """The parent table combination that lacks child boxes still retains the complete structure and table title, and does not cover the table title on the cell."""
    html = "<table>" + "<tr><td>GROUP CELL</td></tr>" * 5 + "</table>"
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 240, 150), html, caption=True, missing=True)]))
    page = PdfReader(BytesIO(renderer.render())).pages[0]
    item = renderer.items[10]
    assert page.extract_text().count("GROUP CELL") == 5
    assert page.extract_text().count("UNIQUE CAPTION") == 1
    assert item.fit.height <= 90 + 1e-6
    assert len(page.images) == 0
    assert len(item.fit.measurements) == 2


@pytest.mark.parametrize(
    "html,image",
    [
        ("", True),
        ("<table><tr><td rowspan='3'>BROKEN</td></tr></table>", True),
        ("<table><tr><td rowspan='3'>VISIBLE</td></tr></table>", False),
        ("", False),
    ],
)
def test_missing_or_invalid_html_uses_image_text_or_placeholder(html: str, image: bool) -> None:
    """When HTML is unavailable, click on the block to find out. Missing pictures will not interrupt the entire PDF."""
    with collect_pdf_diagnostics() as diagnostics:
        payload = render_pdf(_middle([_table(10, (40, 60, 240, 180), html, image=image)]))
    page = PdfReader(BytesIO(payload)).pages[0]
    assert len(page.images) == int(image)
    if not image:
        assert ("VISIBLE" if html else "table unavailable") in page.extract_text()
    assert any(d.code == "pdf_table_fallback" for d in diagnostics)


def test_drawing_failure_is_detected_before_final_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    """When an error occurs in the actual drawing phase, the pre-drawn canvas is discarded and part of the structural content is not superimposed on the fallback map."""

    class BrokenTable(Table):
        def draw(self) -> None:
            """Simulation ReportLab The exception was discovered during the drawing process."""
            self.canv.drawString(0, 0, "PARTIAL TABLE")
            raise ValueError("drawing rejected")

    def broken(self: OriginalPdfRenderer, content: str, **kwargs: object) -> list[Table]:
        """Only the table materialization result is replaced, and the real fit, pre-drawing and material extraction are still performed."""
        return [BrokenTable([["CELL"]], colWidths=[100])]

    monkeypatch.setattr(OriginalPdfRenderer, "_html_tables", broken)
    with collect_pdf_diagnostics() as diagnostics:
        payload = render_pdf(_middle([_table(10, (40, 60, 240, 180), "<table><tr><td>CELL</td></tr></table>")]))
    page = PdfReader(BytesIO(payload)).pages[0]
    assert len(page.images) == 1
    assert "PARTIAL TABLE" not in page.extract_text()
    assert any("drawing rejected" in d.message for d in diagnostics)


def test_rich_nested_cells_and_images_are_preserved() -> None:
    """Nested tables, superscripts and subscripts, mixed Chinese and English, formulas, links and cell images are still rendered by shared cells."""
    html = (
        "<table><tr><th colspan='2'>标题 Header</th></tr><tr><td rowspan='2'>"
        "<b>Bold</b> H<sub>2</sub>O <eq>x^2</eq><a href='https://example.com'>LINK</a></td>"
        "<td><table><tr><td>Nested</td></tr></table></td></tr>"
        f"<tr><td><img src='{_png()}'></td></tr></table>"
    )
    with collect_pdf_diagnostics() as diagnostics:
        payload = render_pdf(_middle([_table(10, (40, 60, 360, 400), html)]))
    page = PdfReader(BytesIO(payload)).pages[0]
    text = page.extract_text()
    assert all(word in text for word in ["Header", "Bold", "Nested", "LINK"])
    assert len(page.images) == 1
    assert len(page.get("/Annots", [])) == 1
    assert not any(d.code == "pdf_table_fallback" for d in diagnostics)


def test_repeated_fit_rebuilds_columns_and_restores_font_size() -> None:
    """When the narrow and short layout is relaxed again, the column width, font size and height return to the new materialized results."""
    html = "<table><tr><td>Name</td><td>Value</td></tr><tr><td>Long descriptive name</td><td>12</td></tr></table>"
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 360, 400), html)]))
    renderer.render()
    flow = renderer.items[10].flowables[0]
    assert isinstance(flow, SpatialTableContent)
    flow.fit(320, 300)
    expected = (flow.width, flow.height, flow.font_size, tuple(flow.tables[0]._colWidths))
    flow.fit(15, 5)
    flow.fit(320, 300)
    assert (flow.width, flow.height, flow.font_size, tuple(flow.tables[0]._colWidths)) == expected
    assert flow.failure is None
    assert renderer.styles.table_cell.fontSize == 8.5
    assert renderer.styles.table_cell.leading == 11


def test_spatial_options_do_not_change_reflow_defaults() -> None:
    """Compact font size and content column width are only explicitly enabled by the original format, shared reflow tables still use fixed-width columns."""
    html = "<table><tr><td>A long name here</td><td>1</td></tr></table>"
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 350, 200), html)]))
    block = renderer.middle_json.pages[0].blocks[0].content[0]
    renderer._html_tables(html, page_idx=0, block=block, available_width=300, spatial=SpatialTableOptions(6))
    table = renderer._html_tables(html, page_idx=0, block=block, available_width=300)[0]
    assert table._colWidths == [150, 150]
    assert table._cellStyles[0][0].leftPadding == 5
    assert table._cellStyles[0][0].topPadding == 4
    assert renderer.styles.table_cell.fontSize == 8.5


def test_long_cell_formula_is_measured_at_natural_width_before_region_scaling() -> None:
    """Long formulas are accommodated by area scaling, and the measured vectors are consistent with the actual drawn Drawing dimensions."""
    formula = "+".join(f"x_{{{index}}}^2" for index in range(40))
    html = f"<table><tr><td><eq>{formula}</eq></td><td>VALUE</td></tr></table>"
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 240, 180), html)]))
    with collect_pdf_diagnostics() as diagnostics:
        renderer.render()
    flow = renderer.items[10].flowables[0]
    assert flow.failure is None
    assert flow.width <= 200 + 1e-6
    for image in renderer.inline_context.formula_images.values():
        assert image.vector.width == pytest.approx(image.vector.drawing.width)
    assert not any(d.code == "pdf_table_fallback" for d in diagnostics)


def test_rotated_table_expansion_preserves_original_page_margins() -> None:
    """Rotating tables cannot occupy the original margin space in order to increase the font size."""
    html = "<table>" + "<tr><td>FIRST</td><td>SECOND</td></tr>" * 14 + "</table>"
    renderer = _RecordingRenderer(_middle([_table(10, (60, 100, 180, 450), html)], 270))
    renderer.render()
    rect = renderer.items[10].draw_rect
    assert rect[1] >= 100 - 1e-6 and rect[3] <= 450 + 1e-6


def test_tall_inline_formula_reserves_its_ink_height_in_table_row() -> None:
    """The actual height of the high score within the cell must fit into the row height and cannot cross adjacent row borders."""
    source = r"\frac{\sum_{i=1}^N x_i^2}{\frac{a}{b}}"
    html = f"<table><tr><td><eq>{source}</eq></td></tr><tr><td>NEXT ROW</td></tr></table>"
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 360, 300), html)]))
    renderer.render()
    flow = renderer.items[10].flowables[0]
    assert flow.failure is None
    vectors = [image.vector for image in renderer.inline_context.formula_images.values()]
    assert flow.tables[0]._rowHeights[0] >= max(vector.height for vector in vectors) + 2 - 0.01


def test_long_cjk_cell_wraps_without_forcing_entire_table_to_tiny_font() -> None:
    """Continuous Chinese characters are wrapped according to the existing CJK rules. Do not mistake the entire paragraph for the minimum column width and reduce the entire table."""
    text = "这是用于验证表格换行和字号的中文内容" * 5
    html = f"<table><tr><td>{text}</td><td>123</td></tr></table>"
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 260, 350), html)]))
    renderer.render()
    flow = renderer.items[10].flowables[0]
    assert flow.failure is None
    assert flow.font_size == 8.5 and flow.scale == 1.0
    assert flow.height > 20


def test_short_nested_table_does_not_claim_entire_parent_width() -> None:
    """Nested short tables use the inherent width of the content and do not force the outer allocated whitespace into the minimum column width."""
    html = "<table><tr><td>LEFT</td><td><table><tr><td>Nested</td></tr></table></td></tr></table>"
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 280, 240), html)]))
    renderer.render()
    flow = renderer.items[10].flowables[0]
    assert flow.failure is None
    assert flow.font_size == 8.5 and flow.scale == 1.0
