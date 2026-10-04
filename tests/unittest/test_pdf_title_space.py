"""Verify that titles use white space to expand, maintain text hierarchy, and do not overlap adjacent content."""

from copy import deepcopy

import pytest

from docvortex.schema import MiddleJson
from docvortex.document.pdf import PDFDocument
from test_pdf_original_layout import _middle, _png_uri, _text
from test_pdf_title_font_plan import _render, _sizes


def _block(text, rect, *, title=False, index=0, **kwargs):
    """Use point coordinates to define composition pages to avoid normalized values from obscuring spacing boundaries."""
    x0, y0, x1, y1 = rect
    if title:
        kwargs.update(type="paragraph_title", level=2)
    return _text(text, index=index, bbox=(x0 / 400, y0 / 600, x1 / 400, y1 / 600), **kwargs)


def _image(rect, index):
    """Construct a real area map so that the title extension must respect the footprint of the visual content."""
    x0, y0, x1, y1 = rect
    bbox = (x0 / 400, y0 / 600, x1 / 400, y1 / 600)
    return {
        "type": "image",
        "index": index,
        "bbox": bbox,
        "content": [
            {"type": "image_body", "index": index, "bbox": bbox, "content": "", "image_base64": _png_uri()},
        ],
    }


def _one_page(blocks):
    """Construct a single page strict document, all measurements and drawings still go through public access."""
    blocks = deepcopy(blocks)
    for index, block in enumerate(blocks):
        block["index"] = index
        if block["type"] == "image":
            block["content"][0]["index"] = index
    return _middle([{"page_idx": 0, "blocks": blocks}])


def _title(plans, index=0):
    """Read the actual font size and safe drawing range of the current title."""
    return plans["paragraph_title:level=2"].titles[index]


def _clear(record, obstacles):
    """Check page boundaries, original left edge, and at least 2 pt spacing from other original frames."""
    x0, y0, x1, y1 = record["draw_bbox_pt"]
    assert x0 == pytest.approx(record["original_bbox_pt"][0], abs=0.001)
    assert 0 <= x0 < x1 <= 400 and -0.001 <= y0 < y1 <= 600.001
    for bx0, by0, bx1, by1 in obstacles:
        assert x1 + 2 <= bx0 + 0.001 or bx1 + 2 <= x0 + 0.001 or y1 + 2 <= by0 + 0.001 or by1 + 2 <= y0 + 0.001


def test_original_frame_stays_when_it_already_fits():
    """When the original frame is large enough, it will not be moved or expanded, and the title font size will still be 2 pt larger than the actual text."""
    rect = (40, 40, 300, 70)
    artifact, plans = _render(_one_page([_block("TITLE", rect, title=True), _block("BODY", (40, 100, 300, 180), index=1)]))
    record = _title(plans)
    assert record["draw_bbox_pt"] == pytest.approx(rect)
    assert not record["expanded"]
    assert _sizes(artifact.content)["TITLE"] == pytest.approx(_sizes(artifact.content)["BODY"] + 2, abs=0.01)


@pytest.mark.parametrize("kind,level,increment", [("paragraph_title", 6, 2), ("doc_title", 1, 4)])
def test_body_reference_overrides_old_style_caps_and_handles_no_chapters(kind, level, increment):
    """The last-level title can exceed the upper limit of the old style. When there is no chapter title, the main title uses the main text plus 4 pt."""
    artifact, _ = _render(
        _one_page(
            [
                _text("TITLE", type=kind, level=level, bbox=(0.1, 0.1, 0.9, 0.2)),
                _block("BODY", (40, 150, 300, 250), index=1),
            ]
        )
    )
    sizes = _sizes(artifact.content)
    assert sizes["TITLE"] == pytest.approx(sizes["BODY"] + increment, abs=0.01)


def test_only_upward_space_is_used_without_moving_body():
    """When the text is immediately below, the space is borrowed upward, and the original position and font size are still used for the text."""
    obstacles = [(40, 20, 300, 60), (40, 107, 300, 180)]
    artifact, plans = _render(
        _one_page(
            [
                _block("PREVIOUS", obstacles[0], index=1),
                _block("TITLE", (40, 100, 140, 105), title=True),
                _block("BODY", obstacles[1], index=2),
            ]
        )
    )
    record = _title(plans)
    assert record["draw_bbox_pt"][1] < 100 and record["draw_bbox_pt"][3] <= 105.001
    assert record["final_font_size"] == 12.5
    assert _sizes(artifact.content)["BODY"] == 10.5
    _clear(record, obstacles)


