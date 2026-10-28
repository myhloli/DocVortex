"""通栏上下文中的孤立并列双块排序，以及栏续行和视觉屏障保护回归。"""

from copy import deepcopy
from dataclasses import replace
import json

from bs4 import BeautifulSoup
import pytest

from _ofd_test_utils import build_ofd_package, page_xml, text_object
from _span_test_utils import inline_text
from test_ofd_inline_layout import _layout_line
from test_ofd_inline_order import _without_content_box
from docvortex import parse
from docvortex.api import render
from docvortex.analyzers.native.ofd import assembly, reading_order
from docvortex.analyzers.native.ofd.models import ImageItem, OfdPageScene
from docvortex.analyzers.native.ofd.table import OfdTableRegion


def _repro5_payload(*, reverse=False, angle=0, bottom="—1—") -> bytes:
    """在内存复建附件的坐标和字体，允许逆序、旋转以及替换非语义底部文字。"""
    rows = [
        (20, "上方通栏说明行第一行，提供页面文字布局上下文。", "25.5973 40.0 160.0 5.6444", 5.63, 2),
        (21, "上方通栏说明行第二行，继续提供上下文。", "25.5973 49.7 160.0 5.6444", 5.63, 2),
        (22, "测试单位甲", "42.2 70.0 35.0 5.6444", 5.63, 1),
        (23, "测试单位乙名称较长", "126.87 70.0 58.0 5.6444", 5.63, 1),
        (26, bottom, "95.0 260.0 20.0 5.0", 4.5, 2),
    ]
    objects = [
        text_object(
            oid, text, boundary=boundary, size=size, x=1.41, y=4.85, delta_x=" ".join(["5.63"] * (len(text) - 1))
        ).replace('Font="1"', f'Font="{font}"')
        for oid, text, boundary, size, font in rows
    ]
    content = "".join(reversed(objects) if reverse else objects)
    physical_box = "0 0 210 297"
    if angle:
        ctm = {90: "0 1 -1 0 297 0", 180: "-1 0 0 -1 210 297", 270: "0 -1 1 0 0 210"}[angle]
        content = f'<ofd:PageBlock CTM="{ctm}">{content}</ofd:PageBlock>'
        if angle in {90, 270}:
            physical_box = "0 0 297 210"
    return _without_content_box(
        build_ofd_package(
            [("Pages/Page_0/Content.xml", page_xml(content, physical_box=physical_box, page_res="PageRes.xml"))],
            extra_parts={
                "Doc_0/Pages/Page_0/PageRes.xml": (
                    '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Fonts>'
                    '<ofd:Font ID="1" FontName="SimHei"/><ofd:Font ID="2" FontName="FangSong"/>'
                    "</ofd:Fonts></ofd:Res>"
                )
            },
        )
    )


def _no_parallel_groups(*args, **kwargs):
    """关闭新排序组以获得同一输入的既有分块、样式和几何基线。"""
    return []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("bottom", ["—1—", "底部短句"])
def test_repro5_changes_only_block_order(reverse, angle, bottom, monkeypatch) -> None:
    """两单位保持独立文本块且排在底部文字前，内容、样式和几何完整保留，并阻止错误续段。"""
    payload = _repro5_payload(reverse=reverse, angle=angle, bottom=bottom)
    actual = parse(payload, file_suffix="ofd", keep_model_json=True)
    with monkeypatch.context() as context:
        context.setattr(reading_order, "source_parallel_row_groups", _no_parallel_groups)
        baseline = parse(payload, file_suffix="ofd", keep_model_json=True)
    expected_texts = [
        "上方通栏说明行第一行，提供页面文字布局上下文。上方通栏说明行第二行，继续提供上下文。",
        "测试单位甲",
        "测试单位乙名称较长",
        bottom,
    ]
    expected = deepcopy(baseline.model_json.to_dict())
    expected["pages"][0].sort(key=lambda block: expected_texts.index(inline_text(block["content"])))
    for block in expected["pages"][0][1:]:
        block["_paragraph_boundary"] = True
    assert actual.model_json.to_dict() == expected
    assert [inline_text(block["content"]) for block in actual.model_json.pages[0]] == expected_texts
    middle = actual.middle_json.pages[0].blocks
    assert [inline_text(block.content) for block in middle] == expected_texts
    assert all(block.type == "text" for block in middle)
    expected_middle = deepcopy(baseline.to_dict())
    expected_middle["pages"][0]["blocks"].sort(key=lambda block: expected_texts.index(inline_text(block["content"])))
    for index, block in enumerate(expected_middle["pages"][0]["blocks"]):
        block["index"] = index
        if index:
            # 独立并列行同时阻止旋转坐标导致的既有错误续段；原始内容与几何继续完整比较。
            block.pop("continues_prev", None)
    assert actual.to_dict() == expected_middle
    assert all(not block.continues_prev for block in middle)
    markdown = render(actual.middle_json, "markdown").content.decode()
    assert markdown.strip() == "\n\n".join(expected_texts)
    html = BeautifulSoup(render(actual.middle_json, "html").content, "html.parser")
    assert [element.get_text() for element in html.select("p")] == expected_texts
    structured = json.loads(render(actual.middle_json, "structured_content").content)
    assert [block["content"] for block in structured["pages"][0]["blocks"]] == expected_texts
    public = json.dumps([actual.model_json.to_dict(), actual.to_dict(), structured], ensure_ascii=False)
    for private in ("has_nonzero_delta_y", "source_order", "source_bbox", "_source_line_id", "_members"):
        assert private not in public


