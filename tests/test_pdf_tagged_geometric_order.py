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


def apply_layout(pages):
    """将完整物理页顺序送入实际的候选验证及 XY-Cut++。"""
    sources = [SimpleNamespace(size=(600, 800), page_index=i) for i in range(len(pages))]
    roles = {
        item["_pdf_structure"]["node_id"]: "P"
        for page in pages
        for item in page
        if item["type"] == "text" and item.get("_pdf_structure")
    }
    prefer_geometric_tagged_order(pages, sources, roles, SimpleNamespace(page_count=len(pages)))


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
    """即使独立图有逆序证据，几何候选改变其余列号次序时保留原序。"""
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
    assert pages[0] == contents and cap["type"] == "text"


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
    """相邻两页数字没有正确递增时不得误判边缘文字并重排。"""
    result = parse(_folio_pdf(2), parse_mode="txt")
    page = result.middle_json.pages[1]
    assert [b.type for b in page.blocks] == ["text", "text", "text", "image"]


@pytest.mark.parametrize("control", ["valid", "wrong_folio", "side_column", "overlapping", "author_order", "no_conflict"])
def test_front_folio_only_releases_a_clear_residual_paragraph(control):
    """只有得到跨页确认、来源内顺序稳定且同栏不重叠的错位正文才归位。"""
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
    if control == "no_conflict":
        blocks = [folio, heading, first, second, residual, last, note]
    pages = [[previous], blocks.copy(), [following]]
    apply_layout(pages)
    if control == "valid":
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