def test_only_rightward_space_keeps_title_on_one_line():
    """When both top and bottom are blocked, use the space on the right side of the same column to retain the original top edge without squeezing the front and rear text."""
    obstacles = [(40, 20, 300, 100), (40, 121, 300, 180)]
    artifact, plans = _render(
        _one_page(
            [
                _block("PREVIOUS", obstacles[0], index=1),
                _block("LONG HEADING", (40, 102, 68, 119), title=True),
                _block("BODY", obstacles[1], index=2),
            ]
        )
    )
    record = _title(plans)
    assert record["draw_bbox_pt"][1] == pytest.approx(102)
    assert record["draw_bbox_pt"][2] > 68
    assert record["final_font_size"] == 12.5
    assert "LONG HEADING" in _sizes(artifact.content)
    _clear(record, obstacles)


def test_long_title_can_wrap_inside_expanded_column():
    """Long titles can be formatted on multiple lines within the safe column width, rather than being squeezed back into a short original frame."""
    body = (40, 220, 300, 300)
    _, plans = _render(
        _one_page(
            [
                _block(
                    "A longer heading with several words repeated across multiple lines " * 3, (40, 80, 130, 85), title=True
                ),
                _block("BODY", body, index=1),
            ]
        )
    )
    record = _title(plans)
    assert record["final_font_size"] == 12.5
    assert record["draw_bbox_pt"][3] - record["draw_bbox_pt"][1] > 20
    _clear(record, [body])


def test_consecutive_titles_share_gap_without_double_claiming_it():
    """Adjacent titles are allocated space with the center line of the gap, and the original order and safe spacing are maintained after expansion."""
    _, plans = _render(
        _one_page(
            [
                _block("FIRST", (40, 100, 130, 105), title=True),
                _block("SECOND", (40, 125, 180, 130), title=True, index=1),
                _block("BODY", (40, 160, 300, 250), index=2),
            ]
        )
    )
    first, second = plans["paragraph_title:level=2"].titles
    assert first["final_font_size"] == second["final_font_size"] == 12.5
    assert first["draw_bbox_pt"][3] <= 114.001
    assert second["draw_bbox_pt"][1] >= 115.999
    _clear(first, [second["draw_bbox_pt"], (40, 160, 300, 250)])
    _clear(second, [first["draw_bbox_pt"], (40, 160, 300, 250)])


def test_two_columns_and_nearby_image_bound_right_expansion():
    """Even if there is white space next to the right column image, it cannot be invaded by the expansion of the left column title."""
    obstacles = [(40, 100, 180, 180), (220, 30, 380, 180)]
    _, plans = _render(
        _one_page(
            [
                _block("LEFT TITLE", (40, 50, 90, 55), title=True),
                _block("BODY", obstacles[0], index=1),
                _image(obstacles[1], 2),
            ]
        )
    )
    record = _title(plans)
    assert record["draw_bbox_pt"][2] <= 180.001
    assert record["final_font_size"] == 12.5
    _clear(record, obstacles)


def test_existing_spanning_title_preserves_its_span():
    """The original cross-column title retains the original span, and the following cross-column text determines the safe right boundary."""
    obstacles = [(40, 20, 170, 60), (220, 20, 360, 60), (40, 130, 360, 210)]
    _, plans = _render(
        _one_page(
            [
                _block("LEFT", obstacles[0], index=1),
                _block("RIGHT", obstacles[1], index=2),
                _block("SPANNING HEADING", (40, 80, 330, 85), title=True),
                _block("WIDE BODY", obstacles[2], index=3),
            ]
        )
    )
    record = _title(plans)
    assert 330 <= record["draw_bbox_pt"][2] <= 360
    assert record["final_font_size"] == 12.5
    _clear(record, obstacles)


def test_unknown_column_does_not_borrow_the_whole_page_width():
    """When there is only one column of text, use the entire text number, but do not use this to guess the width of the column where the title is located."""
    _, plans = _render(
        _one_page(
            [
                _block("SMALL", (40, 100, 90, 105), title=True),
                _block("OTHER COLUMN", (220, 150, 360, 230), index=1),
            ]
        )
    )
    record = _title(plans)
    assert record["draw_bbox_pt"][2] == 90
    assert record["final_font_size"] == 12.5


def test_title_at_page_edge_stays_inside_page_and_clear_of_images():
    """The title at the top of the page can only borrow space downwards and cannot cross the page or invade the image on the right."""
    obstacles = [(40, 40, 170, 100), (200, 0, 390, 50)]
    _, plans = _render(
        _one_page(
            [
                _block("EDGE", (40, 0, 130, 5), title=True),
                _block("BODY", obstacles[0], index=1),
                _image(obstacles[1], 2),
            ]
        )
    )
    record = _title(plans)
    assert record["draw_bbox_pt"][1] == 0 and record["final_font_size"] == 12.5
    _clear(record, obstacles)


