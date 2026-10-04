"""Validating eligibility judgments, numerical equivalence, and candidate state isolation for the second round of table optimization."""

from copy import deepcopy

import pytest
from reportlab.lib import colors

from docvortex.render._internal.pdf.inline import _PdfParaParser
from docvortex.render._internal.pdf.table import SpatialTableOptions
from test_pdf_structured_tables import _middle, _RecordingRenderer, _table


def _renderer(html):
    """Construct a single-table test context without the interference of the original image."""
    renderer = _RecordingRenderer(_middle([_table(10, (40, 60, 350, 200), html, image=False)]))
    return renderer, renderer.middle_json.pages[0].blocks[0].content[0]


def _build(renderer, block, size, width=310, spatial=True):
    """Get the current candidate through the real table construction entry, without bypassing the shared paragraph structure."""
    return renderer._html_tables(
        block.content, page_idx=0, block=block, available_width=width, spatial=SpatialTableOptions(size) if spatial else None
    )[0]


def test_plain_fragments_are_parsed_once_across_font_sizes(monkeypatch):
    """Font size changes do not reparse the bare text, and candidates still have independent fragments and the correct font size."""
    renderer, block = _renderer("<table><tr><td>中文 Alpha 123 &amp; 数据</td></tr></table>")
    calls = []
    original = _PdfParaParser.parse

    def parse(self, *args, **kwargs):
        """Count the actual number of markup parsing and check whether cross-font size reuse is effective."""
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_PdfParaParser, "parse", parse)
    paragraphs = []
    for size in (8.5, 6, 7.3, 8.5):
        table = _build(renderer, block, size)
        paragraph = table._cellvalues[0][0][0]
        assert all(fragment.fontSize == size for fragment in paragraph.frags)
        assert paragraph.style.leading == size * 11 / 8.5
        paragraphs.append(paragraph)
        table.wrap(310, 1e9)
    assert len(calls) == 1
    assert paragraphs[0].wrap(310, 1e9) == paragraphs[-1].wrap(310, 1e9)
    assert paragraphs[0].frags[0] is not paragraphs[-1].frags[0]
    assert paragraphs[0].frags[0].us_lines is not paragraphs[-1].frags[0].us_lines


def test_fragment_template_follows_font_and_color_changes():
    """Template reuse does not cover fonts, colors, or subsequent style updates."""
    renderer, block = _renderer("<table><tr><td>Alpha 中文</td></tr></table>")
    before = _build(renderer, block, 8.5)._cellvalues[0][0][0]
    renderer.styles.table_cell.fontName = "Courier"
    renderer.styles.table_cell.textColor = colors.red
    after = _build(renderer, block, 6)._cellvalues[0][0][0]
    fresh, fresh_block = _renderer(block.content)
    fresh.styles.table_cell.fontName = "Courier"
    fresh.styles.table_cell.textColor = colors.red
    expected = _build(fresh, fresh_block, 6)._cellvalues[0][0][0]
    assert vars(after.frags[0]) == vars(expected.frags[0])
    assert after.frags[0].fontName != before.frags[0].fontName
    assert after.frags[0].textColor == colors.red
    assert before.frags[0].textColor != colors.red


@pytest.mark.parametrize(
    "html,spatial",
    [
        ("<b>Bold 中文</b>", True),
        ("H<sub>2</sub>O", True),
        ('<a href="https://example.com">LINK</a>', True),
        ("<eq>x^2</eq>", True),
        ("普通文本", False),
    ],
)
def test_complex_cells_and_reflow_do_not_use_cross_size_templates(html, spatial):
    """Rich text and reflow maintain existing construction rules and do not enter the cross-font size path that is limited to ORIGINAL."""
    renderer, block = _renderer(f"<table><tr><td>{html}</td></tr></table>")
    before = deepcopy(renderer.middle_json)
    for size in (8.5, 6):
        _build(renderer, block, size, spatial=spatial)
    prepared = renderer._table_contents[id(block)]
    assert all(cell.text_template is None for cell in prepared._cells.values())
    assert renderer.middle_json == before


@pytest.mark.parametrize(
    "text", ["中文", "中文 Alpha 12.3", "中文  A &amp; B  末尾 ", "中文 • Ω 日文カナ 한글", "中文&#160;空格"]
)
@pytest.mark.parametrize("size", [6.0, 6.1, 7.3, 8.5])
def test_simple_cjk_widths_exactly_match_reportlab(text, size):
    """The direct width measurement must be exactly equal to the floating point result of the native natural width, not just approximately equal."""
    from docvortex.render._internal.pdf.table import _simple_cjk_widths, _paragraph_minimum_width

    renderer, block = _renderer(f"<table><tr><td>{text}</td></tr></table>")
    paragraph = _build(renderer, block, size)._cellvalues[0][0][0]
    actual = _simple_cjk_widths(paragraph, renderer._table_contents[id(block)])
    minimum = _paragraph_minimum_width(paragraph)
    paragraph.wrap(1e6, 1e9)
    assert actual == (minimum, max([0.0, *paragraph.getActualLineWidths0()]))


