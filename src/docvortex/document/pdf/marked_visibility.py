"""以实际纸面绘制结果修复 Tagged Figure 局部重新露出的旧位图。"""

from collections import Counter
from itertools import islice
from math import ceil

import numpy as np
from PIL import Image
import pypdfium2.raw as raw

from ...foundation._geometry import normalize_to_int_bbox
from .native_coordinates import _get_raw_object_matrix
from .drawing_visibility import hide_drawings, render_source_patch
from .raster import DEFAULT_MAX_RENDER_EDGE, DEFAULT_PDF_IMAGE_DPI

_MAX_PROOFS = 4
_MAX_CROP_PIXELS = 2_000_000


def _axis_aligned(handle):
    """限制为可直接比较同一纸面坐标的正向顶层位图。"""
    matrix = _get_raw_object_matrix(handle)
    return matrix and matrix[0] > 0 and matrix[3] > 0 and max(abs(matrix[1]), abs(matrix[2])) <= 1e-6


def _area(box):
    """计算源位图在纸面内的外接矩形面积。"""
    return (box[2] - box[0]) * (box[3] - box[1])


def _replacement_pairs(objects, candidates):
    """由两份已独立认领的 Figure、源画序及显著内含关系提名可见性验证。"""
    images = []
    for candidate in candidates:
        if candidate["type"] != "image":
            continue
        members = candidate["_pdf_structure"]["members"]
        rasters = [objects[index] for index in members if objects[index].kind == raw.FPDF_PAGEOBJ_IMAGE]
        if len(rasters) == 1 and not rasters[0].occurrence and rasters[0].bbox and _area(rasters[0].bbox) > 0:
            images.append((rasters[0], members, candidate))

    for lower, members, candidate in images:
        box = lower.bbox
        for upper, upper_members, upper_candidate in images:
            if min(upper_members) <= max(members) or not 0.35 <= _area(upper.bbox) / _area(box) <= 0.95:
                continue
            x0, y0, x1, y1 = upper.bbox
            if min(x0 - box[0], y0 - box[1], box[2] - x1, box[3] - y1) < -0.5:
                continue
            yield lower.index, upper.index, members, upper_members, candidate, upper_candidate


def _pixel_crop(box, size, scale):
    """复用公开 bbox 的整数裁边，并换算 PDFium 需要的裁边距。"""
    width, height = [ceil(length * scale) for length in size]
    pixel_box = normalize_to_int_bbox(
        [coordinate * (width, height)[i % 2] for i, coordinate in enumerate(box)], image_size=(height, width)
    )
    if pixel_box is None or _area(pixel_box) > _MAX_CROP_PIXELS:
        return None
    x0, y0, x1, y1 = pixel_box
    # PDFium 包装器对点边距乘以比例后向上取整；半像素余量避免浮点多裁一行。
    return tuple(max(0, margin - 0.5) / scale for margin in (x0, height - y1, width - x1, y0))


def _opaque_regions(page, scale, crop, handles, index, *, solid):
    """单独回绘一个位图，获取包含真实路径裁剪与软蒙版的墨迹区域。"""
    with hide_drawings(obj for i, (obj, kind) in enumerate(handles) if i != index and kind != raw.FPDF_PAGEOBJ_FORM):
        with render_source_patch(page, scale, crop, transparent=True) as image:
            alpha = np.asarray(image)[..., 3]
            return alpha == 255 if solid else alpha != 0


