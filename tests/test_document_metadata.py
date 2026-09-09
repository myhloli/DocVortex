"""验证源属性提取、轻量边界、异常保留和跨阶段协议。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfWriter
from test_format_matrix import source_payload

import docvortex
from docvortex.document.pdf._document import PDFDocument
from docvortex.document.properties import property_date
from docvortex.errors import DocumentError
from docvortex.schema import FILE_SUFFIXES, DocumentMetadata, DocumentProperties, MiddleJson, Producer


def pdf_payload() -> bytes:
    """构造含源属性的两页 PDF，避免依赖原始文件路径。"""
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "源标题", "/Author": "Alice, Bob", "/CreationDate": "D:20240304050607+08'00'"})
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def ooxml_payload(suffix: str, *, bad_core: bool = False) -> bytes:
    """构造只含属性和目录的 OOXML，证明不必加载正文对象。"""
    stream = BytesIO()
    dc = 'xmlns:dc="http://purl.org/dc/elements/1.1/"'
    dcterms = 'xmlns:dcterms="http://purl.org/dc/terms/"'
    core = (
        f'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" {dc} {dcterms}>'
    )
    core += "<dc:title>中文标题</dc:title><dc:creator>Doe, Jane</dc:creator><dc:language>zh-CN</dc:language>"
    core += "<dc:identifier>urn:test:1</dc:identifier><dc:description>说明</dc:description>"
    core += "<dcterms:created>2024-03-04T05:06:07+08:00</dcterms:created></cp:coreProperties>"
    main = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}[suffix]
    bodies = {
        "docx": "<document/>",
        "pptx": '<presentation xmlns="http://schemas.openxmlformats.org/presentationml/2006/main"><sldIdLst><sldId/><sldId/></sldIdLst></presentation>',
        "xlsx": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="a"/><sheet name="b" state="hidden"/></sheets></workbook>',
    }
    with ZipFile(stream, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("docProps/core.xml", "<bad>" if bad_core else core)
        package.writestr(
            "docProps/app.xml",
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Source App</Application><Pages>7</Pages></Properties>',
        )
        package.writestr(main, bodies[suffix])
    return stream.getvalue()


@pytest.mark.parametrize("suffix", sorted(FILE_SUFFIXES))
def test_every_native_format_has_independent_metadata(suffix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """全部原生输入可轻量读取，不允许触发正文预测或 PDF 分类。"""
    from docvortex.analyzers.native import models

    def forbidden(*args: object, **kwargs: object) -> None:
        """任何正文分析或分类调用都说明突破了轻量接口边界。"""
        raise AssertionError("metadata must not analyze body or classify PDF")

    for name in (
        "PdfModel",
        "DocModel",
        "DocxModel",
        "PptModel",
        "PptxModel",
        "XlsModel",
        "XlsxModel",
        "RtfModel",
        "CsvModel",
        "HtmlModel",
        "EpubModel",
        "OfdModel",
        "OdtModel",
        "OdsModel",
        "OdpModel",
    ):
        monkeypatch.setattr(getattr(models, name), "predict", forbidden)
    monkeypatch.setattr(PDFDocument, "classify", forbidden)
    result = docvortex.extract_metadata(source_payload(suffix), file_suffix=suffix)
    assert result.metadata.file_suffix == suffix
    assert result.metadata.document is not None
    assert result.diagnostics == ()


@pytest.mark.parametrize("suffix", sorted(FILE_SUFFIXES))
def test_metadata_matches_full_analysis(suffix: str) -> None:
    """独立读取和原生分析使用相同源数据，后处理保留完整属性。"""
    source = source_payload(suffix)
    expected = docvortex.extract_metadata(source, file_suffix=suffix).metadata.document
    analysis = docvortex.analyze(source, file_suffix=suffix)
    assert analysis.model_json.metadata.document == expected
    result = docvortex.postprocess_document(analysis)
    assert result.middle_json.metadata.document == expected


@pytest.mark.parametrize("suffix,count,kind", [("docx", 7, "declared"), ("pptx", 2, "slide"), ("xlsx", 2, "sheet")])
def test_ooxml_fields_and_counts(suffix: str, count: int, kind: str) -> None:
    """多类型 Office 只读元数据并区分声明页数与目录项计数。"""
    props = docvortex.extract_metadata(ooxml_payload(suffix), file_suffix=suffix).metadata.document
    assert props is not None
    assert (props.title, props.authors, props.languages) == ("中文标题", ["Doe, Jane"], ["zh-CN"])
    assert props.identifiers == ["urn:test:1"]
    assert props.created_at == "2024-03-04T05:06:07+08:00"
    assert (props.page_count, props.page_count_kind) == (count, kind)


def test_bad_optional_xml_preserves_count_and_application() -> None:
    """坏的核心属性 XML 只生成诊断，不丢失其他属性与结构计数。"""
    result = docvortex.extract_metadata(ooxml_payload("pptx", bad_core=True), file_suffix="pptx")
    assert result.metadata.document.page_count == 2
    assert result.metadata.document.creator_application == "Source App"
    assert result.diagnostics[0].code == "read_metadata_failed"


def test_pdf_selection_ownership_and_bundle(tmp_path: Path) -> None:
    """选页保留整本属性，调用方句柄保持可用，结果包脱离源文件往返。"""
    with PDFDocument(pdf_payload()) as document:
        original = docvortex.extract_metadata(document).metadata.document
        result = docvortex.parse(document, page_range="2", keep_model_json=True)
        assert document.page_count == 2
        assert result.middle_json.metadata.document == original
        assert result.model_json.metadata.document == original
        assert original.created_at == "2024-03-04T05:06:07+08:00"
        assert original.authors == ["Alice, Bob"]
    bundle = tmp_path / "result.bundle"
    result.save_bundle(bundle)
    restored = docvortex.load_bundle(bundle)
    assert restored.middle_json.metadata.document == original
    structured = restored.middle_json.to_dict()
    assert MiddleJson.from_dict(structured).metadata.document == original


def test_static_html_properties_and_no_body_inference() -> None:
    """HTML 标准字段优先，模板和正文伪属性不参与提取。"""
    data = b'<html lang="en"><head><title>Standard</title><meta property="og:title" content="Other"><meta name="author" content="A"><meta name="author" content="B"><meta name="author" content="A"><template><meta name="author" content="Bad"></template></head><body><h1>Not metadata</h1><meta name="author" content="Bad2"></body></html>'
    props = docvortex.extract_metadata(data, file_suffix="html").metadata.document
    assert (props.title, props.authors, props.languages) == ("Standard", ["A", "B"], ["en"])


def test_rtf_explicit_time_count_and_application() -> None:
    """RTF 只提取 info 与 generator，保留分钟精度。"""
    data = rb"{\rtf1\ansi{\*\generator SourceApp;}{\info{\title Sample}{\author A}{\creatim\yr2024\mo3\dy4\hr5\min6}\nofpages12} Body}"
    props = docvortex.extract_metadata(data, file_suffix="rtf").metadata.document
    assert props.created_at == "2024-03-04T05:06"
    assert (props.page_count, props.page_count_kind) == (12, "declared")
    assert props.creator_application == "SourceApp"


@pytest.mark.parametrize(
    "value,expected",
    [("D:2024", "2024"), ("D:202402", "2024-02"), ("2024-03-04", "2024-03-04"), ("D:202413", None), ("yesterday", None)],
)
def test_dates_never_invent_missing_parts(value: str, expected: str | None) -> None:
    """非法日期保持未知，年和月精度不能补成任意日期。"""
    assert property_date(value) == expected


def test_old_metadata_remains_optional_and_new_fields_copy() -> None:
    """旧 2.0 属性不补造 document，多值字段独立复制且稳定去重。"""
    old = DocumentMetadata(file_suffix="pdf", producer=Producer(name="old", version="1"))
    assert "document" not in old.to_dict()
    old.document = DocumentProperties(authors=[" A ", "A", "B"], title="Test")
    clone = old.model_copy(deep=True)
    clone.document.authors.append("C")
    assert old.document.authors == ["A", "B"]


def test_unreadable_source_has_stable_error(tmp_path: Path) -> None:
    """缺失输入和损坏容器使用统一错误码而非空的伪成功结果。"""
    with pytest.raises(DocumentError, match="Cannot read") as exc:
        docvortex.extract_metadata(tmp_path / "missing.docx")
    assert exc.value.code == "open_failed"
    with pytest.raises(DocumentError) as exc:
        docvortex.extract_metadata(b"broken", file_suffix="docx")
    assert exc.value.code == "open_failed"


def replace_zip_part(data: bytes, part: str, value: bytes) -> bytes:
    """只替换夹具的指定属性 part，保留其他目录和正文。"""
    output = BytesIO()
    with ZipFile(BytesIO(data)) as original, ZipFile(output, "w", ZIP_DEFLATED) as changed:
        for name in original.namelist():
            changed.writestr(name, value if name == part else original.read(name))
    return output.getvalue()


def test_epub_extended_properties_and_multiple_authors() -> None:
    """OPF 多作者、语言、标识符与出版日期语义在唯一映射中保留。"""
    import lxml.etree as etree

    data = source_payload("epub")
    with ZipFile(BytesIO(data)) as package:
        part = next(name for name in package.namelist() if name.endswith(".opf"))
        root = etree.fromstring(package.read(part))
    metadata = next(element for element in root if element.tag.endswith("}metadata"))
    dc = "{http://purl.org/dc/elements/1.1/}"
    for tag, value in (
        ("creator", "Bob"),
        ("creator", "Alice"),
        ("language", "zh-CN"),
        ("publisher", "Publisher"),
        ("identifier", "urn:second"),
        ("description", "Description"),
    ):
        etree.SubElement(metadata, dc + tag).text = value
    date = etree.SubElement(metadata, dc + "date")
    date.set("{http://www.idpf.org/2007/opf}event", "creation")
    date.text = "2024-03"
    modified = etree.SubElement(metadata, "{http://www.idpf.org/2007/opf}meta", property="dcterms:modified")
    modified.text = "2024-03-04T05:06:07Z"
    props = docvortex.extract_metadata(replace_zip_part(data, part, etree.tostring(root)), file_suffix="epub").metadata.document
    assert props.authors == ["Alice", "Bob"]
    assert props.languages == ["zh-CN"]
    assert props.identifiers[-1] == "urn:second"
    assert props.publisher == "Publisher" and props.description == "Description"
    assert (props.created_at, props.modified_at) == ("2024-03", "2024-03-04T05:06:07Z")


def test_invalid_date_retains_other_office_properties() -> None:
    """非法日期记录诊断，但保留标题和其他可用属性。"""
    data = ooxml_payload("docx")
    with ZipFile(BytesIO(data)) as package:
        core = package.read("docProps/core.xml").replace(b"2024-03-04T05:06:07+08:00", b"not-a-date")
    result = docvortex.extract_metadata(replace_zip_part(data, "docProps/core.xml", core), file_suffix="docx")
    assert result.metadata.document.title == "中文标题"
    assert result.metadata.document.created_at is None
    assert any("Invalid declared date" in item.message for item in result.diagnostics)


def test_ofd_document_dates_identifiers_and_keyword_list() -> None:
    """OFD 多 DocBody 汇总页数，同时保留 DocInfo 的日期、标识符和关键词列表。"""
    import lxml.etree as etree

    data = source_payload("ofd")
    with ZipFile(BytesIO(data)) as package:
        root = etree.fromstring(package.read("OFD.xml"))
    info = next(element for element in root.iter() if element.tag.endswith("}DocInfo"))
    namespace = info.tag.rsplit("}", 1)[0] + "}"
    for name, value in (("DocID", "original-id"), ("CreationDate", "2024-03-04"), ("ModDate", "2024-04-05")):
        etree.SubElement(info, namespace + name).text = value
    keywords = etree.SubElement(info, namespace + "Keywords")
    for value in ("中文", "second", "中文"):
        etree.SubElement(keywords, namespace + "Keyword").text = value
    props = docvortex.extract_metadata(
        replace_zip_part(data, "OFD.xml", etree.tostring(root)), file_suffix="ofd"
    ).metadata.document
    assert props.page_count == 2 and props.page_count_kind == "physical"
    assert props.identifiers == ["original-id"]
    assert props.keywords == ["中文", "second"]
    assert (props.created_at, props.modified_at) == ("2024-03-04", "2024-04-05")


def test_bad_odf_metadata_does_not_block_body() -> None:
    """ODF 的可选 meta.xml 损坏不阻止正文转换，且保留诊断。"""
    data = replace_zip_part(source_payload("odt"), "meta.xml", b"<broken>")
    analysis = docvortex.analyze(data, file_suffix="odt")
    assert analysis.model_json.pages
    assert any(item.code == "read_metadata_failed" for item in analysis.diagnostics)
    assert analysis.model_json.metadata.document.title is None


def test_partial_ole_properties_survive_optional_stream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟后续属性流损坏，不能丢掉 olefile 已读出的标题和作者。"""
    import olefile

    def partially_read(document: olefile.OleFileIO) -> None:
        """底层库读取部分属性后遇到坏的可选流。"""
        document.metadata = olefile.OleMetadata()
        document.metadata.title = "Saved title"
        document.metadata.author = "Saved author"
        raise ValueError("broken optional property stream")

    monkeypatch.setattr(olefile.OleFileIO, "get_metadata", partially_read)
    result = docvortex.extract_metadata(source_payload("doc"), file_suffix="doc")
    assert result.metadata.document.title == "Saved title"
    assert result.metadata.document.authors == ["Saved author"]
    assert result.diagnostics[0].code == "read_metadata_failed"


