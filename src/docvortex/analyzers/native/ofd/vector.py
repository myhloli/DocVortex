"""将受限的 OFD 路径场景直接绘制为 PNG，不调用 PDF 或 OCR。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree as ET

from lxml import etree
from PIL import Image, ImageChops

from ....foundation._image_payload import MAX_DECODED_RASTER_DIMENSION, MAX_DECODED_RASTER_PIXELS
from ....schema import BBox
from .constants import MAX_ENTRY_BYTES
from .errors import OfdResourceLimitError
from .geometry import Affine, bbox_intersection, parse_affine, parse_numbers, parse_st_box, transform_bbox
from .models import OfdPageScene
from .package import OfdPackage, element_text, first_child, local_name
from .path import OfdPathBudget, PathCommand, parse_path_commands


class UnsupportedVector(ValueError):
    """表示无法忠实绘制的路径或绘制参数，必须向调用方报告。"""


@dataclass(frozen=True, slots=True)
class ClipPath:
    """保留一个裁剪区域内的数值路径和对象坐标系变换。"""

    commands: tuple[PathCommand, ...]
    transform: Affine
    rule: str


@dataclass(frozen=True, slots=True)
class VectorPath:
    """保留完整路径、绘制上下文以及各裁剪区的区域并集。"""

    commands: tuple[PathCommand, ...]
    transform: Affine
    boundary_clip: BBox
    style: dict[str, str]
    clips: tuple[tuple[ClipPath, ...], ...]


def _number(value: str | None, default: float) -> float:
    """读取有限绘制数值，非法值明确报告而不交给栅格器猜测。"""
    result = default if value is None else float(value)
    if not math.isfinite(result):
        raise UnsupportedVector("non-finite drawing parameter")
    return result


def _affine(value: str | None) -> Affine:
    """校验可选变换矩阵，避免非法 CTM 被静默替换成单位矩阵。"""
    if value is not None and parse_numbers(value, expected=6) is None:
        raise UnsupportedVector("invalid affine matrix")
    return parse_affine(value)


def _matrix(transform: Affine) -> str:
    """只使用已验证的数值生成 SVG 仿射矩阵。"""
    return (
        "matrix("
        + " ".join(
            format(value, ".12g") for value in (transform.a, transform.b, transform.c, transform.d, transform.e, transform.f)
        )
        + ")"
    )


def _data(commands: tuple[PathCommand, ...]) -> str:
    """逐命令映射 OFD 到 SVG，避免紧缩命令与 SVG 同名字母含义冲突。"""
    names = {"S": "M", "M": "M", "L": "L", "CM": "L", "Q": "Q", "B": "C", "A": "A", "C": "Z"}
    return " ".join(names[operator] + " ".join(format(value, ".12g") for value in values) for operator, values in commands)


def _rule(value: str | None) -> str:
    """将 OFD 的非零与奇偶填充规则映射为 SVG 标准值。"""
    if value in (None, "NonZero"):
        return "nonzero"
    if value == "Even-Odd":
        return "evenodd"
    raise UnsupportedVector(f"unsupported fill rule: {value}")


def build_vector_path(
    element: etree._Element,
    *,
    parent_transform: Affine,
    parent_clip: BBox,
    style: dict[str, str],
    budget: OfdPathBudget,
    parent_clips: tuple[tuple[ClipPath, ...], ...] = (),
) -> VectorPath | None:
    """解析一次完整路径及裁剪，供页面绘制与表格边框分类共同使用。"""
    if style.get("Visible", "true").casefold() in {"false", "0"} or _number(style.get("Alpha"), 255) == 0:
        return None
    boundary = parse_st_box(element.get("Boundary"))
    if boundary is None:
        raise UnsupportedVector("invalid path boundary")
    page_box = transform_bbox(boundary, parent_transform)
    clip = bbox_intersection(page_box, parent_clip) if page_box else None
    if clip is None:
        return None
    transform = parent_transform.compose(Affine.translation(boundary[0], boundary[1])).compose(_affine(element.get("CTM")))
    source = element_text(first_child(element, "AbbreviatedData"))
    commands = parse_path_commands(source, budget)
    if not commands or commands[0][0] not in {"S", "M"}:
        raise UnsupportedVector("invalid or unsupported path commands")
    clips = parent_clips + parse_clip_groups(element, transform, budget)
    return VectorPath(commands, transform, clip, style, clips)


def parse_clip_groups(
    element: etree._Element, base_transform: Affine, budget: OfdPathBudget
) -> tuple[tuple[ClipPath, ...], ...]:
    """将对象或祖先组的裁剪区域转换到页面坐标，同组取并集、多组取交集。"""
    clips = []
    clips_element = first_child(element, "Clips")
    for clip_element in clips_element if clips_element is not None else []:
        areas = []
        for area in clip_element:
            if local_name(area.tag) != "Area":
                continue
            objects = [child for child in area if isinstance(child.tag, str)]
            if len(objects) != 1 or local_name(objects[0].tag) != "Path":
                raise UnsupportedVector("only path clip regions are supported")
            path = objects[0]
            if path.get("Stroke", "true").casefold() not in {"false", "0"} and path.get("Fill", "false").casefold() not in {
                "true",
                "1",
            }:
                raise UnsupportedVector("stroke-only clip region is unsupported")
            clip_commands = parse_path_commands(element_text(first_child(path, "AbbreviatedData")), budget)
            if not clip_commands or clip_commands[0][0] not in {"M", "S"}:
                raise UnsupportedVector("invalid clip commands")
            area_transform = _affine(area.get("CTM"))
            path_boundary = parse_st_box(path.get("Boundary"))
            if path_boundary:
                area_transform = area_transform.compose(Affine.translation(path_boundary[0], path_boundary[1]))
            area_transform = area_transform.compose(_affine(path.get("CTM")))
            areas.append(ClipPath(clip_commands, base_transform.compose(area_transform), _rule(path.get("Rule"))))
        if not areas:
            raise UnsupportedVector("empty clip region")
        clips.append(tuple(areas))
    return tuple(clips)


def _color(style: dict[str, str], prefix: str) -> tuple[str, str]:
    """读取 Gray、RGB 或 CMYK 直接颜色；未解析的色空间和渐变明确降级。"""
    if prefix + ".Unsupported" in style or prefix + ".ColorSpace" in style:
        raise UnsupportedVector("unsupported color resource or paint")
    values = [_number(value, 0) for value in style.get(prefix + ".Value", "0 0 0").split()]
    if any(not 0 <= value <= 255 for value in values):
        raise UnsupportedVector("color component outside byte range")
    if len(values) == 1:
        values *= 3
    elif len(values) == 4:
        c, m, y, k = [value / 255 for value in values]
        values = [255 * (1 - value) * (1 - k) for value in (c, m, y)]
    if len(values) != 3:
        raise UnsupportedVector("unsupported color components")
    alpha = _number(style.get(prefix + ".Alpha"), 255)
    if not 0 <= alpha <= 255:
        raise UnsupportedVector("color alpha outside byte range")
    return "rgb(" + ",".join(str(round(value)) for value in values) + ")", str(alpha / 255)


def _paint(style: dict[str, str]) -> dict[str, str]:
    """生成受控的填充和描边属性，保留线型与透明度。"""
    alpha = _number(style.get("Alpha"), 255)
    if not 0 <= alpha <= 255:
        raise UnsupportedVector("alpha outside byte range")
    result = {"fill": "none", "stroke": "none", "opacity": str(alpha / 255), "fill-rule": _rule(style.get("Rule"))}
    for prefix, name, default in [("FillColor", "fill", "false"), ("StrokeColor", "stroke", "true")]:
        if style.get(name.capitalize(), default).casefold() in {"true", "1"}:
            color, opacity = _color(style, prefix)
            result.update({name: color, name + "-opacity": opacity})
    width = _number(style.get("LineWidth"), 0.353)
    if width < 0:
        raise UnsupportedVector("negative stroke width")
    result["stroke-width"] = str(width)
    for source, target, choices, default in [
        ("Cap", "stroke-linecap", {"Butt": "butt", "Round": "round", "Square": "square"}, "Butt"),
        ("Join", "stroke-linejoin", {"Miter": "miter", "Round": "round", "Bevel": "bevel"}, "Miter"),
    ]:
        value = style.get(source, default)
        if value not in choices:
            raise UnsupportedVector("unsupported stroke style")
        result[target] = choices[value]
    limit = _number(style.get("MiterLimit"), 3.528)
    if limit < 1:
        raise UnsupportedVector("invalid miter limit")
    result["stroke-miterlimit"] = str(limit)
    if "DashPattern" in style:
        if len(style["DashPattern"]) > 4096:
            raise OfdResourceLimitError("OFD dash pattern exceeds drawing parameter budget")
        dash = [_number(value, 0) for value in style["DashPattern"].split()]
        if not dash or min(dash) < 0 or not any(dash):
            raise UnsupportedVector("invalid dash pattern")
        result["stroke-dasharray"] = " ".join(str(value) for value in dash)
        result["stroke-dashoffset"] = str(_number(style.get("DashOffset"), 0))
    return result


def render_vector_page(scene: OfdPageScene, package: OfdPackage) -> bytes | None:
    """按页受限绘制路径，完全白色结果保留为空页，其余返回 PNG 字节。"""
    import resvg_py

    box = scene.physical_box
    width, height = box[2] - box[0], box[3] - box[1]
    scale = min(
        200 / 25.4,
        MAX_DECODED_RASTER_DIMENSION / width,
        MAX_DECODED_RASTER_DIMENSION / height,
        math.sqrt(MAX_DECODED_RASTER_PIXELS / width / height),
    )
    pixels = max(1, int(width * scale)), max(1, int(height * scale))
    if scale < 200 / 25.4:
        scene.diagnostics.append(
            {"code": "ofd_vector_downscaled", "message": "Vector page was downscaled to the raster size limit"}
        )
    svg = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "width": str(pixels[0]),
            "height": str(pixels[1]),
            "viewBox": " ".join(str(value) for value in (box[0], box[1], width, height)),
        },
    )
    definitions = ET.SubElement(svg, "defs")
    clip_ids: dict[int, str] = {}
    for index, path in enumerate(scene.vector_paths):
        identifier = f"boundary{index}"
        boundary = ET.SubElement(definitions, "clipPath", {"id": identifier, "clipPathUnits": "userSpaceOnUse"})
        x0, y0, x1, y1 = path.boundary_clip
        ET.SubElement(boundary, "rect", {"x": str(x0), "y": str(y0), "width": str(x1 - x0), "height": str(y1 - y0)})
        outer = ET.SubElement(svg, "g", {"clip-path": f"url(#{identifier})"})
        parent = outer
        for areas in path.clips:
            # 祖先裁剪按对象身份复用，避免同一大轮廓随每个子路径重复展开。
            clip_id = clip_ids.get(id(areas))
            if clip_id is None:
                clip_id = f"clip{len(clip_ids)}"
                clip_ids[id(areas)] = clip_id
                definition = ET.SubElement(definitions, "clipPath", {"id": clip_id, "clipPathUnits": "userSpaceOnUse"})
                for area in areas:
                    ET.SubElement(
                        definition,
                        "path",
                        {"d": _data(area.commands), "transform": _matrix(area.transform), "clip-rule": area.rule},
                    )
            parent = ET.SubElement(parent, "g", {"clip-path": f"url(#{clip_id})"})
        ET.SubElement(parent, "path", {"d": _data(path.commands), "transform": _matrix(path.transform), **_paint(path.style)})
    markup = ET.tostring(svg, encoding="unicode")
    if len(markup.encode("utf-8")) > MAX_ENTRY_BYTES:
        raise OfdResourceLimitError("OFD generated SVG exceeds XML entry budget")
    payload = resvg_py.svg_to_bytes(svg_string=markup, skip_system_fonts=True, background="white")
    package.charge_generated_asset(len(payload))
    with Image.open(BytesIO(payload)) as image:
        rgb = image.convert("RGB")
        try:
            with Image.new("RGB", rgb.size, "white") as background:
                with ImageChops.difference(rgb, background) as difference:
                    blank = difference.getbbox() is None
        finally:
            rgb.close()
    return None if blank else payload
