"""独立编号表格之间的题注是边界，单元格、同表重复题注及相邻正文不能误切。"""

from types import SimpleNamespace

import pytest

from docvortex.analyzers.native.pdf.models import _LocalAxisLine


def _rail(box, orientation="horizontal"):
    """创建原生几何使用的局部坐标轴线。"""
    return _LocalAxisLine(box, box, orientation, 0.6)


@pytest.mark.parametrize("placement", ["above", "below"])
@pytest.mark.parametrize("case", ["separate", "repeated", "connected", "adjacent-column", "single", "cell-reference"])
@pytest.mark.parametrize("dx,width,em", [(0, 205, 10), (38, 306, 8)])
def test_caption_barriers_require_independent_table_numbers(placement, case, dx, width, em):
    """固定字号改变栏宽和位置，上置、下置均保留表体；表内及同编号反例保留连续网格。"""
    from docvortex.analyzers.native.pdf.table_rules import _numbered_rule_captions, _separate_captioned_rule_groups

    y = [40, 53, 98, 138, 153, 195]
    lines = [_rail((dx, v - 0.2, dx + width, v + 0.2)) for v in y]
    tops = [20, 118] if placement == "above" else [108, 206]
    labels = ["表 18 元件测量值", "表 19 轨道测量值"]
    if case == "repeated":
        labels[1] = "表 18 后续测量"
    elif case == "cell-reference":
        labels = ["Station C", "Device E"]
    if case == "adjacent-column":
        dx += width + 16
    captions = [
        (SimpleNamespace(text=label), (dx + 15, y0, dx + width - 15, y0 + 8))
        for y0, label in zip(tops, labels, strict=True)
    ]
    if case == "single":
        captions = captions[:1]
    vertical = [_rail((dx + 22, 97, dx + 22.5, 138), "vertical")] if case == "connected" else []
    numbered = _numbered_rule_captions([], captions, (500, 700), 0)
    observed = _separate_captioned_rule_groups([lines], numbered, vertical, em)
    assert observed == ([lines[:3], lines[3:]] if case == "separate" else [lines])


def test_caption_barriers_keep_sparse_border_pair_without_header_rule():
    """只有外边两条横线的一侧没有独立表头证据，同一张表仍允许跨过内部标题行。"""
    from docvortex.analyzers.native.pdf.table_rules import _numbered_rule_captions, _separate_captioned_rule_groups

    rules = [_rail((20, y, 300, y + 1)) for y in (40, 140, 225, 250, 295)]
    captions = [(SimpleNamespace(text=f"Table {i}: Summary"), (30, y, 100, y + 10)) for i, y in [(1, 24), (2, 204)]]
    numbered = _numbered_rule_captions([], captions, (500, 700), 0)
    assert _separate_captioned_rule_groups([rules], numbered, [], 10) == [rules]


@pytest.mark.parametrize("numbers", [("4", "5"), ("2.1", "2.2"), ("4-1", "4-2")])
@pytest.mark.parametrize("second_top, title_offset", [(491, 12), (505, 21)])
def test_two_captioned_grids_keep_separate_values_in_public_pdf(numbers, second_top, title_offset):
    """文档公开入口应把两个真实数据表保持为独立可编辑表，题注和各组格值也独立。"""
    from io import BytesIO

    from bs4 import BeautifulSoup
    from reportlab.pdfgen import canvas

    from docvortex import parse

    stream = BytesIO()
    page = canvas.Canvas(stream, pagesize=(500, 700))
    for top, name, label, start in [(600, f"Table {numbers[0]}: Indoor counts", "Inside", 10), (second_top, f"Table {numbers[1]}: Road counts", "Outside", 20)]:
        page.setFont("Helvetica-Bold", 11)
        page.drawCentredString(255, top + title_offset, name)
        for i in range(4):
            page.line(55, top - i * 20, 455, top - i * 20)
        for x in (55, 185, 330, 455):
            page.line(x, top, x, top - 60)
        page.setFont("Helvetica", 10)
        for i, values in enumerate([["Site", "Batch", "Total"], [label, "Early", str(start)], [label, "Late", str(start + 7)]]):
            for x, value in zip((60, 190, 335), values, strict=True):
                page.drawString(x, top - i * 20 - 15, value)
    page.showPage()
    page.save()
    result = parse(stream.getvalue(), file_suffix="pdf", parse_mode="txt")
    tables = [b for b in result.to_dict()["pages"][0]["blocks"] if b["type"] == "table"]
    assert len(tables) == 2
    for block, number, spot, start in zip(tables, numbers, ("Inside", "Outside"), (10, 20), strict=True):
        html = next(c["content"] for c in block["content"] if c["type"] == "table_body")
        rows = BeautifulSoup(html, "html.parser").find_all("tr")
        grid = [[c.get_text(strip=True) for c in row.find_all(["td", "th"])] for row in rows]
        assert grid == [["Site", "Batch", "Total"], [spot, "Early", str(start)], [spot, "Late", str(start + 7)]]
        caption = next(c for c in block["content"] if c["type"] == "table_caption")
        assert f"Table {number}" in str(caption["content"])

