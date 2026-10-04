from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from _docx_equationxml_test_utils import (
    M_NS,
    WORD_2003_NS,
    build_equationxml_docx,
    build_word_2003_equation_xml,
    build_word_2003_equation_xml_from_omml,
    build_word_2003_fraction_equation_xml,
)
from _mtef_test_utils import formula_corpus
from _native_test_utils import analyze_native_test_document
from _span_test_utils import equation
from _span_test_utils import inline as text_spans
from docx import Document
from lxml import etree  # type: ignore[reportAttributeAccessIssue]

import docvortex.analyzers.native.office.docx.equationxml as equationxml_module
from docvortex.analyzers.native import DocxModel
from docvortex.analyzers.native.office.docx.docx_converter import DocxConverter
from docvortex.analyzers.native.office.docx.equationxml import DocxEquationXmlDecoder
from docvortex.analyzers.native.office.errors import LegacyOfficeResourceLimitError
from docvortex.export.middle import export_middle_json
from docvortex.render._internal.docx.math import latex_to_omml
from docvortex.schema import BlockType


def _equation_contents(pages: list[list[dict]]) -> list[str]:
    """Extract the contents of LaTeX of the independent formula block in paginated order."""

    return [block["content"] for page in pages for block in page if block["type"] == BlockType.EQUATION]


def _invalid_equationxml_with_multiple_math() -> str:
    """Constructs a non-canonical Equation XML containing two ``m:oMath``."""

    root = etree.fromstring(build_word_2003_equation_xml("x").encode())
    math_paragraph = root.find(f".//{{{M_NS}}}oMathPara")
    assert math_paragraph is not None
    second = etree.SubElement(math_paragraph, f"{{{M_NS}}}oMath")
    run = etree.SubElement(second, f"{{{M_NS}}}r")
    etree.SubElement(run, f"{{{M_NS}}}t").text = "y"
    return etree.tostring(root, encoding="unicode")


def _doctype_equationxml() -> str:
    """Constructs Equation XML with external entity declaration, validation parser will not read the entity."""

    return (
        '<!DOCTYPE w:wordDocument [<!ENTITY probe SYSTEM "file:///etc/passwd">]>'
        f'<w:wordDocument xmlns:w="{WORD_2003_NS}" xmlns:m="{M_NS}">'
        "<w:body><w:p><m:oMathPara><m:oMath><m:r><m:t>&probe;</m:t>"
        "</m:r></m:oMath></m:oMathPara></w:p></w:body></w:wordDocument>"
    )


def _equationxml_with_forbidden_pict() -> str:
    """Constructing an unrecoverable Equation XML containing Word 2003 ``pict``."""

    root = etree.fromstring(build_word_2003_equation_xml("x").encode())
    run = root.find(f".//{{{M_NS}}}r")
    assert run is not None
    etree.SubElement(run, f"{{{WORD_2003_NS}}}pict")
    return etree.tostring(root, encoding="unicode")


def _fallback_shape_text_docx(text: str) -> bytes:
    """Construct DOCX that can only read text from shape-text fallback."""
    document = Document()
    document.add_paragraph("before")
    source_buffer = BytesIO()
    document.save(source_buffer)
    with ZipFile(BytesIO(source_buffer.getvalue())) as source:
        members = {name: source.read(name) for name in source.namelist()}

    word_namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    drawing_namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    root = etree.fromstring(members["word/document.xml"])
    body = root.find(f"{{{word_namespace}}}body")
    assert body is not None
    drawing = etree.Element(f"{{{word_namespace}}}drawing")
    text_body = etree.SubElement(drawing, f"{{{drawing_namespace}}}txBody")
    etree.SubElement(text_body, f"{{{drawing_namespace}}}bodyPr")
    etree.SubElement(text_body, f"{{{drawing_namespace}}}t").text = text
    body.insert(max(0, len(body) - 1), drawing)
    members["word/document.xml"] = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name, payload in members.items():
            target.writestr(name, payload)
    return output.getvalue()


def test_equationxml_decoder_converts_spec_document_and_fraction() -> None:
    """Validation Specification Word 2003 XML packaging reuses existing OMML converters."""

    decoder = DocxEquationXmlDecoder()

    assert decoder.decode(build_word_2003_equation_xml()) == "x+y"
    assert decoder.decode(build_word_2003_fraction_equation_xml()) == r"\frac{a}{b}"


