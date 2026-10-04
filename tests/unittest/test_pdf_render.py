from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
from unittest.mock import MagicMock

import pytest
import ziamath
from _span_test_utils import inline as _inline
from bs4 import BeautifulSoup
from PIL import Image
from pypdf import PdfReader
from reportlab.graphics.shapes import Drawing

from docvortex.render import render_pdf
from docvortex.render._internal.pdf import assets as pdf_assets
from docvortex.render._internal.pdf import formula as formula_module
from docvortex.render._internal.pdf.formula import FormulaRenderer, FormulaVector, PdfFormulaError
from docvortex.render._internal.pdf.table import _html_cell_spans
from docvortex.schema import (
    AlgorithmBodyBlock,
    ChartAnnotationBlock,
    ChartBlock,
    ChartBodyBlock,
    CodeBlock,
    CodeBodyBlock,
    DocTitleBlock,
    EquationBlock,
    ImageBlock,
    ImageBodyBlock,
    IndexBlock,
    ListBlock,
    MiddleJson,
    PageAuxTextBlock,
    PageBlock,
    PageFootnoteBlock,
    PageInfo,
    ParagraphTitleBlock,
    Producer,
    TableBlock,
    TextBlock,
)


def _middle(*pages: PageInfo) -> MiddleJson:
    """Construct MiddleJson, a rigorous test that does not require source coordinate layout."""
    return MiddleJson(
        pages=list(pages),
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )


def _page(page_idx: int, *blocks: PageBlock) -> PageInfo:
    """Construct a page of test content in caller order."""
    return PageInfo(page_idx=page_idx, blocks=list(blocks))


def _png_bytes(size: tuple[int, int] = (20, 10)) -> bytes:
    """Generates a PNG that can be read by the Pillow, ReportLab and pypdf tests."""
    output = BytesIO()
    Image.new("RGB", size, (30, 80, 130)).save(output, format="PNG")
    return output.getvalue()


def _png_uri() -> str:
    """Returns valid PNG data URI."""
    return f"data:image/png;base64,{base64.b64encode(_png_bytes()).decode('ascii')}"


def _reader(payload: bytes) -> PdfReader:
    """Open PDF from memory bytes and verify the fixed signature first."""
    assert payload.startswith(b"%PDF-")
    return PdfReader(BytesIO(payload))


def _page_text(reader: PdfReader, page_index: int) -> str:
    """Extracts the searchable text of the specified PDF page."""
    return reader.pages[page_index].extract_text() or ""


def test_pdf_uses_default_planner_without_source_page_boundaries() -> None:
    """Verify fixed default PDF merges continuations, hides auxiliary blocks, collapses empty source pages and input unchanged."""
    middle = _middle(
        _page(
            0,
            PageAuxTextBlock(type="header", index=0, content=_inline("HEADER")),
            TextBlock(type="text", index=1, content=_inline("inter-")),
        ),
        _page(5),
        _page(
            9,
            TextBlock(type="text", index=0, content=_inline("national"), continues_prev=True),
            PageAuxTextBlock(type="footer", index=1, content=_inline("FOOTER")),
        ),
    )
    original = deepcopy(middle)

    payload = render_pdf(middle, document_title="PDF Contract")
    assert payload == render_pdf(middle, document_title="PDF Contract")
    reader = _reader(payload)

    assert len(reader.pages) == 1
    assert "international" in _page_text(reader, 0).replace("\n", "")
    assert "HEADER" not in _page_text(reader, 0) and "FOOTER" not in _page_text(reader, 0)
    assert reader.metadata.title == "PDF Contract"
    assert reader.metadata.creation_date is not None and reader.metadata.creation_date.year == 2000
    assert middle == original


def test_pdf_renders_inline_and_display_formulas_as_vector_paths_with_links() -> None:
    """Verify in-line/inter-line formula vectorization, labels, Chinese mixing, and internal and external links."""
    middle = _middle(
        _page(
            0,
            ParagraphTitleBlock(type="paragraph_title", index=0, level=2, anchor="section-one", content=_inline("章节一")),
            TextBlock(
                type="text",
                index=1,
                content=[
                    {"type": "text", "content": "中文 Before "},
                    {"type": "equation_inline", "content": r"c=\pm\sqrt{a^2+b^2}"},
                    {"type": "text", "content": " after "},
                    {"type": "hyperlink", "url": "#section-one", "content": _inline("jump")},
                    {"type": "text", "content": " and "},
                    {"type": "hyperlink", "url": "https://example.com", "content": _inline("external")},
                ],
            ),
            EquationBlock(type="equation", index=2, content=r"\frac{1}{1-x^2}\tag{7}"),
        )
    )

    reader = _reader(render_pdf(middle))
    text = _page_text(reader, 0)
    resources = reader.pages[0]["/Resources"]
    xobjects = resources.get("/XObject", {})
    image_xobjects = [value for value in xobjects.values() if value.get_object().get("/Subtype") == "/Image"]

    assert "章节一" in text and "中文 Before" in text and "after" in text
    assert "jump" in text and "external" in text
    assert not image_xobjects
    assert len(reader.pages[0].get("/Annots", [])) >= 2
    stream = reader.pages[0].get_contents().get_data()
    assert b" m" in stream and (b" c" in stream or b" l" in stream)