@pytest.mark.parametrize(
    "labels, expected, already_classified",
    [
        (["Table 2.1: Field readings", "Table 2.2: Air readings"], True, True),
        (["表 4-1：电压", "表 4-2：电流"], True, True),
        (["表3产品尺寸", "表4产品质量"], True, False),
        (["表 4-1机械尺寸", "表 4-2接线尺寸"], True, False),
        (["Table 2.1: Readings", "Table 2.1 (continued)"], False, True),
        (["表 4-1：元件", "Table 4.1: Component"], False, True),
        (["Table 2: Reference", "Table 3 shows the reference value"], False, True),
        (["Table 2", "Table 3"], False, True),
    ],
)
def test_local_caption_tokens(labels, expected, already_classified):
    """逐段核对编号，不把续表与双语同编号看成相邻新表；只有编号的换行标签保留原路。"""
    from docvortex.analyzers.native.pdf.table_rules import _numbered_rule_captions, _separate_captioned_rule_groups

    rules = [_rail((20, y, 340, y + 1)) for y in [40, 52, 99, 145, 160, 208]]
    sources = [SimpleNamespace(text=t, bbox=(25, y, 180, y+10)) for t, y in zip(labels, [17, 122], strict=True)]
    classified = [(line, line.bbox) for line in sources] if already_classified else []
    candidates = _numbered_rule_captions(sources, classified, (400, 600), 0)
    grouped = _separate_captioned_rule_groups([rules], candidates, [], 10)
    assert len(grouped) == (2 if expected else 1)


def test_supported_caption_pair_does_not_own_unproven_rail_prefix():
    """两张各自有题注、三条横线的表，不得借用更前面的页边或残缺表轨。"""
    from docvortex.analyzers.native.pdf.table_rules import _separate_captioned_rule_groups

    positions = [23, 85, 126, 139, 178, 210, 227, 280]
    rules = [_rail((20, y, 360, y+0.4)) for y in positions]
    labels = [((80, 110, 280, 120), "7"), ((72, 193, 288, 206), "8")]
    assert _separate_captioned_rule_groups([rules], labels, [], 10) == [rules[:2], rules[2:5], rules[5:]]


@pytest.mark.parametrize("direction", ["above", "below"])
@pytest.mark.parametrize("variant", ["single-line", "wrapped", "touches-body", "cross-column", "ambiguous"])
def test_confirmed_caption_parents_reach_public_attachment(direction, variant):
    """编号题注用源首行关联对应表；换行后的大框不影响，异栏或冲突证据不强绑。"""
    from docvortex.analyzers.native.pdf.visual_annotations import _classify_and_bind_visual_annotations
    from tests.unittest.test_flash_pdf_visual_annotations import _text_block, _visual_block

    upper = _visual_block((20, 65, 175, 99), block_type="table")
    lower = _visual_block((20, 152, 175, 193), block_type="table")
    y = 103 if direction == "above" else 127
    if variant == "touches-body":
        y = 142 if direction == "above" else 98
    first = (28, y, 156, y + 11)
    title_box = (28, y, 156, y + (19 if variant == "wrapped" else 11))
    label = _text_block("Table 9: Experimental results", title_box, block_type="caption", local_bbox=first)
    owner = lower if direction == "above" else upper
    row = (28.3, y - 0.5, 155.7, y + 12.5)
    if variant == "cross-column":
        row = (187.3, y, 285.7, y + 12)
    if variant == "ambiguous":
        (upper if owner is lower else lower)["_confirmed_table_caption_rows"] = ((row, direction == "above"),)
    owner["_confirmed_table_caption_rows"] = ((row, direction == "above"),)
    if variant == "wrapped":
        label["_local_line_bboxes"].append((28, y + 12, 117, y + 19))
    _classify_and_bind_visual_annotations([upper, label, lower], (320, 250))
    if variant in {"ambiguous", "cross-column"}:
        assert "_native_annotation_parent_bbox" not in label
    else:
        assert label["_native_annotation_parent_bbox"] == owner["bbox"]
        assert label["_visual_annotation_direction"] == direction


def test_separate_printed_table_and_figure_captions_bound_expansion(monkeypatch):
    """未绑定的表题只有同时得到独立表轨与数据列支持时才成为图题扩张屏障。"""
    from docvortex.analyzers.native.pdf import pipeline, table_detection, layout_visual_role_risk
    from docvortex.analyzers.native.pdf.models import _LineItem, _PageSource

    area = (60, 40, 300, 100)
    labels = [_LineItem("Table 1: Performance scores", (50, 102, 305, 114), 0, 9, effective_height=12),
              _LineItem("Figure 2: Test curves", (70, 198, 300, 212), 0, 10, effective_height=12)]
    source = _PageSource((400, 600), labels, [], [])
    table = SimpleNamespace(bbox=area, core_bbox=area, angle=0, line_indices={2, 3, 4}, annotations=[])
    monkeypatch.setattr(table_detection, "_detect_table_candidates", lambda page: [table])
    monkeypatch.setattr(table_detection, "has_native_closed_grid", lambda page, candidate: False)
    monkeypatch.setattr(pipeline, "_build_caption_graphic_blocks", lambda page, **kw: (kw["table_bboxes"], set()))
    def confirmed(*args):
        """用实际源表区模拟完整轨道验证通过的返回。"""
        return (layout_visual_role_risk.VisualTableEvidence(area, 3, 3, "闭合横竖轨"),)
    monkeypatch.setattr(layout_visual_role_risk, "collect_visual_tables", confirmed)
    assert pipeline._build_caption_supported_graphics(source)[0] == [area]
    monkeypatch.setattr(layout_visual_role_risk, "collect_visual_tables", lambda *args: ())
    assert pipeline._build_caption_supported_graphics(source)[0] == []