def _parallel_lines(*, angle=0):
    """构造两个通栏上下文、宽间距并列行和远处底部文字，来源连续且同域。"""
    rows = [
        ("通栏上下文第一行", (10, 40, 160, 45), 45, (10, 40, 165, 45)),
        ("通栏上下文第二行", (10, 50, 160, 55), 55, (10, 50, 165, 55)),
        ("左侧独立单位", (20, 69, 45, 74), 74, (20, 69, 50, 74)),
        ("右侧独立单位", (100, 69, 145, 74), 74, (100, 69, 150, 74)),
        ("普通底部文字", (65, 260, 85, 265), 265, (65, 260, 90, 265)),
    ]
    return [
        replace(_layout_line(text, bbox, baseline, source, angle=angle), source_order=(1, index, index))
        for index, (text, bbox, baseline, source) in enumerate(rows)
    ]


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_parallel_group_keeps_inputs_and_styles(angle) -> None:
    """并列证据与传入列表顺序无关，四个直角方向均不改动文字、样式及几何。"""
    lines = _parallel_lines(angle=angle)
    lines[2] = replace(lines[2], styles=("bold",))
    lines[3] = replace(lines[3], styles=("italic",))
    snapshot = deepcopy(lines)
    assert assembly.source_parallel_row_groups(list(reversed(lines))) == [(id(lines[2]), id(lines[3]))]
    assert lines == snapshot