def test_pdf_unicode_fallback_and_script_styles_avoid_black_squares() -> None:
    """Validation Latin Extended, independent accent, Greek and Cyrillic use deterministic font fallback."""
    middle = _middle(
        _page(
            0,
            TextBlock(
                type="text",
                index=0,
                content=[
                    {"type": "text", "content": "Jędrzej J˛edrzej λ Ж 中文 x"},
                    {"type": "text", "content": "2", "styles": ["superscript"]},
                ],
            ),
        )
    )

    reader = _reader(render_pdf(middle))
    text = _page_text(reader, 0)
    stream = reader.pages[0].get_contents().get_data()

    assert "Jędrzej J˛edrzej λ Ж 中文 x2" in text.replace("\n", "")
    assert "■" not in text
    assert b" Ts" in stream


def test_pdf_formula_failures_fall_back_to_visible_latex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both inline and interline formulas remain visible in LaTeX when validating ZiaMath fails."""
    original_render = FormulaRenderer.render

    def fail_bad_formula(
        self: FormulaRenderer,
        latex: str,
        *,
        inline: bool,
        font_size: float,
        color: str = "#1f2937",
    ) -> object:
        """Only let the test formula enter the stable fallback, and the other formulas follow the real implementation."""
        if latex == "bad":
            raise PdfFormulaError("synthetic formula failure")
        return original_render(self, latex, inline=inline, font_size=font_size, color=color)

    monkeypatch.setattr(FormulaRenderer, "render", fail_bad_formula)
    middle = _middle(
        _page(
            0,
            TextBlock(
                type="text",
                index=0,
                content=[{"type": "text", "content": "inline "}, {"type": "equation_inline", "content": "bad"}],
            ),
            EquationBlock(type="equation", index=1, content="bad"),
        )
    )

    text = _page_text(_reader(render_pdf(middle)), 0)

    assert "$bad$" in text
    assert "bad" in text


@pytest.mark.parametrize(
    "source,inline",
    [
        (r"\frac{1}{1-x^2}", False),
        (r"\sqrt[3]{x+y}", True),
        (r"\sum_{i=1}^{n} i^2", False),
        (r"\int_0^\infty e^{-x}\,dx", False),
        (r"\begin{bmatrix}a&b\\c&d\end{bmatrix}", False),
        (r"\left(\frac{x}{y}\right)^2", True),
        (r"\begin{aligned}a&=b+c\\d&=e\end{aligned}", False),
        (r"\color{red}{x}+\boxed{y}", True),
    ],
)
def test_ziamath_vector_corpus_covers_inline_and_display_constructs(source: str, inline: bool) -> None:
    """Verify that fractions, radicals, integrals, matrices, aligned, and colors all produce valid vector geometry."""
    vector = FormulaRenderer().render(source, inline=inline, font_size=12)

    assert vector.width > 0 and vector.height > 0
    assert vector.ascent >= 0 and vector.descent >= 0
    assert vector.drawing.contents


def test_formula_cache_is_bounded_and_rejects_oversized_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation of overlong formula fails before ZiaMath, cache is not expanded when unique items exceed budget."""
    calls: list[str] = []

    def fake_render(latex: str, *, inline: bool, font_size: float, color: str) -> FormulaVector:
        """Returns a fixed vector and records the actual number of conversions."""
        calls.append(latex)
        return FormulaVector(Drawing(1, 1), width=1, height=1, ascent=1, descent=0)

    monkeypatch.setattr(formula_module, "_render_ziamath_formula", fake_render)
    renderer = FormulaRenderer()
    oversized = "x" * (formula_module.MAX_FORMULA_CHARACTERS + 1)
    with pytest.raises(PdfFormulaError, match="max_formula_characters"):
        renderer.render(oversized, inline=True, font_size=10)
    for index in range(formula_module.MAX_CACHED_FORMULAS + 3):
        renderer.render(f"x_{index}", inline=True, font_size=10)

    assert oversized not in calls
    assert len(renderer._cache) == formula_module.MAX_CACHED_FORMULAS  # noqa: SLF001


