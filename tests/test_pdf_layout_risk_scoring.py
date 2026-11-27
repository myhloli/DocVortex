"""验证原生布局冲突与逐页伪置信度。"""

from unittest.mock import patch

from docvortex.analyzers.native.pdf.layout_risk_scoring import score_layout_pages


def test_confidence_uses_the_largest_structural_penalty_in_page_order():
    """弱证据和空页得满分，同页多个结构冲突只取最大扣分。"""
    weak = {"marginal_role": (60, "页眉"), "column_reentry": (60, "读序")}
    table = {**weak, "table_structure": (30, "真实截表")}
    with (
        patch("docvortex.analyzers.native.pdf.layout_risk_scoring.extract_layout_risk", side_effect=[{}, weak, table, table]),
        patch(
            "docvortex.analyzers.native.pdf.layout_risk_scoring.spatial_fragmentation",
            side_effect=[None, None, None, (57, "正文碎裂")],
        ),
    ):
        assert score_layout_pages([[], [], [], []]) == (1.0, 1.0, 0.70, 0.43)


def test_only_native_control_inside_equation_lowers_confidence():
    """程序的实际控制行必须进入公式裁片，独立算式、仅一行疑似代码及已正确装配的程序均受保护。"""
    from docvortex.analyzers.native.pdf.layout_program_risk import collect_program_controls
    from docvortex.analyzers.native.pdf.models import _LineItem

    src = [
        _LineItem(text, (10, y, 48, y + 2), 0, i)
        for i, (y, text) in enumerate([(30, "a[k]=seed;"), (33, "for (int k=0; k<n; k++) {"), (36, "value[k]=a[k];")])
    ]
    boxes = collect_program_controls(src, (100, 100))
    assert len(boxes) == 1
    blocks = [
        {"type": "text", "bbox": [0.10, 0.30, 0.48, 0.32], "content": "a[k]=seed;"},
        {"type": "equation", "bbox": [0.10, 0.33, 0.48, 0.35], "content": ""},
        {"type": "text", "bbox": [0.10, 0.36, 0.48, 0.38], "content": "value[k]=a[k];"},
    ]
    assert score_layout_pages([blocks], program_controls={0: boxes}) == (0.70,)
    assert score_layout_pages([blocks], program_controls={0: ((0.62, 0.33, 0.86, 0.35),)}) == (1.0,)
    assert score_layout_pages([[{**b, "type": "code"} for b in blocks]], program_controls={0: boxes}) == (1.0,)
    assert not collect_program_controls(src[1:2], (100, 100))


def test_spatial_confidence_uses_repeated_prose_cuts_inside_one_region():
    """同样多的框只有反复紧贴地切开同一区域的多行正文才应降低伪置信度。"""

    def page(*, grid=False, wide_gaps=False, separate_columns=False, owner=False):
        """生成字宽相同而切点错开或列位固定的印刷文本行。"""
        lines = []
        for i in range(6):
            x = 0.08 + (0.50 if separate_columns and i % 2 else 0)
            y = 0.12 + i * (0.055 if separate_columns else 0.02)
            parts = (
                ["ordinary", "words in", "a longer", "passage", "here"]
                if grid
                else [
                    "abcdefghijklmno"[: 1 + i * 2],
                    "much longer words"[: 15 - i],
                    "ordinary phrase"[: 4 + i],
                    "typography"[: 2 + i],
                    "remaining prose",
                ]
            )
            for part in parts:
                end = x + len(part) * 0.006
                lines.append({"type": "text", "content": part, "bbox": (x, y, end, y + 0.014)})
                x = end + (0.035 if wide_gaps else 0.003)
        if owner:
            lines.append({"type": "table", "content": "", "bbox": (0.06, 0.10, 0.72, 0.27)})
        return lines

    full = page()
    scores = score_layout_pages(
        [full, full[:5], page(grid=True), page(wide_gaps=True), page(separate_columns=True), page(owner=True)]
    )
    assert scores == (0.40, 1.0, 1.0, 1.0, 0.76, 1.0)


