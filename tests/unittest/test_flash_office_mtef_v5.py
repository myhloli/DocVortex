from __future__ import annotations

from collections.abc import Callable
from io import BytesIO

import pytest
from _docx_equationxml_test_utils import (
    build_equationxml_docx,
    build_word_2003_equation_xml,
)
from _legacy_ppt_test_utils import build_equation_ppt
from _legacy_xls_test_utils import build_equation_xls, label_cell
from _mtef_test_utils import build_equation_doc, formula_corpus
from _mtef_v5_test_utils import v5_formula_corpus
from _ooxml_mtef_test_utils import (
    build_equation_docx,
    build_equation_pptx,
    build_equation_xlsx,
)
from _span_test_utils import equation

from docvortex.analyzers.native import DocModel, DocxModel, PptModel, PptxModel, XlsModel, XlsxModel
from docvortex.analyzers.native.office.docx.docx_converter import DocxConverter
from docvortex.analyzers.native.office.pptx.pptx_converter import PptxConverter
from docvortex.analyzers.native.office.xlsx.xlsx_converter import XlsxConverter
from docvortex.schema import BlockType


def _equation_contents(pages: list[list[dict]]) -> list[str]:
    """Collects independent formula content in paginated and block order."""

    return [block["content"] for page in pages for block in page if block.get("type") == BlockType.EQUATION]


def _has_preview_image(pages: list[list[dict]]) -> bool:
    """Determine whether model-list retains at least one picture for fallback."""

    return any(block.get("type") == BlockType.IMAGE for page in pages for block in page)


def test_six_office_models_decode_the_full_mtef_v5_corpus() -> None:
    """Verify that the output of DOC/DOCX/PPT/PPTX/XLS/XLSX is consistent with the complete v5 corpus."""

    corpus = v5_formula_corpus()
    formulas = [mtef for _name, mtef, _expected in corpus]
    expected = [latex for _name, _mtef, latex in corpus]
    prog_id = "Equation.DSMT4"
    cases = [
        (
            DocModel(),
            build_equation_doc(
                [(1000 + index, mtef) for index, mtef in enumerate(formulas)],
                prog_id=prog_id,
            ),
        ),
        (
            DocxModel(),
            build_equation_docx(formulas, prog_id=prog_id),
        ),
        (
            PptModel(),
            build_equation_ppt(formulas, preview=False, prog_id=prog_id),
        ),
        (
            PptxModel(),
            build_equation_pptx(formulas, prog_id=prog_id),
        ),
        (
            XlsModel(),
            build_equation_xls(
                [(2000 + index, mtef) for index, mtef in enumerate(formulas)],
                preview=False,
                prog_id=prog_id,
            ),
        ),
        (
            XlsxModel(),
            build_equation_xlsx(formulas, prog_id=prog_id),
        ),
    ]

    for model, file_bytes in cases:
        assert _equation_contents(model.predict(BytesIO(file_bytes))) == expected


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (DocxModel(), build_equation_docx),
        (PptxModel(), build_equation_pptx),
        (XlsxModel(), build_equation_xlsx),
    ],
    ids=["docx", "pptx", "xlsx"],
)
def test_ooxml_prog_id_and_mtef_version_are_orthogonal(
    model: DocxModel | PptxModel | XlsxModel,
    builder: Callable[..., bytes],
) -> None:
    """Verify that Equation.3 can carry v5 and DSMT4/Equation can also carry v3."""

    _v5_name, v5, v5_expected = v5_formula_corpus()[1]
    _v3_name, v3, v3_expected = formula_corpus()[1]

    assert _equation_contents(model.predict(BytesIO(builder([v5], prog_id="Equation.3")))) == [v5_expected]
    assert _equation_contents(model.predict(BytesIO(builder([v3], prog_id="Equation.DSMT4")))) == [v3_expected]
    assert _equation_contents(model.predict(BytesIO(builder([v5], prog_id="Equation")))) == [v5_expected]


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (DocxModel(), build_equation_docx),
        (PptxModel(), build_equation_pptx),
        (XlsxModel(), build_equation_xlsx),
    ],
    ids=["docx", "pptx", "xlsx"],
)
def test_non_equation_ooxml_prog_id_is_not_probed(
    model: DocxModel | PptxModel | XlsxModel,
    builder: Callable[..., bytes],
) -> None:
    """Validating non-formula or empty suffix ProgID does not detect valid v5 OLE content."""

    _name, mtef, _expected = v5_formula_corpus()[0]
    for prog_id in ("Package", "Equation."):
        pages = model.predict(BytesIO(builder([mtef], prog_id=prog_id)))
        assert _equation_contents(pages) == []


