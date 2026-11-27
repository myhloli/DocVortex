"""回放真实和反例 PDF，核查结构图像与独立链接的公共输出。"""

from io import BytesIO
from pathlib import Path
import os
from unittest.mock import patch

from PIL import Image
from pypdf.generic import ArrayObject, DecodedStreamObject, NameObject, NumberObject, TextStringObject
import pytest

from docvortex import parse
from docvortex.api import render
from docvortex.analyzers.native.pdf import tagged
from docvortex.analyzers.native.pdf.graphics import _is_external_raster_link
from docvortex.analyzers.native.pdf.models import _LineItem
from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary

REAL_PDF = os.environ.get("DOCVORTEX_SANDAG_PDF")


def _linked_legend_pdf(mode, *, x, y, size, pages=1):
    """建立位图、语义 Frame、直接的错位文字和实际注解，可单独改变一个前提。"""
    page = TaggedPdfBuilder(pages)
    native_page = page.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(b"\xff\x00\x00\x00\xff\x00" * 2)
    stream.update(
        pdf_dictionary(
            Type=NameObject("/XObject"),
            Subtype=NameObject("/Image"),
            ColorSpace=NameObject("/DeviceRGB"),
            Height=NumberObject(2),
            Width=NumberObject(2),
            BitsPerComponent=NumberObject(8),
        )
    )
    native_page["/Resources"][NameObject("/XObject")] = pdf_dictionary(Im=page.writer._add_object(stream))
    page.next_mcid[0] += 1
    if mode == "source_paths":
        # 两次先绘制同一底板，作为两个源 Path 分别保护归属。
        stripe = int(size * 0.67)
        command = f"q 1 1 1 rg {x - 1} {y + size - stripe} {size + 2} {stripe} re f Q\n".encode()
        page.commands[0].extend(command * 2)
    page.commands[0].extend(f"/Figure <</MCID 0>> BDC q {size} 0 0 {size} {x} {y} cm /Im Do Q EMC\n".encode())
    if mode == "foreign_overlay":
        page.commands[0].extend(f"q 0 0 1 rg {x + 4} {y + 4} {size // 2} {size // 2} re f Q\n".encode())
    raster = pdf_dictionary(Type=NameObject("/MCR"), Pg=native_page.indirect_reference, MCID=NumberObject(0))
    figure_child = page.node("Caption", [raster]) if mode == "nested_caption" else raster
    figure = page.node("Figure", [figure_child], alt="Full color key")
    children = [figure]
    if mode != "pure":
        line_x, line_y = (x + 4, y + 10) if mode == "mixed" else (x + size + 16, y + 16)
        children.append(page.text("Other words", x=line_x, y=line_y))
    target = {"invalid": "javascript:alert(1)"}.get(mode, "https://example.com/key")
    offset = size if mode == "wrong_rect" else 0
    if mode != "no_annot":
        ref = page.writer._add_object(
            pdf_dictionary(
                Type=NameObject("/Annot"),
                Subtype=NameObject("/Link"),
                Rect=ArrayObject([NumberObject(v) for v in (x + offset, y, x + size + offset, y + size)]),
                A=pdf_dictionary(S=NameObject("/URI"), URI=TextStringObject(target)),
            )
        )
        native_page[NameObject("/Annots")] = ArrayObject([ref])
        children.append(pdf_dictionary(Type=NameObject("/OBJR"), Obj=ref, Pg=native_page.indirect_reference))
    roots = [page.node("Link", children), page.node("P", [page.text("Independent paragraph above", x=32, y=350)])]
    if mode == "interior":
        page.text("Native label", x=x + 4, y=y + 20)
    if pages > 1:
        roots.append(page.node("P", [page.text("Second physical page body", page=1, y=195)], page=1))
    return page.finish(roots)


@pytest.mark.parametrize(("x", "y", "size"), [(35, 130, 63), (72, 174, 81)])
def test_linked_raster_claims_only_coincident_figure(x, y, size):
    """在两种缩放和平移下保留整个真实位图及被错误链接的链外正文。"""
    result = parse(_linked_legend_pdf("positive", x=x, y=y, size=size), keep_model_json=True, parse_mode="txt")
    blocks = result.model_json.pages[0]
    (image,) = [b for b in blocks if b.get("_pdf_structure", {}).get("link_target")]
    assert image["type"] == "image"
    assert image["bbox"] == pytest.approx([x / 300, (400 - y - size) / 400, (x + size) / 300, (400 - y) / 400])
    assert len(image["_pdf_structure"]["members"]) == 1
    mark = image["_pdf_structure"]["node_id"]
    extension = result.middle_json.extensions["docvortex_pdf_structure"]
    assert extension["nodes"][mark]["link_target"] == "https://example.com/key"
    markdown = render(result.middle_json, "markdown", assets=result.assets).content.decode()
    assert markdown.count("Other words") == 1
    assert markdown.count("Independent paragraph above") == 1
    assert "[Other words]" not in markdown


