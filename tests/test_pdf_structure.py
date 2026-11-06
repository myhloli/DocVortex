"""验证 Tagged PDF 从真实内容流到公共输出的结构优先解析。"""

from unittest.mock import patch
from pathlib import Path

import pytest
from pypdf.generic import ArrayObject, NameObject, NumberObject, TextStringObject

import docvortex
from docvortex.api import render
from docvortex.analyzers.native import PdfModel
from docvortex.content.spans import inline_span_plain_text
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.marked_content import read_marked_page
from docvortex.document.pdf.structure import read_pdf_structure
from docvortex.schema import MiddleJson, ModelJson

from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary, serialized


def block_text(block):
    """提取测试块的可见文字，兼容模型字典与严格 Middle 对象。"""
    content = block.get("content") if isinstance(block, dict) else block.content
    return (
        inline_span_plain_text(item if isinstance(item, dict) else item.model_dump() for item in content)
        if isinstance(content, list)
        else content
    )


def ordered_pdf(pages=1):
    """生成绘制和视觉位置均与语义顺序相反的标题、段落。"""
    builder = TaggedPdfBuilder(pages)
    roots = []
    for page in range(pages):
        body = builder.text(f"Body {page + 1}", page=page, y=350)
        title = builder.text(f"Heading {page + 1}", page=page, y=250)
        roots.extend([builder.node("H2", [title], page=page), builder.node("P", [body], page=page)])
    return builder.finish(roots)


def test_structure_order_headings_and_roundtrip():
    """结构树决定块顺序和标题级别，来源关联在序列化后仍可读取。"""
    result = docvortex.parse(ordered_pdf(), keep_model_json=True)
    blocks = result.middle_json.pages[0].blocks
    assert [block_text(block).strip() for block in blocks] == ["Heading 1", "Body 1"]
    assert blocks[0].type == "paragraph_title" and blocks[0].level == 3
    extension = result.middle_json.extensions["docvortex_pdf_structure"]
    assert any(node.get("heading_level") == 2 for node in extension["nodes"].values())
    assert extension["blocks"]
    assert MiddleJson.from_json(result.middle_json.to_json()).to_dict() == result.middle_json.to_dict()
    assert ModelJson.from_json(result.model_json.to_json()).to_dict() == result.model_json.to_dict()
    assert "_pdf_structure" not in str(result.middle_json.pages)
    assert "Heading 1" in render(result.middle_json, "markdown").content.decode()


@pytest.mark.parametrize("selection,expected", [("", [0, 1, 2]), ("2", [1]), ("1,3", [0, 2])])
@pytest.mark.parametrize("source_kind", ["bytes", "document", "path"])
def test_selection_uses_original_structure(selection, expected, source_kind, tmp_path):
    """抽页后的结构读取仍来自原件，三种输入保持原始页号和调用方所有权。"""
    data = ordered_pdf(3)
    with PDFDocument(data) as document:
        path = tmp_path / "tagged.pdf"
        path.write_bytes(data)
        source = {"bytes": data, "document": document, "path": path}[source_kind]
        result = docvortex.parse(source, page_range=selection, keep_model_json=True)
        assert [page.page_idx for page in result.middle_json.pages] == expected
        assert [block_text(page.blocks[0]).strip() for page in result.middle_json.pages] == [
            f"Heading {page + 1}" for page in expected
        ]
        assert document.page_count == 3


def test_selected_page_does_not_export_unselected_replacement_text():
    """选页扩展仅保留对应节点，跨页替代范围回退时也避免输出未选中的文字。"""
    builder = TaggedPdfBuilder(2)
    private = builder.node("P", [builder.text("First page")], actual="unselected private text")
    selected = builder.node("P", [builder.text("Selected content", page=1)], page=1)
    result = docvortex.parse(builder.finish([private, selected]), page_range="2", keep_model_json=True)
    assert "unselected private text" not in result.middle_json.to_json()
    assert "unselected private text" not in result.model_json.to_json()

    builder = TaggedPdfBuilder(2)
    references = [builder.text("First page"), builder.text("Selected content", page=1)]
    shared = builder.node("P", references, actual="replacement includes unselected private text")
    result = docvortex.parse(builder.finish([shared]), page_range="2", parse_mode="txt", keep_model_json=True)
    assert "unselected private text" not in result.middle_json.to_json()
    assert block_text(result.middle_json.pages[0].blocks[0]).strip() == "Selected content"


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("invisible", [False, True])
def test_marked_actualtext_and_hidden_text(named, invisible):
    """直接或命名属性的 ActualText 及隐藏 OCR 文字经原生路径恢复。"""
    builder = TaggedPdfBuilder()
    ref = builder.text("wrong", actual="correct", named=named, invisible=invisible)
    data = builder.finish([builder.node("P", [ref])])
    with patch.object(PDFDocument, "classify", side_effect=AssertionError("structure is sufficient")):
        result = docvortex.parse(data)
    assert block_text(result.middle_json.pages[0].blocks[0]).strip() == "correct"


