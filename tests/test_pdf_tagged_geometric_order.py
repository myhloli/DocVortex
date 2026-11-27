"""针对带图题的结构逆序、页码和双栏编号的回归。"""

from types import SimpleNamespace

import pytest

from docvortex.api import parse, render
from docvortex.analyzers.native.pdf.tagged_order import prefer_geometric_tagged_order
from docvortex.content.spans import inline_span_plain_text
from docvortex.render.contracts import HtmlRenderOptions, RenderMode
from _tagged_pdf_fixtures import TaggedPdfBuilder


def text(name, value, bounds):
    """用独立作者段落及真实行框契约构造可排序的行片段。"""
    return {
        "_pdf_structure": {"node_id": name},
        "type": "text",
        "content": [{"type": "text", "content": value}],
        "bbox": bounds,
        "lines": [{"bbox": bounds}],
    }


def image(name, bounds):
    """构造保留源身份的独立图块。"""
    return {"_pdf_structure": {"node_id": name}, "type": "image", "bbox": bounds}


def apply_layout(pages, *, declared_ua=None, repairs=None):
    """将完整物理页顺序送入实际的候选验证及 XY-Cut++。"""
    sources = [SimpleNamespace(size=(600, 800), page_index=i) for i in range(len(pages))]
    roles = {
        item["_pdf_structure"]["node_id"]: ("Caption" if item["type"] in {"caption", "image_caption"} else "P")
        for page in pages
        for item in page
        if item["type"] in {"text", "caption", "image_caption"} and item.get("_pdf_structure")
    }
    prefer_geometric_tagged_order(
        pages,
        sources,
        roles,
        SimpleNamespace(page_count=len(pages)),
        declared_ua=declared_ua,
        repairs=repairs,
    )


@pytest.mark.parametrize(
    "tail,shift,wanted",
    [
        ("(e) Last panel", 0, ["lead", "next", "tail"]),
        ("(f) Skipped panel", 0, ["lead", "next"]),
        ("Another paragraph", 0, ["lead", "next"]),
        ("(e) Other column", 0.32, ["lead", "next"]),
    ],
)
def test_numbered_caption_keeps_only_consecutive_same_lane_subfigures(tail, shift, wanted):
    """任意字母起点、多行续注和同行误归的正反例共同核验。"""
    lead = text("lead", "Fig. 7. (c) First panel", [0.27, 0.415, 0.73, 0.435])
    next_ = text("next", "(d) Second panel", [0.275, 0.443, 0.725, 0.463])
    last = text("tail", tail, [0.28 + shift, 0.471, 0.72 + shift, 0.491])
    body = text("body", "The following complete section continues on the same page.", [0.13, 0.52, 0.86, 0.6])
    figure = image("figure", [0.16, 0.10, 0.84, 0.40])
    pages = [[lead, next_, last, body, figure]]
    apply_layout(pages)
    assert pages[0][0] is figure
    owned = [b["_pdf_structure"] for b in pages[0] if b["type"] == "image_caption"]
    assert [m["node_id"] for m in owned] == wanted
    assert all(m["inferred_visual_parent"] == "figure" for m in owned)
    assert body["type"] == "text"


def test_subfigure_line_can_contain_multiple_consecutive_markers():
    """续注的一条源行可以同时说明多个相邻子图。"""
    cap = text("lead", "Fig. 12: (b.) Setup (c.) Verification", [0.27, 0.415, 0.73, 0.435])
    follow = text("d-e", "(D) Condition (E) Result", [0.275, 0.443, 0.725, 0.463])
    last = text("f", "(f) Comparison data", [0.28, 0.471, 0.72, 0.491])
    body = text("body", "The next section continues.", [0.13, 0.52, 0.86, 0.6])
    figure = image("figure", [0.16, 0.10, 0.84, 0.40])
    pages = [[cap, follow, last, body, figure]]
    apply_layout(pages)
    assert [b["type"] for b in pages[0]] == ["image", "image_caption", "image_caption", "image_caption", "text"]


