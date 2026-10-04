from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from _docx_equationxml_test_utils import (
    build_equationxml_docx,
    build_word_2003_equation_xml,
)
from _image_mtef_test_utils import (
    apps_mfcc_comments,
    baseline_wmf_comment,
    build_baseline_only_gif,
    build_gif_with_mtef,
    build_wmf,
)
from _legacy_ppt_test_utils import build_equation_ppt
from _legacy_xls_test_utils import build_equation_xls, label_cell
from _mtef_test_utils import build_equation_doc, formula_corpus
from _mtef_v5_test_utils import v5_formula_corpus
from _native_test_utils import analyze_native_test_document
from _office_image_mtef_test_utils import (
    build_image_docx,
    build_image_pptx,
    build_image_xlsx,
)
from _ooxml_mtef_test_utils import (
    build_equation_docx,
    build_equation_pptx,
    build_equation_xlsx,
)
from _span_test_utils import equation, inline_text
from _span_test_utils import inline as text_spans

from docvortex.analyzers.native import DocModel, DocxModel, PptModel, PptxModel, XlsModel, XlsxModel
from docvortex.analyzers.native.office.doc.doc_converter import DocConverter
from docvortex.analyzers.native.office.doc.models import (
    DocImage,
    DocImagePayload,
    DocParagraph,
    DocTable,
    DocTableCell,
    DocTableRow,
)
from docvortex.analyzers.native.office.docx.docx_converter import DocxConverter
from docvortex.analyzers.native.office.equation import image as image_equation_module
from docvortex.analyzers.native.office.image import serialize_office_image
from docvortex.analyzers.native.office.pptx.pptx_converter import PptxConverter
from docvortex.analyzers.native.office.xlsx.xlsx_converter import XlsxConverter
from docvortex.export.middle import export_middle_json
from docvortex.schema import BlockType


def _equation_contents(pages: list[list[dict]]) -> list[str]:
    """Collects stand-alone equation block content in paginated order."""

    return [block["content"] for page in pages for block in page if block.get("type") == BlockType.EQUATION]


def _has_image(pages: list[list[dict]]) -> bool:
    """Determine whether model-list retains at least one picture block."""

    return any(block.get("type") == BlockType.IMAGE for page in pages for block in page)


def _wmf_formula(mtef: bytes) -> bytes:
    """Wrap MTEF across chunk AppsMFCC WMF."""

    return build_wmf(
        apps_mfcc_comments(
            mtef,
            chunk_size=7,
        ),
        placeable=True,
    )


def test_legacy_doc_ppt_xls_recover_wmf_comment_after_bad_native() -> None:
    """Recover formulas with same preview WMF comment after failure to validate old three formats Native."""

    _name, mtef, expected = v5_formula_corpus()[1]
    preview = _wmf_formula(mtef)
    invalid = b"invalid native"
    cases = [
        (
            DocModel(),
            build_equation_doc(
                [(1, invalid)],
                preview_storage_ids={1},
                preview_payloads={1: preview},
            ),
        ),
        (
            PptModel(),
            build_equation_ppt(
                [invalid],
                preview_payload=preview,
            ),
        ),
        (
            XlsModel(),
            build_equation_xls(
                [(1, invalid)],
                preview_payload=preview,
            ),
        ),
    ]

    for model, file_bytes in cases:
        pages = model.predict(BytesIO(file_bytes))
        assert _equation_contents(pages) == [expected]
        assert not _has_image(pages)


def test_legacy_doc_direct_gif_comment_recovers_after_bad_native() -> None:
    """Verify DOC PICF magic fallback Preserves and parses the original GIF/001."""

    _name, mtef, expected = v5_formula_corpus()[0]
    image = build_gif_with_mtef(mtef, chunk_size=3)
    pages = DocModel().predict(
        BytesIO(
            build_equation_doc(
                [(1, b"invalid native")],
                preview_storage_ids={1},
                preview_payloads={1: image},
            )
        )
    )

    assert _equation_contents(pages) == [expected]
    assert not _has_image(pages)