@pytest.mark.parametrize(
    ("model", "builder"),
    [
        (DocxModel(), build_equation_docx),
        (PptxModel(), build_equation_pptx),
        (XlsxModel(), build_equation_xlsx),
    ],
    ids=["docx", "pptx", "xlsx"],
)
def test_mtef_v5_icon_mode_keeps_icon_preview(
    model: DocxModel | PptxModel | XlsxModel,
    builder: Callable[..., bytes],
) -> None:
    """Verify that the icon mode does not expand the valid v5, and only retains the original icon preview."""

    _name, mtef, _expected = v5_formula_corpus()[0]
    pages = model.predict(
        BytesIO(
            builder(
                [mtef],
                prog_id="Equation.DSMT4",
                show_as_icon=True,
            )
        )
    )

    assert _equation_contents(pages) == []
    assert _has_preview_image(pages)


def test_xlsx_linked_mtef_v5_object_never_loads_external_target() -> None:
    """Verify that the XLSX external link MathType object only retains the in-package preview and does not access the target."""

    _name, mtef, _expected = v5_formula_corpus()[0]
    pages = XlsxModel().predict(
        BytesIO(
            build_equation_xlsx(
                [mtef],
                prog_id="Equation.DSMT4",
                linked=True,
            )
        )
    )

    assert _equation_contents(pages) == []
    assert _has_preview_image(pages)


@pytest.mark.parametrize(
    ("model", "file_bytes"),
    [
        (
            DocModel(),
            build_equation_doc(
                [(1, bytes([4, 1, 0, 3, 5, 0]))],
                preview_storage_ids={1},
                prog_id="Equation.DSMT4",
            ),
        ),
        (
            DocxModel(),
            build_equation_docx(
                [bytes([4, 1, 0, 3, 5, 0])],
                prog_id="Equation.DSMT4",
            ),
        ),
        (
            PptModel(),
            build_equation_ppt(
                [bytes([4, 1, 0, 3, 5, 0])],
                prog_id="Equation.DSMT4",
            ),
        ),
        (
            PptxModel(),
            build_equation_pptx(
                [bytes([4, 1, 0, 3, 5, 0])],
                prog_id="Equation.DSMT4",
            ),
        ),
        (
            XlsModel(),
            build_equation_xls(
                [(1, bytes([4, 1, 0, 3, 5, 0]))],
                prog_id="Equation.DSMT4",
            ),
        ),
        (
            XlsxModel(),
            build_equation_xlsx(
                [bytes([4, 1, 0, 3, 5, 0])],
                prog_id="Equation.DSMT4",
            ),
        ),
    ],
    ids=["doc", "docx", "ppt", "pptx", "xls", "xlsx"],
)
def test_mtef_v4_keeps_preview_in_all_six_formats(
    model: DocModel | DocxModel | PptModel | PptxModel | XlsModel | XlsxModel,
    file_bytes: bytes,
) -> None:
    """Verify that none of the six formats encountered by v4 are parsed and cached previews are retained."""

    pages = model.predict(BytesIO(file_bytes))

    assert _equation_contents(pages) == []
    assert _has_preview_image(pages)


def test_mtef_v5_enters_docx_xls_xlsx_table_cells() -> None:
    """Verify that v5 formulas for the three document/table formats enter cell HTML and are not output repeatedly."""

    _name, mtef, expected = v5_formula_corpus()[0]
    docx = DocxModel().predict(
        BytesIO(
            build_equation_docx(
                [mtef],
                table=True,
                prog_id="Equation.DSMT4",
            )
        )
    )
    xls = XlsModel().predict(
        BytesIO(
            build_equation_xls(
                [(10, mtef)],
                cell_records=label_cell(0, 0, "value"),
                prog_id="Equation.DSMT4",
            )
        )
    )
    xlsx = XlsxModel().predict(
        BytesIO(
            build_equation_xlsx(
                [mtef],
                cell_value="value",
                prog_id="Equation.DSMT4",
            )
        )
    )

    for pages in (docx, xls, xlsx):
        assert _equation_contents(pages) == []
        assert pages[0][0]["type"] == BlockType.TABLE
        assert f"<eq>{expected}</eq>" in pages[0][0]["content"]


