"""验证原生顶层框的轻度侵入、正常邻接及与严重证据的优先关系。"""

from unittest.mock import patch

import pytest

from docvortex.analyzers.native.pdf.layout_risk_scoring import score_layout_pages


def _block(kind, box, content="", *, lines=None):
    """按物理页归一化坐标构造独立原生块，可显式保留行框。"""
    if lines is None:
        lines = [box] if kind == "text" else []
    return {"type": kind, "bbox": box, "content": content, "lines": [{"bbox": line} for line in lines]}


@pytest.mark.parametrize(
    ("blocks", "expected"),
    [
        ([_block("equation", (0.1, 0.7, 0.5, 0.9)), _block("text", (0.32, 0.8, 0.49, 0.82), "otherwise")], 0.82),
        ([_block("image", (0.48, 0.1, 0.91, 0.35)), _block("table", (0.1, 0.1, 0.91, 0.16))], 0.75),
        ([_block("image", (0.64, 0.1, 0.91, 0.35)), _block("table", (0.1, 0.1, 0.91, 0.16))], 0.96),
        ([_block("image", (0.1, 0.1, 0.8, 0.6)), _block("text", (0.35, 0.39, 0.92, 0.43), "A separate line of text.")], 0.92),
        (
            [
                _block("text", (0.32, 0.55, 0.87, 0.95), "Paragraph across two columns."),
                _block("text", (0.34, 0.62, 0.62, 0.90), "Independent paragraph in one column."),
            ],
            0.92,
        ),
        (
            [
                _block(
                    "paragraph_title",
                    (0.1, 0.40, 0.7, 0.44),
                    "A heading across two lines",
                    lines=[(0.1, 0.40, 0.7, 0.42), (0.1, 0.42, 0.20, 0.44)],
                ),
                _block(
                    "text",
                    (0.1, 0.42, 0.7, 0.56),
                    "Body starts on the last heading baseline.",
                    lines=[(0.23, 0.42, 0.7, 0.44), (0.1, 0.45, 0.7, 0.47)],
                ),
            ],
            0.95,
        ),
    ],
)
def test_minor_overlap_reduces_confidence_with_stratified_severity(blocks, expected):
    """不同冲突类型按严重度分级扣分，重度图表冲突跨过 0.80，且与块顺序无关。"""
    a, b = score_layout_pages([blocks, list(reversed(blocks))])
    assert a == b == expected


def test_background_related_captions_numbers_and_wrapping_remain_clean():
    """整页背景、图注、公式编号、结构归属以及仅占段落留白的图片都不应扣分。"""
    text = _block(
        "text",
        (0.1, 0.1, 0.82, 0.4),
        "Indented text wraps around an illustration.",
        lines=[(0.1, 0.1, 0.4, 0.13), (0.1, 0.16, 0.39, 0.19), (0.1, 0.29, 0.82, 0.32)],
    )
    image = _block("image", (0.50, 0.14, 0.78, 0.26))
    pages = [
        [text, image],
        [image, _block("text", (0.51, 0.21, 0.70, 0.24), "Printed on an image backdrop")],
        [text, _block("image", (0, 0, 1, 1))],
        [_block("equation", (0.1, 0.1, 0.85, 0.21)), _block("text", (0.79, 0.17, 0.84, 0.19), "(12a)")],
        [image, _block("caption", (0.51, 0.21, 0.70, 0.24), "Figure 2")],
        [image, _block("text", (0.51, 0.21, 0.70, 0.24), "No line evidence", lines=[])],
        [image, {**_block("text", (0.51, 0.21, 0.70, 0.24), "panel label"), "_native_annotation_parent_bbox": image["bbox"]}],
        [_block("table", (0.1, 0.1, 0.8, 0.8)), image],
        [
            _block("paragraph_title", (0.1, 0.10, 0.8, 0.13), "Heading"),
            _block("text", (0.1, 0.129, 0.8, 0.16), "Text directly below the heading"),
        ],
    ]
    assert score_layout_pages(pages) == (1.0,) * len(pages)


def test_independent_label_inside_projected_image_differs_from_a_tagged_caption():
    """有文字投影的图片与无归属正文争用区域；结构树已归到该图的文字则受保护。"""
    image = {**_block("image", (0.1, 0.2, 0.7, 0.5), "Pressure reading"), "_pdf_structure": {"node_id": "fig"}}
    label = _block("text", (0.2, 0.3, 0.4, 0.34), "Pressure reading")
    attached = {**label, "_pdf_structure": {"node_id": "label", "visual_parent": "fig"}}
    assert score_layout_pages([[image, label], [image, attached]]) == (0.90, 1.0)


def test_multiple_overlap_pairs_and_families_escalate_and_combine_with_vectors():
    """多处重叠按频次与跨域 Noisy-OR 递增；同页更严重矢量证据仍会执行并联合扣分。"""
    body = _block(
        "text",
        (0.1, 0.1, 0.45, 0.3),
        "Independent text in two blocks.",
        lines=[(0.10, 0.10, 0.45, 0.12), (0.10, 0.275, 0.45, 0.30)],
    )
    image = _block("image", (0.02, 0.08, 0.25, 0.35))
    heading = _block("paragraph_title", (0.1, 0.27, 0.45, 0.32), "End of paragraph")
    blocks = [body, image, heading]
    single, both = score_layout_pages([[body, image], blocks])
    assert (single, both) == (0.96, 0.90)
    equation = _block("equation", (0.52, 0.6, 0.9, 0.74))
    clause = _block("text", (0.65, 0.65, 0.83, 0.67), "otherwise")
    second_clause = _block("text", (0.65, 0.68, 0.83, 0.70), "whereby")
    repeated_img, multi_family, multi_eq = score_layout_pages(
        [[body, image, image], [*blocks, equation, clause], [equation, clause, second_clause]]
    )
    assert repeated_img == 0.90 < single
    assert multi_family == 0.74 <= 0.80
    assert multi_eq == 0.74 <= 0.80
    with patch(
        "docvortex.analyzers.native.pdf.layout_risk_scoring.vector_conflict", return_value=("missing_chart", "图面缺失")
    ):
        assert score_layout_pages([blocks], vector_geometry=lambda _: ((500, 600), None)) == (0.36,)