def test_legacy_native_mtef_precedes_conflicting_wmf_comment() -> None:
    """Validate that the old three formats Native take precedence over WMF with different content comment."""

    _native_name, native, expected = formula_corpus()[0]
    _image_name, image_mtef, _image_expected = v5_formula_corpus()[1]
    preview = _wmf_formula(image_mtef)
    cases = [
        (
            DocModel(),
            build_equation_doc(
                [(1, native)],
                preview_storage_ids={1},
                preview_payloads={1: preview},
            ),
        ),
        (
            PptModel(),
            build_equation_ppt([native], preview_payload=preview),
        ),
        (
            XlsModel(),
            build_equation_xls([(1, native)], preview_payload=preview),
        ),
    ]

    for model, file_bytes in cases:
        assert _equation_contents(model.predict(BytesIO(file_bytes))) == [expected]


def test_xls_wmf_comment_equation_enters_table_cell() -> None:
    """Verify that the XLS picture comment formula enters cell HTML and does not repeat the output."""

    _name, mtef, expected = v5_formula_corpus()[0]
    pages = XlsModel().predict(
        BytesIO(
            build_equation_xls(
                [(1, b"invalid native")],
                preview_payload=_wmf_formula(mtef),
                cell_records=label_cell(0, 0, "value"),
            )
        )
    )

    assert _equation_contents(pages) == []
    assert len(pages[0]) == 1
    assert pages[0][0]["type"] == BlockType.TABLE
    assert f"<eq>{expected}</eq>" in pages[0][0]["content"]


def test_doc_image_comment_equation_enters_nested_table_html() -> None:
    """Verify that DOC paragraph pictures and independent pictures are written as eq in the nested table."""

    payload = DocImagePayload(
        data=b"",
        extension="wmf",
        content_type="image/wmf",
        equation_latex="x+y",
    )
    paragraph = DocParagraph(
        cp_start=0,
        cp_end=1,
        images=[payload],
    )
    nested = DocTable(
        cp_start=1,
        cp_end=2,
        rows=[DocTableRow(cells=[DocTableCell(blocks=[DocImage(cp=1, payload=payload)])])],
    )
    table = DocTable(
        cp_start=0,
        cp_end=2,
        rows=[DocTableRow(cells=[DocTableCell(blocks=[paragraph, nested])])],
    )

    html = DocConverter._table_html(table)

    assert html.count("<eq>x+y</eq>") == 2
    assert "<img" not in html


def test_docx_picture_comment_flows_inline_table_header_and_standalone() -> None:
    """Verification DOCX Common WMF/GIF picture enters the unified formula token to rebuild the link."""

    first = v5_formula_corpus()[0]
    second = v5_formula_corpus()[1]
    wmf = _wmf_formula(first[1])
    gif = build_gif_with_mtef(second[1], chunk_size=5)
    standalone = DocxModel().predict(BytesIO(build_image_docx(wmf)))
    inline = DocxModel().predict(BytesIO(build_image_docx(gif, inline=True)))
    table = DocxModel().predict(BytesIO(build_image_docx(gif, table=True)))
    header = DocxModel().predict(BytesIO(build_image_docx(gif, header=True)))

    assert standalone == [[{"type": BlockType.EQUATION, "content": first[2]}]]
    assert inline == [
        [
            {
                "type": BlockType.TEXT,
                "content": [*text_spans("before "), equation(second[2]), *text_spans(" after")],
            }
        ]
    ]
    assert table[0][0]["type"] == BlockType.TABLE
    assert f"<eq>{second[2]}</eq>" in table[0][0]["content"]
    assert "<img" not in table[0][0]["content"]
    assert header == [
        [
            {
                "type": BlockType.HEADER,
                "content": [equation(second[2])],
            }
        ]
    ]


def test_docx_picture_comment_flows_title_and_list() -> None:
    """Verify that DOCX picture formulas maintain eq inline semantics in titles and lists."""

    _name, mtef, expected = v5_formula_corpus()[0]
    image = build_gif_with_mtef(mtef)
    title = DocxModel().predict(
        BytesIO(
            build_image_docx(
                image,
                inline=True,
                paragraph_style="Title",
            )
        )
    )
    bullet = DocxModel().predict(
        BytesIO(
            build_image_docx(
                image,
                inline=True,
                paragraph_style="List Bullet",
            )
        )
    )

    assert title[0][0] == {
        "type": BlockType.DOC_TITLE,
        "level": 1,
        "content": [*text_spans("before "), equation(expected), *text_spans(" after")],
    }
    assert bullet[0][0]["type"] == BlockType.LIST
    assert inline_text(bullet[0][0]["content"][0]["content"]) == f"before {expected} after"


