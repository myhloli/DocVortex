"""合并单元格、纯路径绘制及资源预算的端到端回归。"""

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
    """构造以红色填充为默认值的路径，测试可覆盖颜色与线型。"""
    return f'<ofd:PathObject ID="9" Boundary="0 0 100 100" {attributes}><ofd:FillColor Value="255 0 0"/>{children}<ofd:AbbreviatedData>{data}</ofd:AbbreviatedData></ofd:PathObject>'


def _parse(content: str, *, extra_parts: dict | None = None, page_res: str | None = None):
    """经过完整 API 转换最小 OFD，覆盖模型与诊断的传递。"""
    return docvortex.parse(
        build_ofd_package([("Pages/Page_0/Content.xml", page_xml(content, page_res=page_res))], extra_parts=extra_parts),
        file_suffix="ofd",
        keep_model_json=True,
    )


def _raster(result) -> Image.Image:
    """解码原始模型中的 PNG，以真实像素验证绘制语义。"""
    source = result.model_json.pages[0][0]["image_base64"]
    with Image.open(BytesIO(base64.b64decode(source.split(",", 1)[1]))) as image:
        return image.convert("RGB")


def _pixel(image: Image.Image, x: float, y: float) -> tuple[int, int, int]:
    """按测试页面毫米坐标读取远离抗锯齿边缘的像素。"""
    return image.getpixel((int(x / 100 * image.width), int(y / 100 * image.height)))


def test_colspan_assigns_text_and_image_to_logical_cell() -> None:
    """上行跨列单元格可容纳横跨虚拟分隔轨道的图片与文字。"""
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
    """左列缺少水平内边时恢复跨行单元格，重复边框不影响拓扑。"""
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
    """局部断边及 L 形连通区域拒绝表格候选，原文字不被吞掉。"""
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
    """细长填充矩形只产生一条线，背景和字形曲线不会污染网格。"""
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
    """真实绘制区分奇偶孔洞与非零填充，诊断通过完整 API 到达调用方。"""
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
    """二次、三次曲线及圆弧均通过真实栅格器绘制，不被降成直线。"""
    result = _parse(_path(data, attributes='Fill="true" Stroke="false"'))
    with _raster(result) as image:
        assert _pixel(image, 5, 5) == (255, 255, 255)
        assert any(pixel == (255, 0, 0) for pixel in image.getdata())
    assert not any("failed" in d.code for d in result.diagnostics)


def test_clipping_transform_alpha_and_stroke_style() -> None:
    """矩阵、裁剪与半透明颜色同时生效，描边参数能被安全序列化。"""
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
    """继承的白色填充保持白底，显式红色覆盖只影响对应对象。"""
    resource = '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016"><ofd:DrawParams><ofd:DrawParam ID="4" Fill="true" Stroke="false"><ofd:FillColor Value="255 255 255"/></ofd:DrawParam><ofd:DrawParam ID="5" Relative="4"/></ofd:DrawParams></ofd:Res>'
    white = '<ofd:PathObject ID="10" Boundary="0 0 100 100" DrawParam="5"><ofd:AbbreviatedData>M 0 0 L 100 0 L 100 100 L 0 100 C</ofd:AbbreviatedData></ofd:PathObject>'
    kwargs = {"extra_parts": {"Doc_0/Pages/Page_0/PageRes.xml": resource}, "page_res": "PageRes.xml"}
    assert _parse(white, **kwargs).middle_json.pages[0].blocks == []
    result = _parse(white + _path("M 20 20 L 40 20 L 40 40 L 20 40 C", attributes='DrawParam="5"'), **kwargs)
    with _raster(result) as image:
        assert _pixel(image, 30, 30) == (255, 0, 0)
        assert _pixel(image, 10, 10) == (255, 255, 255)


def test_mixed_pages_do_not_duplicate_native_text() -> None:
    """含原生文本时不生成整页图片，不把原生内容重复展示。"""
    result = _parse(
        _path("M 20 20 L 40 20 L 40 40 L 20 40 C", attributes='Fill="true" Stroke="false"')
        + text_object(10, "正常正文", boundary="10 60 50 10", delta_x="5 5 5")
    )
    assert all(block.type != "image" for block in result.middle_json.pages[0].blocks)
    assert not result.diagnostics


@pytest.mark.parametrize("data", ["M 10 10 Z", "M 10 10 Q", "M 10 10 A 2 2 0 7 0 20 20"])
def test_unsupported_paths_report_diagnostics(data: str) -> None:
    """未知或不完整路径不伪装成正常空页，也不生成残缺图片。"""
    result = _parse(_path(data, attributes='Fill="true" Stroke="false"'))
    assert not result.middle_json.pages[0].blocks
    assert any(d.code == "ofd_vector_unsupported" for d in result.diagnostics)


def test_generated_asset_and_path_budgets_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """生成 PNG 与输入路径的预算超限均抛出资源错误，而非被降级成空结果。"""
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
    """图像受像素上限约束，结果包恢复后 HTML 图片仍能解码。"""
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
    """传入已解析命令时，表格检测不会重复消耗路径 token 预算。"""
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
    """在 CI 的全部系统与 Python 版本上验证二进制依赖导入及最小绘制。"""
    import resvg_py

    payload = resvg_py.svg_to_bytes(
        svg_string='<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><path d="M 0 0 L 8 0 L 8 8 Z" fill="red"/></svg>',
        skip_system_fonts=True,
    )
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (8, 8)
        assert image.convert("RGB").getpixel((6, 2)) == (255, 0, 0)


def test_pageblock_transform_without_boundary_and_mixed_image_guard() -> None:
    """无 Boundary 的页面组仍应用 CTM，存在图片对象时不生成重复整页回退。"""
    path = _path("M 0 0 L 20 0 L 20 20 L 0 20 C", attributes='Fill="true" Stroke="false"')
    result = _parse(f'<ofd:PageBlock CTM="1 0 0 1 20 20">{path}</ofd:PageBlock>')
    with _raster(result) as image:
        assert _pixel(image, 30, 30) == (255, 0, 0)
        assert _pixel(image, 10, 10) == (255, 255, 255)
    result = _parse(path + '<ofd:ImageObject ID="11" Boundary="40 40 20 20"/>')
    assert not any(d.code == "ofd_vector_rasterized" for d in result.diagnostics)


def test_unsupported_paint_is_reported_and_valid_color_replaces_it() -> None:
    """渐变不被伪装成纯色，子对象完整颜色覆盖时不残留父级不支持标记。"""
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
    """父组裁剪与子对象平移共同生效，不把祖先裁剪重复乘以子对象矩阵。"""
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
    """本地语料存在时固定真实样本的逻辑单元格及路径页回退结果。"""
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
