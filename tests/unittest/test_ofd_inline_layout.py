"""OFD 源对象邻接与续行证据的组行、阅读顺序和导出回归。"""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from bs4 import BeautifulSoup

from _ofd_test_utils import build_ofd_package, page_xml, text_object
from _span_test_utils import inline_text
from docvortex import parse
from docvortex.analyzers.native.ofd import assembly
from docvortex.analyzers.native.ofd.geometry import rect_quad
from docvortex.analyzers.native.ofd.models import AxisLine, GlyphItem, ImageItem, OfdPageScene, TextLine
from docvortex.analyzers.native.ofd.package import OfdPackage
from docvortex.analyzers.native.ofd.reading_order import OfdReadingOrderProjector
from docvortex.analyzers.native.ofd.scene import OfdSceneBuilder
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.render import render_html, render_markdown
from docvortex.schema import ModelJson


def _repro_payload(*, reverse: bool = False, angle: int = 0) -> bytes:
    """在内存中复建 issue 5666 的四个对象，保留字号、位置、步进和不同字体资源。"""
    objects = []
    for object_id, text, boundary, size, font in (
        (20, "测试文档", "75.0 48.7 60.0 7.58", 6.35, 1),
        (21, "第一条", "36.9 88.96 19.6 5.64", 5.63, 1),
        (22, "这是一段用于复现同行块排序问题的正文文本，", "59.4 88.96 130.0 5.64", 5.63, 2),
        (23, "正文在下一行继续，完整句子应当连贯阅读。", "25.6 98.65 160.0 5.64", 5.63, 2),
    ):
        objects.append(
            text_object(
                object_id,
                text,
                boundary=boundary,
                size=size,
                x=1.41,
                y=4.85,
                delta_x=" ".join(["5.63"] * (len(text) - 1)),
            ).replace('Font="1"', f'Font="{font}"')
        )
    if reverse:
        objects.reverse()
    content = "".join(objects)
    if angle:
        ctm = {90: "0 1 -1 0 300 0", 180: "-1 0 0 -1 300 300", 270: "0 -1 1 0 0 300"}[angle]
        content = f'<ofd:PageBlock CTM="{ctm}">{content}</ofd:PageBlock>'
    payload = build_ofd_package(
        [("Pages/Page_0/Content.xml", page_xml(content, physical_box="0 0 300 300", page_res="PageRes.xml"))],
        extra_parts={
            "Doc_0/Pages/Page_0/PageRes.xml": (
                '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Fonts>'
                '<ofd:Font ID="2" FontName="FangSong"/></ofd:Fonts></ofd:Res>'
            )
        },
    )
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "Doc_0/Document.xml":
                # 附件没有 ContentBox；去掉通用夹具的小页内容框，避免旋转后被误判为页边文字。
                data = data.replace(b"<ofd:ContentBox>5 5 90 90</ofd:ContentBox>", b"")
            target.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_issue_5666_preserves_inline_order_and_continues_paragraph(reverse: bool, angle: int) -> None:
    """正序及逆序 XML 均恢复条号、同行正文和续行，结构化结果与真实导出一致。"""
    result = parse(_repro_payload(reverse=reverse, angle=angle), file_suffix="ofd", keep_model_json=True)
    expected = "第一条 这是一段用于复现同行块排序问题的正文文本，正文在下一行继续，完整句子应当连贯阅读。"
    assert [inline_text(block["content"]) for block in result.model_json.pages[0]] == ["测试文档", expected]
    assert [len(block["lines"]) for block in result.model_json.pages[0]] == [1, 2]
    assert [inline_text(block.content) for block in result.middle_json.pages[0].blocks] == ["测试文档", expected]
    assert render_markdown(result.middle_json).strip() == "测试文档\n\n" + expected
    html = BeautifulSoup(render_html(result.middle_json), "html.parser")
    assert [element.get_text() for element in html.select("p")] == ["测试文档", expected]
    assert "source_bbox" not in str(result.model_json.to_dict())


def _layout_line(text: str, bbox: tuple, baseline: float, source_bbox: tuple | None, *, angle: int = 0) -> TextLine:
    """构造带源布局范围的文字行，并将字形、基线和源框一起旋转。"""
    quad = tuple(assembly.upright_point(x, y, -angle) for x, y in rect_quad(bbox))
    rotated = (min(p[0] for p in quad), min(p[1] for p in quad), max(p[0] for p in quad), max(p[1] for p in quad))
    glyph = GlyphItem(text, rotated, quad, assembly.upright_point(bbox[0], baseline, -angle))
    source = assembly.upright_box(source_bbox, -angle) if source_bbox else None
    return TextLine(text, rotated, [glyph], angle, 5, 0, None, "Body", None, source_bbox=source)