@pytest.mark.parametrize("broken", [False, True])
def test_pdf_xmp_fills_missing_properties_without_replacing_info(broken: bool) -> None:
    """PDF Info 优先，XMP 补缺并保留月精度；坏 XMP 不丢有效 Info。"""
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "Info title"})
    xml = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:CreateDate="2024-03"><dc:title><rdf:Alt><rdf:li xml:lang="x-default">XMP title</rdf:li></rdf:Alt></dc:title><dc:creator><rdf:Seq><rdf:li>Alice</rdf:li><rdf:li>Bob</rdf:li></rdf:Seq></dc:creator><dc:subject><rdf:Bag><rdf:li>one</rdf:li><rdf:li>two</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>'
    stream = DecodedStreamObject()
    stream.set_data(b"<broken>" if broken else xml)
    stream[NameObject("/Type")] = NameObject("/Metadata")
    stream[NameObject("/Subtype")] = NameObject("/XML")
    writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    result = docvortex.extract_metadata(output.getvalue(), file_suffix="pdf")
    assert result.metadata.document.title == "Info title"
    assert result.metadata.document.page_count == 1
    if broken:
        assert result.diagnostics
    else:
        assert result.diagnostics == ()
        assert result.metadata.document.authors == ["Alice", "Bob"]
        assert result.metadata.document.keywords == ["one", "two"]
        assert result.metadata.document.created_at == "2024-03"