def test_geometry_cannot_reorder_an_unrelated_numbered_column():
    """局部修复只归位已证实倒置的图题，即使几何候选会打乱下方无关分栏也完整保留下方作者顺序。"""
    cap = text("cap", "Figure 12. Chart", [0.265, 0.415, 0.735, 0.435])
    body = text("body", "The chapter continues.", [0.13, 0.465, 0.86, 0.55])
    left = [
        text(f"l{i}", f"[{i}] Left reference", [0.10, y, 0.50, y + 0.02])
        for i, y in zip((30, 31, 32), (0.65, 0.70, 0.75), strict=True)
    ]
    right = [
        text(f"r{i}", f"[{i}] Right reference", [0.503, y, 0.90, y + 0.02])
        for i, y in zip((33, 34, 35), (0.65, 0.70, 0.75), strict=True)
    ]
    fig = image("figure", [0.16, 0.10, 0.84, 0.4])
    contents = [cap, body, *left, *right, fig]
    pages = [contents.copy()]
    apply_layout(pages)
    assert pages[0] == [fig, cap, body, *left, *right]
    assert cap["type"] == "image_caption"
    assert cap["_pdf_structure"]["inferred_visual_parent"] == "figure"


def test_only_adjacent_caption_gets_a_figure_if_second_number_is_below():
    """下方另一个宽图题不能夺走第一条紧邻图题的图体。"""
    first = text("lead", "Fig. 7. Near figure", [0.27, 0.411, 0.73, 0.433])
    second = text("second", "Fig. 8. Independent figure description", [0.26, 0.455, 0.74, 0.520])
    body = text("body", "A new section begins.", [0.13, 0.58, 0.86, 0.63])
    fig = image("figure", [0.16, 0.10, 0.84, 0.40])
    pages = [[first, second, body, fig]]
    apply_layout(pages)
    assert pages[0] == [fig, first, second, body]
    assert first["type"] == "image_caption"
    assert second["type"] == "text"


def _folio_pdf(increment):
    """生成孤立页码在结构树首位、真实图题贴图下方的三页原件。"""
    doc = TaggedPdfBuilder(pages=3)
    previous = doc.node("P", [doc.text("19", page=0, x=96, y=22)])
    middle = doc.node("P", [doc.text("20", page=1, x=96, y=22)])
    caption = doc.node("P", [doc.text("Fig. 7. Chart", page=1, x=74, y=215)])
    body = doc.node("P", [doc.text("The source paragraph follows this figure.", page=1, x=32, y=155)])
    figure = doc.node("Figure", [doc.figure(page=1, x=80, y=230)])
    following = doc.node("P", [doc.text(str(20 + increment), page=2, x=96, y=22)])
    return doc.finish([previous, middle, caption, body, figure, following])


@pytest.mark.parametrize("range_,expected", [("", 1), ("2", 0)])
def test_public_tagged_order_and_page_number_are_confirmed_across_original_pages(range_, expected):
    """整档与选页都返回图及其说明、正文、辅助页码，并保持默认导出阅读流。"""
    result = parse(_folio_pdf(1), parse_mode="txt", page_range=range_, keep_model_json=True)
    page = result.middle_json.pages[expected]
    assert page.page_idx == 1
    assert [b.type for b in page.blocks] == ["image", "text", "page_number"]
    assert [b.type for b in page.blocks[0].content] == ["image_body", "image_caption"]
    model = result.model_json.pages[expected]
    assert inline_span_plain_text(model[-1]["content"]) == "20"
    subset = result.middle_json.model_copy(update={"pages": [page]})
    default = render(subset, "html", options=HtmlRenderOptions(mode=RenderMode.DEFAULT)).content
    assert b'data-block-type="page_number"' not in default


def test_unconfirmed_numeric_edge_keeps_source_order():
    """相邻两页数字没有正确递增时不得误判边缘文字为页码或重排其位置，同时局部图文倒置仍可独立修正。"""
    result = parse(_folio_pdf(2), parse_mode="txt", keep_model_json=True)
    page = result.middle_json.pages[1]
    assert [b.type for b in page.blocks] == ["text", "image", "text"]
    model = result.model_json.pages[1]
    assert model[0]["type"] == "text" and inline_span_plain_text(model[0]["content"]) == "20"
    assert [b.type for b in page.blocks[1].content] == ["image_body", "image_caption"]