@pytest.mark.parametrize(
    "mode", ["mixed", "pure", "invalid", "wrong_rect", "no_annot", "interior", "nested_caption", "foreign_overlay"]
)
def test_ambiguous_or_unverified_link_never_claims_raster_by_annotation(mode):
    """正确混合链接、不可信 URI 与注解、缺失或内部字形均走原路径。"""
    result = parse(_linked_legend_pdf(mode, x=35, y=130, size=65), keep_model_json=True, parse_mode="txt")
    assert not any(b.get("_pdf_structure", {}).get("link_target") for b in result.model_json.pages[0])
    with patch.object(tagged._PageAssembler, "_linked_raster", return_value=None):
        prior = parse(_linked_legend_pdf(mode, x=35, y=130, size=65), keep_model_json=True, parse_mode="txt")
    assert prior.model_json.pages == result.model_json.pages
    assert prior.to_dict() == result.to_dict()
    assert dict(prior.assets) == dict(result.assets)
    assert (
        render(result.middle_json, "markdown", assets=result.assets).content.decode().count("Independent paragraph above") == 1
    )


@pytest.mark.parametrize(
    ("bbox", "rasters", "expected"),
    [
        ((11, 71, 59, 79), [(10, 20, 100, 70)], True),
        ((11, 59, 59, 67), [(10, 20, 100, 70)], False),
        ((28, 71, 75, 79), [(10, 20, 100, 70)], False),
        ((11, 71, 59, 79), [(10, 20, 20, 70)], False),
    ],
)
def test_external_link_requires_position_and_raster_support(bbox, rasters, expected):
    """仅保护有位图证据的左下外部导航，内部信息和内部比例尺继续留在图内。"""
    line = _LineItem("Open location", bbox, 0, 42, em_height=8)
    assert _is_external_raster_link(line, (10, 20, 100, 70), rasters) == expected


@pytest.mark.skipif(not REAL_PDF, reason="set DOCVORTEX_SANDAG_PDF to the reviewed SANDAG source")
@pytest.mark.parametrize("mode", ["default", "native"])
def test_sandag_actual_members_and_exports(mode):
    """开两种公共请求的真实包，检查完整图例底端和页内独立链接。"""
    result = parse(
        Path(REAL_PDF), keep_model_json=True, **({} if mode == "default" else {"parse_mode": "txt", "pdf_structure": "auto"})
    )
    images = [b for b in result.model_json.pages[0] if b["type"] == "image"]
    (legend,) = [b for b in images if b.get("content") == "Bike Map Legend"]
    assert legend["bbox"] == pytest.approx((0.0547, 0.3643, 0.2013, 0.736), abs=0.0001)
    (map_body,) = [b for b in images if "Find address or place" in b["content"]]
    assert map_body["bbox"] == [0.217, 0.328, 0.936, 0.781]
    assert not any(0.050 < b["bbox"][0] < 0.060 and 0.36 < b["bbox"][1] and b["bbox"][3] < 0.7 for b in images)
    markdown = render(result.middle_json, "markdown", assets=result.assets).content.decode()
    actual = render(result.middle_json, "html", assets=result.assets).content.decode()
    for value in (
        "With more than 1,570 miles of bikeways",
        "Viewing the 2018 PDF Maps",
        "Neither SANDAG nor the cities and counties",
        "View Larger Map",
    ):
        assert markdown.count(value) == 1
        assert actual.count(value) == 1
    assert "[View Larger Map](https://sandag.maps.arcgis.com/" in markdown
    assert ">View Larger Map</a>" in actual
    assert markdown.index("Bike Map Legend") < markdown.index("View Larger Map") < markdown.index("Viewing the 2018 PDF Maps")
    images = [i.content[0] for i in result.middle_json.pages[0].blocks if i.type == "image"]
    (body,) = [b for b in images if b.content == "Bike Map Legend"]
    asset = result.assets[body.image_path]
    with Image.open(BytesIO(asset)) as im:
        assert im.height > 700
        pixels = im.convert("L")
        assert (
            sum(
                value < 85
                for value in pixels.crop((2, int(0.88 * im.height), im.width - 2, int(0.97 * im.height))).get_flattened_data()
            )
            > 30
        )


@pytest.mark.parametrize("mode, member_count", [("positive", 1), ("source_paths", 3)])
def test_automatic_route_and_exact_source_underlay_claim(mode, member_count):
    """显式和默认路由都保持可读，两个真正的底板原语只产生一个完整图。"""
    pdf = _linked_legend_pdf(mode, x=52, y=162, size=85)
    auto = parse(pdf, keep_model_json=True)
    native = parse(pdf, keep_model_json=True, parse_mode="txt")
    assert native.model_json.pages == auto.model_json.pages
    assert native.to_dict() == auto.to_dict()
    blocks = auto.model_json.pages[0]
    (image,) = [block for block in blocks if block["type"] == "image"]
    assert len(image["_pdf_structure"]["members"]) == member_count
    assert len(set(image["_pdf_structure"]["members"])) == member_count
    assert image["bbox"] == pytest.approx([52 / 300, 153 / 400, 137 / 300, 238 / 400])
    html = render(auto.middle_json, "html", assets=auto.assets).content.decode()
    assert html.count("Other words") == 1
    assert len(auto.assets) == 1


def test_linked_visual_can_merge_with_unpositioned_fallback():
    """没有空间排名的原生块仍使用旧的队尾顺序，图例插入不会丢块。"""
    from docvortex.analyzers.native.pdf.tagged import _ordered_merge

    visual = {"type": "image", "bbox": [0.1, 0.1, 0.3, 0.5], "_linked_raster_underlay": True}
    unpositioned = {"type": "text"}
    assert _ordered_merge([visual], [unpositioned], (400, 600)) == [visual, unpositioned]
