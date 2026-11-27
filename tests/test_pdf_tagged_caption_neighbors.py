"""用真实标记 PDF 检查孤立图题的所有权、来源关系及引用句。"""

import pytest
from docvortex import parse
from _tagged_pdf_fixtures import TaggedPdfBuilder


@pytest.mark.parametrize("role,above,expected", [("P", False, True), ("Caption", True, True), ("P", True, False)])
def test_declared_caption_and_numbered_paragraph_have_distinct_direction_evidence(role, above, expected):
    """显式图题可在图上；普通段落在图上时保留原来的独立阅读身份。"""
    page = TaggedPdfBuilder()
    figure = page.node("Figure", [page.figure(x=70, y=150)])
    caption = page.node(role, [page.text("Fig. 2. Chart", x=62, y=195 if above else 134)])
    result = parse(page.finish([caption, figure]), keep_model_json=True)
    blocks = result.to_dict()["pages"][0]["blocks"]
    parent = next(block for block in blocks if block["type"] == "image")
    captions = [block for block in parent["content"] if block["type"] == "image_caption"]
    assert len(captions) == int(expected)
    assert len(blocks) == (1 if expected else 2)
    if expected:
        marker = result.model_json.pages[0][0]["_pdf_structure"]
        assert "visual_parent" not in marker
        extension = result.middle_json.extensions["docvortex_pdf_structure"]
        assert extension["nodes"][marker["node_id"]]["role"] == role
        assert len(extension["blocks"][marker["node_id"]][0]["block_path"]) == 2
        assert result.assets[parent["content"][0]["image_path"]]


@pytest.mark.parametrize("mode", ["reference", "intervening", "two_panels", "existing_caption"])
def test_ambiguous_tagged_caption_keeps_existing_owner(mode):
    """正文引用、中间正文、同排两图及已有显式图题均不得被新关系覆盖。"""
    page = TaggedPdfBuilder()
    roots = []
    children = [page.figure(x=70, y=150)]
    if mode == "existing_caption":
        children.append(page.node("Caption", [page.text("Printed title", y=220)]))
    roots.append(page.node("Figure", children))
    if mode == "two_panels":
        roots.append(page.node("Figure", [page.figure(x=122, y=150)]))
    if mode == "intervening":
        roots.append(page.node("P", [page.text("Body here", x=72, y=145)]))
    value = {"reference": "Fig. 2 shows the result.", "two_panels": "Fig. 2. Both panels together"}.get(mode, "Fig. 2. Chart")
    roots.append(page.node("P", [page.text(value, x=62, y=130)]))
    result = parse(page.finish(roots))
    assert any(b.type == "text" for b in result.middle_json.pages[0].blocks)
    assert sum(
        ch.type == "image_caption" for b in result.middle_json.pages[0].blocks if b.type == "image" for ch in b.content
    ) == int(mode == "existing_caption")


@pytest.mark.parametrize("mode", ["later_section", "panel_group"])
def test_inferred_caption_preserves_source_order_and_whole_figure_panels(mode):
    """小节后的沉底图和近邻等高子图均不能取得独立段落的局部图题所有权。"""
    pdf = TaggedPdfBuilder()
    centre = pdf.node("Figure", [pdf.figure(x=82, y=160)])
    caption = pdf.node("P", [pdf.text("Fig. 3. Set", x=81, y=145)])
    if mode == "later_section":
        section = pdf.node("H2", [pdf.text("Following section", x=40, y=100)])
        roots = [caption, section, centre]
    else:
        side = pdf.node("Figure", [pdf.figure(x=28, y=160)])
        roots = [side, centre, caption]
    result = parse(pdf.finish(roots))
    blocks = result.middle_json.pages[0].blocks
    assert [block.type for block in blocks] == (
        ["text", "paragraph_title", "image"] if mode == "later_section" else ["image", "image", "text"]
    )
