"""End-to-end regression on merged cells, pure path drawing, and resource budgeting."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from lxml import etree
from PIL import Image

import docvortex
from _ofd_test_utils import build_ofd_package, page_xml, text_object
from test_ofd_assembly import _grid, _image, _line, _project
from docvortex.analyzers.native.ofd.geometry import Affine
from docvortex.analyzers.native.ofd.models import AxisLine
from docvortex.analyzers.native.ofd.path import OfdPathBudget, build_axis_lines, parse_path_commands
from docvortex.analyzers.native.ofd.table import OfdTableBudget, recover_tables
from docvortex.analyzers.native.ofd.errors import OfdResourceLimitError


def _path(data: str, *, attributes: str = "", children: str = "") -> str:
    """Construct a path with red fill as the default value, and test the color and line style."""
    return f'<ofd:PathObject ID="9" Boundary="0 0 100 100" {attributes}><ofd:FillColor Value="255 0 0"/>{children}<ofd:AbbreviatedData>{data}</ofd:AbbreviatedData></ofd:PathObject>'


def _parse(content: str, *, extra_parts: dict | None = None, page_res: str | None = None):
    """After complete API conversion to minimum OFD, covering the transfer of models and diagnostics."""
    return docvortex.parse(
        build_ofd_package([("Pages/Page_0/Content.xml", page_xml(content, page_res=page_res))], extra_parts=extra_parts),
        file_suffix="ofd",
        keep_model_json=True,
    )


def _raster(result) -> Image.Image:
    """Decode PNG in the original model to verify drawing semantics with real pixels."""
    source = result.model_json.pages[0][0]["image_base64"]
    with Image.open(BytesIO(base64.b64decode(source.split(",", 1)[1]))) as image:
        return image.convert("RGB")


def _pixel(image: Image.Image, x: float, y: float) -> tuple[int, int, int]:
    """Read pixels away from the anti-aliased edge in millimeter coordinates of the test page."""
    return image.getpixel((int(x / 100 * image.width), int(y / 100 * image.height)))


def test_colspan_assigns_text_and_image_to_logical_cell() -> None:
    """The upper row of cross-column cells can accommodate images and text across the virtual divider track."""
    axis = [line for line in _grid() if line.bbox[0] != 50 or line.orientation != "vertical"]
    axis.append(AxisLine((50, 40, 50, 70), "vertical", 0.1, 0, None))
    lines = [
        _line("跨列标题文字", (30, 15, 70, 20), 20),
        _line("底部左侧", (15, 50, 35, 55), 55),
        _line("底部右侧", (55, 50, 75, 55), 55),
    ]
    blocks = _project(lines, axis=axis, images=[_image((35, 25, 65, 35))])
    assert [b["type"] for b in blocks] == ["table"]
    soup = BeautifulSoup(blocks[0]["content"], "html.parser")
    assert len(soup.select("td")) == 3
    assert soup.select("td")[0]["colspan"] == "2"
    assert len(soup.select("td")[0].select("img")) == 1
    assert "跨列标题文字" in soup.select("td")[0].get_text()


def test_rowspan_and_duplicate_borders() -> None:
    """Restore cross-row cells when the left column lacks a horizontal inner border, and repeated borders do not affect the topology."""
    axis = [line for line in _grid() if line.bbox[1] != 40 or line.orientation != "horizontal"]
    axis.append(AxisLine((50, 40, 90, 40), "horizontal", 0.1, 0, None))
    lines = [
        _line("左侧内容", (15, 20, 35, 25), 25),
        _line("右上内容", (55, 20, 75, 25), 25),
        _line("右下内容", (55, 50, 75, 55), 55),
    ]
    table = recover_tables(axis * 3, lines, OfdTableBudget())[0]
    cells = BeautifulSoup(table.html, "html.parser").select("td")
    assert len(cells) == 3 and cells[0]["rowspan"] == "2"


@pytest.mark.parametrize("partial", [False, True])
def test_partial_edges_and_nonrectangular_cells_release_content(partial: bool) -> None:
    """Partial broken edges and L-shaped connected areas reject table candidates, and the original text is not swallowed up."""
    axis = [
        line
        for line in _grid()
        if not (line.orientation == "vertical" and line.bbox[0] == 50)
        and not (line.orientation == "horizontal" and line.bbox[1] == 40)
    ]
    axis += [
        AxisLine((50, 20 if partial else 40, 50, 70), "vertical", 0.1, 0, None),
        AxisLine((50, 40, 90, 40), "horizontal", 0.1, 0, None),
    ]
    lines = [_line("第一段文字", (15, 20, 35, 25), 25), _line("第二段文字", (55, 50, 75, 55), 55)]
    assert recover_tables(axis, lines, OfdTableBudget()) == []
    blocks = _project(lines, axis=axis)
    assert all(block["type"] != "table" for block in blocks)
    assert "".join(span["content"] for block in blocks for span in block["content"]) == "第一段文字第二段文字"


@pytest.mark.parametrize(
    ("data", "count"),
    [
        ("M 10 10 L 90 10 L 90 10.4 L 10 10.4 C", 1),
        ("M 10 10 L 90 10 L 90 90 L 10 90 C", 0),
        ("M 10 10 Q 50 50 90 10 L 90 90 C", 0),
    ],
)
def test_filled_rules_exclude_background_and_glyph_outlines(data: str, count: int) -> None:
    """A slender filled rectangle produces only a single line, and background and glyph curves do not pollute the grid."""
    element = etree.fromstring(_path(data, attributes='Fill="true" Stroke="false"').replace("ofd:", "").encode())
    lines = build_axis_lines(
        element,
        parent_transform=Affine(),
        parent_clip=(0, 0, 100, 100),
        paint_order=0,
        template_id=None,
        budget=OfdPathBudget(),
    )
    assert len(lines) == count


@pytest.mark.parametrize(("rule", "expected"), [("Even-Odd", (255, 255, 255)), ("NonZero", (255, 0, 0))])
def test_path_fill_rule_holes_and_diagnostics(rule: str, expected: tuple[int, int, int]) -> None:
    """True rendering distinguishes odd and even holes from non-zero padding, and diagnostics reach the caller via the full API."""
    data = "M 10 10 L 90 10 L 90 90 L 10 90 C M 30 30 L 70 30 L 70 70 L 30 70 C"
    result = _parse(_path(data, attributes=f'Fill="true" Stroke="false" Rule="{rule}"'))
    with _raster(result) as image:
        assert _pixel(image, 50, 50) == expected
        assert _pixel(image, 20, 20) == (255, 0, 0)
    assert [(d.code, d.page_index) for d in result.diagnostics] == [("ofd_vector_rasterized", 0)]
    assert result.middle_json.pages[0].blocks[0].type == "image"


@pytest.mark.parametrize(
    "data",
    [
        "M 10 10 Q 50 90 90 10 L 90 90 L 10 90 C",
        "M 10 10 B 30 90 70 90 90 10 L 90 90 L 10 90 C",
        "M 30 50 A 20 20 0 1 0 70 50 A 20 20 0 1 0 30 50 C",
    ],
)
def test_curve_commands_produce_visible_pixels(data: str) -> None:
    """Quadratic and cubic curves and arcs are drawn through the real rasterizer and are not reduced to straight lines."""
    result = _parse(_path(data, attributes='Fill="true" Stroke="false"'))
    with _raster(result) as image:
        assert _pixel(image, 5, 5) == (255, 255, 255)
        assert any(pixel == (255, 0, 0) for pixel in image.getdata())
    assert not any("failed" in d.code for d in result.diagnostics)


def test_clipping_transform_alpha_and_stroke_style() -> None:
    """Matrix, clipping, and translucent colors work simultaneously, and stroke parameters can be safely serialized."""
    clip = '<ofd:Clips><ofd:Clip><ofd:Area CTM="1 0 0 1 0 0"><ofd:Path Fill="true" Stroke="false"><ofd:AbbreviatedData>M 0 0 L 20 0 L 20 20 L 0 20 C</ofd:AbbreviatedData></ofd:Path></ofd:Area></ofd:Clip></ofd:Clips>'
    result = _parse(
        _path(
            "M 0 0 L 60 0 L 60 60 L 0 60 C",
            attributes='Fill="true" Stroke="true" CTM="1 0 0 1 20 20" Alpha="128" Cap="Round" Join="Bevel" DashPattern="2 1" LineWidth="1"',
            children=clip,
        )
    )
    with _raster(result) as image:
        assert _pixel(image, 30, 30) == (255, 127, 127)
        assert _pixel(image, 50, 50) == (255, 255, 255)
        assert _pixel(image, 10, 10) == (255, 255, 255)


def test_drawparam_color_inheritance_and_white_pages() -> None:
    """Inherited white fill maintains a white background, explicit red override only affects the corresponding object."""
    resource = '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016"><ofd:DrawParams><ofd:DrawParam ID="4" Fill="true" Stroke="false"><ofd:FillColor Value="255 255 255"/></ofd:DrawParam><ofd:DrawParam ID="5" Relative="4"/></ofd:DrawParams></ofd:Res>'
    white = '<ofd:PathObject ID="10" Boundary="0 0 100 100" DrawParam="5"><ofd:AbbreviatedData>M 0 0 L 100 0 L 100 100 L 0 100 C</ofd:AbbreviatedData></ofd:PathObject>'
    kwargs = {"extra_parts": {"Doc_0/Pages/Page_0/PageRes.xml": resource}, "page_res": "PageRes.xml"}
    assert _parse(white, **kwargs).middle_json.pages[0].blocks == []
    result = _parse(white + _path("M 20 20 L 40 20 L 40 40 L 20 40 C", attributes='DrawParam="5"'), **kwargs)
    with _raster(result) as image:
        assert _pixel(image, 30, 30) == (255, 0, 0)
        assert _pixel(image, 10, 10) == (255, 255, 255)


def test_mixed_pages_do_not_duplicate_native_text() -> None:
    """When containing native text, full-page images will not be generated and native content will not be displayed repeatedly."""
    result = _parse(
        _path("M 20 20 L 40 20 L 40 40 L 20 40 C", attributes='Fill="true" Stroke="false"')
        + text_object(10, "正常正文", boundary="10 60 50 10", delta_x="5 5 5")
    )
    assert all(block.type != "image" for block in result.middle_json.pages[0].blocks)
    assert not result.diagnostics


@pytest.mark.parametrize("data", ["M 10 10 Z", "M 10 10 Q", "M 10 10 A 2 2 0 7 0 20 20"])
def test_unsupported_paths_report_diagnostics(data: str) -> None:
    """Unknown or incomplete paths do not pretend to be normal empty pages, nor do they generate incomplete images."""
    result = _parse(_path(data, attributes='Fill="true" Stroke="false"'))
    assert not result.middle_json.pages[0].blocks
    assert any(d.code == "ofd_vector_unsupported" for d in result.diagnostics)


def test_generated_asset_and_path_budgets_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generating PNG and input path budget overrun both throw resource errors instead of being downgraded to an empty result."""
    from docvortex.analyzers.native.ofd import package, path

    content = _path("M 10 10 L 90 10 L 90 90 L 10 90 C", attributes='Fill="true" Stroke="false"')
    with monkeypatch.context() as patch:
        patch.setattr(package, "MAX_ASSET_TOTAL_BYTES", 1)
        with pytest.raises(OfdResourceLimitError):
            _parse(content)
    with monkeypatch.context() as patch:
        patch.setattr(path, "MAX_PATH_COMMANDS", 1)
        with pytest.raises(OfdResourceLimitError):
            _parse(content)