@pytest.mark.parametrize(
    "case",
    [
        "one-context",
        "no-context",
        "context-misses-gap",
        "context-too-far",
        "context-gap-too-large",
        "context-font",
        "missing-source",
        "missing-order",
        "different-parent",
        "nonconsecutive",
        "overlap-order",
        "overlap-source",
        "baseline",
        "font",
        "direction",
        "layer",
        "template",
        "close-fragments",
        "third-fragment",
        "foreign-crossing",
        "left-continuation",
        "right-continuation",
        "two-columns",
        "rtl",
        "unknown-direction",
    ],
)
def test_ambiguous_parallel_rows_keep_column_order(case) -> None:
    """上下文、来源或方向证据不足及独立栏续行时，不创建临时排序组。"""
    lines = _parallel_lines()
    if case == "one-context":
        del lines[0]
    elif case == "no-context":
        del lines[:2]
    elif case == "context-misses-gap":
        lines[0] = _layout_line("较窄的上方文字", (10, 40, 90, 45), 45, None)
    elif case == "context-too-far":
        lines[1] = _layout_line("通栏上下文第二行", (10, 48, 160, 53), 53, None)
    elif case == "context-gap-too-large":
        lines[0] = _layout_line("通栏上下文第一行", (10, 30, 160, 35), 35, None)
    elif case == "context-font":
        lines[0] = replace(lines[0], font_size=9)
    elif case == "missing-source":
        lines[2] = replace(lines[2], source_bbox=None)
    elif case == "missing-order":
        lines[2] = replace(lines[2], source_order=None)
    elif case == "different-parent":
        lines[3] = replace(lines[3], source_order=(2, 3, 3))
    elif case == "nonconsecutive":
        lines[3] = replace(lines[3], source_order=(1, 5, 5))
    elif case == "overlap-order":
        lines[3] = replace(lines[3], source_order=(1, 2, 3))
    elif case == "overlap-source":
        lines[2] = replace(lines[2], source_bbox=(20, 69, 120, 74))
    elif case == "baseline":
        lines[3] = replace(_layout_line(lines[3].text, (100, 71, 145, 76), 76, (100, 71, 150, 76)), source_order=(1, 3, 3))
    elif case == "font":
        lines[3] = replace(lines[3], font_size=8)
    elif case == "direction":
        lines[3] = replace(lines[3], angle=90)
    elif case == "layer":
        lines[3] = replace(lines[3], layer_type="Foreground")
    elif case == "template":
        lines[3] = replace(lines[3], template_id=1)
    elif case == "close-fragments":
        lines[3] = replace(_layout_line(lines[3].text, (54, 69, 145, 74), 74, (53, 69, 150, 74)), source_order=(1, 3, 3))
    elif case in {"third-fragment", "foreign-crossing"}:
        line = _layout_line("穿过间隙的文字", (60, 69, 85, 74), 74, None)
        lines.append(replace(line, layer_type="Foreground") if case == "foreign-crossing" else line)
    elif case in {"left-continuation", "right-continuation", "two-columns"}:
        if case != "right-continuation":
            lines.append(_layout_line("左栏续行", (20, 79, 45, 84), 84, None))
        if case != "left-continuation":
            lines.append(_layout_line("右栏续行", (100, 79, 145, 84), 84, None))
    elif case == "rtl":
        lines[2] = replace(lines[2], text="مرحبا")
    elif case == "unknown-direction":
        lines[2] = replace(lines[2], text="12345")
    assert assembly.source_parallel_row_groups(lines) == []


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("barrier", [(60, 70, 85, 73), (10, 60, 150, 63)])
def test_parallel_row_visual_barriers(angle, barrier) -> None:
    """候选行内部或通栏上下文之间的视觉对象阻止排序关联，旋转后约束相同。"""
    lines = _parallel_lines(angle=angle)
    assert assembly.source_parallel_row_groups(lines, barriers=[assembly.upright_box(barrier, -angle)]) == []


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_protected_context_or_member_prevents_parallel_group(index) -> None:
    """标题、辅助文字及其他受保护行不能作为并列成员或通栏证据。"""
    lines = _parallel_lines()
    assert assembly.source_parallel_row_groups(lines, excluded_line_ids=frozenset({id(lines[index])})) == []


@pytest.mark.parametrize("kind", ["image", "table", "title", "auxiliary"])
def test_projector_supplies_parallel_barriers_and_exclusions(kind, monkeypatch) -> None:
    """通过真实页面投影核对图片、表格及受保护文字均传入并列证据判定。"""
    lines = _parallel_lines()
    scene = OfdPageScene(0, (0, 0, 210, 297), None, lines)
    barrier = (60, 70, 85, 73)
    if kind == "image":
        scene.images.append(ImageItem(barrier, None, 5, None, "Body", None))
    elif kind == "table":

        def recover_table(*args, **kwargs):
            """返回位于两成员之间的已认领表格，核对真实投影器的视觉屏障传递。"""
            return [OfdTableRegion(barrier, "<table></table>", 5, frozenset())]

        monkeypatch.setattr(reading_order, "recover_tables", recover_table)
    elif kind == "title":
        lines[2] = replace(lines[2], font_size=10)
    else:
        scene.content_box = (0, 80, 210, 297)
    original = assembly.source_parallel_row_groups
    observed = []

    def inspect_groups(items, **kwargs):
        """记录生产投影器传入的排除集合和屏障，再执行真实候选判定。"""
        groups = original(items, **kwargs)
        observed.append((groups, kwargs))
        return groups

    monkeypatch.setattr(reading_order, "source_parallel_row_groups", inspect_groups)
    reading_order.OfdReadingOrderProjector([scene]).project_page(scene)
    assert observed and observed[0][0] == []
    if kind in {"image", "table"}:
        assert barrier in observed[0][1]["barriers"]
    else:
        assert observed[0][1]["excluded_line_ids"]