def test_docx_fallback_shape_text_uses_inline_spans() -> None:
    """Verify non-standard shape text fallback outputs Span without interrupting the entire parsing."""
    pages = DocxModel().predict(BytesIO(_fallback_shape_text_docx("  shape text  ")))

    assert pages[0][-1] == {
        "type": BlockType.TEXT,
        "content": text_spans("shape text"),
    }


@pytest.mark.parametrize(
    ("source_latex", "expected_latex"),
    [
        (r"\sqrt{x}", r"\sqrt{x}"),
        (r"x^2", r"x^{2}"),
        (r"\int_0^1 x", r"\int_{0}^{1}x"),
        (r"\left(x\right)", r"\left(x\right)"),
        (
            r"\begin{matrix}a&b\\c&d\end{matrix}",
            r"\begin{matrix}a&b\\c&d\end{matrix}",
        ),
        ("α+β", r"\alpha +\beta"),
    ],
)
def test_equationxml_decoder_reuses_omml_formula_coverage(
    source_latex: str,
    expected_latex: str,
) -> None:
    """Verify radicals, scripts, integrals, delimiters, matrices, and Unicode inherited OMML capabilities."""

    equation = latex_to_omml(source_latex, display=False)
    equation_xml = build_word_2003_equation_xml_from_omml(equation)

    assert DocxEquationXmlDecoder().decode(equation_xml) == expected_latex


@pytest.mark.parametrize(
    "payload",
    [
        "<broken",
        "<m:oMath xmlns:m='http://schemas.openxmlformats.org/officeDocument/2006/math'/>",
        _invalid_equationxml_with_multiple_math(),
        _doctype_equationxml(),
        _equationxml_with_forbidden_pict(),
        "<!--comment-->" + build_word_2003_equation_xml("x"),
    ],
)
def test_equationxml_decoder_rejects_malformed_or_unsafe_documents(
    payload: str,
) -> None:
    """Validation of corrupted, bare OMML, multi-formula and entity document overall rollbacks."""

    assert DocxEquationXmlDecoder().decode(payload) is None