@pytest.mark.parametrize("control", ["valid", "wrong_folio", "side_column", "overlapping", "author_order", "front_confirmed"])
def test_front_folio_only_releases_a_clear_residual_paragraph(control):
    """只有得到跨页确认、来源内顺序稳定且同栏不重叠的错位正文才归位；已归位正文的确认前置页码自动移至页尾。"""
    edge = [0.46, 0.935, 0.50, 0.948]
    previous = text("previous", "80", edge)
    folio = text("folio", "81", edge)
    following = text("following", "86" if control == "wrong_folio" else "82", edge)
    heading = text("heading", "A section", [0.12, 0.10, 0.35, 0.12])
    heading["type"] = "paragraph_title"
    first = text("first", "Opening paragraph", [0.12, 0.13, 0.88, 0.28])
    second = text("second", "Second native paragraph", [0.12, 0.30, 0.88, 0.44])
    last = text("last", "Last native paragraph", [0.12, 0.75, 0.88, 0.86])
    residual = text("residual", "This is a long native paragraph with many words.", [0.12, 0.62, 0.88, 0.71])
    residual.pop("_pdf_structure")
    residual["lines"] = [{"bbox": [0.12, 0.62, 0.88, 0.66]}, {"bbox": [0.12, 0.67, 0.85, 0.71]}]
    note = text("note", "* Comment for this paragraph", [0.12, 0.875, 0.63, 0.89])
    note.pop("_pdf_structure")
    note["type"] = "page_footnote"
    blocks = [residual, note, folio, heading, first, second, last]
    if control == "side_column":
        residual["bbox"] = [0.55, 0.62, 0.95, 0.71]
    if control == "overlapping":
        last["bbox"] = [0.12, 0.70, 0.88, 0.86]
    if control == "author_order":
        blocks[4:6] = [second, first]
    if control == "front_confirmed":
        blocks = [folio, heading, first, second, residual, last, note]
    pages = [[previous], blocks.copy(), [following]]
    apply_layout(pages)
    if control in {"valid", "front_confirmed"}:
        assert pages[1] == [heading, first, second, residual, last, note, folio]
        assert folio["type"] == "page_number"
    else:
        assert pages[1] == blocks
        assert folio["type"] == "text"


def test_public_tagged_merge_releases_unclaimed_body_behind_front_folio():
    """真实标记 PDF 的两种来源各自保序，公开输出按原页插回独立残余段落。"""
    doc = TaggedPdfBuilder(pages=3)
    previous = doc.node("P", [doc.text("19", page=0, x=96, y=22)])
    folio = doc.node("P", [doc.text("20", page=1, x=96, y=22)])
    heading = doc.node("H2", [doc.text("A section", page=1, x=26, y=350)])
    first = doc.node("P", [doc.text("The reader sees this first printed paragraph.", page=1, x=26, y=312)])
    second = doc.node("P", [doc.text("The reader sees this second printed paragraph.", page=1, x=26, y=271)])
    doc.text("The early physics experiment compares the results", page=1, x=26, y=190)
    doc.text("and asks to compare the model with the handbook.", page=1, x=26, y=176)
    last = doc.node("P", [doc.text("The reader sees this final printed paragraph.", page=1, x=26, y=120)])
    following = doc.node("P", [doc.text("21", page=2, x=96, y=22)])
    result = parse(
        doc.finish([previous, folio, heading, first, second, last, following]), parse_mode="txt", keep_model_json=True
    )
    blocks = result.model_json.pages[1]
    labels = [inline_span_plain_text(block["content"]) for block in blocks]
    assert labels[0] == "A section"
    assert labels[3].startswith("The early physics experiment")
    assert labels[-2:] == ["The reader sees this final printed paragraph.", "20"]
    assert blocks[-1]["type"] == "page_number"


