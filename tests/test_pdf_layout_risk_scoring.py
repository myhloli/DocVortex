"""验证原生布局冲突与逐页伪置信度。"""

from unittest.mock import MagicMock, patch

from docvortex.analyzers.native.pdf.layout_risk_scoring import (
    PageLayoutAssessment,
    assess_layout_pages,
    score_layout_pages,
)


def test_confidence_uses_intra_domain_max_and_cross_domain_noisy_or():
    """空页与弱证据得满分；同域取最大扣分，跨域按 Noisy-OR 独立合成。"""
    weak = {"marginal_role": (60, "页眉"), "column_reentry": (60, "读序")}
    table = {**weak, "table_structure": (30, "真实截表")}
    formula = {"formula_ownership": (30, "公式碎片")}
    with (
        patch(
            "docvortex.analyzers.native.pdf.layout_risk_scoring.extract_scored_layout_risk",
            side_effect=[{}, weak, table, table, formula],
        ),
        patch(
            "docvortex.analyzers.native.pdf.layout_risk_scoring.spatial_fragmentation",
            side_effect=[None, None, None, (57, "正文碎裂"), (57, "正文碎裂")],
        ),
    ):
        assert score_layout_pages([[], [], [], [], []]) == (1.0, 1.0, 0.70, 0.30, 0.43)


def test_non_severe_structural_risk_does_not_short_circuit_vector_check():
    """30 分结构冲突不得遮蔽 60 分矢量检测，且同页仅读取一次矢量几何。"""
    from docvortex.document.pdf.native_contracts import PDFPageVectorGeometry

    table_block = {"type": "table", "bbox": [0.1, 0.1, 0.9, 0.6], "content": "<table></table>"}
    read_vector = MagicMock(return_value=((600, 800), PDFPageVectorGeometry((), ())))
    read_lines = MagicMock(return_value=[])
    with (
        patch(
            "docvortex.analyzers.native.pdf.layout_risk_scoring.extract_scored_layout_risk",
            return_value={"table_structure": (30, "表格截断")},
        ),
        patch("docvortex.analyzers.native.pdf.layout_risk_scoring.wants_vectors", return_value=True),
        patch("docvortex.analyzers.native.pdf.layout_risk_scoring.vector_conflict", return_value=None),
        patch("docvortex.analyzers.native.pdf.layout_risk_scoring.table_container_risk", return_value=(40, "表格吞并正文")),
    ):
        assessments = assess_layout_pages([[table_block]], vector_geometry=read_vector, source_lines=read_lines)
    assert read_vector.call_count == 1
    assert len(assessments) == 1
    item = assessments[0]
    assert isinstance(item, PageLayoutAssessment)
    assert item.confidence == 0.60
    assert item.risk_points == 40
    assert item.primary_domain == "container_integrity"
    assert item.domain_points == {
        "container_integrity": 40,
        "fragmentation": 0,
        "spatial_collision": 0,
        "grouping_and_order": 0,
    }
    assert item.reasons == ("container_integrity: 表格吞并正文",)


def test_overlapping_independent_equations_trigger_fragmentation_fallback():
    """两个显著重叠的独立公式裁片应计入公式归属 60 分，而分离或轻触公式保持满分。"""
    first = {"type": "equation", "bbox": [0.20, 0.40, 0.52, 0.48], "content": ""}
    second = {"type": "equation", "bbox": [0.30, 0.41, 0.50, 0.47], "content": ""}
    stacked = {"type": "equation", "bbox": [0.20, 0.48, 0.52, 0.55], "content": ""}
    touching = {"type": "equation", "bbox": [0.20, 0.46, 0.52, 0.54], "content": ""}
    assessments = assess_layout_pages([[first, second], [first, stacked], [first, touching]])
    assert [item.confidence for item in assessments] == [0.40, 1.0, 1.0]
    assert assessments[0].primary_domain == "fragmentation"
    assert any("交叠" in reason for reason in assessments[0].reasons)


def test_high_precision_diagnostics_bridge_to_container_integrity_and_ignore_reading_boundary():
    """TEXT_OWNERSHIP、VISUAL_ROLE_CONFLICT 与大面积主体缺图接入容器完整性，READING_BOUNDARY 不拉低置信度。"""
    from docvortex.analyzers.native.pdf import MISSING_VISUAL, READING_BOUNDARY, TEXT_OWNERSHIP, VISUAL_ROLE_CONFLICT
    from docvortex.analyzers.native.pdf.layout_diagnostics import LayoutDiagnosticPage
    from docvortex.result import Diagnostic

    diagnostics = (
        Diagnostic(TEXT_OWNERSHIP, "可见正文源行 12/12 个字形无归属，页面未输出内容", 0),
        Diagnostic(VISUAL_ROLE_CONFLICT, "原生表区支持 5 行 4 列，图件覆盖表区且缺少表格容器", 1),
        Diagnostic(READING_BOUNDARY, "同一双栏正文带的输出顺序为 左→右→左", 2),
        Diagnostic(MISSING_VISUAL, "位图框占整页 4.0%，图面覆盖 0.0%", 2),
    )
    assessments = assess_layout_pages(
        [[], [], [], []],
        diagnostics=diagnostics,
        major_missing_visuals={3: "大面积位图缺失（占整页 45.0%，图面覆盖 10.0%）"},
    )
    assert [item.confidence for item in assessments] == [0.40, 0.60, 1.0, 0.60]
    assert [item.primary_domain for item in assessments] == ["container_integrity", "container_integrity", None, "container_integrity"]

    # 验证 major_missing_visual 的面积门控、整页底图排除以及矢量图组允许原生表格承接。
    evidence = LayoutDiagnosticPage((100.0, 100.0))
    evidence.record_visuals("位图", [(10, 10, 60, 50)])  # 20% 面积
    assert evidence.major_missing_visual([]) is not None
    assert evidence.major_missing_visual([{"type": "image", "bbox": [0.1, 0.1, 0.6, 0.5]}]) is None

    backdrop = LayoutDiagnosticPage((100.0, 100.0))
    backdrop.record_visuals("矢量图组", [(0, 0, 100, 95)])  # 95% 整页背景框
    assert backdrop.major_missing_visual([]) is None

    vec_table = LayoutDiagnosticPage((100.0, 100.0))
    vec_table.record_visuals("矢量图组", [(10, 10, 60, 50)])  # 20% 矢量图组
    assert vec_table.major_missing_visual([]) is not None
    assert vec_table.major_missing_visual([{"type": "table", "bbox": [0.1, 0.1, 0.6, 0.5]}]) is None


def test_skip_vector_conflict_avoids_redundant_geometry_reads():
    """已知路径不足以触发矢量冲突的页应直接跳过 vector_conflict 的几何读取。"""
    read_vector = MagicMock(return_value=((600, 800), None))
    with patch("docvortex.analyzers.native.pdf.layout_risk_scoring.wants_vectors", return_value=True):
        assessments = assess_layout_pages([[]], vector_geometry=read_vector, skip_vector_conflict={0})
    assert read_vector.call_count == 0
    assert assessments[0].confidence == 1.0


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