def test_structure_actualtext_replaces_nested_content_once():
    """结构节点的替代文字覆盖多个嵌套片段，成员文字只认领一次。"""
    builder = TaggedPdfBuilder()
    a = builder.node("Span", [builder.text("first")])
    b = builder.node("Span", [builder.text("second", x=100)])
    data = builder.finish([builder.node("P", [a, b], actual="replacement")])
    result = docvortex.parse(data)
    assert block_text(result.middle_json.pages[0].blocks[0]) == "replacement"


@pytest.mark.parametrize("named", [False, True])
def test_actualtext_preserves_cjk_and_supplementary_unicode(named):
    """中文和代理对字符通过标记属性解码并完整进入公共文本。"""
    builder = TaggedPdfBuilder()
    reference = builder.text("placeholder", actual="结构解析🙂", named=named)
    result = docvortex.parse(builder.finish([builder.node("P", [reference])]))
    assert block_text(result.middle_json.pages[0].blocks[0]) == "结构解析🙂"


def test_repeated_nested_actualtext_keeps_distinct_mark_instances():
    """相同替代值的相邻标记分别输出，单个标记跨多个绘图对象仍只输出一次。"""
    builder = TaggedPdfBuilder()
    reference = builder.text("placeholder")
    actual = serialized(TextStringObject("🙂"))
    first = (
        b"/Span <</ActualText " + actual + b">> BDC BT /F1 12 Tf 20 350 Td (one) Tj ET BT /F1 12 Tf 50 350 Td (two) Tj ET EMC "
    )
    second = b"/Span <</ActualText " + actual + b">> BDC BT /F1 12 Tf 100 350 Td (three) Tj ET EMC "
    builder.commands[0] = bytearray(b"/P <</MCID 0>> BDC " + first + second + b"EMC")
    result = docvortex.parse(builder.finish([builder.node("P", [reference])]))
    assert block_text(result.middle_json.pages[0].blocks[0]).count("🙂") == 2


def test_outer_actualtext_replaces_nested_marked_content_once():
    """最外层替代范围覆盖内部替代范围，中文和补充平面字符完整保留。"""
    builder = TaggedPdfBuilder()
    reference = builder.text("placeholder")
    actual = serialized(TextStringObject("整体🙂"))
    builder.commands[0] = bytearray(
        b"/P <</MCID 0 /ActualText "
        + actual
        + b">> BDC /Span <</ActualText (inner)>> BDC BT /F1 12 Tf 20 350 Td (one) Tj ET EMC BT /F1 12 Tf 90 350 Td (two) Tj ET EMC"
    )
    result = docvortex.parse(builder.finish([builder.node("P", [reference])]))
    assert block_text(result.middle_json.pages[0].blocks[0]) == "整体🙂"


def test_structure_actualtext_retains_explicit_emphasis():
    """结构替代文字继续应用当前节点声明的强调语义。"""
    builder = TaggedPdfBuilder()
    strong = builder.node("Strong", [builder.text("source")], actual="replacement")
    result = docvortex.parse(builder.finish([builder.node("P", [strong])]))
    assert result.middle_json.pages[0].blocks[0].content[0].styles == ["bold"]


def test_form_scope_and_repeated_instances():
    """页面及 Form 复用 MCID 零号时保持作用域，重复调用保留独立实例。"""
    builder = TaggedPdfBuilder()
    page_ref = builder.text("Page text")
    form = builder.form()
    form_ref = builder.place_form(form)
    builder.place_form(form, y=100)
    data = builder.finish([builder.node("P", [form_ref]), builder.node("P", [page_ref])])
    with PDFDocument(data) as document:
        evidence = read_marked_page(document, 0)
        assert len({obj.occurrence for obj in evidence.objects if obj.chars and obj.stream != "page:0"}) == 2
        structure = read_pdf_structure(document)
        document.close()
        assert len(list(structure.nodes())) == 2
        assert evidence.geometry.chars
    result = docvortex.parse(data)
    assert block_text(result.middle_json.pages[0].blocks[0]).count("Form text") == 2
    assert block_text(result.middle_json.pages[0].blocks[1]).strip() == "Page text"


