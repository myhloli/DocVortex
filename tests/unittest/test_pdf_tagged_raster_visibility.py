"""用带结构树的真实 PDF 验证前画位图裁图的最终像素。"""

from io import BytesIO

import numpy as np
from PIL import Image
import pytest
from pypdf.generic import DecodedStreamObject, NameObject, NumberObject

from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary
from docvortex.api import parse, render

_SHELL = (225, 34, 29)
_SCREEN = (11, 53, 228)
_BANNER = (26, 197, 49)
_TEXT = "Independent native text"


def _fixture(case, offset, *, anonymous=False):
    """以真实图像资源、裁剪路径和后画矢量构造前后层，保留两份独立 Alt。"""
    builder = TaggedPdfBuilder()
    page = builder.pages[0]
    commands = builder.commands[0]
    x0, y0 = 45 + offset, 60 + offset
    lower = (x0, y0, 160, 200)
    upper = (x0 + 15, y0 + 20, 130, 155)
    if case == "reverse":
        upper = (x0 - 12, y0 + 20, 130, 155)
    if case.startswith("covered"):
        upper = (x0 - 4, y0 - 4, 168, 208)
    resources = pdf_dictionary()
    page["/Resources"][NameObject("/XObject")] = resources
    nodes = {}
    for name in ("lower", "upper") if case != "reverse" else ("upper", "lower"):
        is_upper = name == "upper"
        rgb, rect = (_SCREEN, upper) if is_upper else (_SHELL, lower)
        stream = DecodedStreamObject()
        stream.set_data(bytes(rgb * 36))
        stream.update(
            pdf_dictionary(
                Type=NameObject("/XObject"),
                Subtype=NameObject("/Image"),
                ColorSpace=NameObject("/DeviceRGB"),
                Width=NumberObject(6),
                Height=NumberObject(6),
                BitsPerComponent=NumberObject(8),
            )
        )
        resources[NameObject(f"/{name}")] = builder.writer._add_object(stream)
        x, y, w, h = rect
        mcid = builder.next_mcid[0]
        builder.next_mcid[0] += 1
        clip = ""
        if is_upper and case in {"hole", "covered-hole"}:
            clip = f"{x} {y} {w} {h} re {x + 38} {y + 34} 45 37 re W* n "
        operator = f"/{name} Do" if not (is_upper and case == "vector") else "0.043 0.208 0.894 rg 0 0 1 1 re f"
        commands.extend(f"/Figure <</MCID {mcid}>> BDC q {clip}{w} 0 0 {h} {x} {y} cm {operator} Q EMC\n".encode())
        nodes[name] = builder.node(
            "Figure",
            [pdf_dictionary(Type=NameObject("/MCR"), Pg=page.indirect_reference, MCID=NumberObject(mcid))],
            alt=None if anonymous else f"{name} figure",
        )
    if case in ("plain", "hole"):
        banner_mcid = builder.next_mcid[0]
        builder.next_mcid[0] += 1
        commands.extend(
            f"/Figure <</MCID {banner_mcid}>> BDC q 0.102 0.773 0.192 rg {x0 + 25} {y0 + 142} 108 20 re f Q EMC\n".encode()
        )
        nodes["banner"] = builder.node(
            "Figure",
            [pdf_dictionary(Type=NameObject("/MCR"), Pg=page.indirect_reference, MCID=NumberObject(banner_mcid))],
            alt=None if anonymous else "banner figure",
        )
    nodes["text"] = builder.node("P", [builder.text(_TEXT, x=x0 + 23, y=y0 + 103)])
    if case == "covered-independent":
        return builder.finish([builder.node("Sect", [node]) for node in nodes.values()])
    return builder.finish([builder.node("Sect", list(nodes.values()))])


def _bodies(result):
    """依据结构 Alt 到实际公共图像映射，检查源成员仍然在同一结构节点。"""
    extension = result.middle_json.extensions["docvortex_pdf_structure"]
    paths = [(tuple(item["bbox"]), item["_pdf_structure"]) for item in result.model_json.pages[0] if item["type"] == "image"]
    output = {}
    for block in result.middle_json.pages[0].blocks:
        if block.type != "image":
            continue
        (marker,) = [marker for bbox, marker in paths if bbox == tuple(block.bbox)]
        alt = extension["nodes"][marker["node_id"]]["alt"]
        (body,) = block.content
        with Image.open(BytesIO(result.assets[body.image_path])) as image:
            output[alt] = np.asarray(image.convert("RGB")).copy()
        assert len(marker["members"]) == 1
    return output