def test_image_claiming_a_local_table_and_neighbouring_prose():
    """大图独占一侧的多行表和另一侧的段落才计分；原生已交付的正文或完整表都受保护。"""
    from docvortex.document.pdf.native_contracts import PDFDrawingLine, PDFPageVectorGeometry

    fields = [f"Field {i}    {i} units" for i in range(5)]
    body = [
        "The neighbouring paragraph begins here",
        "and continues on the following printed line",
        "before ending in its own column.",
    ]
    projected = {"type": "image", "bbox": [0.05, 0.15, 0.95, 0.82], "content": "\n".join(fields + body)}
    lines = [
        {"bbox": (60, y, 248, y + 9), "spans": [{"text": text}], "rotation": 0}
        for y, text in zip((240, 256, 272, 288, 304), fields)
    ] + [{"bbox": (390, y, 642, y + 10), "spans": [{"text": text}], "rotation": 0} for y, text in zip((240, 252, 264), body)]
    rules = tuple(PDFDrawingLine((60, y), (274, y), (60, y, 274, y), 1, "horizontal") for y in (252, 268, 284, 300, 316))
    geometry = PDFPageVectorGeometry(rules, ())

    def evaluate(blocks, evidence=lines, *, drawings=geometry):
        """记录按需打开可见源行的次数，并走实际页级评分入口。"""
        requests = []

        def source(index):
            """按调用保留页序供测试验证惰性读取。"""
            requests.append(index)
            return evidence

        scores = score_layout_pages([blocks], vector_geometry=lambda _: ((720, 800), drawings), source_lines=source)
        return scores[0], requests

    assert evaluate([projected]) == (0.40, [0])
    delivered = {"type": "text", "content": " ".join(body), "bbox": [0.53, 0.29, 0.91, 0.35]}
    table = {"type": "table", "content": "matrix", "bbox": [0.08, 0.28, 0.39, 0.40]}
    assert evaluate([projected, delivered])[0] == 1.0
    assert evaluate([projected, table])[0] == 1.0
    assert evaluate([projected], lines[:5])[0] == 1.0
    assert evaluate([projected], [ln for ln in lines if ln not in lines[:2]])[0] == 1.0
    assert (
        evaluate(
            [projected],
            [*lines[:5], *[dict(ln, bbox=(390, 460 + i * 12, 642, 470 + i * 12)) for i, ln in enumerate(lines[5:])]],
        )[0]
        == 1.0
    )
    assert evaluate([projected], drawings=PDFPageVectorGeometry((), ()))[1] == []
    # 表边和列位相同的两个远距片段，中间独立说明不得被算作与它们并列。
    split_grid = [
        {**line, "bbox": (x, y + offset, end, base + offset)}
        for line, offset in zip(lines, (0, 0, 144, 144, 144, 60, 60, 60))
        for x, y, end, base in [line["bbox"]]
    ]
    split_rules = tuple(PDFDrawingLine((60, y), (274, y), (60, y, 274, y), 1, "horizontal") for y in (252, 268, 428, 444, 460))
    assert evaluate([projected], split_grid, drawings=PDFPageVectorGeometry(split_rules, ()))[0] == 1.0
    # 同一表内有格线穿过说明列，不能把这一列当作独立正文区。
    extended = (*rules, *(PDFDrawingLine((365, y), (670, y), (365, y, 670, y), 1, "horizontal") for y in (252, 268)))
    assert evaluate([projected], drawings=PDFPageVectorGeometry(extended, ()))[0] == 1.0
    # 表在下方时，只有一段普通表注不足；它与另一栏独立的长段同时被吞才成立。
    upper = []
    for x, label in ((80, "Western"), (345, "Eastern")):
        for i, noun in enumerate(("region", "project", "report", "group")):
            y = 136 + 12 * i
            upper.append(
                {
                    "bbox": (x, y, x + 245, y + 10),
                    "spans": [{"text": f"{label} {noun} has its own separate narrative."}],
                    "rotation": 0,
                }
            )
    merged = {**projected, "content": "\n".join(fields + [line["spans"][0]["text"] for line in upper])}
    assert evaluate([merged], [*lines[:5], *upper])[0] == 0.40
    assert evaluate([merged], [*lines[:5], *upper[:4]])[0] == 1.0
    delivered_column = {"type": "text", "bbox": [0.46, 0.16, 0.84, 0.24], "content": "Independent column"}
    assert evaluate([merged, delivered_column], [*lines[:5], *upper])[0] == 1.0
    blank = {**projected, "content": ""}
    assert evaluate([blank, delivered, {**blank, "bbox": [0.68, 0.54, 0.84, 0.74]}]) == (1.0, [])