def test_partial_and_invalid_tags_keep_uncovered_text():
    """未标记正文和悬空引用回到 Flash，可信标题顺序与来源仍可查询。"""
    builder = TaggedPdfBuilder()
    title = builder.node("H1", [builder.text("Tagged heading")])
    builder.text("Uncovered body", y=200)
    broken = builder.text("Broken body", y=100)
    broken[NameObject("/MCID")] = NumberObject(99)
    data = builder.finish([title, builder.node("P", [broken])])
    result = docvortex.parse(data, parse_mode="txt")
    texts = [block_text(block).strip() for block in result.middle_json.pages[0].blocks]
    assert texts.count("Tagged heading") == 1
    assert "Uncovered body" in texts and "Broken body" in texts
    assert any(item.code == "pdf_structure_fallback" for item in result.diagnostics)


def test_separate_space_text_object_keeps_word_boundary():
    """PDFium 未返回对象归属的独立空格仍保留在相邻单词之间。"""
    builder = TaggedPdfBuilder()
    left = builder.text("bear", x=20)
    space = builder.text(" ", x=44)
    right = builder.text("this", x=48)
    result = docvortex.parse(builder.finish([builder.node("P", [left, space, right])]))
    assert block_text(result.middle_json.pages[0].blocks[0]) == "bear this"


def test_nested_list_survives_postprocessing():
    """标签列表直接保留条目和嵌套结构，通用列表整理消费已确认成员。"""
    builder = TaggedPdfBuilder()
    label = builder.node("Lbl", [builder.text("1.", x=20)])
    body = builder.node("LBody", [builder.node("P", [builder.text("First item", x=45)])])
    nested = builder.node(
        "L", [builder.node("LI", [builder.node("LBody", [builder.node("P", [builder.text("Nested item", y=300)])])])]
    )
    data = builder.finish([builder.node("L", [builder.node("LI", [label, body, nested])])])
    result = docvortex.parse(data)
    block = result.middle_json.pages[0].blocks[0]
    assert block.type == "list"
    assert block_text(block.content[0]).strip() == "1. First item"
    assert block.content[1].type == "list"
    assert block_text(block.content[1].content[0]).strip() == "Nested item"


def test_tagged_table_spans_and_formula():
    """标签表格支持无边框和跨列，单元格 MathML 转换为现有公式 HTML。"""
    builder = TaggedPdfBuilder()
    head = builder.node(
        "TH", [builder.text("Header")], attributes=pdf_dictionary(O=NameObject("/Table"), ColSpan=NumberObject(2))
    )
    a = builder.node("TD", [builder.text("A", y=300)])
    formula = builder.node(
        "Formula", [builder.text("x", y=300, x=100)], mathml=b"<math><msup><mi>x</mi><mn>2</mn></msup></math>"
    )
    b = builder.node("TD", [formula])
    data = builder.finish([builder.node("Table", [builder.node("TR", [head]), builder.node("TR", [a, b])])])
    result = docvortex.parse(data)
    table = result.middle_json.pages[0].blocks[0]
    assert table.type == "table"
    assert 'colspan="2"' in table.content[0].content
    assert "<eq>{x}^{2}</eq>" in table.content[0].content


def test_inconsistent_tagged_table_grid_releases_all_members():
    """缺少占位的标签表格整体回退，其原始文字交给规则解析且各出现一次。"""
    builder = TaggedPdfBuilder()
    top = builder.node("TR", [builder.node("TD", [builder.text("Alpha")])])
    bottom = builder.node(
        "TR", [builder.node("TD", [builder.text("Beta", y=280)]), builder.node("TD", [builder.text("Gamma", x=150, y=280)])]
    )
    result = docvortex.parse(builder.finish([builder.node("Table", [top, bottom])]), parse_mode="txt")
    text = render(result.middle_json, "markdown", assets=result.assets).content.decode()
    assert all(text.count(word) == 1 for word in ("Alpha", "Beta", "Gamma"))
    assert any("Incomplete table grid" in item.message for item in result.diagnostics)


def test_vector_figure_and_formula_crop():
    """纯矢量 Figure 与无数学编码的 Formula 均获得可独立导出的图片载荷。"""
    builder = TaggedPdfBuilder()
    data = builder.finish(
        [builder.node("Figure", [builder.figure()], alt="Blue square"), builder.node("Formula", [builder.figure(x=130)])]
    )
    result = docvortex.parse(data)
    figure, formula = result.middle_json.pages[0].blocks
    assert figure.type == "image" and figure.content[0].image_path
    assert "Blue square" in figure.content[0].content
    assert formula.type == "equation" and formula.image_path and formula.content == ""