@pytest.mark.parametrize("carrier", ["wmf", "gif"])
def test_pptx_picture_comment_equation_keeps_shape_order(carrier: str) -> None:
    """Verify PPTX normal picture comment output equation in the order of original shape."""

    _name, mtef, expected = v5_formula_corpus()[2]
    image = _wmf_formula(mtef) if carrier == "wmf" else build_gif_with_mtef(mtef, chunk_size=7)

    pages = PptxModel().predict(BytesIO(build_image_pptx(image)))

    assert _equation_contents(pages) == [expected]
    assert not _has_image(pages)


def test_pptx_ordinary_gif_over_formula_limit_keeps_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that after ordinary GIF exceeds the formula budget, PPTX still retains static images consistent with the original serialization logic."""
    image = build_baseline_only_gif()
    expected = serialize_office_image(image, content_type="image/gif")
    assert expected is not None
    monkeypatch.setattr(image_equation_module, "MAX_ENTRY_BYTES", 1)
    monkeypatch.setattr(image_equation_module, "MAX_EQUATION_CANDIDATE_TOTAL_BYTES", 0)

    pages = PptxModel().predict(BytesIO(build_image_pptx(image)))

    images = [block for page in pages for block in page if block.get("type") == BlockType.IMAGE]
    assert len(pages) == 1
    assert [block["image_base64"] for block in images] == [expected]
    assert not _equation_contents(pages)


@pytest.mark.parametrize("carrier", ["wmf", "gif"])
def test_pptx_notes_picture_comment_becomes_page_footnote(
    carrier: str,
) -> None:
    """Verify the WMF/GIF picture formula in PPTX notes outputs page_footnote."""

    _name, mtef, expected = v5_formula_corpus()[0]
    image = _wmf_formula(mtef) if carrier == "wmf" else build_gif_with_mtef(mtef)
    pages = PptxModel().predict(
        BytesIO(
            build_image_pptx(
                image,
                notes=True,
            )
        )
    )

    assert pages == [
        [
            {
                "type": BlockType.PAGE_FOOTNOTE,
                "content": [equation(expected)],
            }
        ]
    ]


def test_pptx_notes_bad_ole_native_uses_wmf_preview_comment() -> None:
    """Verify notes in OLE Native continues parsing the same object WMF preview when it fails."""

    _name, mtef, expected = v5_formula_corpus()[0]
    pages = PptxModel().predict(
        BytesIO(
            build_equation_pptx(
                [b"invalid native"],
                notes=True,
                preview_image=_wmf_formula(mtef),
            )
        )
    )

    assert pages == [
        [
            {
                "type": BlockType.PAGE_FOOTNOTE,
                "content": [equation(expected)],
            }
        ]
    ]


@pytest.mark.parametrize("carrier", ["wmf", "gif"])
def test_xlsx_image_comment_equation_enters_visual_and_table_paths(
    carrier: str,
) -> None:
    """Verify XLSX Normal WMF/GIF comment Press anchor to export or enter table cell."""

    _name, mtef, expected = v5_formula_corpus()[0]
    image = _wmf_formula(mtef) if carrier == "wmf" else build_gif_with_mtef(mtef, chunk_size=3)
    standalone = XlsxModel().predict(BytesIO(build_image_xlsx(image)))
    table = XlsxModel().predict(BytesIO(build_image_xlsx(image, cell_value="value")))

    assert _equation_contents(standalone) == [expected]
    assert _equation_contents(table) == []
    assert table[0][0]["type"] == BlockType.TABLE
    assert f"<eq>{expected}</eq>" in table[0][0]["content"]


def test_xlsx_cell_image_comment_returns_eq_html() -> None:
    """Verify XLSX cellimages media Try the comment formula before returning img."""

    _name, mtef, expected = v5_formula_corpus()[0]
    image = build_gif_with_mtef(mtef)
    package = BytesIO()
    with ZipFile(package, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/media/image1.gif", image)
    converter = XlsxConverter()
    converter.zf = ZipFile(BytesIO(package.getvalue()))
    converter.cell_image_map = {"image-id": "media/image1.gif"}
    try:
        html = converter._resolve_cell_image('DISPIMG("image-id")')
    finally:
        converter.zf.close()

    assert html == f"<eq>{expected}</eq>"


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (DocxModel(), build_equation_docx),
        (PptxModel(), build_equation_pptx),
        (XlsxModel(), build_equation_xlsx),
    ],
    ids=["docx", "pptx", "xlsx"],
)
def test_ooxml_bad_native_recovers_from_wmf_preview_comment(
    model: DocxModel | PptxModel | XlsxModel,
    builder: Callable[..., bytes],
) -> None:
    """Verification of modern three formats OLE Native failed and the upgrade worked after failure to WMF preview."""

    _name, mtef, expected = v5_formula_corpus()[1]
    pages = model.predict(
        BytesIO(
            builder(
                [b"invalid native"],
                prog_id="Equation.DSMT4",
                preview_image=_wmf_formula(mtef),
            )
        )
    )

    assert _equation_contents(pages) == [expected]
    assert not _has_image(pages)


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (DocxModel(), build_equation_docx),
        (PptxModel(), build_equation_pptx),
        (XlsxModel(), build_equation_xlsx),
    ],
    ids=["docx", "pptx", "xlsx"],
)
def test_ooxml_native_precedes_conflicting_wmf_preview_comment(
    model: DocxModel | PptxModel | XlsxModel,
    builder: Callable[..., bytes],
) -> None:
    """Verify that modern three formats are valid OLE Native takes precedence over image comment."""

    _native_name, native, expected = formula_corpus()[0]
    _image_name, image_mtef, _image_expected = v5_formula_corpus()[1]
    pages = model.predict(
        BytesIO(
            builder(
                [native],
                prog_id="Equation.DSMT4",
                preview_image=_wmf_formula(image_mtef),
            )
        )
    )

    assert _equation_contents(pages) == [expected]


def test_docx_equationxml_and_ooxml_omml_precede_image_comment() -> None:
    """Verify that equationxml/OMML continues to be higher than the same object picture, comment."""

    _name, image_mtef, _expected = v5_formula_corpus()[0]
    preview = _wmf_formula(image_mtef)
    docx = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [build_word_2003_equation_xml("x")],
                keep_mtef=True,
                mtef_payloads=[b"invalid native"],
                preview_image=preview,
            )
        )
    )
    pptx = PptxModel().predict(
        BytesIO(
            build_equation_pptx(
                [b"invalid native"],
                alternate_omml=True,
                preview_image=preview,
            )
        )
    )
    xlsx = XlsxModel().predict(
        BytesIO(
            build_equation_xlsx(
                [b"invalid native"],
                anchor_mode="drawing",
                alternate_omml=True,
                preview_image=preview,
            )
        )
    )

    assert _equation_contents(docx) == ["x"]
    assert _equation_contents(pptx) == ["z"]
    assert _equation_contents(xlsx) == ["z"]


def test_bad_image_comment_keeps_original_picture_or_placeholder() -> None:
    """Verification baseline-only WMF does not upgrade the formula and continues to output pictures."""

    image = build_wmf([baseline_wmf_comment(0)], placeable=True)
    cases = [
        (DocxModel(), build_image_docx(image)),
        (PptxModel(), build_image_pptx(image)),
    ]

    for model, file_bytes in cases:
        pages = model.predict(BytesIO(file_bytes))
        assert _equation_contents(pages) == []
        assert _has_image(pages)


def test_bad_image_comment_preview_exports_to_sidecar(tmp_path: Path) -> None:
    """Verify that sidecar is complete and JSON does not retain base64 after exporting the non-upgraded image."""

    middle, _model = analyze_native_test_document(build_image_docx(build_baseline_only_gif()), file_suffix="docx")

    result = export_middle_json(middle, tmp_path / "image-mtef-fallback")

    assert len(result.image_paths) == 1
    assert result.image_paths[0].stat().st_size > 0
    assert "base64," not in result.json_path.read_text(encoding="utf-8")


def test_modern_converter_reuse_resets_image_comment_decoder() -> None:
    """Verified that image formula caching and resource budgeting do not string documents when reusing modern converter."""

    first = v5_formula_corpus()[0]
    second = v5_formula_corpus()[1]
    cases = [
        (DocxConverter(), build_image_docx),
        (PptxConverter(), build_image_pptx),
        (XlsxConverter(), build_image_xlsx),
    ]

    for converter, builder in cases:
        converter.convert(BytesIO(builder(build_gif_with_mtef(first[1]))))
        assert _equation_contents(converter.pages) == [first[2]]
        converter.convert(BytesIO(builder(build_gif_with_mtef(second[1]))))
        assert _equation_contents(converter.pages) == [second[2]]
