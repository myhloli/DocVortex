"""验证原生表格与最终图件承接区域发生冲突时的页诊断。"""

import pytest
from pypdf.generic import NameObject, NumberObject

from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary
from docvortex.analyzers.native import PdfModel
from docvortex.analyzers.native.pdf import VISUAL_ROLE_CONFLICT
from docvortex.analyzers.native.pdf.layout_diagnostics import LayoutDiagnosticPage
from docvortex.analyzers.native.pdf.models import _AxisLine, _LineItem
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.native_contracts import PDFPathInfo


def _model_block(kind, box, page_size):
    """按实际最终模型契约输出单位化边界。"""
    width, height = page_size
    x0, y0, x1, y1 = box
    return {"type": kind, "bbox": [x0 / width, y0 / height, x1 / width, y1 / height]}


def _source_rows(rows, *, offset=0, scale=1):
    """构造带三列文字的原生行，各列宽度互相独立。"""
    lines = []
    for y, values in rows:
        for (x, width), value in zip(((30, 77), (130, 19), (179, 24)), values, strict=True):
            x0 = x * scale + offset
            y0 = y * scale + offset
            lines.append(_LineItem(value, (x0, y0, x0 + width * scale, y0 + 8 * scale), 0, len(lines)))
    return lines


@pytest.mark.parametrize(("offset", "scale"), [(0, 1), (21, 1.5)])
def test_three_rule_numeric_table_conflicts_with_larger_figure(offset, scale):
    """横轨与文字列确认表区，可发现表图合并，平移和缩放保持同一结论。"""
    rows = [
        (32, ("Interface", "P@10", "Score")),
        (48, ("Redesigned module", "42.0", "75.1")),
        (61, ("Current module", "40.0", "74.0")),
    ]
    lines = _source_rows(rows, offset=offset, scale=scale)
    rails = [
        _AxisLine(
            (25 * scale + offset, y * scale + offset, 220 * scale + offset, (y + 0.5) * scale + offset), 0.5, "horizontal"
        )
        for y in (27, 44, 72)
    ]
    evidence = LayoutDiagnosticPage((400, 500))
    evidence.record_visual_tables(lines, rails, [])
    assert [(item.rows, item.columns) for item in evidence.visual_tables] == [(3, 3)]
    image_box = (15 * scale + offset, 25 * scale + offset, 230 * scale + offset, 160 * scale + offset)
    image = _model_block("image", image_box, evidence.page_size)
    assert [item.code for item in evidence.diagnose([image], 0)] == [VISUAL_ROLE_CONFLICT]
    table = _model_block("table", evidence.visual_tables[0].bbox, evidence.page_size)
    assert not evidence.diagnose([image, table], 0)
    too_small = _model_block("image", (15, 130, 225, 160), evidence.page_size)
    assert not evidence.diagnose([too_small], 0)


def test_closed_grid_validates_bands_with_multiline_source_cells():
    """有独立横竖轨的三列说明表允许单元格换行，未闭合图例不算表。"""
    rows = [
        (29, ("Problem", "Cause", "Resolution")),
        (53, ("Screen is empty", "Loose wire", "Check input")),
        (66, ("Still no signal", "Old driver", "Reinstall")),
        (83, ("Dim display", "Low power", "Enable mode")),
        (107, ("Screen flickers", "Refresh rate", "Change mode")),
    ]
    horizontal = [_AxisLine((25.0, y, 220.0, y + 0.5), 0.5, "horizontal") for y in (25.0, 46.0, 77.0, 100.0, 124.0)]
    vertical = [_AxisLine((x, 25.0, x + 0.5, 125.0), 0.5, "vertical") for x in (25.0, 119.0, 167.0, 220.0)]
    evidence = LayoutDiagnosticPage((250, 250))
    evidence.record_visual_tables(_source_rows(rows), [*horizontal, *vertical], [])
    assert [(item.rows, item.columns) for item in evidence.visual_tables] == [(4, 3)]
    full_crop = _model_block("image", (24, 23, 224, 133), evidence.page_size)
    assert [item.code for item in evidence.diagnose([full_crop], 0)] == [VISUAL_ROLE_CONFLICT]
    evidence.record_visual_tables(_source_rows(rows), horizontal, [])
    assert not evidence.diagnose([full_crop], 0)


def test_shaded_amount_rows_validate_union_of_disjoint_crops():
    """较细的分项横轨和多行底纹提供支撑，重叠裁图按面积并集计算。"""
    rows = [(30 + 12 * n, (f"Subtotal {n}", "(1,100)", "1,510")) for n in range(5)]
    rails = [_AxisLine((123.0, y, 155.0, y + 0.5), 0.5, "horizontal") for y in (41.0, 53.0, 65.0)]
    fills = [
        PDFPathInfo(box, 4, True, False, 0, i, (204, 238, 255, 255), (box,))
        for i, box in enumerate((x, y - 1.0, x + 29.0, y + 9.0) for y in (30.0, 54.0, 78.0) for x in (126.0, 175.0))
    ]
    evidence = LayoutDiagnosticPage((240, 200))
    evidence.record_visual_tables(_source_rows(rows), rails, fills)
    assert [(item.rows, item.columns) for item in evidence.visual_tables] == [(5, 3)]
    first = _model_block("image", (25, 29, 135, 88), evidence.page_size)
    other = _model_block("image", (130, 29, 208, 88), evidence.page_size)
    assert not evidence.diagnose([first, first], 0)
    assert [item.code for item in evidence.diagnose([first, other], 0)] == [VISUAL_ROLE_CONFLICT]
    evidence.record_visual_tables(_source_rows(rows), rails, [])
    assert not evidence.diagnose([first, other], 0)


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("artifact", [False, True])
def test_tagged_figure_uses_only_nonartifact_table_rails(partial, artifact):
    """完整或混合结构页都核查图内印刷数值，Artifact 的横线不作为表区证据。"""
    builder = TaggedPdfBuilder()
    # 两角图件先绘制为底图，排除 Artifact 后最终图片仍包住三个文字行。
    references = [builder.figure(x=22, y=241), builder.figure(x=229, y=276)]
    for y, values in (
        (292, ("Variant", "Precision", "Recall")),
        (269, ("Current model", "42.0", "75.1")),
        (251, ("New model", "40.0", "74.0")),
    ):
        references.extend(builder.text(value, x=x, y=y) for value, x in zip(values, (34, 144, 230), strict=True))
    for y in (309, 285, 244):
        command = f"q 1 w 25 {y} m 275 {y} l S Q EMC\n".encode()
        if artifact:
            builder.commands[0].extend(b"/Artifact BMC " + command)
        else:
            mcid = builder.next_mcid[0]
            builder.next_mcid[0] += 1
            builder.commands[0].extend(f"/Figure <</MCID {mcid}>> BDC ".encode() + command)
            references.append(
                pdf_dictionary(Type=NameObject("/MCR"), Pg=builder.pages[0].indirect_reference, MCID=NumberObject(mcid))
            )
    figure = builder.node("Figure", references)
    if partial:
        builder.text("Residual paragraph from native layout", x=34, y=117)
    with PDFDocument(builder.finish([figure])) as document:
        predictor = PdfModel()
        default = predictor.predict(document)
        model = predictor.predict(document, collect_layout_diagnostics=True)

    assert model == default
    assert [block["type"] for block in model[0]] == (["image", "text"] if partial else ["image"])
    diagnostics = [item.code for item in predictor.diagnostics if item.code == VISUAL_ROLE_CONFLICT]
    assert diagnostics == ([] if artifact else [VISUAL_ROLE_CONFLICT])