def test_structure_off_and_invalid_parameters():
    """显式关闭选择几何路径，非法参数和非 PDF 的显式参数返回稳定错误。"""
    result = docvortex.parse(ordered_pdf(), pdf_structure="off", parse_mode="txt")
    assert "docvortex_pdf_structure" not in result.middle_json.extensions
    assert block_text(result.middle_json.pages[0].blocks[0]).strip() == "Body 1"
    for source, options in [
        (ordered_pdf(), {"pdf_structure": "wrong"}),
        (b"<p>text</p>", {"file_suffix": "html", "pdf_structure": "off"}),
    ]:
        with pytest.raises(ValueError) as captured:
            docvortex.parse(source, **options)
        assert captured.value.code == "pdf_structure_invalid"
    with PDFDocument(ordered_pdf()) as document:
        assert PdfModel().predict(document)[0][0]["level"] == 3


@pytest.mark.parametrize("mode", ["global", "namespace", "class"])
def test_role_mapping_namespaces_and_attribute_classes(mode):
    """自定义角色和 PDF 2.0 命名空间解析为现有标题，属性类保存在来源扩展。"""
    builder = TaggedPdfBuilder()
    node = builder.node("CustomHeading", [builder.text("Mapped heading")])
    if mode == "namespace":
        namespace = builder.writer._add_object(
            pdf_dictionary(NS=TextStringObject("urn:example:custom"), RoleMapNS=pdf_dictionary(CustomHeading=NameObject("/H3")))
        )
        node.get_object()[NameObject("/NS")] = namespace
    else:
        builder.root[NameObject("/RoleMap")] = pdf_dictionary(CustomHeading=NameObject("/H3"))
    if mode == "class":
        builder.root[NameObject("/ClassMap")] = pdf_dictionary(
            HeadingStyle=pdf_dictionary(O=NameObject("/Layout"), Placement=NameObject("/Block"))
        )
        node.get_object()[NameObject("/C")] = NameObject("/HeadingStyle")
    result = docvortex.parse(builder.finish([node]))
    assert result.middle_json.pages[0].blocks[0].level == 4
    entry = next(iter(result.middle_json.extensions["docvortex_pdf_structure"]["nodes"].values()))
    assert entry["raw_role"] == "CustomHeading"
    if mode == "class":
        assert entry["attributes"]["Layout"]["Placement"] == "/Block"


@pytest.mark.parametrize("defect", ["role-cycle", "tree-cycle", "parent-conflict"])
def test_invalid_structure_recovers_visible_text(defect):
    """角色环、结构环和父树冲突均回退正文，并提供受损节点诊断。"""
    builder = TaggedPdfBuilder()
    node = builder.node("P", [builder.text("Recover this text")])
    if defect == "role-cycle":
        builder.root[NameObject("/RoleMap")] = pdf_dictionary(P=NameObject("/Custom"), Custom=NameObject("/P"))
    elif defect == "tree-cycle":
        node.get_object()["/K"].append(node)
    else:
        builder.parents[0][0] = builder.node("Span")
    result = docvortex.parse(builder.finish([node]), parse_mode="txt")
    assert any("Recover this text" in block_text(block) for block in result.middle_json.pages[0].blocks)
    assert any(item.code.startswith("pdf_structure_") for item in result.diagnostics)


@pytest.mark.parametrize("selection,expected", [("", [False, True, True]), ("1,3", [False, False]), ("2-3", [False, True])])
def test_cross_page_paragraph_continuation_uses_source_identity(selection, expected):
    """同一结构段落跨连续原页建立续接，选页间断作为独立片段输出。"""
    builder = TaggedPdfBuilder(3)
    references = [builder.text(f"Paragraph part {page + 1}", page=page) for page in range(3)]
    result = docvortex.parse(builder.finish([builder.node("P", references)]), page_range=selection)
    assert [bool(page.blocks[0].continues_prev) for page in result.middle_json.pages] == expected


def test_declared_artifacts_are_excluded_from_body():
    """内容流 Artifact 与显式 Artifact 结构节点均按装饰成员处理。"""
    builder = TaggedPdfBuilder()
    body = builder.node("P", [builder.text("Only body")])
    builder.text("Watermark", y=100, artifact=True)
    artifact = builder.node("Artifact", [builder.text("Page furniture", y=50)])
    result = docvortex.parse(builder.finish([body, artifact]))
    assert [block_text(block).strip() for block in result.middle_json.pages[0].blocks] == ["Only body"]