def test_ordered_merge_lis_prevents_trusted_and_pending_head_of_line_blocking():
    """前置大秩离群结构块与乱序残余队头均不得阻塞或提前冲刷几何位置正常的残余正文。"""
    from docvortex.analyzers.native.pdf.tagged import _ordered_merge

    outlier = text("t0", "Bottom callout", [0.10, 0.90, 0.90, 0.94])
    t1 = text("t1", "Top paragraph", [0.10, 0.10, 0.90, 0.22])
    t2 = text("t2", "Middle paragraph", [0.10, 0.30, 0.90, 0.45])
    t3 = text("t3", "Lower paragraph", [0.10, 0.72, 0.90, 0.82])
    f_late = {"type": "text", "content": "Residual near bottom", "bbox": [0.10, 0.83, 0.90, 0.88]}
    f_mid = {"type": "text", "content": "Residual in middle", "bbox": [0.10, 0.55, 0.90, 0.65]}
    merged = _ordered_merge([outlier, t1, t2, t3], [f_late, f_mid], (600, 800))
    assert merged == [outlier, t1, t2, f_mid, t3, f_late]


def test_sort_tagged_blocks_protects_marginals_visual_groups_and_body_median():
    """受保护排序将页眉置顶、页脚置底，保持已绑定图文组原子性，且不被窄小标签拉偏正文跨栏中位宽。"""
    from docvortex.analyzers.native.pdf.tagged_order import sort_tagged_blocks

    header = {"type": "header", "bbox": [0.10, 0.03, 0.90, 0.05]}
    footer = {"type": "footer", "bbox": [0.10, 0.95, 0.90, 0.97]}
    fig = image("fig1", [0.10, 0.20, 0.45, 0.42])
    cap = text("cap1", "Fig. 1. Atomic caption", [0.10, 0.43, 0.45, 0.46])
    cap["type"] = "image_caption"
    cap["_pdf_structure"]["visual_parent"] = "fig1"
    right_top = text("rt", "Right column top", [0.52, 0.20, 0.90, 0.35])
    right_mid = text("rm", "Right column mid", [0.52, 0.37, 0.90, 0.50])
    blocks = [footer, right_top, cap, fig, right_mid, header]
    order = sort_tagged_blocks(blocks, (600, 800))
    assert order[0] == 5  # header first
    assert order[-1] == 0  # footer last
    # 已绑定视觉父子组保持原子相邻，不被右侧栏块插队拆开
    pos_cap, pos_fig = order.index(2), order.index(3)
    assert abs(pos_cap - pos_fig) == 1

    # 双栏图文卡片页上，较短的单行居中标注不得把 root_median_width 拉低到小于栏内图宽，避免误判跨栏元素。
    cards = [
        image("l_img1", [0.10, 0.14, 0.44, 0.38]),
        text("l_t1", "3. Enter your", [0.17, 0.39, 0.37, 0.42]),
        text("l_t2", "address", [0.21, 0.46, 0.33, 0.49]),
        image("l_img2", [0.10, 0.51, 0.43, 0.76]),
        text("l_t3", "5. Create password", [0.16, 0.78, 0.38, 0.84]),
        image("r_img1", [0.57, 0.14, 0.90, 0.38]),
        text("r_t1", "4. Input your full name", [0.55, 0.39, 0.89, 0.42]),
        image("r_img2", [0.55, 0.51, 0.89, 0.76]),
    ]
    card_order = sort_tagged_blocks(cards, (600, 800))
    assert card_order[:5] == [0, 1, 2, 3, 4]
    assert card_order[5:] == [5, 6, 7]


