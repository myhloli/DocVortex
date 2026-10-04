"""OFD Synthetic regression of visual row, conservative paragraph and cell picture attributions."""

from __future__ import annotations

import base64
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from docvortex.analyzers.native.ofd.assembly import line_html, merge_same_baseline_lines, upright_point
from docvortex.analyzers.native.ofd.geometry import rect_quad
from docvortex.analyzers.native.ofd.models import AxisLine, GlyphItem, ImageItem, OfdPageScene, TextLine
from docvortex.analyzers.native.ofd.reading_order import OfdReadingOrderProjector
from docvortex.analyzers.native.ofd.table import OfdTableBudget, _cell_index, recover_tables
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.render import render_html, render_markdown
from docvortex.schema import ModelJson


def _line(
    text: str, bbox: tuple[float, float, float, float], baseline: float, *, styles: tuple[str, ...] = (), angle: int = 0
) -> TextLine:
    """Construct rotatable text fragments with independent glyph boxes and true baselines."""
    quad = tuple(upright_point(x, y, -angle) for x, y in rect_quad(bbox))
    rotated = (min(p[0] for p in quad), min(p[1] for p in quad), max(p[0] for p in quad), max(p[1] for p in quad))
    origin = upright_point(bbox[0], baseline, -angle)
    glyph = GlyphItem(text=text, bbox=rotated, quad=quad, origin=origin)
    return TextLine(text, rotated, [glyph], angle, 5, 0, None, "Body", None, styles)


def _image(bbox: tuple[float, float, float, float], *, decoded: bool = True) -> ImageItem:
    """Construct actual exportable images or degraded payloadless images."""
    buffer = BytesIO()
    Image.new("RGB", (20, 10), "blue").save(buffer, format="PNG")
    source = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    return ImageItem(bbox, source if decoded else None, 0, None, "Body", None)


def _grid() -> list[AxisLine]:
    """Construct a complete grid with two rows and two columns for reuse in content attribution testing."""
    return [AxisLine((10, y, 90, y), "horizontal", 0.1, 0, None) for y in (10, 40, 70)] + [
        AxisLine((x, 10, x, 70), "vertical", 0.1, 0, None) for x in (10, 50, 90)
    ]


def _project(lines: list[TextLine], *, images: list[ImageItem] | None = None, axis: list[AxisLine] | None = None) -> list[dict]:
    """Run real page projections to verify interactions between lines, paragraphs, and visual blocks."""
    scene = OfdPageScene(0, (0, 0, 210, 297), None, lines, axis or [], images or [])
    return OfdReadingOrderProjector([scene]).project_page(scene)


def _plain(block: dict) -> str:
    """Read the structured text of the test output, retaining all characters and spaces."""
    return "".join(span["content"] for span in block["content"])


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_mixed_glyph_heights_and_styles_share_real_baseline(angle: int) -> None:
    """Chinese, English and punctuation glyphs with different heights still form the same line and retain local styles."""
    lines = [
        _line("中文", (10, 10, 20, 15), 15, angle=angle),
        _line("ZIP", (21, 11.5, 29, 14.5), 15, styles=("bold",), angle=angle),
        _line("，", (30, 14, 31, 15), 15, angle=angle),
        _line("正文", (32, 10, 42, 15), 15, styles=("italic",), angle=angle),
    ]
    result = merge_same_baseline_lines(list(reversed(lines)))
    assert len(result) == 1
    assert result[0].text == "中文ZIP，正文"
    assert line_html(result[0]) == "中文<strong>ZIP</strong>，<em>正文</em>"


def test_parallel_columns_and_template_layers_remain_separate() -> None:
    """Two distant columns of the same baseline and different template sources will not be accidentally spliced."""
    left = _line("left column", (10, 10, 60, 15), 15)
    right = _line("right column", (90, 10, 150, 15), 15)
    assert len(merge_same_baseline_lines([right, left])) == 2
    adjacent = replace(_line("template", (61, 10, 81, 15), 15), template_id=7)
    assert len(merge_same_baseline_lines([left, adjacent])) == 2