def test_inline_math_and_graphic_fallback_keep_interleaved_order():
    """行内 MathML 保留公式 Span，纯图形公式在段落中的原位置形成裁图块。"""
    builder = TaggedPdfBuilder()
    first = builder.node("Span", [builder.text("Before ", x=20)])
    formula = builder.node("Formula", [builder.text("x", x=70)], mathml=b"<math><mi>x</mi></math>")
    graphic = builder.node("Formula", [builder.figure(x=100, y=330)])
    last = builder.node("Span", [builder.text("After", x=170)])
    result = docvortex.parse(builder.finish([builder.node("P", [first, formula, graphic, last])]))
    blocks = result.middle_json.pages[0].blocks
    assert [block.type for block in blocks] == ["text", "equation", "text"]
    assert any(span.type == "equation_inline" for span in blocks[0].content)
    assert blocks[1].image_path
    assert block_text(blocks[2]).strip() == "After"


def test_formula_unknown_mathml_uses_crop():
    """现有 MathML 转换器无法完整表达的算子保留公式裁图，防止静默降为普通子项。"""
    builder = TaggedPdfBuilder()
    formula = builder.node("Formula", [builder.figure()], mathml=b'<math><menclose notation="box"><mi>x</mi></menclose></math>')
    result = docvortex.parse(builder.finish([formula]))
    assert result.middle_json.pages[0].blocks[0].content == ""
    assert result.middle_json.pages[0].blocks[0].image_path


def test_mathml_structure_elements_read_their_marked_text():
    """MathML 命名空间结构中的叶子从 MCID 获取文字，并优先于替代描述形成公式。"""
    builder = TaggedPdfBuilder()
    x = builder.node("mi", [builder.text("x")])
    exponent = builder.node("mn", [builder.text("2", x=35, y=355)])
    math = builder.node("math", [builder.node("msup", [x, exponent])])
    formula = builder.node("Formula", [math], actual="x squared")
    result = docvortex.parse(builder.finish([builder.node("P", [formula])]))
    assert result.middle_json.pages[0].blocks[0].content[0].type == "equation_inline"
    assert result.middle_json.pages[0].blocks[0].content[0].content == "{x}^{2}"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_mathml_attachment_preserves_tex_annotation_without_delimiters(encoding):
    """MathML 附件按 XML 编码解析，源 TeX annotation 去除外层公式定界符。"""
    builder = TaggedPdfBuilder()
    xml = f'<?xml version="1.0" encoding="{encoding}"?><math><semantics><mi>x</mi><annotation encoding="application/x-tex">$x^2$</annotation></semantics></math>'
    formula = builder.node("Formula", [builder.text("x")], mathml=xml.encode(encoding))
    result = docvortex.parse(builder.finish([formula]))
    assert result.middle_json.pages[0].blocks[0].content == "x^2"


def test_marked_actualtext_replaces_vector_content():
    """没有字形的标记内容也可以通过 ActualText 直接产生原生正文。"""
    builder = TaggedPdfBuilder()
    reference = builder.figure()
    builder.commands[0] = bytearray(b"/P <</MCID 0 /ActualText (Vector lettering)>> BDC 0 0 0 rg 20 50 50 40 re f EMC")
    result = docvortex.parse(builder.finish([builder.node("P", [reference])]))
    assert block_text(result.middle_json.pages[0].blocks[0]) == "Vector lettering"


def test_caption_membership_and_list_visual_splitting():
    """列表图像独立输出，同时保留结构图注归属及后续列表片段。"""
    builder = TaggedPdfBuilder()
    first = builder.node("P", [builder.text("First item")])
    caption = builder.node("Caption", [builder.text("Figure caption", y=100)])
    figure = builder.node("Figure", [builder.figure(y=140), caption], alt="Description")
    last = builder.node("P", [builder.text("Following item", y=50)])
    body = builder.node("LBody", [first, figure, last])
    result = docvortex.parse(builder.finish([builder.node("L", [builder.node("LI", [body])])]))
    blocks = result.middle_json.pages[0].blocks
    assert [block.type for block in blocks] == ["list", "image", "list"]
    assert blocks[1].content[1].type == "image_caption"
    assert "Figure caption" in block_text(blocks[1].content[1])


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_rotated_cropped_page_preserves_structure(angle):
    """裁剪框原点和旋转通过现有视觉坐标转换，结构框保持有效且顺序固定。"""
    builder = TaggedPdfBuilder()
    builder.pages[0][NameObject("/Rotate")] = NumberObject(angle)
    builder.pages[0][NameObject("/CropBox")] = ArrayObject([NumberObject(v) for v in (10, 20, 290, 390)])
    a = builder.node("P", [builder.text("First", y=100)])
    b = builder.node("P", [builder.text("Second", y=300)])
    result = docvortex.parse(builder.finish([a, b]))
    assert [block_text(block).strip() for block in result.middle_json.pages[0].blocks] == ["First", "Second"]
    assert all(all(0 <= value <= 1 for value in block.bbox) for block in result.middle_json.pages[0].blocks)