def _inline_lines(*, angle: int = 0) -> list[TextLine]:
    """构造字形间距较大、源框相邻且有正常续行的通用三片段场景。"""
    return [
        _layout_line("任意前缀", (20, 10, 32, 15), 15, (20, 10, 36, 15), angle=angle),
        _layout_line("这是同行的完整正文", (40, 10, 100, 15), 15, (39, 10, 105, 15), angle=angle),
        _layout_line("这是同一段的后续文字", (10, 20, 95, 25), 25, (10, 20, 100, 25), angle=angle),
    ]


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_source_supported_row_keeps_styles_geometry_and_input(angle: int) -> None:
    """一般文本也能依据几何组行，四个直角方向均保留局部样式、字形和输入所有权。"""
    lines = _inline_lines(angle=angle)
    lines[0] = replace(lines[0], styles=("bold",))
    lines[1] = replace(lines[1], styles=("italic",))
    result = assembly.merge_source_adjacent_lines(assembly.merge_same_baseline_lines(list(reversed(lines))))
    assert len(result) == 2
    assert result[0].text == "任意前缀 这是同行的完整正文"
    assert assembly.line_html(result[0]) == "<strong>任意前缀</strong> <em>这是同行的完整正文</em>"
    assert assembly.upright_box(result[0].bbox, angle) == pytest.approx((20, 10, 100, 15))
    assert result[0].glyphs == lines[0].glyphs + lines[1].glyphs
    assert [line.text for line in lines] == ["任意前缀", "这是同行的完整正文", "这是同一段的后续文字"]


@pytest.mark.parametrize(
    "case", ["missing", "wide", "padding", "overlap", "no-following", "columns", "previous-columns", "template", "baseline"]
)
def test_source_adjacency_requires_independent_continuation_evidence(case: str) -> None:
    """缺失或不可靠的源框、独立双栏及不相容来源均不能用新规则强行组行。"""
    left, right, following = _inline_lines()
    if case == "missing":
        left = replace(left, source_bbox=None)
    elif case == "wide":
        left = replace(left, source_bbox=(20, 10, 33, 15))
    elif case == "padding":
        left = replace(left, bbox=(20, 10, 28, 15))
    elif case == "overlap":
        left = replace(left, source_bbox=(20, 10, 41, 15))
    elif case == "template":
        right = replace(right, template_id=7)
    elif case == "baseline":
        right = _layout_line(right.text, (40, 12, 100, 17), 17, (39, 12, 105, 17))
    following_lines = [] if case == "no-following" else [following]
    if case == "columns":
        following_lines = [
            _layout_line("左栏后续文字", (20, 20, 32, 25), 25, (20, 20, 36, 25)),
            _layout_line("右栏后续文字", (40, 20, 100, 25), 25, (39, 20, 105, 25)),
            _layout_line("后面出现的通栏段落", (10, 30, 100, 35), 35, (10, 30, 105, 35)),
        ]
    lines = [left, right, *following_lines]
    if case == "previous-columns":
        lines = [
            _layout_line("左栏前面的文字", (20, 0, 32, 5), 5, (20, 0, 36, 5)),
            _layout_line("右栏前面的文字", (40, 0, 100, 5), 5, (39, 0, 105, 5)),
            *lines,
        ]
    assert assembly.merge_source_adjacent_lines(lines) == lines


@pytest.mark.parametrize("barrier", [(33, 11, 38, 14), (10, 16, 105, 19)])
def test_visual_barrier_blocks_source_supported_row(barrier: tuple) -> None:
    """行内间隙或到续行之间有视觉块时，不能跨过该块引用续行证据。"""
    assert len(assembly.merge_source_adjacent_lines(_inline_lines(), barriers=[barrier])) == 3


@pytest.mark.parametrize("kind", ["image", "table"])
def test_projector_supplies_visual_barriers_before_source_assembly(kind: str) -> None:
    """通过真实投影入口验证已认领表格和无载荷图片也会阻断源布局组行。"""
    scene = OfdPageScene(0, (0, 0, 210, 297), None, _inline_lines())
    if kind == "image":
        scene.images = [ImageItem((33, 11, 38, 14), None, 0, 100, "Body", None)]
    else:
        scene.axis_lines = [AxisLine((30, y, 70, y), "horizontal", 0.1, 0, None) for y in (16, 19)] + [
            AxisLine((x, 16, x, 19), "vertical", 0.1, 0, None) for x in (30, 50, 70)
        ]
        scene.text_lines += [
            _layout_line("甲", (32, 17, 37, 18), 18, None),
            _layout_line("乙", (52, 17, 57, 18), 18, None),
        ]
    blocks = OfdReadingOrderProjector([scene]).project_page(scene)
    texts = [inline_text(block["content"]) for block in blocks if block["type"] == "text"]
    assert "任意前缀" in texts
    assert "这是同行的完整正文" in texts
    if kind == "table":
        assert sum(block["type"] == "table" for block in blocks) == 1


def test_three_source_fragments_share_one_row() -> None:
    """同一视觉行的三个独立对象顺次组装，联合源框只在当前行内传递。"""
    left, _, following = _inline_lines()
    middle = _layout_line("中间文字", (40, 10, 60, 15), 15, (39, 10, 64, 15))
    right = _layout_line("最后文字", (68, 10, 100, 15), 15, (67, 10, 105, 15))
    lines = assembly.merge_source_adjacent_lines(assembly.merge_same_baseline_lines([right, following, middle, left]))
    assert [line.text for line in lines] == ["任意前缀 中间文字 最后文字", following.text]


