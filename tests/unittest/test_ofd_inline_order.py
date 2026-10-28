"""无续行的 OFD 同行排序、XML 来源隔离及公开输出回归。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from bs4 import BeautifulSoup
import pytest

from _ofd_test_utils import build_ofd_package, page_xml, text_object
from _span_test_utils import inline_text
from test_ofd_inline_layout import _layout_line
from docvortex import parse
from docvortex.analyzers.native.ofd import assembly, reading_order
from docvortex.analyzers.native.ofd.models import ImageItem, OfdPageScene, TextLine
from docvortex.analyzers.native.ofd.package import OfdPackage
from docvortex.analyzers.native.ofd.scene import OfdSceneBuilder
from docvortex.analyzers.native.ofd.table import OfdTableRegion
from docvortex.render import render_html, render_markdown

_BODY = "这是条号与正文同行的末行，本页内没有跨过间隙"


def _without_content_box(payload: bytes) -> bytes:
    """移除通用夹具的内容框，保持真实附件的无 ContentBox 语义。"""
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, source.read(name).replace(b"<ofd:ContentBox>5 5 90 90</ofd:ContentBox>", b""))
    return output.getvalue()


def _repro3_payload(
    *,
    reverse: bool = False,
    angle: int = 0,
    source_width: float = 24.0,
    split_marker: bool = False,
    page_number: bool = True,
    cross_page: bool = False,
) -> bytes:
    """复建 repro3 的六个对象，并覆盖逆序、旋转、拆分条号和真正的页尾跨页段落。"""
    row_y = 245.0 if cross_page else 88.9598
    markers = (
        [(21, "第", f"36.9006 {row_y} {source_width} 5.6444", 5.63, 1), (38, "一条", f"42.5306 {row_y} 18.37 5.6444", 5.63, 1)]
        if split_marker
        else [(21, "第一条", f"36.9006 {row_y} {source_width} 5.6444", 5.63, 1)]
    )
    rows = [
        (20, "测试文档", "75.0 48.7 60.0 7.58", 6.35, 1),
        (24, "上方通栏段落第一行，用于给正文块提供横向重叠证据。", "25.5973 60.0 160.0 5.6444", 5.63, 2),
        (25, "上方通栏段落第二行，继续提供重叠证据。", "25.5973 69.7 160.0 5.6444", 5.63, 2),
        *markers,
        (22, _BODY, f"59.464 {row_y} 123.2597 5.6444", 5.63, 2),
    ]
    if page_number:
        rows.append((26, "—1—", "95.0 280.0 20.0 5.0" if cross_page else "95.0 260.0 20.0 5.0", 4.5, 2))
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
    pages = [("Pages/Page_0/Content.xml", page_xml(content, physical_box=physical_box))]
    if cross_page:
        continuation = "正文在下一页继续，完整句子应当连贯阅读。"
        pages.append(
            (
                "Pages/Page_1/Content.xml",
                page_xml(
                    text_object(
                        50,
                        continuation,
                        boundary="25.5973 20.0 160 5.6444",
                        size=5.63,
                        x=1.41,
                        y=4.85,
                        delta_x=" ".join(["5.63"] * (len(continuation) - 1)),
                    ),
                    physical_box="0 0 210 297",
                ),
            )
        )
    payload = build_ofd_package(pages)
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "Doc_0/PublicRes.xml":
                data = data.replace(b"</ofd:Fonts>", b'<ofd:Font ID="2" FontName="FangSong"/></ofd:Fonts>')
            target.writestr(name, data)
    return _without_content_box(output.getvalue())


def _keep_original_order(blocks, groups):
    """关闭末端顺序校正以取得同一输入的既有分块基线。"""
    return blocks


def _assert_only_inline_order_changes(payload: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """经真实解析和导出入口核对结果只交换目标块，原段落、样式和几何完全保留。"""
    actual = parse(payload, file_suffix="ofd", keep_model_json=True)
    with monkeypatch.context() as context:
        context.setattr(reading_order, "_restore_inline_order", _keep_original_order)
        baseline = parse(payload, file_suffix="ofd", keep_model_json=True)
    expected = deepcopy(baseline.model_json.to_dict())
    page = expected["pages"][0]
    marker = next(block for block in page if inline_text(block["content"]) == "第一条")
    body = next(block for block in page if inline_text(block["content"]) == _BODY)
    page.remove(marker)
    page.insert(page.index(body), marker)
    assert actual.model_json.to_dict() == expected
    actual_page = actual.model_json.pages[0]
    texts = [inline_text(block["content"]) for block in actual_page]
    assert texts.index(_BODY) == texts.index("第一条") + 1
    assert len(marker["lines"]) == len(body["lines"]) == 1
    middle_texts = [inline_text(block.content) for block in actual.middle_json.pages[0].blocks]
    assert middle_texts.index(_BODY) == middle_texts.index("第一条") + 1
    assert "第一条\n\n" + _BODY in render_markdown(actual.middle_json)
    paragraphs = [element.get_text() for element in BeautifulSoup(render_html(actual.middle_json), "html.parser").select("p")]
    assert paragraphs.index(_BODY) == paragraphs.index("第一条") + 1
    for private in ("source_bbox", "source_order", "_source_line_id"):
        assert private not in str(actual.model_json.to_dict())
        assert private not in str(actual.middle_json.to_dict())


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("source_width", [19.5929, 24.0])
@pytest.mark.parametrize("split_marker", [False, True])
def test_repro3_changes_only_inline_order(reverse, angle, source_width, split_marker, monkeypatch) -> None:
    """容器相邻或重叠、XML 逆序、拆分条号和四个阅读方向均只校正顺序。"""
    _assert_only_inline_order_changes(
        _repro3_payload(
            reverse=reverse,
            angle=angle,
            source_width=source_width,
            split_marker=split_marker,
        ),
        monkeypatch,
    )


@pytest.mark.parametrize("page_number", [False, True])
@pytest.mark.parametrize("cross_page", [False, True])
def test_repro3_without_page_number_or_with_next_page(page_number, cross_page, monkeypatch) -> None:
    """删除页码或将续行移到下一页，当前页的同行顺序均不依赖其他页内容。"""
    _assert_only_inline_order_changes(_repro3_payload(page_number=page_number, cross_page=cross_page), monkeypatch)


def _order_lines(*, angle: int = 0) -> list[TextLine]:
    """构造无语义条号、无续行的相邻源对象，避免排序规则依赖特定文本。"""
    return [
        replace(_layout_line("任意前缀", (20, 10, 32, 15), 15, (20, 10, 36, 15), angle=angle), source_order=(7, 3, 3)),
        replace(
            _layout_line("这是同行的完整正文", (40, 10, 100, 15), 15, (39, 10, 105, 15), angle=angle), source_order=(7, 4, 4)
        ),
    ]


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_xml_adjacent_fragments_without_continuation(angle) -> None:
    """无续行时仍可获得按文字方向排列的片段组，输入列表顺序不参与判定。"""
    left, right = _order_lines(angle=angle)
    assert assembly.source_inline_order_groups([right, left]) == [(id(left), id(right))]
    assert assembly.merge_source_adjacent_lines([left, right]) == [left, right]


@pytest.mark.parametrize(
    "case",
    [
        "missing-source",
        "missing-order",
        "different-parent",
        "nonconsecutive",
        "overlapping-order",
        "wide-gap",
        "wide-source-gap",
        "source-padding",
        "baseline",
        "font-size",
        "direction",
        "layer",
        "template",
    ],
)
def test_unverified_or_incompatible_fragments_are_not_linked(case) -> None:
    """缺失来源、非相邻对象及几何或来源冲突均保留既有排序，不把绘制序号当作邻接证明。"""
    left, right = _order_lines()
    if case == "missing-source":
        left = replace(left, source_bbox=None)
    elif case == "missing-order":
        left = replace(left, source_order=None)
    elif case == "different-parent":
        right = replace(right, source_order=(8, 4, 4))
    elif case == "nonconsecutive":
        right = replace(right, source_order=(7, 5, 5))
    elif case == "overlapping-order":
        right = replace(right, source_order=(7, 3, 4))
    elif case == "wide-gap":
        left = replace(left, source_bbox=(20, 10, 37, 15))
        right = replace(_layout_line(right.text, (44, 10, 100, 15), 15, (39, 10, 105, 15)), source_order=right.source_order)
    elif case == "wide-source-gap":
        left = replace(left, source_bbox=(20, 10, 34, 15))
    elif case == "source-padding":
        left = replace(left, source_bbox=(20, 10, 38, 15))
    elif case == "baseline":
        right = replace(
            _layout_line(right.text, (40, 11.01, 100, 16.01), 16.01, right.source_bbox), source_order=right.source_order
        )
    elif case == "font-size":
        right = replace(right, font_size=6.76)
    elif case == "direction":
        right = replace(right, angle=90)
    elif case == "layer":
        right = replace(right, layer_type="Foreground")
    elif case == "template":
        right = replace(right, template_id=5)
    assert assembly.source_inline_order_groups([right, left]) == []


@pytest.mark.parametrize("following", [False, True])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_neighboring_columns_block_inline_order(following, angle) -> None:
    """前方或后方的独立双栏都阻止同行关联，不能越过该行寻找通栏证据。"""
    left, right = _order_lines(angle=angle)
    y = 20 if following else 0
    neighbors = [
        _layout_line("左栏独立正文", (20, y, 32, y + 5), y + 5, None, angle=angle),
        _layout_line("右栏独立正文", (40, y, 100, y + 5), y + 5, None, angle=angle),
        _layout_line(
            "更远的通栏段落",
            (10, 30 if following else -10, 100, 35 if following else -5),
            35 if following else -5,
            None,
            angle=angle,
        ),
    ]
    assert assembly.source_inline_order_groups([right, *neighbors, left]) == []


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_barriers_and_protected_lines_block_inline_order(angle) -> None:
    """视觉屏障和受保护文字均不能成为同行排序链的一部分。"""
    left, right = _order_lines(angle=angle)
    barrier = assembly.upright_box((33, 11, 38, 14), -angle)
    assert assembly.source_inline_order_groups([right, left], barriers=[barrier]) == []
    assert assembly.source_inline_order_groups([right, left], excluded_line_ids=frozenset({id(left)})) == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("case", ["adjacent", "missing", "parent", "gap", "overlap"])
def test_source_order_survives_only_contiguous_merges(reverse, case) -> None:
    """组行只延续同一容器的连续来源区间，XML 逆序合并也保留完整首末序号。"""
    first = (3, 4, 5)
    second = {"adjacent": (3, 6, 6), "missing": None, "parent": (8, 6, 6), "gap": (3, 7, 7), "overlap": (3, 5, 6)}[case]
    if reverse:
        first, second = second, first
    left, right = _order_lines()
    left = replace(left, source_order=first)
    right = replace(_layout_line(right.text, (34, 10, 100, 15), 15, (33, 10, 105, 15)), source_order=second)
    merged = assembly.merge_same_baseline_lines([right, left])
    assert len(merged) == 1
    assert merged[0].source_order == ((3, 4, 6) if case == "adjacent" else None)


def test_multiple_fragments_restore_only_their_contiguous_interval() -> None:
    """三个片段按阅读方向重排，区间外对象和所有块内容保持原对象及原值。"""
    left, _ = _order_lines()
    middle = replace(_layout_line("中间片段", (40, 10, 60, 15), 15, (39, 10, 64, 15)), source_order=(7, 4, 4))
    right = replace(_layout_line("最后片段", (68, 10, 100, 15), 15, (67, 10, 105, 15)), source_order=(7, 5, 5))
    lines = [left, middle, right]
    groups = assembly.source_inline_order_groups(lines)
    scene = OfdPageScene(0, (0, 0, 210, 297), None, lines)
    projector = reading_order.OfdReadingOrderProjector([scene])
    blocks = [projector._text_block(line, scene) for line in lines]
    before = {"type": "image", "content": "before"}
    after = {"type": "image", "content": "after"}
    original = [before, blocks[2], blocks[0], blocks[1], after]
    snapshot = deepcopy(original)
    restored = reading_order._restore_inline_order(original, groups)
    assert restored == [before, *blocks, after]
    assert original == snapshot
    assert all(a is b for a, b in zip(restored, [before, *blocks, after], strict=True))


@pytest.mark.parametrize("case", ["other-line", "image", "merged-paragraph", "title", "missing-member"])
def test_final_reorder_never_crosses_other_blocks_or_splits_paragraphs(case) -> None:
    """合段后出现非成员块、已合并段落或缺失成员时，整个候选组保持原序。"""
    lines = _order_lines()
    scene = OfdPageScene(0, (0, 0, 210, 297), None, lines)
    projector = reading_order.OfdReadingOrderProjector([scene])
    left, right = [projector._text_block(line, scene) for line in lines]
    blocks = [right, left]
    if case in {"other-line", "image"}:
        blocks.insert(1, {"type": "text" if case == "other-line" else "image", "content": "屏障"})
    elif case == "merged-paragraph":
        right["line_bboxes_mm"].append((40, 20, 100, 25))
    elif case == "title":
        right["type"] = "paragraph_title"
    elif case == "missing-member":
        blocks = [right]
    assert reading_order._restore_inline_order(blocks, assembly.source_inline_order_groups(lines)) == blocks


@pytest.mark.parametrize("count", [64, 65])
def test_dense_rows_have_a_fixed_scan_limit(count) -> None:
    """同行对象达到上限仍可处理，超过上限保留旧序并限制邻域扫描开销。"""
    lines = [
        replace(
            _layout_line(
                f"文字{i}",
                (20 + i * 20, 10, 32 + i * 20, 15),
                15,
                (19 + i * 20, 10, 36 + i * 20, 15),
            ),
            source_order=(1, i, i),
        )
        for i in range(count)
    ]
    assert assembly.source_inline_order_groups(lines) == ([tuple(id(line) for line in lines)] if count == 64 else [])


@pytest.mark.parametrize("kind", ["same-parent", "different-layer", "different-page-block", "intervening-path", "multi-code"])
def test_scene_records_actual_parent_and_child_positions(kind) -> None:
    """通过 XML 入口核验来源身份和真实子节点序号，不以相同 Layer Type 或对象 ID 推断相邻。"""
    first = text_object(900, "甲乙", boundary="20 10 16 5", size=5, delta_x="5")
    second = text_object(2, "丙丁", boundary="40 10 20 5", size=5, delta_x="5")
    content = first + second
    if kind == "different-layer":
        content = first + '</ofd:Layer><ofd:Layer ID="9" Type="Body">' + second
    elif kind == "different-page-block":
        content = f"<ofd:PageBlock>{first}</ofd:PageBlock><ofd:PageBlock>{second}</ofd:PageBlock>"
    elif kind == "intervening-path":
        content = (
            first
            + '<ofd:PathObject ID="7" Boundary="0 0 1 1"><ofd:AbbreviatedData>M 0 0 L 1 1</ofd:AbbreviatedData></ofd:PathObject>'
            + second
        )
    elif kind == "multi-code":
        content = first.replace("</ofd:TextObject>", '<ofd:TextCode X="0" Y="5">戊</ofd:TextCode></ofd:TextObject>') + second
    with OfdPackage(_without_content_box(build_ofd_package([("Pages/Page_0/Content.xml", page_xml(content))]))) as package:
        lines = OfdSceneBuilder(package).build()[0].text_lines
    if kind == "multi-code":
        assert all(line.source_order is None for line in lines if line.object_id == 900)
    else:
        left, right = lines
        assert left.source_order is not None and right.source_order is not None
        if kind == "same-parent":
            assert left.source_order[0] == right.source_order[0]
            assert left.source_order[1:] == (0, 0) and right.source_order[1:] == (1, 1)
        elif kind == "intervening-path":
            assert left.source_order[0] == right.source_order[0]
            assert right.source_order[1:] == (2, 2)
        else:
            assert left.source_order[0] != right.source_order[0]


@pytest.mark.parametrize("kind", ["template", "composite"])
def test_repeated_resource_expansions_have_distinct_source_parents(kind) -> None:
    """相同模板或复合资源的重复展开，即使对象 ID 和资源节点相同，也拥有独立来源实例。"""
    text = text_object(30, "同一资源", boundary="20 10 30 5", size=5, delta_x="5 5 5")
    if kind == "template":
        page = page_xml("", template_id=5).replace(
            '<ofd:Template TemplateID="5" ZOrder="Background"/>',
            '<ofd:Template TemplateID="5" ZOrder="Background"/><ofd:Template TemplateID="5" ZOrder="Background"/>',
        )
        payload = build_ofd_package(
            [("Pages/Page_0/Content.xml", page)], templates={5: ("Tpls/Tpl_0/Content.xml", page_xml(text))}
        )
    else:
        page = page_xml(
            '<ofd:CompositeObject ID="11" ResourceID="7" Boundary="0 0 100 100"/>'
            '<ofd:CompositeObject ID="12" ResourceID="7" Boundary="0 0 100 100"/>',
            page_res="PageRes.xml",
        )
        payload = build_ofd_package(
            [("Pages/Page_0/Content.xml", page)],
            extra_parts={
                "Doc_0/Pages/Page_0/PageRes.xml": (
                    '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016"><ofd:CompositeGraphicUnits>'
                    '<ofd:CompositeGraphicUnit ID="7" Width="100" Height="100"><ofd:Content>'
                    + text
                    + "</ofd:Content></ofd:CompositeGraphicUnit></ofd:CompositeGraphicUnits></ofd:Res>"
                )
            },
        )
    with OfdPackage(_without_content_box(payload)) as package:
        lines = OfdSceneBuilder(package).build()[0].text_lines
    assert len(lines) == 2
    assert lines[0].object_id == lines[1].object_id == 30
    assert all(line.source_order is not None for line in lines)
    assert lines[0].source_order[0] != lines[1].source_order[0]
    assert lines[0].source_order[1:] == lines[1].source_order[1:]


@pytest.mark.parametrize("kind", ["image", "table", "title", "auxiliary"])
def test_projector_passes_barriers_and_exclusions_to_inline_order(kind, monkeypatch) -> None:
    """通过真实投影流程验证屏障来源，包含没有导出载荷的图片和被保护的文字。"""
    left, right = _order_lines()
    scene = OfdPageScene(0, (0, 0, 210, 297), None, [left, right])
    scenes = [scene]
    barrier = (33, 11, 38, 14)
    if kind == "image":
        scene.images.append(ImageItem(barrier, None, 2, 100, "Body", None))
    elif kind == "table":
        table = OfdTableRegion(barrier, "<table><tr><td>表格</td></tr></table>", 2, frozenset())

        def recover_table(*args, **kwargs):
            """提供已认领的表格区域，单独验证投影层是否把它作为排序屏障。"""
            return [table]

        monkeypatch.setattr(reading_order, "recover_tables", recover_table)
    else:
        protected = _layout_line("标题" if kind == "title" else "辅助文字", barrier, 14, None)
        protected = replace(protected, font_size=10) if kind == "title" else replace(protected, template_id=5)
        scene.text_lines.append(protected)
        if kind == "auxiliary":
            # 两页重复的模板文字形成真实页眉，模板作用域隔离避免它先被紧邻组行吞入正文。
            scenes.append(OfdPageScene(1, scene.physical_box, None, [replace(protected)]))
    original = assembly.source_inline_order_groups
    observed = []

    def inspect_groups(lines, **kwargs):
        """记录真正传入的屏障和排序候选，避免只验证独立辅助函数。"""
        result = original(lines, **kwargs)
        observed.append((kwargs, result))
        return result

    monkeypatch.setattr(reading_order, "source_inline_order_groups", inspect_groups)
    reading_order.OfdReadingOrderProjector(scenes).project_page(scene)
    assert len(observed) == 1
    assert barrier in observed[0][0]["barriers"]
    assert observed[0][1] == []
    if kind in {"title", "auxiliary"}:
        assert id(scene.text_lines[-1]) in observed[0][0]["excluded_line_ids"]


@pytest.mark.parametrize("gap,expected", [(4.0, False), (4.001, True), (10.0, True), (10.001, False)])
def test_visible_gap_thresholds_are_bounded(gap, expected) -> None:
    """真实间隙的下界开放、上界闭合；容器重叠不能绕过上限。"""
    left, right = _order_lines()
    left = replace(left, source_bbox=(20, 10, 105, 15))
    right = replace(
        _layout_line(right.text, (32 + gap, 10, 100, 15), 15, (31 + gap, 10, 105, 15)), source_order=right.source_order
    )
    assert bool(assembly.source_inline_order_groups([right, left])) is expected


def test_oversized_neighbor_row_disables_candidate_scan() -> None:
    """相邻行过于复杂时同样限制扫描，不因当前候选行较短而放宽预算。"""
    lines = _order_lines()
    neighbors = [_layout_line(f"片段{i}", (i * 20, 20, i * 20 + 10, 25), 25, None) for i in range(65)]
    assert assembly.source_inline_order_groups([*lines, *neighbors]) == []