def test_above_table_and_above_figure_numbered_captions_repair_locally():
    """物理位于表/图正上方的编号题注在逆序时局部归位，同时不影响无编号或已正序的块。"""
    cap_tbl = text("tcap", "Table 6. Part Number", [0.10, 0.15, 0.70, 0.18])
    tbl = {"_pdf_structure": {"node_id": "tbl6"}, "type": "table", "bbox": [0.10, 0.19, 0.90, 0.40], "content": "<table/>"}
    prose = text("p1", "Following narrative text.", [0.10, 0.45, 0.90, 0.52])
    pages = [[tbl, cap_tbl, prose]]
    apply_layout(pages)
    assert pages[0] == [cap_tbl, tbl, prose]

    # 已绑定 visual_parent 的表题物理位于表正上方时，即使上方存在更远的无关图片，也应翻回 [table_caption, table]。
    upper_img = image("fig_above", [0.119, 0.485, 0.881, 0.789])
    cap_bound = text("tcap2", "Table 2. Bound above table", [0.119, 0.822, 0.827, 0.846])
    cap_bound["type"] = "table_caption"
    cap_bound["_pdf_structure"]["visual_parent"] = "tbl2"
    tbl_bound = {
        "_pdf_structure": {"node_id": "tbl2"},
        "type": "table",
        "bbox": [0.128, 0.855, 0.625, 0.883],
        "content": "<table/>",
    }
    pages_bound = [[upper_img, tbl_bound, cap_bound]]
    apply_layout(pages_bound)
    assert pages_bound[0] == [upper_img, cap_bound, tbl_bound]

    # 图题在图正上方但中间被正文隔开时，图移到图题正下方并绑定。
    cap_fig = text("fcap", "Fig. 8. Variation of lost volume.", [0.15, 0.12, 0.85, 0.15])
    cap_fig["type"] = "caption"
    lower_prose = text("p2", "Discussion below the figure.", [0.10, 0.48, 0.90, 0.58])
    fig = image("fig8", [0.15, 0.16, 0.85, 0.42])
    pages2 = [[cap_fig, lower_prose, fig]]
    apply_layout(pages2)
    assert pages2[0] == [cap_fig, fig, lower_prose]
    assert cap_fig["type"] == "image_caption"
    assert cap_fig["_pdf_structure"]["inferred_visual_parent"] == "fig8"


def test_stacked_figures_resolve_closer_above_image_and_abstain_when_equidistant():
    """多图纵排页上，紧贴上方图且远离下方图的图题正确归位；与上下两图等距时保持保守弃权。"""
    cap = text("cap16", "Fig. 24.16. Finer grid.", [0.315, 0.330, 0.685, 0.341])
    prose = text("p_mid", "Next figure discussion below.", [0.168, 0.618, 0.837, 0.643])
    upper_fig = image("img_up", [0.266, 0.114, 0.734, 0.325])  # gap = 0.005
    lower_fig = image("img_lo", [0.330, 0.357, 0.693, 0.601])  # opp_gap = 0.016 (3.2x)
    pages = [[cap, prose, upper_fig, lower_fig]]
    apply_layout(pages)
    assert pages[0] == [upper_fig, cap, prose, lower_fig]
    assert cap["type"] == "image_caption"
    assert cap["_pdf_structure"]["inferred_visual_parent"] == "img_up"

    # 上下间隙相近（0.010 vs 0.012）时有真实归属歧义，必须放弃重排。
    cap_eq = text("cap_eq", "Fig. 24.16. Ambiguous.", [0.315, 0.335, 0.685, 0.346])
    prose_eq = text("p_eq", "Narrative below.", [0.168, 0.618, 0.837, 0.643])
    fig_a = image("img_a", [0.266, 0.114, 0.734, 0.325])  # gap = 0.010
    fig_b = image("img_b", [0.330, 0.358, 0.693, 0.601])  # opp_gap = 0.012
    pages_eq = [[cap_eq, prose_eq, fig_a, fig_b]]
    apply_layout(pages_eq)
    assert pages_eq[0] == [cap_eq, prose_eq, fig_a, fig_b]


def test_non_ua_severe_vertical_backtrack_falls_back_to_protected_geometry_while_ua_is_immune():
    """非 PDF/UA 页面出现显著同栏回跳时保留全部语义块并按受保护几何重排；声明 PDF/UA 的页面保持原序。"""
    title = text("t0", "Main Heading", [0.10, 0.10, 0.80, 0.15])
    title["type"] = "paragraph_title"
    bottom_a = text("t1", "Bottom section paragraph one.", [0.10, 0.72, 0.88, 0.78])
    bottom_b = text("t2", "Bottom section paragraph two.", [0.10, 0.80, 0.88, 0.86])
    upper_a = text("t3", "Upper section paragraph one.", [0.10, 0.24, 0.88, 0.32])
    upper_b = text("t4", "Upper section paragraph two.", [0.10, 0.36, 0.88, 0.44])
    scrambled = [title, bottom_a, bottom_b, upper_a, upper_b]

    non_ua = [scrambled.copy()]
    apply_layout(non_ua, declared_ua=None)
    assert non_ua[0] == [title, upper_a, upper_b, bottom_a, bottom_b]
    assert non_ua[0][0]["type"] == "paragraph_title"

    ua_pages = [scrambled.copy()]
    apply_layout(ua_pages, declared_ua="1")
    assert ua_pages[0] == scrambled