def _merged_components(page, marked, handles, pair, candidates, frame_parents, actual, covered, scale, crop):
    """消融同父匿名图组，只有纸面贡献已完整包含在成品时才合并结构关联。"""
    _, _, _, upper_members, lower, upper = pair
    source_marker = lower["_pdf_structure"]
    owner = source_marker["node_id"]
    source_parent = frame_parents.get(owner)
    attached = {
        m.get("visual_parent") or m.get("inferred_visual_parent")
        for block in candidates
        if (m := block.get("_pdf_structure", {}))
    }

    def eligible(block):
        """保留具有独立链接、图注、原生文字或已合成组件的图体。"""
        marker = block.get("_pdf_structure", {})
        ident = marker.get("node_id")
        return (
            block["type"] == "image"
            and source_parent is not None
            and frame_parents.get(ident) == source_parent
            and ident not in attached
            and not marker.get("link_target")
            and not marker.get("component_node_id")
            and bool(marker.get("members"))
            and all(marked.objects[index].kind != raw.FPDF_PAGEOBJ_TEXT for index in marker["members"])
        )

    if not eligible(lower) or not eligible(upper):
        return []
    sx0, sy0, sx1, sy1 = upper["bbox"]
    if any(
        block is not lower
        and block is not upper
        and block["type"] in {"image", "chart", "table", "equation"}
        and block.get("bbox")
        and not eligible(block)
        and (
            max(0, min(sx1, block["bbox"][2]) - max(sx0, block["bbox"][0]))
            * max(0, min(sy1, block["bbox"][3]) - max(sy0, block["bbox"][1]))
            >= 0.75 * _area(upper["bbox"])
        )
        for block in candidates
    ):
        return []

    def contained(box):
        """只比较上屏范围内的图层，容许外接框的亚像素量化。"""
        x0, y0, x1, y1 = box
        width, height = marked.size
        return min((x0 - sx0) * width, (y0 - sy0) * height, (sx1 - x1) * width, (sy1 - y1) * height) >= -0.5

    def absorbed(block):
        """关掉组件所有叶子，确保任何真实改变都在覆盖区域内。"""
        visible_members = (handles[i][0] for i in block["_pdf_structure"]["members"] if handles[i][1] != raw.FPDF_PAGEOBJ_FORM)
        with hide_drawings(visible_members), render_source_patch(page, scale, crop) as absent:
            delta = np.any(np.asarray(actual) != np.asarray(absent), axis=-1)
        return delta.any() and not np.any(delta & ~covered)

    if not absorbed(upper):
        return []
    components = [upper]
    later = (
        block
        for block in candidates
        if block is not lower
        and block is not upper
        and eligible(block)
        and min(block["_pdf_structure"]["members"]) > max(upper_members)
        and contained(block["bbox"])
        and all(marked.objects[i].kind != raw.FPDF_PAGEOBJ_IMAGE for i in block["_pdf_structure"]["members"])
    )
    for block in islice(later, 4):
        if absorbed(block):
            components.append(block)
    return components


def _visible_patch(page, marked, handles, pair, scale, candidates, frame_parents):
    """以源遮挡和两次页面采样证明可替换像素，并保持正文的独立归属。"""
    lower, upper, members, upper_members, candidate, _ = pair
    low_obj, high_obj = handles[lower][0], handles[upper][0]
    crop = _pixel_crop(candidate["bbox"], page.get_size(), scale)
    if crop is None or not (_axis_aligned(low_obj) and _axis_aligned(high_obj)) or raw.FPDFPageObj_HasTransparency(high_obj):
        return None

    covered = _opaque_regions(page, scale, crop, handles, upper, solid=True)
    if np.count_nonzero(covered) < 0.30 * covered.size:
        return None
    ink = _opaque_regions(page, scale, crop, handles, lower, solid=False)
    with render_source_patch(page, scale, crop) as actual:
        with hide_drawings((low_obj,)), render_source_patch(page, scale, crop) as without_lower:
            visible = np.any(np.asarray(actual) != np.asarray(without_lower), axis=-1)
        # 整图消失、细边缘误差及透明层有自己的处理边界；这里只修仍有可见主体的局部覆盖。
        if np.count_nonzero(visible & ink) < max(16, 0.005 * np.count_nonzero(ink)):
            return None
        covered &= ink & ~visible
        if not covered.any():
            return None

        components = _merged_components(page, marked, handles, pair, candidates, frame_parents, actual, covered, scale, crop)
        body = set(members) | set(upper_members)
        if any(
            index > lower and kind == raw.FPDF_PAGEOBJ_TEXT and index not in body for index, (_, kind) in enumerate(handles)
        ):
            with hide_drawings(
                obj
                for index, (obj, kind) in enumerate(handles)
                if index > lower and kind == raw.FPDF_PAGEOBJ_TEXT and index not in body
            ):
                pixels = render_source_patch(page, scale, crop)
        else:
            pixels = actual.copy()
    return pixels, Image.fromarray(covered.astype(np.uint8) * 255), components