def test_no_model_initialization_for_tagged_code():
    """结构解析和代码语言缺省均在阻止 ONNX 初始化时完成。"""
    builder = TaggedPdfBuilder()
    data = builder.finish([builder.node("Code", [builder.text("print(1)")])])
    with patch("docvortex.foundation.language._code_language_classifier", side_effect=AssertionError("model called")):
        result = docvortex.parse(data)
    assert result.middle_json.pages[0].blocks[0].guess_lang == "txt"


def test_cross_page_table_projects_rowspans_and_preserves_identity():
    """跨页行跨度在每页投影到有效网格，续表由同一结构节点建立。"""
    from bs4 import BeautifulSoup

    builder = TaggedPdfBuilder(2)
    first = builder.node(
        "TD", [builder.text("Spanning cell")], attributes=pdf_dictionary(O=NameObject("/Table"), RowSpan=NumberObject(2))
    )
    top = builder.node("TD", [builder.text("Top", x=160)])
    bottom = builder.node("TD", [builder.text("Bottom", page=1, x=160)], page=1)
    table = builder.node("Table", [builder.node("TR", [first, top]), builder.node("TR", [bottom], page=1)])
    result = docvortex.parse(builder.finish([table]))
    assert all(page.blocks[0].type == "table" for page in result.middle_json.pages)
    assert result.middle_json.pages[1].blocks[0].continues_prev is True
    for page in result.middle_json.pages:
        soup = BeautifulSoup(page.blocks[0].content[0].content, "html.parser")
        assert len(soup.find_all("tr")) == 1
        assert len(soup.find_all("td")) == 2
        assert all(cell["rowspan"] == "1" for cell in soup.find_all("td"))


def test_link_objr_and_explicit_span_styles():
    """OBJR 注释关联输出超链接，结构样式属性保留为现有行内样式。"""
    builder = TaggedPdfBuilder()
    annotation = builder.writer._add_object(
        pdf_dictionary(
            Type=NameObject("/Annot"),
            Subtype=NameObject("/Link"),
            Rect=ArrayObject([NumberObject(v) for v in (20, 340, 120, 365)]),
            A=pdf_dictionary(S=NameObject("/URI"), URI=TextStringObject("https://example.org/")),
        )
    )
    builder.pages[0][NameObject("/Annots")] = ArrayObject([annotation])
    reference = pdf_dictionary(Type=NameObject("/OBJR"), Obj=annotation, Pg=builder.pages[0].indirect_reference)
    styled = builder.node(
        "Span",
        [builder.text("Website")],
        attributes=pdf_dictionary(O=NameObject("/Layout"), TextDecorationType=NameObject("/Underline")),
    )
    link = builder.node("Link", [styled, reference])
    result = docvortex.parse(builder.finish([builder.node("P", [link])]))
    span = result.middle_json.pages[0].blocks[0].content[0]
    assert span.type == "hyperlink" and span.url == "https://example.org/"
    assert span.content[0].styles == ["underline"]


def test_caption_reference_and_table_of_contents():
    """独立 Caption 的 Ref 绑定图片，目录条目保留现有 index 容器。"""
    builder = TaggedPdfBuilder()
    figure = builder.node("Figure", [builder.figure()], alt="A square")
    caption = builder.node("Caption", [builder.text("Referenced caption", y=110)])
    caption.get_object()[NameObject("/Ref")] = ArrayObject([figure])
    toc = builder.node("TOC", [builder.node("TOCI", [builder.text("Chapter one 1")])])
    result = docvortex.parse(builder.finish([toc, figure, caption]))
    toc_block, image = result.middle_json.pages[0].blocks
    assert toc_block.type == "index" and block_text(toc_block.content[0]) == "Chapter one 1"
    assert image.content[1].type == "image_caption"


