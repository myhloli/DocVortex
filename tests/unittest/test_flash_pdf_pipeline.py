from __future__ import annotations

import ast
import inspect
import weakref

import pytest
from _flash_pdf_test_utils import (
    _prepared_text_page,
    _text_line,
)

from docvortex.analyzers.native import PdfModel
from docvortex.analyzers.native.pdf import (
    auxiliary_text,
    char_geometry,
    formulas,
    geometry,
    graphics,
    index_blocks,
    line_layout,
    line_merging,
    models,
    native_text,
    pipeline,
    tables,
    text_blocks,
    titles,
    visual_annotations,
)
from docvortex.analyzers.native.pdf.inline import matching as inline_matching
from docvortex.analyzers.native.pdf.inline import types as text_styles
from docvortex.document.pdf._document import PDFImageInfo, PDFPageTextGeometry


def _image_info(
    fingerprint: str | None,
    bbox: tuple[float, float, float, float],
) -> PDFImageInfo:
    """Construct lightweight image information used by cross-page image watermark rules."""

    return PDFImageInfo(bbox=bbox, fingerprint=fingerprint)


def test_prepare_page_materializes_table_against_original_semantic_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validation form detection avoids the pre-categorization row, but the mislabeled footer within core can be seen during the materialization stage."""

    body = _text_line("body", (10.0, 10.0, 90.0, 20.0), 0)
    footer = _text_line(
        "table tail",
        (10.0, 80.0, 40.0, 90.0),
        1,
        semantic_type="footer",
    )
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[body, footer],
        chars=[],
        drawing_lines=[],
    )
    observed: dict[str, list[int]] = {}

    def fake_detect(
        analysis_source: models._PageSource,
        *,
        excluded_bboxes: list[tuple[float, float, float, float]],
    ) -> list[models._TableCandidate]:
        """Record the source lines visible during the candidate detection phase."""

        assert excluded_bboxes == []
        observed["detect"] = [line.source_index for line in analysis_source.lines]
        return []

    def fake_materialize(
        materialization_source: models._PageSource,
        candidates: list[models._TableCandidate],
        **_kwargs: object,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], set[int]]:
        """Record the source rows visible during the materialization phase of the table."""

        assert candidates == []
        observed["materialize"] = [line.source_index for line in materialization_source.lines]
        return [], [], set()

    monkeypatch.setattr(pipeline, "_detect_table_candidates", fake_detect)
    monkeypatch.setattr(pipeline, "_materialize_table_blocks", fake_materialize)

    pipeline._prepare_page_source(source)

    assert observed == {"detect": [0], "materialize": [0, 1]}


def test_prepare_page_uses_only_table_body_bbox_and_keeps_annotations_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that Pipeline only uses the table body frame as a container barrier and sends pre-categorization comments into the fixed block."""

    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[],
        chars=[],
        drawing_lines=[],
    )
    table_block = {
        "type": "table",
        "bbox": (10.0, 30.0, 90.0, 70.0),
        "angle": 0,
        "content": "body",
    }
    caption_block = {
        "type": "caption",
        "bbox": (10.0, 10.0, 50.0, 20.0),
        "angle": 0,
        "content": "Table 1",
    }

    monkeypatch.setattr(
        pipeline,
        "_detect_table_candidates",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline,
        "_materialize_table_blocks",
        lambda *_args, **_kwargs: ([table_block], [caption_block], set()),
    )

    prepared = pipeline._prepare_page_source(source)

    assert prepared.table_bboxes == [table_block["bbox"]]
    assert [block["type"] for block in prepared.fixed_blocks] == [
        "caption",
        "table",
    ]


