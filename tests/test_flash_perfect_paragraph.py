"""核对被自定义 P 容器包住的 Tagged PDF 正文结构。"""

from bs4 import BeautifulSoup
import pytest
from pypdf.generic import ArrayObject, NameObject, NumberObject, TextStringObject

from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary
from docvortex import parse
from docvortex.analyzers.native import PdfModel
from docvortex.api import render
from docvortex.document.pdf import PDFDocument


def test_nested_tagged_paragraphs_keep_heading_list_and_text_boundaries():
    """外层 P 不能吞掉带清晰层级的段落、项目以及带直接字形的标题。"""
    builder = TaggedPdfBuilder(pages=2)
    builder.font.get_object()[NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")
    builder.root[NameObject("/RoleMap")] = pdf_dictionary(SectionHeading2=NameObject("/P"))
    # 标准 P 子对象独立画在第二页，作者用自定义结构角色把标题和其正文包装在一起。
    earlier = builder.node("P", [builder.text("Earlier content.", x=34, y=330)])
    intro = builder.text("The supported devices include the following:", x=34, y=340, page=1)
    attrs = pdf_dictionary(O=NameObject("/Layout"), StartIndent=NumberObject(43), TextIndent=NumberObject(34))
    bullets = [
        builder.node("P", [builder.text(text, x=34, y=y, page=1)], page=1, attributes=attrs)
        for text, y in [("BULLET One independent item.", 310), ("BULLET Another device.", 285)]
    ]
    builder.commands[1] = builder.commands[1].replace(b"(BULLET ", b"(\\225 ")
    intro_node = builder.node("P", [intro, *bullets], page=1)
    title = builder.text("3.2 Output geometry", x=12, y=245, page=1)
    body = builder.node("P", [builder.text("The next paragraph is distinct.", x=34, y=213, page=1)], page=1)
    section = builder.node("SectionHeading2", [title, body], page=1)
    source = builder.finish([builder.node("P", [earlier, intro_node, section])])

    with PDFDocument(source) as doc:
        model = PdfModel()
        native = model.predict(doc, collect_layout_diagnostics=True)
    assert [block["type"] for block in native[1]] == ["text", "list", "paragraph_title", "text"]
    assert model.layout_page_confidences == (1.0, 1.0)

    result = parse(source, parse_mode="txt", keep_model_json=True)
    html = BeautifulSoup(render(result.middle_json, "html", assets=result.assets).content, "html.parser")
    assert [node.get_text(" ", strip=True) for node in html.select("p")] == [
        "Earlier content.",
        "The supported devices include the following:",
        "The next paragraph is distinct.",
    ]
    assert [node.get_text(" ", strip=True) for node in html.select("h3")] == ["3.2 Output geometry"]
    assert [node.get_text(" ", strip=True).lstrip("• ") for node in html.select("ul li")] == [
        "One independent item.",
        "Another device.",
    ]


def test_plain_tagged_p_and_actualtext_do_not_inherit_heading_or_list_roles():
    """段中替代文字保持完整，普通句子里的标点不会用来推定标题、项目。"""
    builder = TaggedPdfBuilder()
    builder.root[NameObject("/RoleMap")] = pdf_dictionary(Heading2=NameObject("/P"))
    body = builder.node("Heading2", [builder.text("Spacing is part of ordinary prose.", x=32, y=338)])
    nested = builder.node("P", [builder.text("Hidden source detail.", x=32, y=282)])
    replaced = builder.node("P", [nested], actual="Source replacement remains intact.")
    result = parse(builder.finish([body, replaced]), parse_mode="txt", keep_model_json=True)
    assert [b["type"] for b in result.model_json.pages[0]] == ["text", "text"]
    html = BeautifulSoup(render(result.middle_json, "html", assets=result.assets).content, "html.parser")
    assert [p.get_text(" ", strip=True) for p in html.select("p")] == [
        "Spacing is part of ordinary prose.",
        "Source replacement remains intact.",
    ]


@pytest.mark.parametrize(("x", "y", "joined"), [(50, 341, True), (225, 341, False), (55, 310, False)])
def test_only_same_source_line_numeric_prefix_joins_its_paragraph(x, y, joined):
    """同一物理行内保留题号与题句，同行异栏和下行正文仍独立。"""
    builder = TaggedPdfBuilder()
    number = builder.node("P", [builder.text("111", x=28, y=341)])
    grouping = builder.node("Sect", [number])
    body = builder.text(" Choose a method.", x=x, y=y)
    source = builder.finish([builder.node("P", [grouping, body])])
    result = parse(source, parse_mode="txt")
    html = BeautifulSoup(render(result.middle_json, "html", assets=result.assets).content, "html.parser")
    expected = ["111Chooseamethod."] if joined else ["111", "Chooseamethod."]
    assert ["".join(p.get_text().split()) for p in html.select("p")] == expected


def test_tagged_link_stays_with_form_label_and_complete_anchor_text():
    """题干后相邻的同一链接片段保留同一目标和完整可见文字。"""
    builder = TaggedPdfBuilder()
    number = builder.node("P", [builder.text("111", x=28, y=341)])
    grouping = builder.node("Sect", [number])
    destination = "https://example.org/form"
    annotation = builder.writer._add_object(
        pdf_dictionary(
            Type=NameObject("/Annot"),
            Subtype=NameObject("/Link"),
            Rect=ArrayObject([NumberObject(v) for v in (55, 332, 171, 355)]),
            A=pdf_dictionary(S=NameObject("/URI"), URI=TextStringObject(destination)),
        )
    )
    builder.pages[0][NameObject("/Annots")] = ArrayObject([annotation])
    reference = pdf_dictionary(Type=NameObject("/OBJR"), Obj=annotation, Pg=builder.pages[0].indirect_reference)
    left = builder.node("P", [builder.text("Read the ", x=55, y=341)])
    right = builder.node("P", [builder.text("form guide", x=104, y=341)])
    link = builder.node("Link", [left, right, reference])
    result = parse(builder.finish([builder.node("P", [grouping, link])]), parse_mode="txt")
    html = BeautifulSoup(render(result.middle_json, "html", assets=result.assets).content, "html.parser")
    assert len(html.select("p")) == 1
    assert [(a["href"], "".join(a.get_text().split())) for a in html.select("a")] == [(destination, "Readtheformguide")]


@pytest.mark.parametrize(("after_x", "joined"), [(85, True), (194, False)])
def test_number_inside_a_sentence_needs_both_neighbors_on_its_line(after_x, joined):
    """括在同行文字与标点之间的标记仍在句中，与远处文字之间保持块边界。"""
    builder = TaggedPdfBuilder()
    before = builder.text("Beach at ", x=20, y=341)
    badge = builder.node("P", [builder.text("1", x=75, y=341)])
    after = builder.text(". Follow the signs.", x=after_x, y=341)
    result = parse(builder.finish([builder.node("P", [before, badge, after])]), parse_mode="txt")
    html = BeautifulSoup(render(result.middle_json, "html", assets=result.assets).content, "html.parser")
    paragraphs = ["".join(p.get_text().split()) for p in html.select("p")]
    assert paragraphs == (["Beachat1.Followthesigns."] if joined else ["Beachat", "1", ".Followthesigns."])
