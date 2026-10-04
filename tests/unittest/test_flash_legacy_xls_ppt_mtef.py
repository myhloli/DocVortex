from __future__ import annotations

from collections.abc import Callable
from io import BytesIO

import pytest
from _legacy_ppt_test_utils import build_equation_ppt
from _legacy_xls_test_utils import build_equation_xls, label_cell
from _mtef_test_utils import formula_corpus

from docvortex.analyzers.native import PptModel, XlsModel
from docvortex.analyzers.native.office.errors import LegacyOfficeResourceLimitError
from docvortex.analyzers.native.office.limits import MAX_ENTRY_BYTES
from docvortex.schema import BlockType


def test_xls_equation_editor_corpus_decodes_to_exact_equation_blocks() -> None:
    """Verifying the MBD/OBJ binding of XLS restores the full formula corpus to the exact LaTeX."""

    corpus = formula_corpus()
    file_bytes = build_equation_xls(
        [(100 + index, mtef) for index, (_name, mtef, _expected) in enumerate(corpus)],
        preview=False,
    )

    pages = XlsModel().predict(BytesIO(file_bytes))

    assert pages == [[{"type": BlockType.EQUATION, "content": expected} for _name, _mtef, expected in corpus]]


def test_ppt_equation_editor_corpus_stays_bound_to_its_slides() -> None:
    """Verify that PPT binds ExObjRef/persist to slide to restore the complete formula corpus."""

    corpus = formula_corpus()
    file_bytes = build_equation_ppt(
        [mtef for _name, mtef, _expected in corpus],
        preview=False,
    )

    pages = PptModel().predict(BytesIO(file_bytes))

    assert pages == [[{"type": BlockType.EQUATION, "content": expected}] for _name, _mtef, expected in corpus]


def test_xls_equation_inside_table_is_not_duplicated_as_top_level_block() -> None:
    """Verify that XLS formulas that fall within table coordinates enter cell and HTML and are not output repeatedly."""

    _name, mtef, expected = formula_corpus()[0]
    pages = XlsModel().predict(
        BytesIO(
            build_equation_xls(
                [(42, mtef)],
                cell_records=label_cell(0, 0, "value"),
            )
        )
    )

    assert len(pages[0]) == 1
    assert pages[0][0]["type"] == BlockType.TABLE
    assert f"<eq>{expected}</eq>" in pages[0][0]["content"]


@pytest.mark.parametrize(
    ("model", "valid_file", "invalid_file"),
    [
        (
            XlsModel(),
            lambda mtef: build_equation_xls([(42, mtef)]),
            lambda: build_equation_xls([(43, b"invalid MTEF")]),
        ),
        (
            PptModel(),
            lambda mtef: build_equation_ppt([mtef]),
            lambda: build_equation_ppt([b"invalid MTEF"]),
        ),
    ],
    ids=["xls", "ppt"],
)
def test_native_equation_wins_and_invalid_native_keeps_preview(
    model: XlsModel | PptModel,
    valid_file: Callable[[bytes], bytes],
    invalid_file: Callable[[], bytes],
) -> None:
    """Verified XLS/PPT gives priority to native formulas, bad MTEF retains cached previews."""

    _name, mtef, expected = formula_corpus()[1]
    native_pages = model.predict(BytesIO(valid_file(mtef)))
    fallback_pages = model.predict(BytesIO(invalid_file()))

    assert native_pages == [[{"type": BlockType.EQUATION, "content": expected}]]
    assert fallback_pages[0][0]["type"] == BlockType.IMAGE
    assert fallback_pages[0][0]["image_base64"].startswith("data:image/")


def test_ppt_uncompressed_equation_storage_is_supported() -> None:
    """Verifying the uncompressed ExOleObjStg with recInstance=0 also restores the formula."""

    _name, mtef, expected = formula_corpus()[2]

    pages = PptModel().predict(BytesIO(build_equation_ppt([mtef], compressed=False, preview=False)))

    assert pages == [[{"type": BlockType.EQUATION, "content": expected}]]


def test_ppt_equation_decompression_honors_shared_entry_limit() -> None:
    """Verify that malicious ExOleObjStg statement length triggers stable resource limits."""

    _name, mtef, _expected = formula_corpus()[0]
    file_bytes = build_equation_ppt(
        [mtef],
        declared_size=MAX_ENTRY_BYTES + 1,
        preview=False,
    )

    with pytest.raises(LegacyOfficeResourceLimitError, match="max_entry_bytes"):
        PptModel().predict(BytesIO(file_bytes))