def test_non_ua_same_band_column_reentry_rescues_oscillation_without_harming_two_tier_sections():
    """非 PDF/UA 同带 R→L→R 水平栏折返可回退修复，而上下分带的两段式双栏布局不受误伤。"""
    mid = text("c2", "Middle card text", [0.40, 0.40, 0.62, 0.56])
    left = text("c1", "Left card text", [0.10, 0.40, 0.32, 0.56])
    right = text("c3", "Right card text", [0.70, 0.40, 0.92, 0.56])
    oscillating = [mid, left, right]
    pages = [oscillating.copy()]
    apply_layout(pages, declared_ua=None)
    assert pages[0] == [left, mid, right]

    # 上下两段双栏：Left-top -> Right-top -> Left-bottom，不构成同带水平折返，必须保持原序。
    lt = text("lt", "Item 10 left column", [0.08, 0.15, 0.46, 0.45])
    rt = text("rt", "Item 10 right column", [0.54, 0.15, 0.92, 0.45])
    lb = text("lb", "Item 11 next section", [0.08, 0.55, 0.46, 0.80])
    two_tier = [lt, rt, lb]
    pages_tier = [two_tier.copy()]
    apply_layout(pages_tier, declared_ua=None)
    assert pages_tier[0] == two_tier


def test_non_ua_order_fallback_guards_cross_column_overlap_footnotes_and_in_figure_labels():
    """跨栏续段重叠、页底引用点编号脚注和大图内嵌标注文字均不得触发整页回退。"""
    # 1. 跨栏续段（包围框覆盖左右栏并与其他正文重叠）
    left_p = text("p1", "Left column paragraph.", [0.08, 0.10, 0.46, 0.40])
    wrap_p = text("p_wrap", "Paragraph wrapping across both columns.", [0.08, 0.08, 0.92, 0.85])
    right_low = text("p2", "Lower right paragraph.", [0.54, 0.55, 0.92, 0.72])
    right_top = text("p3", "Upper right callout.", [0.54, 0.10, 0.92, 0.25])
    overlap_page = [left_p, wrap_p, right_low, right_top]
    pages_ov = [overlap_page.copy()]
    apply_layout(pages_ov)
    assert pages_ov[0] == overlap_page

    # 2. 页底编号脚注嵌入正文引用处
    p_a = text("pa", "First paragraph citing note 1.", [0.10, 0.12, 0.88, 0.24])
    fn = text("fn1", "1 Footnote explanation at page bottom.", [0.10, 0.84, 0.88, 0.90])
    p_b = text("pb", "Second paragraph immediately below first.", [0.10, 0.26, 0.88, 0.38])
    footnote_page = [p_a, fn, p_b]
    pages_fn = [footnote_page.copy()]
    apply_layout(pages_fn)
    assert pages_fn[0] == footnote_page

    # 3. 工程大图内部标注文字逆序排列，不得把下方正文章节重排打乱
    diagram = image("diag", [0.12, 0.10, 0.92, 0.38])
    lbl_low = text("l1", "Outer layer", [0.60, 0.33, 0.85, 0.35])
    lbl_high = text("l2", "Inner layer", [0.60, 0.12, 0.85, 0.14])
    sec = text("sec", "8 Process Design", [0.12, 0.42, 0.40, 0.45])
    sec["type"] = "paragraph_title"
    body = text("b1", "Normal specification body.", [0.12, 0.47, 0.90, 0.56])
    fig_page = [diagram, lbl_low, lbl_high, sec, body]
    pages_fig = [fig_page.copy()]
    apply_layout(pages_fig)
    assert pages_fig[0] == fig_page

    # 4. 页顶运行页眉条（y1 <= 0.10）内的左右乱序不得触发整页回退而打乱下方正文。
    h_left = text("hl", "Brand Guidelines", [0.04, 0.07, 0.14, 0.082])
    h_right = text("hr", "2025", [0.93, 0.07, 0.96, 0.082])
    h_mid = text("hm", "1.2", [0.37, 0.07, 0.39, 0.082])
    about = text("ab", "ABOUT", [0.57, 0.36, 0.64, 0.38])
    para = text("pa2", "Body text below heading.", [0.57, 0.42, 0.86, 0.70])
    hdr_page = [h_left, h_right, h_mid, about, para]
    pages_hdr = [hdr_page.copy()]
    apply_layout(pages_hdr)
    assert pages_hdr[0] == hdr_page