def test_prepare_page_resplits_repaired_cross_column_row_before_text_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that the production page is ready to re-cut the column thick lines according to the repair character box and assign a unique source serial number."""

    positions = (0.0, 6.0, 12.0, 18.0, 62.0, 68.0, 74.0, 80.0)
    chars = [
        {
            "char": character,
            "bbox": (position, 10.0, position + 20.0, 20.0),
            "font": {
                "name": "ABCDEF+Body",
                "flags": 0,
                "weight": 400,
                "size": 10.0,
            },
            "char_idx": index,
        }
        for index, (character, position) in enumerate(
            zip("ABCDEFGH", positions, strict=True),
        )
    ]
    line = models._LineItem(
        text="ABCDEFGH",
        bbox=(0.0, 10.0, 100.0, 20.0),
        angle=0,
        source_index=5,
        chars=chars,  # type: ignore[arg-type]
        visual_row_id=3,
        effective_height=10.0,
        em_height=10.0,
    )
    plan = char_geometry.DocumentGeometryPlan(
        char_repairs={
            (0, index): char_geometry.CharLayoutGeometry(
                source_bbox=tuple(float(value) for value in char["bbox"]),  # type: ignore[arg-type]
                tight_bbox=(position, 10.0, position + 5.0, 20.0),
                origin=(position, 20.0),
                layout_bbox=(position, 10.0, position + 5.0, 20.0),
                ink_bbox=(position, 10.0, position + 5.0, 20.0),
                baseline=20.0,
                advance=6.0,
                em_height=10.0,
                x_state="abnormal",
                y_state="healthy",
                confidence=1.0,
            )
            for index, (char, position) in enumerate(
                zip(chars, positions, strict=True),
            )
        },
    )
    source = models._PageSource(
        page_size=(100.0, 100.0),
        lines=[line],
        chars=chars,  # type: ignore[arg-type]
        drawing_lines=[],
    )
    observed_next_indices: list[int] = []
    untouched_style = text_styles.PDFTextStyleLine(
        bbox=(0.0, 30.0, 20.0, 40.0),
        text="plain",
        style_ranges=(),
        source_index=4,
    )
    style_lines = [
        untouched_style,
        text_styles.PDFTextStyleLine(
            bbox=line.bbox,
            text="ABCDEFGH",
            style_ranges=(
                text_styles.PDFTextStyleRange(0, 2, ("bold",)),
                text_styles.PDFTextStyleRange(2, 6, ("bold", "underline")),
                text_styles.PDFTextStyleRange(6, 8, ("underline",)),
            ),
            source_index=5,
        ),
    ]
    link_lines = [
        text_styles.PDFTextLinkLine(
            bbox=line.bbox,
            text="ABCDEFGH",
            link_ranges=(
                text_styles.PDFTextLinkRange(
                    2,
                    6,
                    "https://example.test/split",
                ),
            ),
            source_index=5,
        ),
    ]

    def record_graphic_split_start(
        lines: list[models._LineItem],
        *_args: object,
        source_index_start: int,
    ) -> list[models._LineItem]:
        """Record the next source sequence number passed to the subsequent graphic split after hurdle re-cutting."""

        observed_next_indices.append(source_index_start)
        return lines

    monkeypatch.setattr(
        pipeline,
        "_split_parallel_graphic_rule_rows",
        record_graphic_split_start,
    )

    prepared = pipeline._prepare_page_source(
        source,
        geometry_plan=plan,
        page_index=0,
        style_lines=style_lines,
        link_lines=link_lines,
    )

    assert [member.text for member in prepared.remaining_lines] == ["ABCD", "EFGH"]
    assert [member.source_index for member in prepared.remaining_lines] == [5, 6]
    assert all(member.split_from_row for member in prepared.remaining_lines)
    assert observed_next_indices == [7]
    assert style_lines[0] is untouched_style
    assert style_lines[1:] == [
        text_styles.PDFTextStyleLine(
            bbox=(0.0, 10.0, 23.0, 20.0),
            text="ABCD",
            style_ranges=(
                text_styles.PDFTextStyleRange(0, 2, ("bold",)),
                text_styles.PDFTextStyleRange(2, 4, ("bold", "underline")),
            ),
            source_index=5,
        ),
        text_styles.PDFTextStyleLine(
            bbox=(62.0, 10.0, 85.0, 20.0),
            text="EFGH",
            style_ranges=(
                text_styles.PDFTextStyleRange(0, 2, ("bold", "underline")),
                text_styles.PDFTextStyleRange(2, 4, ("underline",)),
            ),
            source_index=6,
        ),
    ]
    assert link_lines == [
        text_styles.PDFTextLinkLine(
            bbox=(0.0, 10.0, 23.0, 20.0),
            text="ABCD",
            link_ranges=(
                text_styles.PDFTextLinkRange(
                    2,
                    4,
                    "https://example.test/split",
                ),
            ),
            source_index=5,
        ),
        text_styles.PDFTextLinkLine(
            bbox=(62.0, 10.0, 85.0, 20.0),
            text="EFGH",
            link_ranges=(
                text_styles.PDFTextLinkRange(
                    0,
                    2,
                    "https://example.test/split",
                ),
            ),
            source_index=6,
        ),
    ]
    split_blocks = [
        {
            "type": "text",
            "bbox": member.bbox,
            "content": member.text,
        }
        for member in prepared.remaining_lines
    ]
    assert {
        block_index: [line.source_index for line in lines]
        for block_index, lines in inline_matching._assign_lines_to_blocks(
            split_blocks,
            style_lines[1:],
            source.page_size,
        ).items()
    } == {0: [5], 1: [6]}
    assert {
        block_index: [line.source_index for line in lines]
        for block_index, lines in inline_matching._assign_lines_to_blocks(
            split_blocks,
            link_lines,
            source.page_size,
        ).items()
    } == {0: [5], 1: [6]}


def test_prepare_page_realigns_unsplit_repaired_text_evidence() -> None:
    """Styles and links evidence still use the final layout box when verifying normal fix lines are not recut."""

    line = _text_line("linked", (0.0, 80.0, 200.0, 100.0), 5)
    source = models._PageSource(
        page_size=(200.0, 200.0),
        lines=[line],
        chars=[],
        drawing_lines=[],
    )
    repaired_bbox = (20.0, 80.0, 60.0, 100.0)
    plan = char_geometry.DocumentGeometryPlan(
        line_repairs={
            (0, 5): char_geometry.LineGeometryRepair(
                source_bbox=line.bbox,
                layout_bbox=repaired_bbox,
                ink_bbox=repaired_bbox,
                baseline=100.0,
                em_height=20.0,
                state="repair_x",
            )
        }
    )
    style_lines = [
        text_styles.PDFTextStyleLine(
            bbox=line.bbox,
            text=line.text,
            style_ranges=(text_styles.PDFTextStyleRange(0, len(line.text), ("bold",)),),
            source_index=line.source_index,
        )
    ]
    link_lines = [
        text_styles.PDFTextLinkLine(
            bbox=line.bbox,
            text=line.text,
            link_ranges=(text_styles.PDFTextLinkRange(0, len(line.text), "https://example.test/repaired"),),
            source_index=line.source_index,
        )
    ]

    prepared = pipeline._prepare_page_source(
        source,
        geometry_plan=plan,
        page_index=0,
        style_lines=style_lines,
        link_lines=link_lines,
    )

    assert [item.bbox for item in prepared.remaining_lines] == [repaired_bbox]
    assert [item.bbox for item in style_lines] == [repaired_bbox]
    assert [item.bbox for item in link_lines] == [repaired_bbox]
    block = {"type": "text", "bbox": repaired_bbox, "content": line.text}
    assert inline_matching._assign_lines_to_blocks([block], style_lines, source.page_size)
    assert inline_matching._assign_lines_to_blocks([block], link_lines, source.page_size)


def _repeated_separator_source(
    header_text: str,
    *,
    connected_grid: bool = False,
) -> models._PageSource:
    """Construct a repeated top horizontal line, and use this horizontal line as the real top edge of the closed table."""

    lines = [
        _text_line(header_text, (20.0, 15.0, 180.0, 25.0), 0),
        _text_line(f"{header_text} detail", (20.0, 30.0, 180.0, 40.0), 1),
    ]
    drawing_lines = [
        models._AxisLine(
            bbox=(10.0, 50.0, 190.0, 50.1),
            width=0.1,
            orientation="horizontal",
        )
    ]
    if connected_grid:
        lines.extend(
            [
                _text_line("left cell", (20.0, 65.0, 80.0, 75.0), 2),
                _text_line("right cell", (110.0, 65.0, 170.0, 75.0), 3),
            ]
        )
        drawing_lines.extend(
            [
                models._AxisLine((10.0, 90.0, 190.0, 90.1), 0.1, "horizontal"),
                models._AxisLine((10.0, 50.0, 10.1, 90.0), 0.1, "vertical"),
                models._AxisLine((100.0, 50.0, 100.1, 90.0), 0.1, "vertical"),
                models._AxisLine((189.9, 50.0, 190.0, 90.0), 0.1, "vertical"),
            ]
        )
    return models._PageSource(
        page_size=(200.0, 400.0),
        lines=lines,
        chars=[],
        drawing_lines=drawing_lines,
    )


def test_repeated_header_separator_requires_repeated_header_text() -> None:
    """The table rule will not be removed if it is verified that only the horizontal lines are repeated and the normal text above is not repeated."""

    sources = [_repeated_separator_source(text) for text in ("alpha banner", "beta notice", "gamma heading")]

    assert pipeline._detect_repeated_header_separator_bboxes(sources) == [set(), set(), set()]


def test_repeated_header_separator_supports_alternating_headers() -> None:
    """Verify that duplicate mastheads on odd and even pages can jointly confirm the same header separator."""

    sources = [_repeated_separator_source("even journal" if page_index % 2 == 0 else "odd article") for page_index in range(4)]
    expected = {(10.0, 50.0, 190.0, 50.1)}

    assert pipeline._detect_repeated_header_separator_bboxes(sources) == [expected] * 4


def test_repeated_table_top_rule_is_not_header_separator() -> None:
    """Verify that the top edge of the closed grid of the repeating form is not removed by the repeating header above."""

    sources = [_repeated_separator_source("repeated form", connected_grid=True) for _page_index in range(3)]

    assert pipeline._detect_repeated_header_separator_bboxes(sources) == [set(), set(), set()]
    assert [candidate.bbox for candidate in tables._detect_table_candidates(sources[0])] == [(10.0, 50.0, 190.0, 90.1)]


def test_table_detection_excludes_confirmed_masthead_separator() -> None:
    """Verify that the header column divider does not form a giant candidate with the real table boundary below."""

    source = models._PageSource(
        page_size=(200.0, 300.0),
        lines=[
            _text_line("left masthead", (10.0, 10.0, 60.0, 20.0), 0),
            _text_line("center masthead", (70.0, 10.0, 130.0, 20.0), 1),
            _text_line("body", (10.0, 50.0, 190.0, 60.0), 2),
        ],
        chars=[],
        drawing_lines=[
            models._AxisLine(
                bbox=(10.0, 30.0, 190.0, 31.0),
                width=1.0,
                orientation="horizontal",
            ),
            models._AxisLine(
                bbox=(10.0, 100.0, 190.0, 101.0),
                width=1.0,
                orientation="horizontal",
            ),
        ],
    )

    retained = pipeline._table_detection_drawing_lines(
        source,
        {(10.0, 30.0, 190.0, 31.0)},
    )

    assert [line.bbox for line in retained] == [(10.0, 100.0, 190.0, 101.0)]


def test_repeated_large_image_requires_three_distinct_pages() -> None:
    """Verify that the watermark will be hit only if the same page is repeated only once, the area is exactly 8%, and spans three pages."""

    page_sizes = [(100.0, 100.0)] * 4
    page_image_infos = [
        [
            _image_info("watermark", (0.0, 0.0, 40.0, 20.0)),
            _image_info("watermark", (0.0, 0.0, 40.0, 20.0)),
            _image_info("two-pages", (0.0, 0.0, 50.0, 20.0)),
        ],
        [_image_info("watermark", (10.0, 10.0, 50.0, 30.0))],
        [_image_info("watermark", (20.0, 20.0, 60.0, 40.0))],
        [_image_info("two-pages", (0.0, 0.0, 50.0, 20.0))],
    ]

    fingerprints = pipeline._detect_repeated_raster_watermark_fingerprints(
        page_image_infos,
        page_sizes,
    )

    assert fingerprints == {"watermark"}


def test_repeated_watermark_filter_keeps_small_or_unfingerprinted_images() -> None:
    """Only large images will be deleted after verifying that the fingerprint has been hit. Small images and images that failed to read the fingerprint will continue to enter the existing process."""

    image_infos = [
        _image_info("watermark", (0.0, 0.0, 40.0, 20.0)),
        _image_info("watermark", (0.0, 0.0, 39.9, 20.0)),
        _image_info(None, (0.0, 0.0, 80.0, 80.0)),
        _image_info("ordinary", (0.0, 0.0, 80.0, 80.0)),
    ]

    filtered = pipeline._filter_repeated_raster_watermark_bboxes(
        image_infos,
        (100.0, 100.0),
        {"watermark"},
    )

    assert filtered == [
        (0.0, 0.0, 39.9, 20.0),
        (0.0, 0.0, 80.0, 80.0),
        (0.0, 0.0, 80.0, 80.0),
    ]


def test_flash_extractor_has_no_local_ocr_runtime_logic() -> None:
    """Guard Flash extractor no longer introduces or implements native OCR runtime logic."""

    source = "\n".join(
        inspect.getsource(module)
        for module in (
            PdfModel,
            auxiliary_text,
            formulas,
            geometry,
            graphics,
            index_blocks,
            line_layout,
            line_merging,
            models,
            native_text,
            pipeline,
            tables,
            text_blocks,
            titles,
            visual_annotations,
        )
    )
    forbidden_tokens = (
        "import numpy",
        "import cv2",
        "project_ocr_table_text",
        "_load_ocr_runtime",
        "_run_full_page_ocr",
        "_project_ocr_candidate",
        "_recognize_rotated_ocr_table",
        "AtomModelSingleton",
        "run_ocr_inference",
        "get_processing_window_size",
        "bgr_image",
        "pixel_quad",
        "ocr_model",
    )

    assert not [token for token in forbidden_tokens if token in source]


def test_native_pdf_domain_modules_do_not_import_each_other() -> None:
    """The guard domain processor only relies on the shared layer, and the cross-domain combination remains unified in pipeline."""

    domain_modules = (
        auxiliary_text,
        formulas,
        graphics,
        index_blocks,
        tables,
        text_blocks,
        titles,
        visual_annotations,
    )
    domain_names = {module.__name__.rsplit(".", 1)[-1] for module in domain_modules}
    for module in domain_modules:
        tree = ast.parse(inspect.getsource(module))
        relative_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is not None
        }
        assert not relative_imports & domain_names, module.__name__


@pytest.mark.parametrize(
    "block_type",
    [
        "doc_title",
        "paragraph_title",
        "ref_text",
        "header",
        "footer",
        "page_number",
        "caption",
        "footnote",
        "page_footnote",
        "aside_text",
        "index",
        "equation",
    ],
)
def test_output_normalization_preserves_new_flash_types(block_type: str) -> None:
    """Verify that Flash text semantic types and formula types do not fall back text during the normalization phase."""

    block = pipeline._normalize_output_block(
        {"type": block_type, "bbox": (10.0, 20.0, 40.0, 50.0), "angle": 0, "content": "value"},
        (100.0, 100.0),
    )

    assert block is not None
    assert block["type"] == block_type


def test_output_normalization_keeps_empty_equation_but_drops_empty_text() -> None:
    """Verify that pure vector formulas retain empty content, and normal empty text still does not go into model_list."""

    equation = pipeline._normalize_output_block(
        {"type": "equation", "bbox": (10.0, 20.0, 40.0, 50.0), "angle": 0, "content": ""},
        (100.0, 100.0),
    )
    text = pipeline._normalize_output_block(
        {"type": "text", "bbox": (10.0, 20.0, 40.0, 50.0), "angle": 0, "content": ""},
        (100.0, 100.0),
    )

    assert equation == {
        "type": "equation",
        "bbox": [0.1, 0.2, 0.4, 0.5],
        "angle": 0,
        "content": "",
    }
    assert text is None


def test_output_normalization_applies_unicode_content_safety_net() -> None:
    """Verify that final model_list normalization cleans up typographic whitespace and safe zero-width characters."""

    block = pipeline._normalize_output_block(
        {
            "type": "text",
            "bbox": (10.0, 20.0, 40.0, 50.0),
            "angle": 0,
            "content": "alpha\u00a0beta\u200bgamma",
        },
        (100.0, 100.0),
    )

    assert block is not None
    assert block["content"] == "alpha betagamma"


def test_index_is_claimed_before_formula_anchor_growth() -> None:
    """Verify that the page numbers on the right edge of the table of contents first form a complete index and no longer expand into overlapping formulas."""

    heading = _text_line(
        "contents",
        (40.0, 5.0, 60.0, 10.0),
        0,
        effective_height=5.0,
    )
    rows = []
    source_index = 1
    for row_index, top in enumerate((20.0, 30.0, 40.0, 50.0, 60.0, 70.0)):
        visual_row_id = 10 + row_index
        rows.extend(
            [
                _text_line(
                    f"entry-{row_index}",
                    (10.0, top, 80.0, top + 5.0),
                    source_index,
                    visual_row_id=visual_row_id,
                    run_index=0,
                    split_from_row=True,
                    effective_height=5.0,
                ),
                _text_line(
                    str(row_index + 1),
                    (90.0, top, 95.0, top + 5.0),
                    source_index + 1,
                    visual_row_id=visual_row_id,
                    run_index=1,
                    split_from_row=True,
                    effective_height=5.0,
                ),
            ]
        )
        source_index += 2
    page = _prepared_text_page(heading, *rows)

    blocks = pipeline._finalize_prepared_page(page, page_index=1)

    assert [block["type"] for block in blocks].count("index") == 1
    assert not [block for block in blocks if block["type"] == "equation"]
    assert next(block for block in blocks if block["type"] == "index")["content"].count("entry-") == 6
    assert next(block for block in blocks if block["content"] == "contents")["type"] == "paragraph_title"


def test_numbered_formula_rows_are_not_claimed_as_index() -> None:
    """Verify that consecutively numbered formulas first retain formula semantics and are not swallowed up by untitled directory candidates."""

    body_font = ("Body", 0)
    lines = [
        _text_line(
            "ordinary body row",
            (0.0, 10.0 * index, 100.0, 10.0 * index + 7.0),
            index,
            effective_height=7.0,
            font_signature=body_font,
            font_coverage=1.0,
        )
        for index in range(10)
    ]
    source_index = 10
    for row_index, top in enumerate((102.0, 113.0, 124.0, 135.0, 146.0, 157.0)):
        visual_row_id = 100 + row_index
        lines.extend(
            [
                _text_line(
                    f"x{row_index}=y{row_index}",
                    (20.0, top, 55.0, top + 7.0),
                    source_index,
                    visual_row_id=visual_row_id,
                    run_index=0,
                    split_from_row=True,
                    effective_height=7.0,
                    font_signature=("Math", 0),
                    font_coverage=0.6,
                ),
                _text_line(
                    f"({row_index + 1})",
                    (91.0, top, 100.0, top + 7.0),
                    source_index + 1,
                    visual_row_id=visual_row_id,
                    run_index=1,
                    split_from_row=True,
                    effective_height=7.0,
                ),
            ]
        )
        source_index += 2

    blocks = pipeline._finalize_prepared_page(
        _prepared_text_page(*lines, page_size=(100.0, 200.0)),
        page_index=1,
    )

    assert sum(block["type"] == "equation" for block in blocks) == 6
    assert not [block for block in blocks if block["type"] == "index"]


@pytest.mark.parametrize("retain_formula", [False, True])
def test_document_preparation_releases_sources_but_keeps_formula_evidence(
    monkeypatch: pytest.MonkeyPatch,
    retain_formula: bool,
) -> None:
    """Page preparation releases the original character owner while retaining an independent copy explicitly held by the formula reconstruction."""

    class TrackedChar(dict):
        """Allow weak references to observe the actual lifetime of the character dictionary."""

    references: list[weakref.ReferenceType[TrackedChar]] = []

    def sources() -> pipeline._DocumentSources:
        """Construct character sources in independent scopes to avoid testing local variables to extend the lifetime."""

        chars = [TrackedChar(char="x", char_idx=0, bbox=(10.0, 20.0, 20.0, 30.0))]
        references.append(weakref.ref(chars[0]))
        return pipeline._DocumentSources(
            [models._PageSource((100.0, 100.0), [], chars, [])],
            [PDFPageTextGeometry(chars, {}, {})],
            [(100.0, 100.0)],
            [[]],
            [[]],
        )

    def prepare(source: models._PageSource, **kwargs: object) -> models._PreparedPage:
        """Only copies of the characters required for reconstruction of the simulation formula are retained; other original references should be released."""

        prepared = models._PreparedPage(source.page_size, [], [], [], [])
        if retain_formula:
            prepared.canonical_formula_source_lines.append(
                models._LineItem(
                    text="x",
                    bbox=(10.0, 20.0, 20.0, 30.0),
                    angle=0,
                    source_index=0,
                    chars=list(source.chars),
                )
            )
        return prepared

    monkeypatch.setattr(pipeline, "_prepare_page_source", prepare)
    raw = sources()
    prepared = pipeline._prepare_document_sources(raw)
    assert raw.page_sources == []
    assert raw.page_text_geometries == []
    assert (references[0]() is not None) == retain_formula
    prepared[0].canonical_formula_source_lines.clear()
    assert references[0]() is None