@pytest.mark.parametrize("offset", [0, 19])
@pytest.mark.parametrize("case", ["plain", "hole"])
def test_visible_shell_includes_final_screen_and_banner(case, offset):
    """被遮住的旧屏以实际最终图层替换，外壳和真正透明孔原样保留。"""
    result = parse(_fixture(case, offset), parse_mode="txt", keep_model_json=True)
    bodies = _bodies(result)
    assert set(bodies) == {"lower figure", "upper figure", "banner figure"}
    pixels = bodies["lower figure"]
    pixel_count = pixels.shape[0] * pixels.shape[1]
    assert np.count_nonzero(np.all(pixels == _SHELL, axis=-1)) > pixel_count * 0.25
    assert np.count_nonzero(np.all(pixels == _SCREEN, axis=-1)) > pixel_count * 0.40
    assert np.count_nonzero(np.all(np.abs(pixels.astype(int) - _BANNER) <= 1, axis=-1)) > pixel_count * 0.05
    assert not np.any(pixels.max(axis=-1) < 100)
    assert render(result.middle_json, "markdown", assets=result.assets).content.decode().count(_TEXT) == 1
    height, width = pixels.shape[:2]
    center_of_hole = pixels[round(height * 0.62), round(width * 0.46)]
    assert tuple(center_of_hole) == (_SHELL if case == "hole" else _SCREEN)


@pytest.mark.parametrize("case", ["reverse", "vector", "covered-independent", "covered-hole"])
def test_no_patch_without_partial_later_raster_coverage(case):
    """前画内图、不透明矢量和全覆盖独立 Figure 均保持各自结构与裁片。"""
    result = parse(_fixture(case, 0), parse_mode="txt", keep_model_json=True)
    bodies = _bodies(result)
    assert set(bodies) == {"lower figure", "upper figure"}
    assert np.all(bodies["lower figure"][3:-3, 3:-3] == _SHELL)


def test_anonymous_components_share_physical_owner():
    """同父无独立语义的上屏与前景，经像素证明后保留节点和成员但共用一张成品。"""
    result = parse(_fixture("plain", 19, anonymous=True), parse_mode="txt", keep_model_json=True)
    (image,) = [block for block in result.model_json.pages[0] if block["type"] == "image"]
    marker = image["_pdf_structure"]
    assert len(marker["component_node_ids"]) == 2
    assert len(marker["members"]) == 3
    extension = result.middle_json.extensions["docvortex_pdf_structure"]
    for ident in marker["component_node_ids"]:
        assert extension["nodes"][ident]["role"] == "Figure"
        assert extension["blocks"][ident] == extension["blocks"][marker["node_id"]]
    assert len(result.assets) == 1
    body = next(block.content[0] for block in result.middle_json.pages[0].blocks if block.type == "image")
    with Image.open(BytesIO(result.assets[body.image_path])) as png:
        pixels = np.asarray(png.convert("RGB"))
    assert np.any(np.all(pixels == _SCREEN, axis=-1))
    assert np.any(np.all(pixels == _SHELL, axis=-1))
    assert np.any(np.all(np.abs(pixels.astype(int) - _BANNER) <= 1, axis=-1))


def test_anonymous_clip_hole_preserves_component_ownership():
    """真实开孔及半透明边缘仍有前层贡献，不能把两个结构节点压成一张图。"""
    result = parse(_fixture("hole", 19, anonymous=True), parse_mode="txt", keep_model_json=True)
    images = [block["_pdf_structure"] for block in result.model_json.pages[0] if block["type"] == "image"]
    assert len(images) == len(result.assets) == 3
    assert all(len(marker["members"]) == 1 and "component_node_ids" not in marker for marker in images)


def test_fully_hidden_sibling_keeps_alt_and_source_without_image():
    """完全被遮的同父 Figure 保留原 Alt 和 MCID，公开图像只引用有实墨的后层。"""
    result = parse(_fixture("covered", 19), parse_mode="txt", keep_model_json=True)
    images = _bodies(result)
    assert set(images) == {"upper figure"}
    assert np.all(images["upper figure"][3:-3, 3:-3] == _SCREEN)
    extension = result.middle_json.extensions["docvortex_pdf_structure"]
    lower = next(identity for identity, node in extension["nodes"].items() if node.get("alt") == "lower figure")
    assert not extension["blocks"].get(lower)
    (source,) = extension["nodes"][lower]["children"]
    assert source["mcid"] == 0
    assert any(member["mcid"] == source["mcid"] for member in extension["members"]["0"])
    assert render(result.middle_json, "markdown", assets=result.assets).content.decode().count(_TEXT) == 1