def test_sort_tagged_blocks_splits_narrow_positive_vertical_gutter():
    """受保护 Tagged XYCut++ 接纳小于 5 pt 的正向栏沟（>= 1 pt），不改变通用 sort_entries 默认阈值。"""
    from docvortex.analyzers.native._shared.xycut import sort_entries
    from docvortex.analyzers.native.pdf.tagged_order import sort_tagged_blocks

    # 600 pt 宽页上两栏间距仅 2.4 pt（0.496 -> 0.500），且右栏纵向错开使横向无贯通切口
    blocks = [
        text("l1", "Left 1", [0.10, 0.10, 0.496, 0.25]),
        text("r1", "Right 1", [0.500, 0.18, 0.90, 0.33]),
        text("l2", "Left 2", [0.10, 0.27, 0.496, 0.42]),
        text("r2", "Right 2", [0.500, 0.35, 0.90, 0.50]),
    ]
    assert sort_tagged_blocks(blocks, (600, 800)) == [0, 2, 1, 3]
    raw_entries = [
        {"bbox": (b["bbox"][0] * 600, b["bbox"][1] * 800, b["bbox"][2] * 600, b["bbox"][3] * 800), "i": i}
        for i, b in enumerate(blocks)
    ]
    assert [e["i"] for e in sort_entries(raw_entries)] == [0, 1, 2, 3]

    # 多子图并排（间隔 < 5 pt）时，中图在受保护几何序中位于左/右子图之间，仍应与下方居中编号图题正确配对。
    cap = text("mp_cap", "Fig. 18. Three panels.", [0.34, 0.513, 0.66, 0.524])
    body = text("mp_body", "Narrative text below panels.", [0.12, 0.55, 0.88, 0.62])
    img_l = image("mp_l", [0.121, 0.344, 0.359, 0.508])
    img_m = image("mp_m", [0.364, 0.344, 0.602, 0.508])
    img_r = image("mp_r", [0.607, 0.341, 0.845, 0.508])
    pages_mp = [[cap, body, img_l, img_m, img_r]]
    apply_layout(pages_mp)
    assert pages_mp[0][:2] == [img_m, cap]
    assert cap["type"] == "image_caption"
    assert cap["_pdf_structure"]["inferred_visual_parent"] == "mp_m"


def test_confirmed_edge_folios_normalize_with_peer_footer_and_record_repairs():
    """跨三页确认的尾部页码、同行页脚伴随的前置页码均规范标注 page_number 并产出结构化修复遥测。"""
    edge = [0.95, 0.96, 0.98, 0.985]
    p0 = [text("f0", "80", edge)]
    body = text("b1", "Main paragraph content", [0.10, 0.15, 0.88, 0.35])
    peer_footer = text("ft", "Disclaimer on same bottom row", [0.05, 0.962, 0.40, 0.988])
    f1 = text("f1", "81", edge)
    p2 = [text("f2", "82", edge)]

    # 场景 1：已位于尾部的确认底边页码原地标注 page_number（如 LIS 合并后的 2025.pdf p.81-90）
    repairs_tail: list[dict] = []
    pages_tail = [p0.copy(), [body, peer_footer, f1], p2.copy()]
    apply_layout(pages_tail, repairs=repairs_tail)
    assert pages_tail[1] == [body, peer_footer, f1]
    assert f1["type"] == "page_number"
    assert {"page_idx": 1, "action": "edge_folio_type"} in repairs_tail

    # 场景 2：前置底边页码存在同行伴随页脚时仍正常归位到页尾并记录 front_folio_relocate
    f1_front = text("f1b", "81", edge)
    repairs_front: list[dict] = []
    pages_front = [p0.copy(), [f1_front, body, peer_footer], p2.copy()]
    apply_layout(pages_front, repairs=repairs_front)
    assert pages_front[1] == [body, peer_footer, f1_front]
    assert f1_front["type"] == "page_number"
    assert {"page_idx": 1, "action": "front_folio_relocate"} in repairs_front
