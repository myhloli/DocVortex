"""将作者分别标记的空边框及其内屏按原绘制实例合成一个公开图体。"""

from bisect import bisect_left
from collections import Counter, defaultdict

import pypdfium2.raw as raw

from ....document.pdf.structure import ContentReference, StructureNode
from .geometry import _bbox_area, _bbox_overlap_in_first


def figure_siblings(nodes, node_pages, *, allow_alt=False):
    """收集直接引用源对象的同父 Figure；允许 Alt 时仍保留节点的原替代文本。"""
    referenced = {identity for node in nodes for identity in node.references}
    groups = {}
    for parent in nodes:
        if parent.role in {"Link", "Figure", "Table", "Formula", "Caption"}:
            continue
        siblings = [
            child.identity
            for child in parent.children
            if isinstance(child, StructureNode)
            and child.role == "Figure"
            and child.valid
            and not (child.actual_text or child.references or child.identity in referenced)
            and (allow_alt or not child.alt)
            and len(node_pages[child.identity]) == 1
            and all(isinstance(ref, ContentReference) and ref.object_id is None for ref in child.children)
        ]
        if len(siblings) > 1:
            groups.update(dict.fromkeys(siblings, parent.identity))
    return groups


def _candidate_pairs(page, blocks, parents):
    """先以同页存活的语义成员、源画序及包含关系收窄配对，再读取像素。"""
    attached = {
        ident
        for block in blocks
        for marker in (block.get("_pdf_structure", {}),)
        if (ident := marker.get("visual_parent") or marker.get("inferred_visual_parent"))
    }
    groups = defaultdict(list)
    for index, block in enumerate(blocks):
        marker = block.get("_pdf_structure", {})
        identity = marker.get("node_id")
        members = marker.get("members")
        if (
            block["type"] != "image"
            or identity not in parents
            or identity in attached
            or not members
            or marker.get("link_target")
        ):
            continue
        if not any(page.objects[i].kind == raw.FPDF_PAGEOBJ_IMAGE for i in members):
            continue
        box = tuple(block["bbox"])
        groups[parents[identity]].append((box, index, members))
    for siblings in groups.values():
        siblings.sort()
        left_edges = [box[0] for box, _, _ in siblings]
        for outer, fi, members in siblings:
            if len(members) != 1 or page.objects[members[0]].occurrence or _bbox_area(outer) <= 0:
                continue
            start = bisect_left(left_edges, outer[0] - 0.0005)
            for k in range(start, len(siblings)):
                inner, si, subject = siblings[k]
                if inner[0] >= outer[2]:
                    break
                if max(subject) >= members[0] or not 0.30 <= _bbox_area(inner) / _bbox_area(outer) < 0.99:
                    continue
                if _bbox_overlap_in_first(inner, outer) < 0.997:
                    continue
                # 已有独立命名的插图或第二个屏幕竞争同一窗口时，仍保留各原始 Figure。
                if any(
                    index not in (fi, si)
                    and block["type"] in {"image", "chart", "table", "equation"}
                    and max(_bbox_overlap_in_first(inner, block["bbox"]), _bbox_overlap_in_first(block["bbox"], inner)) >= 0.90
                    for index, block in enumerate(blocks)
                ):
                    continue
                yield fi, si, members[0]


def join_tagged_frames(document, page, blocks, parents):
    """在物化 PNG 前合并唯一匹配的成员，保持两个源节点均可回溯到同一公开块。"""
    pairs = list(_candidate_pairs(page, blocks, parents))
    if not pairs:
        return
    from ....document.pdf.frame_apertures import screen_fits_opening
    from ....document.pdf.native_objects import _clipped_objects_of_type
    from ....document.pdf.raster_composites import read_frame_opening

    indices = [obj.index for obj in page.objects if not obj.occurrence and obj.kind == raw.FPDF_PAGEOBJ_IMAGE]
    choices = []
    width, height = page.size
    with document._open_page(page.page_index) as source:
        if source.get_rotation():
            return
        members = [item for item in _clipped_objects_of_type(source, raw.FPDF_PAGEOBJ_IMAGE) if item.depth == 0]
        if len(indices) != len(members):
            return
        handles = dict(zip(indices, members, strict=True))
        apertures = {}
        for fi, si, index in pairs:
            if index not in apertures:
                apertures[index] = read_frame_opening(source, handles[index], page.objects[index].bbox, source.get_bbox(), 0)
            opening = apertures[index]
            inner = tuple(v * (width, height)[i % 2] for i, v in enumerate(blocks[si]["bbox"]))
            if opening is None:
                continue
            screen_boxes = [inner]
            # 同一 Figure 可能把屏内位图和状态栏的字、路径合在一起。以主体位图定位窗口，整组原语一起渲染。
            child_ids = blocks[si]["_pdf_structure"]["members"]
            if len(child_ids) > 1:
                screen_boxes.extend(
                    obj.bbox
                    for child in child_ids
                    if (obj := page.objects[child]).kind == raw.FPDF_PAGEOBJ_IMAGE
                    and obj.bbox is not None
                    and _bbox_area(obj.bbox) >= 0.90 * _bbox_area(inner)
                )
            if any(screen_fits_opening(opening, box) for box in screen_boxes):
                choices.append((fi, si))
    counts = Counter(index for pair in choices for index in pair)
    removed = set()
    for fi, si in choices:
        if counts[fi] != 1 or counts[si] != 1:
            continue
        frame = blocks[fi]
        marker = frame["_pdf_structure"]
        subject = blocks[si]["_pdf_structure"]
        marker["members"] = sorted((*marker["members"], *subject["members"]))
        marker["component_node_id"] = subject["node_id"]
        # 位图裁框及原生子图均取自已核准的两个 Figure，其他原生标题不参与渲染。
        earliest, other = sorted((fi, si))
        blocks[earliest] = frame
        removed.add(other)
    if removed:
        blocks[:] = [block for index, block in enumerate(blocks) if index not in removed]