def test_paragraph_indentation_short_tail_and_punctuation() -> None:
    """Indentation and short tail lines are restored to three paragraphs, and periods at the end of normal lines will not force a paragraph break."""
    lines = [
        _line("这是第一段首行。", (20, 10, 100, 15), 15),
        _line("第一段继续正文", (10, 21, 100, 26), 26),
        _line("第一段末行正文", (10, 32, 55, 37), 37),
        _line("这是第二段首行", (20, 43, 100, 48), 48),
        _line("第二段末行正文", (10, 54, 60, 59), 59),
        _line("这是第三段独立正文", (20, 65, 85, 70), 70),
    ]
    blocks = _project(lines)
    assert [len(block["lines"]) for block in blocks] == [3, 2, 1]
    assert _plain(blocks[0]) == "".join(line.text for line in lines[:3])
    assert blocks[0]["bbox"][1] == pytest.approx(10 / 297, abs=0.0005)
    assert blocks[0]["bbox"][3] == pytest.approx(37 / 297, abs=0.0005)


def test_large_gap_list_and_standalone_short_lines_are_boundaries() -> None:
    """Obvious whitespace, list starts, and standalone short lines block text merging."""
    lines = [
        _line("标题", (10, 10, 20, 15), 15),
        _line("第一段正常正文", (10, 21, 100, 26), 26),
        _line("1. 列表项目正文", (10, 32, 100, 37), 37),
        _line("2. 第二列表项目", (10, 43, 100, 48), 48),
        _line("空白之后的正文", (10, 90, 100, 95), 95),
    ]
    assert len(_project(lines)) == 5


def test_two_columns_do_not_form_cross_column_paragraphs() -> None:
    """The two-column text should be combined into separate paragraphs, and the two adjacent columns in the reading order should not be connected."""
    lines = [_line(f"左栏连续正文{row}", (10, 10 + row * 8, 65, 15 + row * 8), 15 + row * 8) for row in range(3)]
    lines += [_line(f"右栏连续正文{row}", (95, 10 + row * 8, 150, 15 + row * 8), 15 + row * 8) for row in range(3)]
    blocks = _project(lines)
    assert len(blocks) == 2
    assert [len(block["lines"]) for block in blocks] == [3, 3]
    assert all(not ("左" in _plain(block) and "右" in _plain(block)) for block in blocks)


def test_image_is_a_paragraph_barrier() -> None:
    """Independent pictures between the main text maintain a reading order barrier, and do not directly join paragraphs across pictures."""
    lines = [_line("图片上方连续正文", (10, 10, 90, 15), 15), _line("图片下方连续正文", (10, 30, 90, 35), 35)]
    assert [block["type"] for block in _project(lines, images=[_image((10, 18, 90, 27))])] == ["text", "image", "text"]


def test_cells_claim_images_once_and_keep_adjacent_text_separate() -> None:
    """The text in adjacent cells is first assigned and then grouped into rows, and the picture is only output once in the corresponding cell."""
    lines = [_line("甲文字", (40, 15, 49, 20), 20), _line("乙文字", (51, 15, 65, 20), 20)]
    images = [_image((15, 45, 45, 65)), _image((55, 45, 85, 65))]
    blocks = _project(lines, images=images, axis=_grid())
    assert [block["type"] for block in blocks] == ["table"]
    soup = BeautifulSoup(blocks[0]["content"], "html.parser")
    cells = soup.find_all("td")
    assert [cell.get_text() for cell in cells[:2]] == ["甲文字", "乙文字"]
    assert [len(cell.find_all("img")) for cell in cells] == [0, 0, 1, 1]
    middle = model_json_to_middle_json(
        ModelJson(
            pages=[blocks], page_index_map=[], metadata={"file_suffix": "ofd", "producer": {"name": "test", "version": "1"}}
        )
    )
    assert len(BeautifulSoup(render_html(middle), "html.parser").select("td img")) == 2
    assert len(BeautifulSoup(render_markdown(middle), "html.parser").select("td img")) == 2


@pytest.mark.parametrize("bbox", [(40, 45, 60, 65), (5, 5, 95, 75), (100, 45, 120, 65), (49.8, 45, 50.2, 65)])
def test_ambiguous_or_external_images_are_not_claimed(bbox: tuple[float, float, float, float]) -> None:
    """Pictures that are dually attributed across grids, overlays, outside the table, and within tolerances retain independent identities."""
    lines = [_line("甲文字", (15, 15, 35, 20), 20), _line("乙文字", (55, 15, 75, 20), 20)]
    image = _image(bbox)
    tables = recover_tables(_grid(), lines, OfdTableBudget(), images=[image])
    assert len(tables) == 1 and not tables[0].consumed_image_ids
    assert "<img" not in tables[0].html
    blocks = _project(lines, images=[image], axis=_grid())
    assert sum(block["type"] == "image" for block in blocks) == 1


