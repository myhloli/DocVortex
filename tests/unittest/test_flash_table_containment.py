"""验证嵌入横线表的独立认领，以及整页回落需要的外部正文证据。"""

from types import SimpleNamespace as NS

import pytest
from docvortex.analyzers.native.pdf.table_detection import _enclosing_prose_drafts
from docvortex.analyzers.native.pdf.table_rules import _RuleCandidateDraft
from docvortex.analyzers.native.pdf.layout_native_conflicts import table_container_risk, _source_rows


def _row(y, x, members):
    """构造一行带有稳定源索引和真实文字宽度的布局片段。"""
    fragments = [NS(line_index=i, text="independent reading material") for i in members]
    return NS(center_y=y, bbox=(x, y - 4, x + 140, y + 4), fragments=fragments)


@pytest.mark.parametrize("offset,scale", [(0, 1), (27, 1.25)])
@pytest.mark.parametrize("variant,expected", [("independent", {0, 1}), ("rail", set()), ("extra_cells", set())])
def test_independent_ruled_body_survives_merge(offset, scale, variant, expected):
    """真小表拥有全部多列行时释放上下正文；贯穿格线或额外多列行仍支持大表。"""

    def xy(v):
        """平移缩放所有坐标以检查相对几何的泛化。"""
        return offset + v * scale

    def draft(left, top, bottom, rows, caption=None):
        """用两个物理横线边界声明可比较的表候选。"""
        lines = [NS(bbox=tuple(map(xy, (left, y, 290, y)))) for y in (top, bottom)]
        return _RuleCandidateDraft(NS(angle=0, median_height=8 * scale), lines, rows, caption, [], False, 10)

    def row(y, x, ids):
        """同步改变文字框、基线和表线，保留成员身份。"""
        r = _row(y, x, ids)
        r.center_y = xy(y)
        r.bbox = tuple(map(xy, r.bbox))
        return r

    multi = [row(y, 95, [i, i + 1, i + 2]) for i, y in ((10, 65), (20, 78), (30, 90))]
    caption = row(49, 100, [3])
    pre = [row(12, 90, [1]), row(24, 90, [2])]
    post = [row(125, 90, [40]), row(139, 90, [41])]
    outer_rows = pre + [caption] + multi + post
    if variant == "extra_cells":
        outer_rows.append(row(150, 45, [50, 51, 52]))
    drafts = [draft(20, 0, 165, outer_rows), draft(95, 41, 99, [caption] + multi), draft(95, 57, 99, multi, NS(source_index=3))]
    rails = [NS(orientation="vertical", bbox=tuple(map(xy, (20, 0, 20, 165))))] if variant == "rail" else []
    assert _enclosing_prose_drafts(NS(drawing_lines=rails), drafts) == expected


def test_mixed_font_ink_shares_one_baseline():
    """高低字体墨迹分开时，同一实际基线上的字形应归到一行。"""
    chars = [
        {"char": "A", "bbox": (10, 10, 18, 20), "tight_bbox": (10, 10, 18, 14), "origin": (10, 20)},
        {"char": "g", "bbox": (20, 14, 28, 24), "tight_bbox": (20, 19, 28, 24), "origin": (20, 20)},
    ]
    lines = [{"spans": [{"chars": chars}]}]
    assert len(_source_rows(lines, (0, 0, 100, 100), baseline=True)) == 1


def test_explicit_cell_custody_and_single_cell_wrapper():
    """来源表的多格成员可证明说明行归属，单一整页包装不能据此免除外溢。"""
    cols = [(50, "alpha"), (175, "beta"), (310, "gamma")]
    # 连续字形跨过两条列缝，三条说明均为独立物理行。
    prose = "This is one extended line of the long running description"
    lines = []
    for y, values in [(y, cols) for y in (80, 100, 120)] + [(y, [(50, prose)]) for y in (215, 230, 245)]:
        spans = []
        for x, value in values:
            chars = [
                {"char": ch, "bbox": (x + 5 * i, y, x + 5 * i + 4, y + 8), "origin": (x + 5 * i, y + 8)}
                for i, ch in enumerate(value)
            ]
            spans.append({"chars": chars})
        lines.append({"spans": spans})
    block = {"type": "table", "bbox": (0.08, 0.1, 0.88, 0.78), "_pdf_structure": {"node_id": "local"}}
    block["content"] = "<table><tr><td>" + prose + "</td></tr></table>"
    assert table_container_risk([block], (400, 400), lines)[0] == 30
    block["content"] = "<table><tr><td>label</td><td>" + prose + "</td></tr></table>"
    assert table_container_risk([block], (400, 400), lines) is None


@pytest.mark.parametrize("barrier", ["table", "paragraph_title", "doc_title"])
def test_previous_caption_cannot_cross_content_barrier(barrier):
    """前表或节标题结束旧表题，后一张表上方的正文必须保留为正文。"""
    from docvortex.postprocess.visual import fallback_inline_caption_fragments

    blocks = [
        {"index": 0, "type": "caption", "bbox": [20, 10, 190, 20], "content": "A caption"},
        {"index": 1, "type": barrier, "bbox": [20, 21, 190, 38], "content": ""},
        {
            "index": 2,
            "type": "text",
            "bbox": [20, 40, 190, 49],
            "content": "An independent paragraph.",
            "lines": [{"bbox": [20, 40, 190, 49]}],
        },
        {"index": 3, "type": "table", "bbox": [20, 52, 190, 120], "content": ""},
    ]
    fallback_inline_caption_fragments(blocks, {"table": "table"})
    assert blocks[2]["type"] == "text"


@pytest.mark.parametrize("additional_note", [False, True])
def test_caption_row_at_following_table_keeps_all_source_members(additional_note):
    """跨区候选的底边开出下一张表时，只在首带恰好为其表题时释放该带。"""

    def draft(left, top, bottom, rows, caption=None):
        """用已识别的横线和有序源行创建合并前的候选。"""
        edges = [NS(bbox=(left, top, 300, top)), NS(bbox=(left, bottom, 300, bottom))]
        return _RuleCandidateDraft(NS(angle=0, median_height=8), edges, rows, caption, [], False, 10)

    upper = [_row(y, 95, [i, i + 1, i + 2]) for i, y in [(10, 60), (15, 75), (20, 90)]]
    first = draft(20, 0, 165, [_row(12, 90, [1]), _row(26, 90, [2]), *upper, _row(130, 90, [25])])
    inner = draft(95, 45, 100, upper)
    header = _row(191, 45, [51, 52, 53])
    values = [header, _row(206, 45, [54, 55, 56]), _row(222, 45, [57, 58, 59])]
    title = _row(175, 45, [50])
    extra = [_row(180, 45, [75])] if additional_note else []
    shell = draft(20, 165, 235, [title, *extra, *values])
    body = draft(20, 185, 235, values, NS(source_index=50))
    assert _enclosing_prose_drafts(NS(drawing_lines=[]), [first, inner, shell, body]) == ({0} if additional_note else {0, 2})