def marked_visibility_patches(page, marked, handles, candidates, frame_parents, blocks):
    """有限地核验可见图层，返回与现有 Figure 裁图同尺寸的像素和修改掩模。"""
    if page.get_rotation() or len(marked.objects) != len(handles):
        return {}
    scale = min(DEFAULT_PDF_IMAGE_DPI / 72, DEFAULT_MAX_RENDER_EDGE / max(page.get_size()))
    pairs = list(islice(_replacement_pairs(marked.objects, candidates), _MAX_PROOFS + 1))
    if len(pairs) > _MAX_PROOFS:
        return {}
    counts = Counter(id(pair[-2]) for pair in pairs)
    patches = {}
    for pair in pairs:
        identity = id(pair[-2])
        if (
            counts[identity] == 1
            and (patch := _visible_patch(page, marked, handles, pair, scale, blocks, frame_parents)) is not None
        ):
            patches[identity] = patch
    claims = Counter(id(component) for _, _, components in patches.values() for component in components)
    # 多层套叠中，同一节点可同时提名为前层和后层。此时只修像素，保留争议节点的独立归属。
    for owner, (pixels, mask, components) in patches.items():
        if any(claims[id(component)] != 1 or id(component) in patches for component in components):
            patches[owner] = pixels, mask, []
    return patches


def _covered_figure_pairs(marked, blocks, parents):
    """仅提名同一源父节点下、有独立公共裁图的近同位顶层位图。"""
    from .raster_occlusion import _geometry_can_cover

    attached = {
        m.get("visual_parent") or m.get("inferred_visual_parent") for block in blocks if (m := block.get("_pdf_structure", {}))
    }
    siblings = {}
    for block in blocks:
        marker = block.get("_pdf_structure", {})
        identity = marker.get("node_id")
        members = marker.get("members", ())
        if (
            block["type"] != "image"
            or identity not in parents
            or identity in attached
            or marker.get("link_target")
            or len(members) != 1
        ):
            continue
        member = marked.objects[members[0]]
        if member.kind == raw.FPDF_PAGEOBJ_IMAGE and not member.occurrence and member.bbox and _area(member.bbox) > 0:
            siblings.setdefault(parents[identity], []).append((block, member))
    for entries in siblings.values():
        ordered = sorted(entries, key=lambda entry: entry[1].index)
        for i, (lower, member) in enumerate(ordered[:-1]):
            for _, cover in ordered[i + 1 : i + 9]:
                if _area(member.bbox) >= 0.55 * _area(cover.bbox) and _geometry_can_cover(member.bbox, cover.bbox):
                    yield lower, member, cover


def _cover_still_visible(page, size, box, handle):
    """关闭后层重绘公开范围，防止另一个不可见层被当作最终图。"""
    scale = min(DEFAULT_PDF_IMAGE_DPI / 72, DEFAULT_MAX_RENDER_EDGE / max(size))
    crop = _pixel_crop(box, size, scale)
    if crop is None:
        return False
    with render_source_patch(page, scale, crop) as original:
        with hide_drawings((handle,)), render_source_patch(page, scale, crop) as absent:
            return original.tobytes() != absent.tobytes()


def fully_covered_figures(page, marked, handles, blocks, parents):
    """复用位图全遮挡证明，源 Figure 的 Alt、MCID 和成员仍保存在扩展中。"""
    from .native_objects import _ClippedObject
    from .raster_occlusion import _isolated_alpha, _opaque_over_lower, _unchanged_with_inner_disabled

    if page.get_rotation() or len(marked.objects) != len(handles):
        return set()
    size = page.get_size()
    hidden = set()
    for lower, member, cover in islice(_covered_figure_pairs(marked, blocks, parents), _MAX_PROOFS):
        low, high = handles[member.index][0], handles[cover.index][0]
        if id(lower) in hidden or not (_axis_aligned(low) and _axis_aligned(high)):
            continue
        if not (0 <= member.bbox[0] < member.bbox[2] <= size[0] and 0 <= member.bbox[1] < member.bbox[3] <= size[1]):
            continue
        source = _ClippedObject(low, _get_raw_object_matrix(low), (), 0, None)
        overlay = _ClippedObject(high, _get_raw_object_matrix(high), (), 0, None)
        try:
            upper_alpha = _isolated_alpha(overlay, page)
            if upper_alpha is None:
                continue
            with upper_alpha:
                if (
                    _opaque_over_lower(upper_alpha, overlay, member.bbox, page.get_bbox())
                    and _unchanged_with_inner_disabled(page, source, member.bbox, size)
                    and _cover_still_visible(page, size, lower["bbox"], high)
                ):
                    hidden.add(id(lower))
        except (OSError, ValueError):
            # 回绘证据不完整时保留带 Alt 的独立裁片。
            continue
    return hidden
