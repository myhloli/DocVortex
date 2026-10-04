from __future__ import annotations

import pytest
from _flash_pdf_test_utils import _text_line

from docvortex.analyzers.native.pdf import line_layout, pipeline, text_blocks


def test_line_tight_output_bbox_adds_one_point_padding_and_clips_page() -> None:
    """Verified and reliable tight The font frame is expanded by 1pt on each side and is safely cropped at the edge of the page."""

    line = _text_line(
        "value",
        (0.0, 0.0, 80.0, 40.0),
        0,
        ink_bbox=(0.5, 0.25, 40.0, 20.0),
    )

    assert line_layout._line_tight_output_bbox(
        line,
        (100.0, 100.0),
    ) == (0.0, 0.0, 41.0, 21.0)


def test_text_block_applies_tight_output_bbox_after_aggregation() -> None:
    """The verification text is first aggregated by layout, and then block and line of tight+1pt are applied simultaneously to bbox."""

    line = _text_line(
        "value",
        (10.0, 20.0, 90.0, 80.0),
        0,
        ink_bbox=(20.0, 30.0, 40.0, 50.0),
    )
    blocks = text_blocks._build_text_blocks(
        [line],
        [],
        (100.0, 100.0),
    )

    assert len(blocks) == 1
    assert blocks[0]["bbox"] == (10.0, 20.0, 90.0, 80.0)
    pipeline._apply_post_aggregation_tight_bboxes(
        blocks,
        (100.0, 100.0),
    )

    assert blocks[0]["bbox"] == (19.0, 29.0, 41.0, 51.0)
    assert blocks[0]["_local_line_bboxes"] == [
        (19.0, 29.0, 41.0, 51.0),
    ]


def test_direct_formula_tight_bbox_is_applied_and_internal_key_removed() -> None:
    """Verify that aggregation candidates for text formulas replace the exposed box and internal fields do not continue to be exposed."""

    blocks = [
        {
            "type": "equation",
            "bbox": (10.0, 20.0, 90.0, 80.0),
            "angle": 0,
            "content": "x=1",
            "_tight_output_bbox": (19.0, 29.0, 41.0, 51.0),
        }
    ]

    pipeline._apply_post_aggregation_tight_bboxes(
        blocks,
        (100.0, 100.0),
    )

    assert blocks[0]["bbox"] == (19.0, 29.0, 41.0, 51.0)
    assert "_tight_output_bbox" not in blocks[0]


@pytest.mark.parametrize(
    "block_type",
    ["text", "ref_text", "doc_title", "paragraph_title", "caption", "footnote"],
)
def test_output_normalization_exposes_lines_for_pdf_text_types(
    block_type: str,
) -> None:
    """Verify that Flash exposes normalized line boxes for PDF text blocks including ref_text."""

    block = pipeline._normalize_output_block(
        {
            "type": block_type,
            "bbox": (10.0, 20.0, 90.0, 80.0),
            "angle": 0,
            "content": "line one\nline two",
            "_local_line_bboxes": [
                (10.0, 20.0, 90.0, 40.0),
                (20.0, 50.0, 80.0, 80.0),
            ],
        },
        (100.0, 200.0),
    )

    assert block is not None
    assert block["lines"] == [
        {"bbox": [0.1, 0.1, 0.9, 0.2]},
        {"bbox": [0.2, 0.25, 0.8, 0.4]},
    ]


@pytest.mark.parametrize(
    ("angle", "expected_bbox"),
    [
        (0, [0.2, 0.05, 0.4, 0.15]),
        (90, [0.7, 0.1, 0.9, 0.2]),
        (270, [0.1, 0.8, 0.3, 0.9]),
    ],
)
def test_output_normalization_restores_rotated_line_bbox_to_page(
    angle: int,
    expected_bbox: list[float],
) -> None:
    """Verify that the local line frame will be inversely transformed to the original page coordinates in the direction of block."""

    block = pipeline._normalize_output_block(
        {
            "type": "text",
            "bbox": (10.0, 10.0, 90.0, 190.0),
            "angle": angle,
            "content": "value",
            "_local_line_bboxes": [(20.0, 10.0, 40.0, 30.0)],
        },
        (100.0, 200.0),
    )

    assert block is not None
    assert block["lines"] == [{"bbox": expected_bbox}]


@pytest.mark.parametrize("block_type", ["text", "ref_text"])
@pytest.mark.parametrize(
    "local_line_bboxes",
    [
        None,
        [],
        [(10.0, 20.0, 40.0, 30.0), (1.0, 2.0, 1.0, 3.0)],
        [(40.0, 20.0, 10.0, 30.0)],
    ],
)
def test_output_normalization_fails_closed_for_invalid_line_bboxes(
    block_type: str,
    local_line_bboxes: object,
) -> None:
    """Verify that text/ref_text outputs empty lines when the internal line box is missing, empty, or any one is illegal."""

    block = pipeline._normalize_output_block(
        {
            "type": block_type,
            "bbox": (10.0, 20.0, 40.0, 50.0),
            "angle": 0,
            "content": "value",
            "_local_line_bboxes": local_line_bboxes,
        },
        (100.0, 100.0),
    )

    assert block is not None
    assert block["lines"] == []