def test_equationxml_decoder_enforces_entry_total_and_cache_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that single attributes, cumulative resource limits, and the same payload cache are not billed repeatedly."""

    first = build_word_2003_equation_xml("x")
    second = build_word_2003_equation_xml("y")
    monkeypatch.setattr(equationxml_module, "MAX_ENTRY_BYTES", len(first.encode()) + 10)
    monkeypatch.setattr(equationxml_module, "MAX_ASSET_TOTAL_BYTES", len(first.encode()) + 1)
    decoder = DocxEquationXmlDecoder()

    assert decoder.decode(first) == "x"
    assert decoder.decode(first) == "x"
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_asset_total_bytes"):
        decoder.decode(second)

    monkeypatch.setattr(equationxml_module, "MAX_ENTRY_BYTES", 8)
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_entry_bytes"):
        DocxEquationXmlDecoder().decode(first)


def test_docx_equationxml_standalone_inline_table_and_textbox_flows() -> None:
    """Verify text-free, inline, table, and text box Equation XML output semantics."""

    equation_xml = build_word_2003_equation_xml()
    standalone = DocxModel().predict(BytesIO(build_equationxml_docx([equation_xml])))
    inline = DocxModel().predict(BytesIO(build_equationxml_docx([equation_xml], inline=True)))
    table = DocxModel().predict(BytesIO(build_equationxml_docx([equation_xml], table=True)))
    textbox = DocxModel().predict(BytesIO(build_equationxml_docx([equation_xml], textbox=True)))

    assert standalone == [[{"type": BlockType.EQUATION, "content": "x+y"}]]
    assert inline == [
        [
            {
                "type": BlockType.TEXT,
                "content": [*text_spans("before "), equation("x+y"), *text_spans(" after")],
            }
        ]
    ]
    assert table[0][0]["type"] == BlockType.TABLE
    assert "<eq>x+y</eq>" in table[0][0]["content"]
    assert "<img" not in table[0][0]["content"]
    assert textbox == [[{"type": BlockType.EQUATION, "content": "x+y"}]]


def test_docx_equationxml_title_list_header_and_footer_flows() -> None:
    """Verify that titles, lists, and headers and footers follow existing formula content reconstruction paths."""

    first = build_word_2003_equation_xml("x")
    second = build_word_2003_fraction_equation_xml()
    title = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [first],
                inline=True,
                paragraph_style="Title",
            )
        )
    )
    bullet = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [first],
                inline=True,
                paragraph_style="ListBullet",
            )
        )
    )
    header_footer = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [first, second],
                header_footer=True,
            )
        )
    )

    assert title[0][0] == {
        "type": BlockType.DOC_TITLE,
        "level": 1,
        "content": [*text_spans("before "), equation("x"), *text_spans(" after")],
    }
    assert bullet == [
        [
            {
                "type": BlockType.LIST,
                "attribute": "unordered",
                "content": [
                    {
                        "type": BlockType.TEXT,
                        "content": [*text_spans("before "), equation("x"), *text_spans(" after")],
                    }
                ],
                "ilevel": 0,
            }
        ]
    ]
    assert header_footer == [
        [
            {"type": BlockType.HEADER, "content": [equation("x")]},
            {
                "type": BlockType.FOOTER,
                "content": [equation(r"\frac{a}{b}")],
            },
        ]
    ]


def test_docx_equationxml_precedence_and_preview_fallback() -> None:
    """Verify prioritization of OMML, Equation XML, MTEF and preview images."""

    equation_xml = build_word_2003_equation_xml("x")
    _name, mtef, mtef_latex = formula_corpus()[1]
    native_omml = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [equation_xml],
                alternate_omml=True,
                keep_mtef=True,
                mtef_payloads=[mtef],
            )
        )
    )
    equationxml_over_mtef = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [equation_xml],
                keep_mtef=True,
                mtef_payloads=[mtef],
            )
        )
    )
    mtef_over_bad_equationxml = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                ["<broken"],
                keep_mtef=True,
                mtef_payloads=[mtef],
            )
        )
    )
    preview = DocxModel().predict(BytesIO(build_equationxml_docx(["<broken"])))

    assert native_omml == [[{"type": BlockType.EQUATION, "content": "z"}]]
    assert equationxml_over_mtef == [[{"type": BlockType.EQUATION, "content": "x"}]]
    assert mtef_over_bad_equationxml == [[{"type": BlockType.EQUATION, "content": mtef_latex}]]
    assert len(preview[0]) == 1
    assert preview[0][0]["type"] == BlockType.IMAGE


def test_docx_equationxml_same_payload_is_not_semantically_deduplicated() -> None:
    """Verification caching only reduces decoding costs and does not merge two independent formulas shape."""

    equation_xml = build_word_2003_equation_xml("x")
    pages = DocxModel().predict(BytesIO(build_equationxml_docx([equation_xml, equation_xml])))

    assert _equation_contents(pages) == ["x", "x"]


def test_docx_equationxml_shared_preview_is_suppressed_per_shape() -> None:
    """Verifying the shared image relationship only suppresses the shape belonging to the valid formula and does not delete the image globally."""

    pages = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [build_word_2003_equation_xml("x"), "<broken"],
                share_preview=True,
            )
        )
    )

    assert [block["type"] for block in pages[0]] == [
        BlockType.EQUATION,
        BlockType.IMAGE,
    ]
    assert pages[0][0]["content"] == "x"


def test_docx_equationxml_attribute_is_unescaped_exactly_once() -> None:
    """Verify that the outer attributes and inner XML entities are each decoded once, and no secondary expansion occurs."""

    pages = DocxModel().predict(BytesIO(build_equationxml_docx([build_word_2003_equation_xml("x&y")])))

    assert _equation_contents(pages) == [r"x\&y"]


def test_docx_equationxml_converter_reuse_resets_decoder_state() -> None:
    """Verify that page, cache, resource budget, and alarm status are all reset when converter is reused."""

    converter = DocxConverter()
    converter.convert(BytesIO(build_equationxml_docx([build_word_2003_equation_xml("x")])))
    assert _equation_contents(converter.pages) == ["x"]

    converter.convert(BytesIO(build_equationxml_docx([build_word_2003_equation_xml("y")])))
    assert _equation_contents(converter.pages) == ["y"]


def test_invalid_docx_equationxml_preview_exports_to_sidecar(
    tmp_path: Path,
) -> None:
    """Verify that no base64 or JSON remains after exporting the image of bad Equation XML."""

    middle, _model = analyze_native_test_document(build_equationxml_docx(["<broken"]), file_suffix="docx")

    result = export_middle_json(middle, tmp_path / "docx-equationxml")

    assert len(result.image_paths) == 1
    assert result.image_paths[0].stat().st_size > 0
    assert "base64," not in result.json_path.read_text(encoding="utf-8")