@pytest.mark.parametrize("option", ["linebreak", "indent", "callback", "dots", "bullet", "oversized", "latin"])
def test_width_fast_path_rejects_unsupported_paragraphs(option):
    """Forced line breaks, indentations, callbacks, etc. will be conservatively rolled back when the equivalence conditions are not met."""
    from docvortex.render._internal.pdf.table import _simple_cjk_widths

    text = {"linebreak": "中文<br>第二行", "callback": "中文<eq>x^2</eq>", "latin": "English"}.get(option, "中文 Alpha")
    renderer, block = _renderer(f"<table><tr><td>{text}</td></tr></table>")
    paragraph = _build(renderer, block, 8.5)._cellvalues[0][0][0]
    if option == "indent":
        paragraph.style.leftIndent = 3
    elif option == "dots":
        paragraph.style.endDots = "..."
    elif option == "bullet":
        paragraph.bulletText = "1."
    elif option == "oversized":
        paragraph.frags[0].fontSize = 2e6
    assert _simple_cjk_widths(paragraph, renderer._table_contents[id(block)]) is None


def test_character_width_cache_is_bounded():
    """A large number of character and font size combinations cannot allow the word width cache of a single table to grow indefinitely."""
    renderer, block = _renderer("<table><tr><td>中文</td></tr></table>")
    prepared = renderer._table_content(block, block.content)
    for i in range(4200):
        prepared.character_width("Helvetica", 6 + i / 1000, "A")
    assert len(prepared._glyphs) == 4096
    prepared.release_templates()
    assert not prepared._glyphs


def test_trial_cache_reuses_measurements_but_materializes_independent_winners(monkeypatch):
    """Different height areas reuse the same width measurement, and still retain their independent selection tables and pre-drawings."""
    from docvortex.render._internal.pdf.table_layout import SpatialTableContent

    renderer, block = _renderer("<table>" + "<tr><td>中文数据 Alpha</td></tr>" * 15 + "</table>")
    flow = renderer._structured_table(block, 0)
    flow.minimum_height = None
    calls = []
    original = flow.build

    def build(width, size):
        """Only the real Table materializations are counted and the deterministic content is not changed."""
        calls.append((width, size))
        return original(width, size)

    flow.build = build
    assert isinstance(flow, SpatialTableContent)
    flow.fit(310, 165)
    first_calls = len(calls)
    first_table = flow.tables[0]
    assert first_calls > 1
    other = flow.fork()
    other.fit(310, 166)
    assert len(calls) <= first_calls + 2
    assert other.tables[0] is not first_table
    fresh, fresh_block = _renderer(block.content)
    expected = fresh._structured_table(fresh_block, 0)
    expected.fit(310, 166)
    assert (other.width, other.height, other.font_size, other.scale) == (
        expected.width,
        expected.height,
        expected.font_size,
        expected.scale,
    )


def test_trial_cache_follows_content_and_style_changes():
    """After the content and style change, the old trial layout summary cannot be used to make font size decisions."""
    renderer, block = _renderer("<table><tr><td>MMMM 中文</td></tr></table>")
    flow = renderer._structured_table(block, 0)
    flow.fit(100, 30)
    block.content = "<table><tr><td>changed 中文文字 " + "long text " * 25 + "</td></tr></table>"
    renderer.styles.table_cell.fontName = "Courier"
    flow.fit(100, 30)
    fresh, fresh_block = _renderer(block.content)
    fresh.styles.table_cell.fontName = "Courier"
    expected = fresh._structured_table(fresh_block, 0)
    expected.fit(100, 30)
    assert flow.failure is None
    assert (flow.width, flow.height, flow.font_size, flow.scale) == (
        expected.width,
        expected.height,
        expected.font_size,
        expected.scale,
    )