def test_insufficient_space_only_shrinks_the_constrained_title():
    """The safe area is only 4. When pt is high, the current title is reduced individually, but the title of the same level on another page still reaches the target font size."""
    obstacles = [(40, 20, 300, 100), (40, 108, 300, 180)]
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    _block("PREVIOUS", obstacles[0]),
                    _block("TINY", (40, 102, 170, 106), title=True, index=1),
                    _block("BODY0", obstacles[1], index=2),
                ],
            },
            {
                "page_idx": 1,
                "blocks": [_block("ROOMY", (40, 40, 300, 70), title=True), _block("BODY1", (40, 100, 300, 180), index=1)],
            },
        ]
    )
    artifact, plans = _render(middle)
    plan = plans["paragraph_title:level=2"]
    assert plan.target_font_size == 12.5 and plan.exception_count == 1
    assert plan.titles[0]["final_font_size"] < 6 and plan.titles[1]["final_font_size"] == 12.5
    assert plan.titles[0]["reasons"] == ["insufficient_space"]
    assert _sizes(artifact.content)["BODY0"] == _sizes(artifact.content)["BODY1"] == 10.5
    assert any(d.code == "pdf_layout_small_text" for d in artifact.diagnostics)
    _clear(plan.titles[0], obstacles)


def test_existing_geometry_conflict_does_not_expand_occupied_area():
    """Report conflicts when inputs overlap. The title remains within its original frame and does not expand the original overlapping range."""
    original = (40, 80, 180, 90)
    artifact, plans = _render(
        _one_page(
            [
                _block("CONFLICT", original, title=True),
                _block("BODY", (40, 85, 300, 180), index=1),
            ]
        )
    )
    record = _title(plans)
    assert record["geometry_conflict"] and not record["expanded"]
    x0, y0, x1, y1 = record["draw_bbox_pt"]
    assert x0 >= 40 and y0 >= 80 and x1 <= 180 and y1 <= 90.001
    assert any(d.code == "pdf_title_geometry_conflict" for d in artifact.diagnostics)


def test_fallback_uses_only_exported_body_fonts():
    """When there is no body text in the same column, only the exported page will be referenced. When the title page is exported separately, it will fall back to the default text style."""
    middle = _middle(
        [
            {"page_idx": 0, "blocks": [_block("SMALL BODY", (40, 50, 300, 62.2))]},
            {"page_idx": 1, "blocks": [_block("TITLE", (40, 60, 300, 65), title=True)]},
        ]
    )
    _, full = _render(middle)
    _, selected = _render(middle.model_copy(update={"pages": [middle.pages[1]]}))
    assert full["paragraph_title:level=2"].target_font_size == 9.7
    assert selected["paragraph_title:level=2"].target_font_size == 12.5


@pytest.mark.parametrize("formula", ["x^2", r"\frac{\sum_{i=1}^n x_i}{y}"])
@pytest.mark.parametrize("narrow", [False, True])
def test_expanded_rich_title_keeps_formula_links_and_anchor_once(formula, narrow):
    """Rich text, superscripts, subscripts, and formulas are retained when the title is expanded, and links and anchors are only output once."""
    data = _one_page(
        [
            _block("unused", (40, 80, 100, 85), title=True, anchor="head"),
            _block("BODY", (220, 180, 360, 240) if narrow else (40, 180, 300, 240), index=1),
        ]
    ).to_dict()
    data["pages"][0]["blocks"][0]["content"] = [
        {"type": "text", "content": "RICH "},
        {"type": "text", "content": "BOLD ", "styles": ["bold"]},
        {"type": "text", "content": "SUPER ", "styles": ["superscript"]},
        {"type": "equation_inline", "content": formula},
        {"type": "hyperlink", "url": "#head", "content": [{"type": "text", "content": "JUMP"}]},
    ]
    artifact, plans = _render(MiddleJson.from_dict(data))
    assert _title(plans)["expanded"] and _title(plans)["final_font_size"] == 12.5
    sizes = _sizes(artifact.content)
    assert any("RICH" in text for text in sizes) and any("JUMP" in text for text in sizes)
    assert not any(d.code == "pdf_duplicate_anchor" for d in artifact.diagnostics)
    with PDFDocument(artifact.content) as document:
        rect = _title(plans)["draw_bbox_pt"]
        ink = [
            char["tight_bbox"]
            for char in document.get_page_chars_with_geometry(0).chars
            if char["char"].strip() and (char["bbox"][0] < 150 if narrow else char["bbox"][1] < 160) and char["tight_bbox"]
        ]
        ink.extend(path.bbox for path in document.get_page_path_infos(0))
        assert ink
        assert all(
            box[0] >= rect[0] - 0.5 and box[1] >= rect[1] - 0.5 and box[2] <= rect[2] + 0.5 and box[3] <= rect[3] + 0.5
            for box in ink
        )
