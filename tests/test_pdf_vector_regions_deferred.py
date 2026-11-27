"""覆盖分面图延后认领时的标题、重复白字和来源成员守恒。"""

from dataclasses import replace
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from docvortex import parse
from docvortex.analyzers.native.pdf.models import (
    _DocumentBodyProfile,
    _DocumentTitleProfile,
    _LineItem,
    _PreparedPage,
    _TitleStylePrototype,
)
from docvortex.analyzers.native.pdf.pipeline import _finalize_prepared_page
from docvortex.render.markdown import render_markdown

from test_pdf_vector_regions import LABELS, _block_text, _region_pdf


def test_labeled_regions_keep_repeated_white_rows_out_of_markdown():
    """有图的前后页仍为白字空白页提供重复证据，只有有墨迹的分面才接收标签。"""
    output = BytesIO()
    writer = PdfWriter()
    for mode in ("labels", "white-blank", "labels"):
        document = _region_pdf(208, 668, 8, (0.15, 0.55, 0.53), False, mode, page_size=(420, 800))
        writer.add_page(PdfReader(BytesIO(document)).pages[0])
    writer.write(output)
    result = parse(output.getvalue(), parse_mode="txt", keep_model_json=True)
    first, blank, last = result.model_json.pages
    for page in (first, last):
        (figure,) = (block for block in page if block["type"] == "image")
        assert set(figure["content"].split()) == set(LABELS)
        assert not any(block["type"] == "header" for block in page)
    assert not any(block["type"] == "image" for block in blank)
    assert {word for block in blank if block["type"] == "header" for word in _block_text(block).split()} == set(LABELS)
    blank_page = result.middle_json.pages[1]
    middle = result.middle_json.model_copy(update={"pages": [blank_page], "is_full_document": False})
    assert all(label not in render_markdown(middle).split() for label in LABELS)


def _source_line(text, bbox, index, em=7.272, *, semantic_type=None):
    """构造保留物理行、样式和原生身份的紧凑准备阶段证据。"""
    return _LineItem(
        text,
        bbox,
        0,
        index,
        effective_height=em,
        visual_row_id=index,
        font_signature=("DisplayFace", 0),
        font_coverage=1,
        dominant_font_weight=410,
        semantic_type=semantic_type,
    )


def test_deferred_map_does_not_turn_adjacent_multiline_title_into_body():
    """复现图旁三行标题和正文另在既有容器时的局部尺度与原型冲突。"""
    from docvortex.analyzers.native.pdf.models import _DeferredFilledRegion

    headings = [
        _source_line("Regional Conservation Goals", (70.36, 24.33, 302.73, 46.57), 0, 21.816),
        _source_line("and Biological Outcomes", (70.36, 49.33, 267.83, 71.57), 1, 21.816),
        _source_line("by Region", (70.37, 74.33, 167.02, 96.57), 2, 21.816),
    ]
    label_boxes = (
        (466.41, 41.78, 495.30, 49.07),
        (521.94, 30.26, 540.16, 37.55),
        (521.94, 39.26, 544.60, 46.55),
        (470.35, 80.36, 500.40, 87.66),
        (525.59, 58.44, 543.95, 65.86),
        (525.59, 67.56, 548.25, 74.86),
        (482.50, 119.52, 511.39, 126.94),
        (526.52, 93.92, 557.62, 101.22),
        (537.27, 121.55, 560.82, 128.84),
        (447.92, 111.77, 463.04, 119.07),
    )
    labels = [
        _source_line(text, box, index)
        for index, (text, box) in enumerate(
            zip(("Alder", "North", "Lake", "Cedar", "South", "Lake", "Fir", "Pine", "Elm", "Ash"), label_boxes, strict=True), 3
        )
    ]
    section = _source_line("Share information with regional partners", (78.30, 153.40, 294.52, 163.60), 13, 10.0)
    container = {"type": "code", "content": "Separately owned body", "bbox": (76.42, 169.79, 573.29, 562.92), "angle": 0}
    map_box = (432, 16, 581, 144)
    body = _DocumentBodyProfile(10.260, 390, frozenset({("BodyFace", 0)}))
    title = _DocumentTitleProfile(
        tuple(
            _TitleStylePrototype("displayface", 0, 2.126, 410, alignment, offset, 14, 9)
            for alignment, offset in (("left", 0.0), ("center", -0.1))
        )
    )
    expected_title = " ".join(line.text for line in headings)
    early_image = {"type": "image", "bbox": map_box, "content": "Alder Lake Cedar Fir Pine Elm Ash", "angle": 0}
    early = _PreparedPage((612, 792), [replace(line) for line in (*headings, section)], [], [], [dict(container), early_image])
    early_blocks = _finalize_prepared_page(early, 8, document_body_profile=body, document_title_profile=title)
    assert any(block["type"] == "text" and " ".join(block["content"].split()) == expected_title for block in early_blocks)
    prepared = _PreparedPage(
        (612, 792),
        headings + labels + [section],
        [],
        [],
        [container],
        deferred_filled_regions=[_DeferredFilledRegion(map_box, tuple(replace(line) for line in labels))],
    )
    blocks = _finalize_prepared_page(prepared, 8, document_body_profile=body, document_title_profile=title)
    assert any(block["type"] == "paragraph_title" and " ".join(block["content"].split()) == expected_title for block in blocks)
    (figure,) = (block for block in blocks if block["type"] == "image")
    assert set(figure["content"].split()) == {word for line in labels for word in line.text.split()}


@pytest.mark.parametrize("competition", ["mixed", "table", "duplicate", "structural-title"])
def test_deferred_graphic_declines_incomplete_or_competing_block_ownership(competition):
    """混入正文、已有容器或成员重复时放弃整组，保留每个原始装配块。"""
    from docvortex.analyzers.native.pdf.graphics import materialize_deferred_filled_regions
    from docvortex.analyzers.native.pdf.models import _DeferredFilledRegion

    region = (30, 30, 150, 125)
    label = _source_line("Cedar", (60, 35, 92, 44), 1)
    other_label = _source_line("Ash", (76, 98, 98, 107), 2)
    first = {"type": "header", "content": label.text, "bbox": label.bbox, "_text_lines": [label]}
    second = {"type": "text", "content": other_label.text, "bbox": other_label.bbox, "_text_lines": [other_label]}
    if competition == "mixed":
        body = _source_line("A body row extends outside.", (140, 105, 195, 122), 3)
        second = {
            "type": "text",
            "content": "Ash A body row extends outside.",
            "bbox": (76, 98, 195, 122),
            "_text_lines": [other_label, body],
        }
        blocks = [first, second]
    elif competition == "table":
        blocks = [first, second, {"type": "table", "content": "<table></table>", "bbox": (90, 110, 170, 160)}]
    elif competition == "duplicate":
        blocks = [first, second, dict(first)]
    else:
        blocks = [first, {**second, "type": "paragraph_title", "_text_lines": [replace(other_label, structural_title=True)]}]

    original = list(blocks)
    materialize_deferred_filled_regions(blocks, [_DeferredFilledRegion(region, (label, other_label))], (230, 260))
    assert blocks == original
