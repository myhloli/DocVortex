"""验证 PDF 原始块布局的公共接口、页面几何和可移植渲染。"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO

import pytest
from click.testing import CliRunner
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader

from docvortex import load_bundle, parse, render_artifact
from docvortex.cli import main
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.layout import attach_layout_image_rotations, extract_layout_geometry
from docvortex.render import PdfLayout, PdfRenderOptions, RenderFormat, render, render_pdf
from docvortex.render._internal.pdf.formula import FormulaRenderer, PdfFormulaError
from docvortex.schema import MiddleJson, PageInfo, Producer


def _middle(pages: list[dict], *, suffix: str = "pdf", sizes: list[tuple[int, float, float]] | None = None) -> MiddleJson:
    """构造带显式源页几何的严格文档，保留非连续页号以测试映射。"""
    return MiddleJson(
        pages=[PageInfo.model_validate(page) for page in pages],
        is_full_document=False,
        metadata={"file_suffix": suffix, "producer": Producer(name="test", version="1")},
        extensions={
            "docvortex_layout": {
                "version": 1,
                "pages": [
                    {"page_idx": idx, "width_pt": width, "height_pt": height}
                    for idx, width, height in (sizes or [(page["page_idx"], 400, 600) for page in pages])
                ],
            }
        },
    )


def _text(text: str, *, index: int = 0, bbox: tuple = (0.1, 0.1, 0.45, 0.3), **kwargs) -> dict:
    """生成可覆盖不同块类型和续接标记的文本块。"""
    return {"type": "text", "index": index, "bbox": bbox, "content": [{"type": "text", "content": text}], **kwargs}


def _reader(payload: bytes) -> PdfReader:
    """从导出 bytes 检查真实 PDF 的页面和文字。"""
    return PdfReader(BytesIO(payload))


def _png_uri() -> str:
    """创建宽高比与测试区域一致的图片以验证绝对定位。"""
    output = BytesIO()
    Image.new("RGB", (100, 100), "red").save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def _source_pdf() -> bytes:
    """生成包含旋转、非 A4、空白页及横向页的真实源 PDF。"""
    output = BytesIO()
    canvas = Canvas(output, pagesize=(400, 600))
    canvas.drawString(40, 500, "FIRST PAGE")
    canvas.showPage()
    canvas.setPageSize((320, 480))
    canvas.showPage()
    canvas.setPageSize((600, 400))
    canvas.drawString(40, 300, "THIRD PAGE")
    canvas.showPage()
    canvas.save()
    writer = PdfWriter(clone_from=BytesIO(output.getvalue()))
    writer.pages[1].rotate(90)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def test_original_preserves_pages_auxiliaries_continuations_and_input() -> None:
    """辅助文字和续段留在原页，空白页及不同页面尺寸均不被折叠。"""
    middle = _middle(
        [
            {"page_idx": 1, "blocks": [_text("HEADER", type="header"), _text("inter-", index=1, bbox=(0.1, 0.4, 0.5, 0.5))]},
            {"page_idx": 2, "blocks": [_text("national", continues_prev=True)]},
            {"page_idx": 6, "blocks": []},
        ],
        sizes=[(1, 400, 600), (2, 320, 480), (6, 600, 400)],
    )
    before = deepcopy(middle)
    payload = render_pdf(middle)
    reader = _reader(payload)
    assert payload == render_pdf(middle, layout=PdfLayout.ORIGINAL)
    assert len(reader.pages) == 3
    assert [tuple(page.mediabox)[2:] for page in reader.pages] == [(400, 600), (320, 480), (600, 400)]
    assert "HEADER" in reader.pages[0].extract_text()
    assert "inter-" in reader.pages[0].extract_text()
    assert "national" not in reader.pages[0].extract_text()
    assert "national" in reader.pages[1].extract_text()
    assert reader.pages[2].extract_text() == ""
    assert middle == before
    assert len(_reader(render_pdf(middle, layout=PdfLayout.REFLOW)).pages) == 1


def test_text_and_image_positions_match_source_boxes() -> None:
    """用 PDF 原生对象检查文字列位置和图片四边，容差不超过 0.5 pt。"""
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    _text("LEFT"),
                    _text("RIGHT", index=1, bbox=(0.6, 0.1, 0.95, 0.3)),
                    {
                        "type": "image",
                        "index": 2,
                        "bbox": (0.1, 0.5, 0.6, 5 / 6),
                        "content": [
                            {
                                "type": "image_body",
                                "index": 2,
                                "bbox": (0.1, 0.5, 0.6, 5 / 6),
                                "content": "",
                                "image_base64": _png_uri(),
                            },
                        ],
                    },
                ],
            }
        ]
    )
    payload = render_pdf(middle)
    positions = {}

    def visit(text, cm, tm, font, size) -> None:
        """记录 PDF 提取器报告的实际文字坐标变换。"""
        if text.strip():
            positions[text.strip()] = (tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5])

    _reader(payload).pages[0].extract_text(visitor_text=visit)
    assert positions["LEFT"][0] == pytest.approx(40, abs=0.5)
    assert positions["RIGHT"][0] == pytest.approx(240, abs=0.5)
    assert 420 <= positions["LEFT"][1] <= 540
    with PDFDocument(payload) as document:
        image = document.get_page_image_infos(0)[0]
        assert image.bbox == pytest.approx((40, 300, 240, 500), abs=0.5)


@pytest.mark.parametrize("damage", ["missing", "partial", "version", "duplicate", "negative", "boolean", "bbox"])
def test_auto_falls_back_for_incomplete_geometry_and_original_rejects(damage: str) -> None:
    """旧数据和非法几何在 AUTO 整体回退，显式 ORIGINAL 在导出前失败。"""
    middle = _middle([{"page_idx": 3, "blocks": [_text("body")]}])
    extension = middle.extensions["docvortex_layout"]
    if damage == "missing":
        middle.extensions.clear()
    elif damage == "partial":
        extension["pages"] = []
    elif damage == "version":
        extension["version"] = 2
    elif damage == "duplicate":
        extension["pages"] *= 2
    elif damage == "negative":
        extension["pages"][0]["width_pt"] = -1
    elif damage == "bbox":
        middle.pages[0].blocks[0].bbox = None
    else:
        extension["pages"][0]["height_pt"] = True
    artifact = render_artifact(middle, "pdf")
    assert "body" in _reader(artifact.content).pages[0].extract_text()
    assert [item.code for item in artifact.diagnostics] == ["pdf_layout_reflow_fallback"]
    with pytest.raises(ValueError):
        render_pdf(middle, layout=PdfLayout.ORIGINAL)


@pytest.mark.parametrize("suffix", ["ofd", "docx", "html"])
def test_non_pdf_sources_keep_reflow_and_reject_original(suffix: str) -> None:
    """OFD 和流式来源维持重排，即使携带几何扩展也不启用首版还原。"""
    middle = _middle([{"page_idx": 0, "blocks": [_text("body")]}], suffix=suffix)
    artifact = render_artifact(middle, "pdf")
    assert artifact.content == render_pdf(middle, layout=PdfLayout.REFLOW)
    assert not artifact.diagnostics
    with pytest.raises(ValueError, match="source format"):
        render_pdf(middle, layout=PdfLayout.ORIGINAL)


def test_overflow_preserves_all_text_and_reports_small_type() -> None:
    """极小框仍包含末尾文字，并输出低字号与缩放诊断。"""
    middle = _middle([{"page_idx": 0, "blocks": [_text("word " * 100 + "ENDMARK", bbox=(0.1, 0.1, 0.2, 0.12))]}])
    artifact = render_artifact(middle, "pdf")
    text = _reader(artifact.content).pages[0].extract_text()
    assert "ENDMARK" in "".join(text.split())
    assert text.count("word") == 100
    assert {item.code for item in artifact.diagnostics} >= {"pdf_layout_small_text", "pdf_layout_scaled"}
    assert all(item.page_index == 0 for item in artifact.diagnostics)


def test_structured_table_replaces_region_image_without_duplicate_caption() -> None:
    """表格优先结构文字，不叠加整表图片，表题仍独立定位且只出现一次。"""
    bbox = (0.1, 0.1, 0.6, 0.5)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "table",
                        "index": 0,
                        "bbox": bbox,
                        "content": [
                            {
                                "type": "table_body",
                                "index": 0,
                                "bbox": bbox,
                                "content": "<table><tr><td>HIDDEN CELL</td></tr></table>",
                                "image_base64": _png_uri(),
                            },
                            _text("CAPTION", type="table_caption", index=1, bbox=(0.1, 0.55, 0.6, 0.6)),
                        ],
                    },
                ],
            }
        ]
    )
    reader = _reader(render_pdf(middle))
    assert reader.pages[0].extract_text().count("CAPTION") == 1
    assert "HIDDEN CELL" in reader.pages[0].extract_text()
    assert len(reader.pages[0].images) == 0


def test_table_without_image_uses_structured_content() -> None:
    """区域图缺失时正常输出结构文字，不把 HTML 渲染报告为降级。"""
    bbox = (0.1, 0.1, 0.9, 0.8)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "table",
                        "index": 0,
                        "bbox": bbox,
                        "content": [
                            {
                                "type": "table_body",
                                "index": 0,
                                "bbox": bbox,
                                "content": "<table><tr><td>CELL</td></tr></table>",
                            },
                        ],
                    },
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    assert "CELL" in _reader(artifact.content).pages[0].extract_text()
    assert "pdf_table_layout" in {item.code for item in artifact.diagnostics}
    assert "pdf_table_fallback" not in {item.code for item in artifact.diagnostics}


def test_missing_child_geometry_uses_parent_group() -> None:
    """缺少图注坐标的组合整体排入父框，素材与文字均保留。"""
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "image",
                        "index": 0,
                        "bbox": (0.1, 0.1, 0.9, 0.9),
                        "content": [
                            {"type": "image_body", "index": 0, "content": "", "image_base64": _png_uri()},
                            _text("CAPTION", type="image_caption", index=1, bbox=None),
                        ],
                    },
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    assert "CAPTION" in _reader(artifact.content).pages[0].extract_text()
    assert "pdf_layout_approximate" in {item.code for item in artifact.diagnostics}


def test_formula_image_fallback_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """公式矢量失败后使用已有区域图，错误可在高层产物定位。"""

    def fail_formula(*args, **kwargs):
        """模拟无法转换的公式以覆盖图片降级。"""
        raise PdfFormulaError("test unsupported formula")

    monkeypatch.setattr(FormulaRenderer, "render", fail_formula)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "equation",
                        "index": 0,
                        "bbox": (0.1, 0.1, 0.6, 0.5),
                        "content": "bad",
                        "image_base64": _png_uri(),
                    },
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    assert len(_reader(artifact.content).pages[0].images) == 1
    assert "pdf_formula_fallback" in {item.code for item in artifact.diagnostics}


def test_index_links_keep_source_page_numbers() -> None:
    """固定布局目录保留原文页码，并正确链接到后面的正文目标。"""
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "index",
                        "index": 0,
                        "bbox": (0.1, 0.1, 0.9, 0.2),
                        "content": [
                            _text("Chapter 1 ..... 42", anchor="chapter", bbox=(0.1, 0.1, 0.9, 0.2)),
                        ],
                    },
                    _text("Chapter 1", type="paragraph_title", level=2, index=1, anchor="chapter", bbox=(0.1, 0.4, 0.9, 0.5)),
                ],
            }
        ]
    )
    artifact = render_artifact(middle, "pdf")
    page = _reader(artifact.content).pages[0]
    assert "42" in page.extract_text()
    assert len(page["/Annots"]) == 1
    assert not {item.code for item in artifact.diagnostics} & {"pdf_unmatched_link", "pdf_duplicate_anchor"}


def test_pdf_parse_and_bundle_retain_selected_page_geometry(tmp_path) -> None:
    """真实解析、后处理和 bundle 往返后仍可脱离源文件导出选中页面。"""
    result = parse(_source_pdf(), file_suffix="pdf", page_range="1,3", keep_model_json=True)
    extension = result.middle_json.extensions["docvortex_layout"]
    assert extension == result.model_json.extensions["docvortex_layout"]
    assert extension["pages"] == [
        {"page_idx": 0, "width_pt": 400, "height_pt": 600},
        {"page_idx": 2, "width_pt": 600, "height_pt": 400},
    ]
    before = result.middle_json.to_dict()
    result.save_bundle(tmp_path / "bundle")
    restored = load_bundle(tmp_path / "bundle")
    payload = render_artifact(restored.middle_json, "pdf", assets=restored.assets).content
    assert len(_reader(payload).pages) == 2
    assert restored.middle_json.to_dict() == before == result.middle_json.to_dict()


def test_rotation_and_blank_page_geometry_survive_parsing() -> None:
    """旋转空白页的可见宽高只交换一次，原始分页保持完整。"""
    result = parse(_source_pdf(), file_suffix="pdf")
    reader = _reader(render_pdf(result.middle_json))
    assert len(reader.pages) == 3
    assert tuple(reader.pages[1].mediabox)[2:] == (480, 320)
    assert reader.pages[1].extract_text() == ""


def test_geometry_failure_is_nonfatal_during_analysis() -> None:
    """无法取得某页尺寸时保留其诊断，不伪造 A4 尺寸。"""

    class BrokenDocument:
        page_count = 1

        def page_size(self, page_idx):
            """模拟底层页尺寸读取失败。"""
            raise RuntimeError("broken geometry")

    geometry, diagnostics = extract_layout_geometry(BrokenDocument(), [7])
    assert geometry["pages"] == []
    assert diagnostics[0].page_index == 7


def test_layout_options_are_strict_and_unified_render_is_equivalent() -> None:
    """统一入口与直接入口使用相同布局，并拒绝字符串冒充严格枚举。"""
    middle = _middle([{"page_idx": 0, "blocks": [_text("body")]}])
    assert render(middle, RenderFormat.PDF, options=PdfRenderOptions(layout=PdfLayout.ORIGINAL)) == render_pdf(middle)
    with pytest.raises(TypeError, match="PdfLayout"):
        render_pdf(middle, layout="original")
    with pytest.raises(TypeError, match="PdfLayout"):
        PdfRenderOptions(layout="original")


def test_concurrent_diagnostics_do_not_leak() -> None:
    """同时导出旧 PDF 和新 PDF 时，回退诊断只属于对应调用。"""
    middle = _middle([{"page_idx": 0, "blocks": [_text("body")]}])
    legacy = middle.model_copy(deep=True)
    legacy.extensions.clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: render_artifact(value, "pdf"), [middle, legacy]))
    assert not results[0].diagnostics
    assert [item.code for item in results[1].diagnostics] == ["pdf_layout_reflow_fallback"]


def test_cli_pdf_layout_reaches_export(tmp_path) -> None:
    """通过真实 CLI 验证显式布局选项到页面输出的完整链路。"""
    source = tmp_path / "source.pdf"
    source.write_bytes(_source_pdf())
    output = tmp_path / "out.pdf"
    runner = CliRunner()
    result = runner.invoke(main, ["convert", str(source), "-o", str(output), "--format", "pdf", "--pdf-layout", "original"])
    assert result.exit_code == 0, result.output
    assert len(_reader(output.read_bytes()).pages) == 3
    rejected = runner.invoke(main, ["convert", str(source), "-o", str(output), "--format", "html", "--pdf-layout", "original"])
    assert rejected.exit_code != 0
    assert "requires --format pdf" in rejected.output


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_rotated_region_images_restore_source_orientation(angle: int) -> None:
    """通过双色素材的实际渲染像素验证裁图方向被正确撤销。"""
    image = Image.new("RGB", (200, 100), "red")
    image.paste("blue", (100, 0, 200, 100))
    data = BytesIO()
    image.save(data, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()
    width, height = (100, 200) if angle in (90, 270) else (200, 100)
    bbox = (0.1, 0.1, 0.1 + width / 400, 0.1 + height / 600)
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "table",
                        "index": 0,
                        "bbox": bbox,
                        "content": [
                            {"type": "table_body", "index": 0, "bbox": bbox, "content": "", "image_base64": uri},
                        ],
                    },
                ],
            }
        ]
    )
    middle.extensions["docvortex_layout"]["pages"][0]["image_rotations"] = {"0": angle}
    with PDFDocument(render_pdf(middle)) as document:
        rendered = document.render_page(0, scale=1).pil_image.convert("RGB")
        if angle in (90, 270):
            first = rendered.getpixel((40 + width // 2, 60 + height // 4))
            second = rendered.getpixel((40 + width // 2, 60 + height * 3 // 4))
        else:
            first = rendered.getpixel((40 + width // 4, 60 + height // 2))
            second = rendered.getpixel((40 + width * 3 // 4, 60 + height // 2))
    expected = [(255, 0, 0), (0, 0, 255)] if angle == 90 else [(0, 0, 255), (255, 0, 0)]
    assert [first, second] == expected


def test_crop_rotation_metadata_uses_source_page_and_block_indices() -> None:
    """裁图角度按选页后的源页号和最终 raw 块索引关联，缺失素材不声明角度。"""
    extension = {"version": 1, "pages": [{"page_idx": 7, "width_pt": 400, "height_pt": 600}]}
    pages = [
        [
            {"type": "text", "angle": 90},
            {"type": "table", "angle": 270, "image_base64": _png_uri()},
            {"type": "equation", "angle": 90},
        ]
    ]
    attach_layout_image_rotations(extension, pages, [7])
    assert extension["pages"][0]["image_rotations"] == {"1": 270}


def test_rotated_cropped_page_keeps_image_position() -> None:
    """带 CropBox 偏移的旋转页只变换一次，实际图片位置与源页一致。"""
    source_bytes = BytesIO()
    canvas = Canvas(source_bytes, pagesize=(400, 600))
    canvas.drawImage(ImageReader(Image.new("RGB", (100, 100), "red")), 100, 200, width=100, height=100)
    canvas.save()
    writer = PdfWriter(clone_from=BytesIO(source_bytes.getvalue()))
    writer.pages[0].cropbox.lower_left = (20, 30)
    writer.pages[0].cropbox.upper_right = (380, 550)
    writer.pages[0].rotate(90)
    source = BytesIO()
    writer.write(source)
    result = parse(source.getvalue(), file_suffix="pdf", parse_mode="txt")
    artifact = render_artifact(result.middle_json, "pdf", assets=result.assets)
    with PDFDocument(source.getvalue()) as original, PDFDocument(artifact.content) as rebuilt:
        assert original.page_size(0) == rebuilt.page_size(0) == (520, 360)
        assert rebuilt.get_page_image_infos(0)[0].bbox == pytest.approx(original.get_page_image_infos(0)[0].bbox, abs=0.5)


@pytest.mark.parametrize("layout", [PdfLayout.ORIGINAL, PdfLayout.REFLOW])
def test_bullet_text_is_copyable_as_unicode(layout: PdfLayout) -> None:
    """列表符号复制后仍是 Unicode bullet，避免标准字体生成 DEL 控制字符。"""
    middle = _middle([{"page_idx": 0, "blocks": [_text("• 项目 Bullet item")]}])
    text = _reader(render_pdf(middle, layout=layout)).pages[0].extract_text()
    assert "• 项目 Bullet item" in text
    assert "\x7f" not in text
