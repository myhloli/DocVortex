"""验证几何扣分对局部小瑕疵、正常合并单元格和严重跨界的不同处理。"""

from types import SimpleNamespace
import pytest
from docvortex.analyzers.native.pdf.layout_native_conflicts import nested_block_risk, table_container_risk


def _paragraph_with_insets(count):
    """构造同一多行正文内的独立小框，保持外框和行数固定。"""
    return [
        {
            "type": "text",
            "bbox": (0.1, 0.1, 0.9, 0.7),
            "lines": [{"bbox": (0.1, 0.1 + i * 0.05, 0.9, 0.14 + i * 0.05)} for i in range(10)],
        },
        *[{"type": "paragraph_title", "bbox": (0.2 + i * 0.15, 0.2, 0.29 + i * 0.15, 0.23)} for i in range(count)],
    ]


@pytest.mark.parametrize("count,points", [(1, 10), (2, 20), (3, 30)])
def test_nested_local_count_has_severity_boundary(count, points):
    """一个或两个独立小框保持低分，同一正文内三个冲突达到默认门槛。"""
    assert nested_block_risk(_paragraph_with_insets(count))[0] == points


def test_visual_container_is_not_body_intrusion():
    """图里叠放的小图和已交付正文不按正文异常包含扣分。"""
    blocks = _paragraph_with_insets(3)
    blocks[0]["type"] = "image"
    assert nested_block_risk(blocks) is None


def _table_inputs(closed=False, words=False):
    """生成相同几何的三列区域和框内长段，闭合外边界允许它成为真实合并单元格。"""
    size = (600, 800)
    blocks = [{"type": "table", "bbox": (0.1, 0.1, 0.9, 0.8)}]
    spans = []
    for y in (170, 190, 210, 230):
        spans.extend({"text": "Value" if words else "123", "bbox": (x, y, x + 45, y + 10)} for x in (80, 250, 440))
    spans.extend(
        {"text": "This is a long independently printed paragraph line", "bbox": (80, y, 500, y + 10)}
        for y in (320, 340, 360, 380)
    )
    for span in spans:
        x, y = span["bbox"][:2]
        advance = (span["bbox"][2] - x) / len(span["text"])
        span["chars"] = [
            {"char": char, "bbox": (x + advance * i, y, x + advance * (i + 1) - 1, y + 10)}
            for i, char in enumerate(span["text"])
        ]
    lines = [{"spans": spans}]
    edges = (
        [
            ("vertical", (60, 80, 60, 640)),
            ("vertical", (540, 80, 540, 640)),
            ("horizontal", (60, 80, 540, 80)),
            ("horizontal", (60, 640, 540, 640)),
        ]
        if closed
        else []
    )
    geometry = SimpleNamespace(drawing_lines=[SimpleNamespace(orientation=kind, bbox=box) for kind, box in edges])
    return blocks, size, lines, geometry


@pytest.mark.parametrize("words", [False, True])
def test_table_overreach_is_independent_of_numeric_semantics(words):
    """数值换成类别词不改变多列带之外长段被吞的几何判据。"""
    assert table_container_risk(*_table_inputs(words=words))[0] == 40


def test_closed_merged_cells_do_not_trigger():
    """相同文本由完整外边界封闭时，不把正常表内长段判为跨界。"""
    assert table_container_risk(*_table_inputs(closed=True)) is None


def test_margin_notes_in_enclosing_whitespace_do_not_trigger():
    """段落外框被一条长行拉宽时，右侧独立得分标记不应成为严重包含。"""
    blocks = _paragraph_with_insets(3)
    blocks[0]["lines"] = [{"bbox": (0.1, 0.1 + i * 0.04, 0.5, 0.13 + i * 0.04)} for i in range(12)] + [
        {"bbox": (0.1, 0.65, 0.9, 0.68)}
    ]
    for i, block in enumerate(blocks[1:]):
        block["bbox"] = (0.8, 0.22 + i * 0.12, 0.89, 0.24 + i * 0.12)
    assert nested_block_risk(blocks) is None


def test_broken_formula_in_body_corridor_still_counts():
    """公式片段可能没有压到父行墨迹，但位于正文栏内部时仍构成异常分配。"""
    blocks = _paragraph_with_insets(3)
    blocks[0]["lines"] = [{"bbox": (0.1, 0.4 + i * 0.04, 0.9, 0.43 + i * 0.04)} for i in range(5)]
    assert nested_block_risk(blocks)[0] == 30


def test_inline_font_runs_are_one_physical_row():
    """一行三个字体片段不能冒充三个正文行，单行导航的分块不触发严重包含。"""
    blocks = _paragraph_with_insets(3)
    blocks[0]["lines"] = [{"bbox": (0.1, 0.2, 0.3, 0.23)}, {"bbox": (0.31, 0.2, 0.5, 0.23)}, {"bbox": (0.51, 0.2, 0.9, 0.23)}]
    assert nested_block_risk(blocks) is None


def test_span_packaging_does_not_change_table_geometry():
    """相同字形可分成多个单元格span或装入一个大span，风险必须保持一致。"""
    blocks, size, lines, geometry = _table_inputs()
    chars = [char for line in lines for span in line["spans"] for char in span["chars"]]
    packed = [{"spans": [{"text": "all table and paragraph characters", "bbox": (60, 80, 540, 640), "chars": chars}]}]
    assert table_container_risk(blocks, size, lines, geometry) == table_container_risk(blocks, size, packed, geometry)
    cells = [char for char in chars if char["bbox"][1] < 250]
    packed_cells = [{"spans": [{"text": "all table cells", "bbox": (60, 80, 540, 640), "chars": cells}]}]
    assert table_container_risk(blocks, size, packed_cells, geometry) is None


def test_wide_column_continuation_is_not_outside_prose():
    """无竖线表中跨后两列的长单元格仍是表内内容，不应按正文吞并回落。"""
    blocks, size, lines, geometry = _table_inputs()
    for span in lines[0]["spans"]:
        if span["bbox"][1] < 300:
            continue
        y = span["bbox"][1]
        span["bbox"] = (250, y, 540, y + 10)
        step = 290 / len(span["text"])
        span["chars"] = [
            {"char": char, "bbox": (250 + step * i, y, 250 + step * (i + 1) - 1, y + 10)} for i, char in enumerate(span["text"])
        ]
    assert table_container_risk(blocks, size, lines, geometry) is None