def test_multiple_text_objects_apply_marked_actualtext_once():
    """同一 BDC 替代范围包含多个文本对象时只输出一次替代值。"""
    builder = TaggedPdfBuilder()
    ref = builder.text("one", actual="replacement")
    builder.commands[0] = bytearray(
        b"/P <</MCID 0 /ActualText (replacement)>> BDC BT /F1 12 Tf 20 350 Td (one) Tj ET BT /F1 12 Tf 80 350 Td (two) Tj ET EMC"
    )
    result = docvortex.parse(builder.finish([builder.node("P", [ref])]))
    assert block_text(result.middle_json.pages[0].blocks[0]) == "replacement"


def test_reading_order_conflict_is_checked_on_original_before_selection():
    """缺乏标题语义且正文跨多页回挂的标签树回退，选页使用同一裁决。"""
    builder = TaggedPdfBuilder(3)
    roots = [builder.node("P", [builder.text(f"Body {index}", page=index)], page=index) for index in range(3)]
    extra = builder.node("P", [builder.text("Late front matter", y=300)], page=0)
    data = builder.finish([*roots, extra])
    for selection in ("", "1"):
        result = docvortex.parse(data, parse_mode="txt", page_range=selection, keep_model_json=True)
        baseline = docvortex.parse(data, parse_mode="txt", page_range=selection, keep_model_json=True, pdf_structure="off")
        assert result.model_json.pages == baseline.model_json.pages
        assert result.middle_json.extensions["docvortex_pdf_structure"]["used"] is False
        assert any(item.code == "pdf_structure_order_conflict" for item in result.diagnostics)


def test_structure_analysis_cleanup_on_success_and_failure():
    """模型释放临时原件和快照引用，读取异常也按输入所有权关闭引擎文档。"""
    from docvortex import api

    model = PdfModel()
    with PDFDocument(ordered_pdf()) as document:
        model.predict(document)
        assert model._prepared_document is None and model._tagged is None
    captured = []
    prepare = api.prepare_source

    def track(*args, **kwargs):
        """保留真实准备结果以核对异常退出时的两个文档生命周期。"""
        prepared = prepare(*args, **kwargs)
        captured.append(prepared)
        return prepared

    with (
        patch.object(api, "prepare_source", track),
        patch("docvortex.analyzers.native.pdf.tagged.read_marked_page", side_effect=RuntimeError("injected native failure")),
    ):
        with pytest.raises(RuntimeError, match="injected native failure"):
            docvortex.parse(ordered_pdf(2), page_range="2")
    assert captured[0].document._pdf_doc_opened is None
    assert captured[0].structure_document._pdf_doc_opened is None


def test_cli_and_serialized_analysis_use_same_structure_path(tmp_path):
    """CLI 开关与序列化 ModelJson 的后处理保持相同的标题和来源语义。"""
    from click.testing import CliRunner
    from docvortex.cli import main
    from docvortex.api import analyze, postprocess

    source = tmp_path / "source.pdf"
    source.write_bytes(ordered_pdf())
    output = tmp_path / "result.md"
    result = CliRunner().invoke(main, ["convert", str(source), "-o", str(output), "--pdf-structure", "auto"])
    assert result.exit_code == 0, result.output
    assert output.read_text().startswith("### Heading 1")
    analysis = analyze(source)
    restored = postprocess(ModelJson.from_json(analysis.model_json.to_json()))
    assert restored.middle_json.extensions["docvortex_pdf_structure"]["blocks"]
    assert restored.middle_json.pages[0].blocks[0].level == 3


@pytest.mark.parametrize("name", ["invoice", "academic_abstract"])
def test_reference_suite_documents_use_structure_and_export(name, tmp_path):
    """真实 PDF/UA 原件走结构路径，正文与表格可从保存包恢复并导出。"""
    from docvortex.export.bundle import load_bundle

    source = Path(__file__).parent / "fixtures/pdf_structure" / f"{name}.pdf"
    result = docvortex.parse(source, keep_model_json=True)
    assert result.middle_json.extensions["docvortex_pdf_structure"]["used"] is True
    if name == "invoice":
        tables = [block for page in result.middle_json.pages for block in page.blocks if block.type == "table"]
        assert len(tables) == 1
        assert "845.00" in tables[0].content[0].content
    else:
        rendered = render(result.middle_json, "markdown", assets=result.assets).content.decode()
        assert "https://doi.org/10.1145/3342558.3351873" in rendered
        assert "https:\n" not in rendered
    bundle = result.save_bundle(tmp_path / name)
    restored = load_bundle(bundle.path.parent)
    assert restored.middle_json.to_dict() == result.middle_json.to_dict()
    for target in ("markdown", "html", "structured_content", "pdf", "docx", "epub", "latex"):
        artifact = render(restored.middle_json, target, assets=restored.assets)
        assert artifact.content