def test_trial_summary_cache_is_bounded_and_failures_are_not_cached():
    """The digest cache has a maximum of 128 entries, does not log construction exceptions, and supports explicit release."""
    from reportlab.platypus import Table
    from docvortex.render._internal.pdf.table_layout import SpatialTableContent

    def build(width, size):
        """Isolate summary caching behavior with deterministically sized native tables."""
        if width < 0:
            raise ValueError("rejected")
        return [Table([["cell"]], colWidths=[width], rowHeights=[size])]

    flow = SpatialTableContent(build, None, None, angle=0, location="test", page_idx=0, trial_key=lambda: ("v1",))
    flow._measurement_context = ("v1",)
    for width in range(1, 140):
        flow._measure(width, 8.5)
    assert len(flow._trial_cache) == 128
    with pytest.raises(ValueError, match="rejected"):
        flow._measure(-1, 8.5)
    assert len(flow._trial_cache) == 128
    flow.clear_trials()
    assert not flow._trial_cache


@pytest.mark.parametrize("height", [5, 100, 130, 130.0009, 129.9989, 500])
@pytest.mark.parametrize("width", [12, 300])
def test_height_pruning_preserves_font_scale_and_selected_geometry(height, width):
    """Critical heights, extremely narrow boxes, and scaling below 6 pt must be exactly the same as the original process with pruning disabled."""
    html = "<table>" + "<tr><td>中文数据</td><td>12345</td></tr>" * 10 + "</table>"
    renderer, block = _renderer(html)
    flow = renderer._structured_table(block, 0)
    baseline_renderer, baseline_block = _renderer(html)
    baseline = baseline_renderer._structured_table(baseline_block, 0)
    baseline.minimum_height = None
    baseline.trial_key = None
    flow.fit(width, height)
    baseline.fit(width, height)
    assert flow.failure is None and baseline.failure is None
    assert (flow.width, flow.height, flow.font_size, flow.scale) == (
        baseline.width,
        baseline.height,
        baseline.font_size,
        baseline.scale,
    )
    assert flow.tables[0]._colWidths == baseline.tables[0]._colWidths
    assert flow.tables[0]._rowHeights == baseline.tables[0]._rowHeights


def test_height_pruning_keeps_full_six_point_measurement(monkeypatch):
    """Impossibly high font sizes can be skipped, and the full measure of 6 pt must be used to initialize the zoom search."""
    renderer, block = _renderer("<table>" + "<tr><td>Visible 中文</td></tr>" * 20 + "</table>")
    flow = renderer._structured_table(block, 0)
    sizes = []
    original = flow.build

    def build(width, size):
        """Count the actual constructed font size and retain its native measurement results."""
        sizes.append(size)
        return original(width, size)

    flow.build = build
    flow.fit(300, 50)
    assert sizes and set(sizes) == {6.0}
    assert flow.scale < 1


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<tr><td></td></tr>", 0),
        ("<tr><td>  <br></td></tr>", 0),
        ("<tr><td>中文</td></tr><tr><td></td></tr>", 13),
        ("<tr><td rowspan='2'>A</td><td>B</td></tr><tr><td>C</td></tr>", None),
        ("<tr><td>H<sub>2</sub>O</td></tr>", None),
        ("<tr><td><eq>x</eq></td></tr>", None),
    ],
)
def test_height_lower_bound_is_conservative_for_empty_and_complex_cells(html, expected):
    """Empty lines do not assume the presence of text; pruning is disabled when merged lines or rich text have no provable lower bound."""
    renderer, block = _renderer(f"<table>{html}</table>")
    prepared = renderer._table_content(block, block.content)
    assert prepared.minimum_height(8.5, renderer.styles) == expected


def test_custom_table_builder_does_not_enable_trial_cache_or_pruning(monkeypatch):
    """After replacing the underlying construction callback, its purely functional nature is not assumed, and the real construction and pre-drawing must be used."""
    from docvortex.render._internal.pdf.renderer import _PdfRenderer

    renderer, block = _renderer("<table><tr><td>中文</td></tr></table>")
    original = _PdfRenderer._html_tables

    def build(self, *args, **kwargs):
        """Mock the caller replacement constructor instead of modifying the input protocol."""
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_PdfRenderer, "_html_tables", build)
    flow = renderer._structured_table(block, 0)
    assert flow.trial_key() is None
    assert flow.minimum_height(8.5) is None
    flow.fit(200, 100)
    assert not flow._trial_cache


def test_finished_page_releases_trial_summaries():
    """After the actual export is completed, even if the test holds the layout object, the trial layout summary and template will not be retained."""
    from docvortex.render._internal.pdf.table_layout import SpatialTableContent

    renderer, _ = _renderer("<table>" + "<tr><td>中文数据</td></tr>" * 20 + "</table>")
    renderer.render()
    for item in renderer.items.values():
        for flow in item.flowables:
            if isinstance(flow, SpatialTableContent):
                assert not flow._trial_cache
    assert all(not content._cells and not content._glyphs for content in renderer._table_contents.values())