def test_dense_fragment_rows_keep_the_existing_assembly_result() -> None:
    """大量分离片段不进入新增邻域扫描，保持原有顺序并限制额外拼接成本。"""
    lines = [
        _layout_line("文字片段", (20 + i * 20, 10, 32 + i * 20, 15), 15, (19 + i * 20, 10, 36 + i * 20, 15)) for i in range(65)
    ]
    lines.append(_layout_line("后面一行跨越全部片段", (10, 20, 1400, 25), 25, (10, 20, 1405, 25)))
    assert assembly.merge_source_adjacent_lines(lines) == lines


@pytest.mark.parametrize("side", ["left", "right"])
def test_source_assembly_preserves_existing_boundary_whitespace(side: str) -> None:
    """源文字已有边界空白时保留原字符，不再添加第二个分隔空格。"""
    left, right, following = _inline_lines()
    left = replace(left, text=left.text + (" " if side == "left" else ""))
    right = replace(right, text=(" " if side == "right" else "") + right.text)
    assert assembly.merge_source_adjacent_lines([left, right, following])[0].text == "任意前缀 这是同行的完整正文"


@pytest.mark.parametrize("marker", ["第一条", "第12条", "第十二条", "1."])
def test_new_numbered_paragraph_is_not_joined_to_previous(marker: str) -> None:
    """新的法规条号或列表编号保持段落边界，正文续行仍可在各段内部合并。"""
    lines = [
        _layout_line("上一条的完整正文尚未结束", (20, 10, 100, 15), 15, None),
        _layout_line(marker + " 这是下一条的完整正文", (20, 20, 100, 25), 25, None),
        _layout_line("下一条的继续正文", (10, 30, 95, 35), 35, None),
    ]
    scene = OfdPageScene(0, (0, 0, 210, 297), None, lines)
    blocks = OfdReadingOrderProjector([scene]).project_page(scene)
    assert [inline_text(block["content"]) for block in blocks] == [lines[0].text, lines[1].text + lines[2].text]


def test_article_boundary_survives_public_cross_page_postprocessing() -> None:
    """即使两页行宽对齐且上一页没有句末标点，新条款也不能被公共后处理标记为续段。"""
    scenes = [
        OfdPageScene(
            0,
            (0, 0, 210, 297),
            None,
            [
                _layout_line("第一页接近页尾的正文", (10, 250, 100, 255), 255, None),
                _layout_line("第一页最后一行没有结束标点", (10, 258, 100, 263), 263, None),
            ],
        ),
        OfdPageScene(
            1,
            (0, 0, 210, 297),
            None,
            [
                _layout_line("第二条 这是新条款的正文", (10, 20, 100, 25), 25, None),
                _layout_line("新条款仍在继续的正文", (10, 28, 100, 33), 33, None),
            ],
        ),
    ]
    model = ModelJson(
        pages=OfdReadingOrderProjector(scenes).project(),
        page_index_map=[],
        metadata={"file_suffix": "ofd", "producer": {"name": "test", "version": "1"}},
    )
    middle = model_json_to_middle_json(model)
    assert [len(page.blocks) for page in middle.pages] == [1, 1]
    assert not middle.pages[1].blocks[0].continues_prev
    assert "_paragraph_boundary" not in str(middle.to_dict())


@pytest.mark.parametrize("kind", ["single", "two-codes", "delta-y", "clipped-delta-y", "large-box", "clipped-large-box"])
def test_only_verified_single_line_objects_keep_source_layout(kind: str) -> None:
    """源框仅附着到确认单行的对象，多 TextCode、折行步进和高容器不提供邻接证据。"""
    content = '<ofd:TextCode X="1" Y="5" DeltaX="5 5">甲乙丙</ofd:TextCode>'
    boundary = "10 10 30 6"
    if kind == "two-codes":
        content += '<ofd:TextCode X="1" Y="12" DeltaX="5 5">丁戊己</ofd:TextCode>'
        boundary = "10 10 30 15"
    elif kind in {"delta-y", "clipped-delta-y"}:
        delta = "20" if kind == "clipped-delta-y" else "2"
        content = f'<ofd:TextCode X="1" Y="5" DeltaX="5 5" DeltaY="{delta} 0">甲乙丙</ofd:TextCode>'
    elif kind in {"large-box", "clipped-large-box"}:
        boundary = "10 10 30 60"
    obj = f'<ofd:TextObject ID="1" Boundary="{boundary}" Font="1" Size="5">{content}</ofd:TextObject>'
    if kind == "clipped-large-box":
        obj = f'<ofd:PageBlock Boundary="0 0 100 16">{obj}</ofd:PageBlock>'
    payload = build_ofd_package([("Pages/Page_0/Content.xml", page_xml(obj))])
    with OfdPackage(payload) as package:
        lines = OfdSceneBuilder(package).build()[0].text_lines
    assert lines
    if kind == "single":
        assert lines[0].source_bbox == (10, 10, 40, 16)
    else:
        assert all(line.source_bbox is None for line in lines)