def test_long_formulas_scale_or_wrap_without_hiding_surrounding_text() -> None:
    """Verify that super-wide inline and interline formulas retain searchable text before and after scaling."""
    formula = "+".join(f"x_{index}^2" for index in range(45))
    middle = _middle(
        _page(
            0,
            TextBlock(
                type="text",
                index=0,
                content=[
                    {"type": "text", "content": "before long formula "},
                    {"type": "equation_inline", "content": formula},
                    {"type": "text", "content": " after long formula"},
                ],
            ),
            EquationBlock(type="equation", index=1, content=formula + r"\tag{wide}"),
        )
    )

    reader = _reader(render_pdf(middle))
    text = "\n".join(_page_text(reader, index) for index in range(len(reader.pages)))

    assert "before long formula" in text
    assert "after long formula" in text


def test_pdf_uses_asset_resolver_and_replaces_missing_or_remote_images_with_placeholders() -> None:
    """Verify sidecar priority, valid images, corrupt footage, and relaxed placeholder policy for remote URL."""
    requested: list[str] = []

    def resolve_asset(path: str) -> bytes:
        """Log image requests and return valid or corrupted bytes respectively."""
        requested.append(path)
        return _png_bytes() if path == "images/ok.png" else b"broken"

    middle = _middle(
        _page(
            0,
            ImageBlock(
                type="image",
                index=0,
                content=[
                    ImageBodyBlock(
                        type="image_body",
                        index=0,
                        content="valid image",
                        image_path="images/ok.png",
                        image_base64=_png_uri(),
                    )
                ],
            ),
            ImageBlock(
                type="image",
                index=1,
                content=[
                    ImageBodyBlock(
                        type="image_body",
                        index=1,
                        content="external svg",
                        image_base64=(
                            "data:image/svg+xml;base64,"
                            + base64.b64encode(
                                b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
                            ).decode("ascii")
                        ),
                    )
                ],
            ),
            ImageBlock(
                type="image",
                index=2,
                content=[
                    ImageBodyBlock(
                        type="image_body",
                        index=2,
                        content="broken image",
                        image_path="images/broken.png",
                    )
                ],
            ),
            ImageBlock(
                type="image",
                index=3,
                content=[
                    ImageBodyBlock(
                        type="image_body",
                        index=3,
                        content="remote image",
                        image_url="https://example.com/image.png",
                    )
                ],
            ),
        )
    )

    reader = _reader(render_pdf(middle, asset_resolver=resolve_asset))
    text = _page_text(reader, 0)

    assert requested == ["images/ok.png", "images/broken.png"]
    assert text.count("image unavailable") == 3
    assert "https://example.com/image.png" in text
    assert "page_idx=0, block_index=2, block_type=image_body" in text


def test_pdf_oversized_image_uses_placeholder_before_pixel_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that PDF out-of-limit raster is converted to existing relaxed placeholder before load."""
    image = MagicMock()
    image.__enter__.return_value = image
    image.format = "PNG"
    image.size = (4_001, 4_000)
    image.load.side_effect = AssertionError("oversized image must not be decoded")
    monkeypatch.setattr(pdf_assets.Image, "open", lambda _source: image)
    middle = _middle(
        _page(
            0,
            ImageBlock(
                type="image",
                index=0,
                content=[
                    ImageBodyBlock(
                        type="image_body",
                        index=0,
                        content="Oversized image",
                        image_path="images/oversized.png",
                    )
                ],
            ),
        )
    )

    reader = _reader(render_pdf(middle, asset_resolver=lambda _path: b"oversized"))

    assert "image unavailable" in _page_text(reader, 0)
    image.load.assert_not_called()


def test_pdf_oversized_image_bytes_are_rejected_before_pillow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that PDF rejects images that exceed a fixed byte budget before being recognized by Pillow."""
    open_image = MagicMock(side_effect=AssertionError("oversized image must not reach Pillow"))
    monkeypatch.setattr(pdf_assets, "MAX_IMAGE_PAYLOAD_BYTES", 8)
    monkeypatch.setattr(pdf_assets.Image, "open", open_image)

    with pytest.raises(pdf_assets.PdfAssetError, match="Image payload exceeds its byte limit"):
        pdf_assets.prepare_image_bytes(b"oversized")

    open_image.assert_not_called()