def test_native_marked_content_rejects_bad_abi_addresses():
    """新批量桥接在任何解引用前拒绝空地址及错误接口数量。"""
    from docvortex._compute_backend import get_native

    native = get_native()
    for addresses, objects in (([], [1]), ([0] * 6, [1]), ([1] * 6, [0])):
        with pytest.raises(ValueError):
            native.read_pdfium_marked_content(addresses, objects)


def test_vector_figure_excludes_overlapping_foreign_text():
    """矢量图包围框内的外部正文保持独立，图形成员渲染后恢复页面活动状态。"""
    from io import BytesIO
    from PIL import Image
    import numpy as np

    builder = TaggedPdfBuilder()
    figure = builder.node("Figure", [builder.figure()])
    text = builder.node("P", [builder.text("FOREIGN", x=25, y=65)])
    data = builder.finish([figure, text])
    with PDFDocument(data) as document:
        before = document.render_image(0, image_format="png").data
        result = docvortex.parse(document)
        after = document.render_image(0, image_format="png").data
        assert before == after
    image_block = result.middle_json.pages[0].blocks[0]
    with Image.open(BytesIO(result.assets[image_block.content[0].image_path])) as image:
        with image.convert("RGB") as rgb:
            assert not np.any(np.max(np.asarray(rgb), axis=2) < 50)
    assert block_text(result.middle_json.pages[0].blocks[1]) == "FOREIGN"


def test_generic_heading_uses_section_depth():
    """通用 H 按 Sect 层数映射章节级别，并把源级别记录到扩展中。"""
    builder = TaggedPdfBuilder()
    first = builder.node("H", [builder.text("First level")])
    second = builder.node("H", [builder.text("Second level", y=250)])
    section = builder.node("Sect", [first, builder.node("Sect", [second])])
    result = docvortex.parse(builder.finish([section]))
    assert [block.level for block in result.middle_json.pages[0].blocks] == [2, 3]


def test_cross_page_list_marks_only_the_top_level_container():
    """同一列表项跨页时在顶层列表建立续接，嵌套文字遵守公共 schema 约束。"""
    builder = TaggedPdfBuilder(2)
    paragraph = builder.node("P", [builder.text("First part"), builder.text("Second part", page=1)])
    root = builder.node("L", [builder.node("LI", [builder.node("LBody", [paragraph])])])
    result = docvortex.parse(builder.finish([root]))
    assert result.middle_json.pages[1].blocks[0].continues_prev is True
    assert result.middle_json.pages[1].blocks[0].content[0].continues_prev is None


def test_isolated_graphic_restores_objects_after_render_failure():
    """图形渲染中途失败时仍恢复所有已临时隐藏的页面对象。"""
    from docvortex.document.pdf.marked_content import render_marked_visuals
    from docvortex.analyzers.native.pdf.tagged import prepare_tagged_analysis

    builder = TaggedPdfBuilder()
    figure = builder.node("Figure", [builder.figure()])
    paragraph = builder.node("P", [builder.text("Visible body")])
    with PDFDocument(builder.finish([figure, paragraph])) as document:
        evidence = prepare_tagged_analysis(document)
        before = document.render_image(0, image_format="png").data
        with patch("docvortex.document.pdf.raster.page_to_image", side_effect=RuntimeError("render failure")):
            with pytest.raises(RuntimeError, match="render failure"):
                render_marked_visuals(document, 0, evidence.blocks[0])
        assert document.render_image(0, image_format="png").data == before


def test_graphic_isolation_retains_artifact_background():
    """图形裁图保留装饰背景的对比度，防止白色矢量成员在白底上消失。"""
    from io import BytesIO
    import numpy as np
    from PIL import Image

    builder = TaggedPdfBuilder()
    reference = builder.figure()
    builder.commands[0] = bytearray(
        b"/Artifact BMC 0 0 0 rg 0 0 300 400 re f EMC /Figure <</MCID 0>> BDC 1 1 1 rg 20 50 10 40 re f 60 50 10 40 re f EMC"
    )
    result = docvortex.parse(builder.finish([builder.node("Figure", [reference])]))
    path = result.middle_json.pages[0].blocks[0].content[0].image_path
    with Image.open(BytesIO(result.assets[path])) as image:
        pixels = np.asarray(image)
        assert np.any(pixels < 20) and np.any(pixels > 235)