def test_docx_equationxml_and_ooxml_omml_precede_mtef_v5() -> None:
    """Verify that DOCX equationxml as well as PPTX/XLSX OMML take precedence over a valid v5."""

    _name, mtef, _expected = v5_formula_corpus()[0]
    docx = DocxModel().predict(
        BytesIO(
            build_equationxml_docx(
                [build_word_2003_equation_xml("x")],
                keep_mtef=True,
                mtef_payloads=[mtef],
                prog_id="Equation.DSMT4",
            )
        )
    )
    pptx = PptxModel().predict(
        BytesIO(
            build_equation_pptx(
                [mtef],
                alternate_omml=True,
                prog_id="Equation.DSMT4",
            )
        )
    )
    xlsx = XlsxModel().predict(
        BytesIO(
            build_equation_xlsx(
                [mtef],
                anchor_mode="drawing",
                alternate_omml=True,
                prog_id="Equation.DSMT4",
            )
        )
    )

    assert _equation_contents(docx) == ["x"]
    assert _equation_contents(pptx) == ["z"]
    assert _equation_contents(xlsx) == ["z"]


def test_docx_header_footer_and_pptx_notes_accept_mtef_v5() -> None:
    """Verify that v5 maintains ownership in DOCX header footer and PPTX notes."""

    corpus = v5_formula_corpus()
    first = corpus[0]
    second = corpus[1]
    docx = DocxModel().predict(
        BytesIO(
            build_equation_docx(
                [first[1], second[1]],
                header_footer=True,
                prog_id="Equation.DSMT4",
            )
        )
    )
    pptx = PptxModel().predict(
        BytesIO(
            build_equation_pptx(
                [first[1]],
                notes=True,
                prog_id="Equation.DSMT4",
            )
        )
    )

    assert docx == [
        [
            {"type": BlockType.HEADER, "content": [equation(first[2])]},
            {"type": BlockType.FOOTER, "content": [equation(second[2])]},
        ]
    ]
    assert pptx == [
        [
            {
                "type": BlockType.PAGE_FOOTNOTE,
                "content": [equation(first[2])],
            }
        ]
    ]


def test_mtef_v5_model_streams_remain_open_in_all_six_formats() -> None:
    """Verify that none of the six Model paths to v5 close the caller input stream."""

    _name, mtef, _expected = v5_formula_corpus()[0]
    prog_id = "Equation.DSMT4"
    cases = [
        (DocModel(), build_equation_doc([(1, mtef)], prog_id=prog_id)),
        (DocxModel(), build_equation_docx([mtef], prog_id=prog_id)),
        (PptModel(), build_equation_ppt([mtef], prog_id=prog_id)),
        (PptxModel(), build_equation_pptx([mtef], prog_id=prog_id)),
        (XlsModel(), build_equation_xls([(1, mtef)], prog_id=prog_id)),
        (XlsxModel(), build_equation_xlsx([mtef], prog_id=prog_id)),
    ]

    for model, file_bytes in cases:
        stream = BytesIO(file_bytes)
        model.predict(stream)
        assert not stream.closed


def test_modern_office_converter_reuse_resets_v5_state() -> None:
    """Verify that modern Office converter cache and paging state does not string documents when multiplexing v5."""

    first = v5_formula_corpus()[0]
    second = v5_formula_corpus()[1]
    prog_id = "Equation.DSMT4"
    cases = [
        (DocxConverter(), build_equation_docx),
        (PptxConverter(), build_equation_pptx),
        (XlsxConverter(), build_equation_xlsx),
    ]

    for converter, builder in cases:
        converter.convert(BytesIO(builder([first[1]], prog_id=prog_id)))
        assert _equation_contents(converter.pages) == [first[2]]
        converter.convert(BytesIO(builder([second[1]], prog_id=prog_id)))
        assert _equation_contents(converter.pages) == [second[2]]