def test_pdf_table_parser_preserves_standard_inline_style_tags() -> None:
    """Verify that the PDF table parser reverts standard HTML tags to structured text style."""
    soup = BeautifulSoup(
        "<td><strong>bold</strong><em>italic</em><u>under</u><s>strike</s><sup>sup</sup><sub>sub</sub></td>",
        "html.parser",
    )
    cell = soup.td
    assert cell is not None

    spans = _html_cell_spans(cell)

    assert [(span.content, list(getattr(span, "styles", []))) for span in spans] == [
        ("bold", ["bold"]),
        ("italic", ["italic"]),
        ("under", ["underline"]),
        ("strike", ["strikethrough"]),
        ("sup", ["superscript"]),
        ("sub", ["subscript"]),
    ]


def test_pdf_renders_tables_lists_indices_code_algorithm_and_annotations() -> None:
    """Verify the combined output of native table merges, nested tables, directories, lists, code algorithms and instructions."""
    table = TableBlock.model_validate(
        {
            "type": "table",
            "index": 2,
            "content": [
                {
                    "type": "table_body",
                    "index": 2,
                    "content": (
                        "<table><thead><tr><th colspan='2'>Header</th></tr></thead>"
                        "<tbody><tr><td rowspan='2'>A<eq>x^2</eq></td><td>B</td></tr>"
                        "<tr><td><table><tr><td>Nested</td></tr></table></td></tr></tbody></table>"
                    ),
                },
                {"type": "table_caption", "content": _inline("Table caption")},
                {"type": "table_footnote", "content": _inline("Table note")},
            ],
        }
    )
    index = IndexBlock(
        type="index",
        index=1,
        content=[ParagraphTitleBlock(type="paragraph_title", level=2, anchor="target", content=_inline("Target\t9"))],
    )
    code = CodeBlock(
        type="code",
        index=3,
        sub_type="code",
        guess_lang="python",
        content=[
            CodeBodyBlock(type="code_body", index=3, content="print('中文')\nprint(2)"),
            {"type": "code_caption", "content": _inline("Code caption")},
        ],
    )
    algorithm = CodeBlock(
        type="code",
        index=4,
        sub_type="algorithm",
        content=[
            AlgorithmBodyBlock(
                type="algorithm_body",
                index=4,
                content=[{"type": "text", "content": "for "}, {"type": "equation_inline", "content": "n^2"}],
            )
        ],
    )
    chart = ChartBlock(
        type="chart",
        index=5,
        sub_type="bar",
        content=[
            ChartBodyBlock(type="chart_body", index=5, content="<table><tr><th>Year</th></tr><tr><td>2026</td></tr></table>"),
            ChartAnnotationBlock(type="chart_caption", content=_inline("Chart caption")),
        ],
    )
    middle = _middle(
        _page(
            0,
            ParagraphTitleBlock(type="paragraph_title", index=0, level=2, anchor="target", content=_inline("Target")),
            index,
            table,
            code,
            algorithm,
            chart,
            ListBlock(type="list", index=6, content=[TextBlock(type="text", content=_inline("1. item"))]),
            PageFootnoteBlock(type="page_footnote", index=7, anchor="note", content=_inline("Page note")),
        )
    )

    reader = _reader(render_pdf(middle))
    text = "\n".join(_page_text(reader, index) for index in range(len(reader.pages)))

    for expected in (
        "Target",
        "Header",
        "Nested",
        "Table caption",
        "Table note",
        "print('中文')",
        "Code caption",
        "Year",
        "2026",
        "Chart caption",
        "1. item",
        "Page note",
    ):
        assert expected in text


def test_pdf_formula_configuration_is_restored_across_parallel_renders() -> None:
    """Verify that parallel PDF formula rendering does not leak the process-level svg2 configuration of ZiaMath."""
    previous_svg2 = ziamath.config.svg2
    middle = _middle(
        _page(
            0,
            DocTitleBlock(type="doc_title", index=0, level=1, content=_inline("Parallel")),
            EquationBlock(type="equation", index=1, content=r"\sum_{i=1}^{n} i^2"),
        )
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(lambda _index: render_pdf(middle), range(4)))

    assert all(_reader(payload).pages for payload in outputs)
    assert ziamath.config.svg2 is previous_svg2


def test_pdf_public_arguments_remain_strict() -> None:
    """Validation-specific PDF facade rejects old dict, deleted mode with wrong parameter types."""
    middle = _middle(_page(0))

    with pytest.raises(TypeError, match="MiddleJson"):
        render_pdf(middle.to_dict())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="unexpected keyword argument 'mode'"):
        render_pdf(middle, mode="full")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="asset_resolver"):
        render_pdf(middle, asset_resolver="images")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="document_title"):
        render_pdf(middle, document_title=1)  # type: ignore[arg-type]