def test_undecodable_image_keeps_existing_degradation_and_cell_bounds_are_total() -> None:
    """The unloaded image is not swallowed by the table or generates empty img, and the coordinates outside the interval return empty attributes."""
    lines = [_line("甲文字", (15, 15, 35, 20), 20), _line("乙文字", (55, 15, 75, 20), 20)]
    image = _image((15, 45, 45, 65), decoded=False)
    table = recover_tables(_grid(), lines, OfdTableBudget(), images=[image])[0]
    assert not table.consumed_image_ids and "<img" not in table.html
    assert [block["type"] for block in _project(lines, images=[image], axis=_grid())] == ["table"]
    assert _cell_index([10, 50, 90], 100) is None


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("First sentence.", "Next sentence.", "First sentence. Next sentence."),
        ("An English word", "continues here", "An English word continues here"),
        ("Existing space ", "is preserved", "Existing space is preserved"),
        ("Keep the hyphen-", "ated spelling", "Keep the hyphen-ated spelling"),
    ],
)
def test_english_paragraph_boundaries_keep_spacing(first: str, second: str, expected: str) -> None:
    """Punctuation and word breaks at the end of English sentences are filled with spaces, but existing spaces and hyphens are not overwritten."""
    lines = [_line(first, (10, 10, 100, 15), 15), _line(second, (10, 18, 95, 23), 23)]
    blocks = _project(lines)
    assert len(blocks) == 1 and _plain(blocks[0]) == expected
    assert [line.text for line in lines] == [first, second]


def test_page_paragraphs_use_existing_cross_page_continuation() -> None:
    """First, form intra-page paragraphs, then create cross-page continuations based on existing public rules and clean up private geometry."""
    scenes = [
        OfdPageScene(
            0,
            (0, 0, 210, 297),
            None,
            [
                _line("第一页接近页尾的正文", (10, 250, 100, 255), 255),
                _line("下一页仍然继续这段正文", (10, 258, 100, 263), 263),
            ],
        ),
        OfdPageScene(
            1,
            (0, 0, 210, 297),
            None,
            [
                _line("第二页承接前面的正文", (10, 20, 100, 25), 25),
                _line("最后结束这一段文字。", (10, 28, 95, 33), 33),
            ],
        ),
    ]
    pages = OfdReadingOrderProjector(scenes).project()
    assert [len(page) for page in pages] == [1, 1]
    model = ModelJson(
        pages=pages, page_index_map=[], metadata={"file_suffix": "ofd", "producer": {"name": "test", "version": "1"}}
    )
    middle = model_json_to_middle_json(model)
    assert middle.pages[1].blocks[0].continues_prev is True
    assert "lines" not in middle.to_dict()["pages"][1]["blocks"][0]
    assert len(model.pages[0][0]["lines"]) == 2


def test_table_html_assets_survive_export_and_bundle(tmp_path: Path) -> None:
    """Cell pictures can still be exported after being externalized along the public material link, and will be retained in the result package round trip."""
    from docvortex import load_bundle
    from docvortex.api import postprocess

    lines = [_line("甲文字", (15, 15, 35, 20), 20), _line("乙文字", (55, 15, 75, 20), 20)]
    image = _image((15, 45, 45, 65))
    blocks = _project(lines, images=[image], axis=_grid())
    model = ModelJson(
        pages=[blocks], page_index_map=[], metadata={"file_suffix": "ofd", "producer": {"name": "test", "version": "1"}}
    )
    result = postprocess(model, keep_model_json=True)
    result.save_bundle(tmp_path / "bundle")
    restored = load_bundle(tmp_path / "bundle")
    restored.export(tmp_path / "export" / "result.html", output_format="html")
    markup = (tmp_path / "export" / "result.html").read_text()
    soup = BeautifulSoup(markup, "html.parser")
    images = soup.select("td img")
    assert len(images) == len(soup.select("img")) == 1
    path = tmp_path / "export" / images[0]["src"]
    assert path.read_bytes() == base64.b64decode(image.image_base64.split(",", 1)[1])
    assert not images[0]["src"].startswith("data:")
