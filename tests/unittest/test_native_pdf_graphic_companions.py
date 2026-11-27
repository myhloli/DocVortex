"""覆盖闭合主图与侧栏小图的独立源对象所有权。"""

from collections import Counter
from dataclasses import replace

import pytest

from docvortex.analyzers.native.pdf.graphic_partitions import build_native_graphic_partitions
from docvortex.analyzers.native.pdf.models import _AxisLine, _LineItem, _PageSource
from docvortex.document.pdf.native_contracts import PDFPathInfo


def _page(*, x_offset, obstruction=None):
    """构造物理边框、独立图符与行框，给图核检测器提供的结果设定已知边界。"""
    lines, drawing, paths = [], [], []

    def move(box):
        """在字号和图宽固定时单独移动图核、路径和字形。"""
        left, top, right, bottom = box
        return left + x_offset, top, right + x_offset, bottom

    def path(box, segments):
        """创建真有墨迹的原生绘制摘要。"""
        paths.append(PDFPathInfo(move(box), segments, False, True, 0, len(paths)))

    def line(text, box):
        """以唯一源字行标识验证跨组后每个成员只出现一次。"""
        lines.append(_LineItem(text, move(box), 0, len(lines), ink_bbox=move(box), em_height=10))

    def edge(box, axis, *, record):
        """保存闭合框的边或分开的独立细线。"""
        drawing.append(_AxisLine(move(box), 1.0, axis))
        if record:
            path(box, 2)

    def frame(box, *, closed=True):
        """可以用四个散开的绘制对象模拟恰好围成矩形的排版规则。"""
        x, y, right, bottom = box
        if closed:
            path(box, 5)
        for y1 in (y, bottom):
            edge((x, y1 - 0.5, right, y1 + 0.5), "horizontal", record=not closed)
        for x1 in (x, right):
            edge((x1 - 0.5, y, x1 + 0.5, bottom), "vertical", record=not closed)

    main = (26, 28, 508, 571)
    locator = (552, 117, 720, 219)
    legend = (552, 280, 720, 396)
    frame(main, closed=obstruction != "open-frame")
    frame(locator)
    frame(legend)
    for i in range(104):
        x, y = 42 + 54 * (i % 8), 42 + 34 * (i // 8)
        path((x, y, x + 25, y + 18), 8)
    for i in range(12):
        line("Lake ferry" if i == 0 else f"District {i}", (64 + 90 * (i % 4), 65 + i * 14, 123 + 90 * (i % 4), 73 + i * 14))
    line("Lake ferry", (571, 140, 621, 148))
    line("Airport district", (650, 184, 716, 192))
    for i in range(5):
        y = 292 + i * 18
        if obstruction != "no-swatches":
            path((560, y - 1, 584, y + 9), 5)
        line(f"Route type {i}", (592, y, 658, y + 8))
    line("Detailed area", (602, 102, 670, 110))
    line("Route key", (605, 266, 667, 274))
    line("Independent notice for readers", (551, 420, 719, 429))
    line("City departure information", (542, 47, 729, 57))
    line("Symbol", (630, 461, 642, 479))
    line("Drawing scale", (606, 492, 666, 500))
    tick_x = (556, 596, 636, 676, 716) if obstruction != "irregular-ruler" else (556, 580, 636, 661, 716)
    for i, x in enumerate(tick_x):
        edge((x - 0.5, 521, x + 0.5, 529), "vertical", record=True)
        line(str(20 * i), (x - 5, 510, x + 5, 518))
    edge((556, 528.5, 716, 529.5), "horizontal", record=True)
    line("Distance", (613, 532, 659, 540))
    if obstruction == "crossing-word":
        line("Original paragraph intersects the ink boundary", (493, 240, 680, 250))
    source = _PageSource((800, 600), lines, [], drawing, path_infos=paths)
    return source, list(map(move, (main, locator, legend)))


@pytest.mark.parametrize("x_offset", [0, 32])
def test_framed_graphic_companions_assign_each_source_member_once(x_offset):
    """同一个地名字串在两个位置真实绘制，按位置分别保留，独立正文仍可编辑。"""
    source, cores = _page(x_offset=x_offset)
    blocks, claims = build_native_graphic_partitions(source, cores, [])
    assert len(blocks) == 4
    words = Counter(word for block in blocks for word in block["content"].splitlines())
    assert words["Lake ferry"] == 2
    assert words["Independent notice for readers"] == 0
    assert words["City departure information"] == 0
    assert {line.text for line in source.lines if line.source_index not in claims} == {
        "Independent notice for readers",
        "City departure information",
    }
    assert all(word in blocks[3]["content"] for word in ("Symbol", "Drawing scale", "Distance", "80"))
    for block in blocks[1:]:
        assert block["bbox"][0] > blocks[0]["bbox"][2]


@pytest.mark.parametrize("obstruction", ["open-frame", "no-swatches", "crossing-word", "existing-owner", "trusted-text"])
def test_framed_graphic_companions_require_independent_physical_evidence(obstruction):
    """散线、无符号正文、跨框字形和已有文本所有者均阻止整组抢占。"""
    source, cores = _page(x_offset=0, obstruction=obstruction)
    existing = [cores[1]] if obstruction == "existing-owner" else []
    if obstruction == "trusted-text":
        source = replace(source, trusted_text_bboxes=((565, 138, 626, 153),))
    assert build_native_graphic_partitions(source, cores, existing) == ([], set())


def test_disconnected_tick_spacing_keeps_the_drawn_panel():
    """已连通图件可有不等距的刻度，仍应当整体保留图中标记。"""
    source, cores = _page(x_offset=0, obstruction="irregular-ruler")
    blocks, claims = build_native_graphic_partitions(source, cores, [])
    assert len(blocks) == 4
    assert all(line.source_index in claims for line in source.lines if line.text in ("Symbol", "Drawing scale", "Distance"))


@pytest.mark.parametrize("obstruction", ["nearby-wide-text", "underline"])
def test_rail_with_prose_or_underline_has_no_new_partition(obstruction):
    """与图件靠得太近的栏宽正文和单一横线都不能借尺寸被栅格化。"""
    source, cores = _page(x_offset=0)
    if obstruction == "nearby-wide-text":
        row = next(line for line in source.lines if line.text == "Independent notice for readers")
        row.bbox = row.ink_bbox = (551, 438, 719, 449)
    else:
        source = replace(
            source,
            drawing_lines=[line for line in source.drawing_lines if line.bbox[1] < 516 or line.orientation == "horizontal"],
        )
    blocks, claims = build_native_graphic_partitions(source, cores, [])
    assert len(blocks) == 3
    assert all(
        line.source_index not in claims
        for line in source.lines
        if line.text in ("Independent notice for readers", "Symbol", "Drawing scale", "Distance")
    )