def test_raster_size_limit_and_portable_html_assets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The image is subject to an upper pixel limit and the HTML picture can still be decoded after the result package is restored."""
    from docvortex.analyzers.native.ofd import vector

    monkeypatch.setattr(vector, "MAX_DECODED_RASTER_PIXELS", 10000)
    result = _parse(_path("M 10 10 L 90 10 L 90 90 L 10 90 C", attributes='Fill="true" Stroke="false"'))
    assert any(d.code == "ofd_vector_downscaled" for d in result.diagnostics)
    with _raster(result) as image:
        assert image.width * image.height <= 10000
    result.save_bundle(tmp_path / "bundle")
    restored = docvortex.load_bundle(tmp_path / "bundle")
    restored.export(tmp_path / "result.html", output_format="html")
    soup = BeautifulSoup((tmp_path / "result.html").read_text(), "html.parser")
    assert len(soup.select("img")) == 1
    with Image.open(tmp_path / soup.select_one("img")["src"]) as image:
        image.verify()


def test_shared_commands_are_parsed_with_one_budget_charge() -> None:
    """Table detection does not repeatedly consume the path token budget when passed in a parsed command."""
    data = "M 10 10 L 90 10"
    budget = OfdPathBudget()
    commands = parse_path_commands(data, budget)
    before = (budget.command_count, budget.token_count)
    element = etree.fromstring(_path(data).replace("ofd:", "").encode())
    assert build_axis_lines(
        element,
        parent_transform=Affine(),
        parent_clip=(0, 0, 100, 100),
        paint_order=0,
        template_id=None,
        budget=budget,
        commands=commands,
    )
    assert (budget.command_count, budget.token_count) == before


def test_resvg_dependency_smoke() -> None:
    """Verify binary dependency import and minimal drawing on all systems with CI and Python versions."""
    import resvg_py

    payload = resvg_py.svg_to_bytes(
        svg_string='<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><path d="M 0 0 L 8 0 L 8 8 Z" fill="red"/></svg>',
        skip_system_fonts=True,
    )
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (8, 8)
        assert image.convert("RGB").getpixel((6, 2)) == (255, 0, 0)


def test_pageblock_transform_without_boundary_and_mixed_image_guard() -> None:
    """Page groups without Boundary still apply CTM, and duplicate full-page rollbacks will not be generated when image objects exist."""
    path = _path("M 0 0 L 20 0 L 20 20 L 0 20 C", attributes='Fill="true" Stroke="false"')
    result = _parse(f'<ofd:PageBlock CTM="1 0 0 1 20 20">{path}</ofd:PageBlock>')
    with _raster(result) as image:
        assert _pixel(image, 30, 30) == (255, 0, 0)
        assert _pixel(image, 10, 10) == (255, 255, 255)
    result = _parse(path + '<ofd:ImageObject ID="11" Boundary="40 40 20 20"/>')
    assert not any(d.code == "ofd_vector_rasterized" for d in result.diagnostics)


def test_unsupported_paint_is_reported_and_valid_color_replaces_it() -> None:
    """The gradient is not disguised as a solid color, and the parent does not support tags when the child object is completely covered by color."""
    from docvortex.analyzers.native.ofd.resources import merge_drawing_attributes

    assert merge_drawing_attributes(
        {"FillColor.Unsupported": "true", "FillColor.ColorSpace": "3"}, {"FillColor.Value": "255 0 0"}
    ) == {"FillColor.Value": "255 0 0"}
    content = _path(
        "M 10 10 L 90 10 L 90 90 C",
        attributes='Fill="true" Stroke="false"',
        children="<ofd:FillColor><ofd:AxialShd/></ofd:FillColor>",
    )
    result = _parse(content)
    assert not result.middle_json.pages[0].blocks
    assert any(d.code == "ofd_vector_render_failed" for d in result.diagnostics)


def test_ancestor_clip_and_object_transform_compose_in_page_coordinates() -> None:
    """Parent group cropping and child object translation take effect together, and ancestor cropping is not repeatedly multiplied by the child object matrix."""
    clip = '<ofd:Clips><ofd:Clip><ofd:Area><ofd:Path Fill="true" Stroke="false"><ofd:AbbreviatedData>M 0 0 L 30 0 L 30 30 L 0 30 C</ofd:AbbreviatedData></ofd:Path></ofd:Area></ofd:Clip></ofd:Clips>'
    path = _path("M 0 0 L 60 0 L 60 60 L 0 60 C", attributes='Fill="true" Stroke="false" CTM="1 0 0 1 10 10"')
    result = _parse(f'<ofd:PageBlock CTM="1 0 0 1 20 20">{clip}{path}</ofd:PageBlock>')
    with _raster(result) as image:
        assert _pixel(image, 40, 40) == (255, 0, 0)
        assert _pixel(image, 60, 60) == (255, 255, 255)
        assert _pixel(image, 25, 25) == (255, 255, 255)


@pytest.mark.parametrize(
    ("name", "counts"),
    [
        ("issue_121_issue_121_4847a3c74e_testfile.zip_signout_ed321ed91a.ofd", [2]),
        ("issue_245_issue_245_d54bc36bb5_merger.zip_merger_7da0439e92.ofd", [1, 0, 1, 1, 1, 0]),
    ],
)
def test_optional_local_regression_samples(name: str, counts: list[int]) -> None:
    """When local corpus exists, the logical cells and path page rollback results of real samples are fixed."""
    path = Path(__file__).resolve().parents[2] / "tmp/ofd_samples/ofdrw_issues_20260828/ofd" / name
    if not path.exists():
        pytest.skip("optional local OFD corpus is unavailable")
    result = docvortex.parse(path, keep_model_json=True)
    assert [len(page.blocks) for page in result.middle_json.pages] == counts
    if "issue_121" in name:
        table = next(block["content"] for block in result.model_json.pages[0] if block["type"] == "table")
        rows = BeautifulSoup(table, "html.parser").select("tr")
        assert [len(row.select("td")) for row in rows] == [4, 4, 4, 4, 6, 2, 2, 2, 2, 2, 2]
        assert "市委主要领导同志调研后续落实事项的通知" in rows[5].select("td")[1].get_text()
    else:
        assert [d.page_index for d in result.diagnostics if d.code == "ofd_vector_rasterized"] == [0, 2, 3, 4]
