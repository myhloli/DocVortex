"""把 PDF 内容标记连接到字符和绘图对象，保存带作用域的独立页面证据。"""

from __future__ import annotations

import ctypes as ct
import base64
from dataclasses import dataclass, field
from typing import Any

import pypdfium2.raw as raw
from pypdf.generic import ContentStream, create_string_object

from ..._compute_backend import get_native
from ...schema import BBox
from .form_structure import _read_char_form_owners, _visual_bbox
from .native_contracts import PDFPageTextGeometry
from .native_coordinates import _get_raw_object_matrix, _multiply_pdf_matrices, _normalize_pdf_page_bbox, _drawing_page_size
from .structure import ContentReference, dereference, object_identity
from .native_objects import _object_clip_bbox, _clip_object_visual_bbox, _image_bbox_from_matrix

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True)
class MarkedObject:
    """一个实际绘制实例及其文字成员，地址在采集阶段即转换为页内编号。"""

    index: int
    kind: int
    stream: str
    occurrence: tuple[int, ...]
    mcid: int
    bbox: BBox | None
    chars: tuple[dict[str, Any], ...]
    raw_indices: frozenset[int]
    path_index: int | None = None
    artifact: bool = False
    artifact_subtype: str | None = None
    marks: tuple = ()


@dataclass(frozen=True)
class MarkedPage:
    """一页有完整原字符索引的内容证据，可在原件关闭后继续消费。"""

    page_index: int
    size: tuple[float, float]
    objects: tuple[MarkedObject, ...]
    geometry: PDFPageTextGeometry
    annotations: dict[str, tuple[BBox, str | None]] = field(default_factory=dict)
    available: bool = True

    def resolve(self, reference: ContentReference) -> tuple[MarkedObject, ...]:
        """严格按页面、内容流和 MCID 查找实际绘制实例。"""
        if reference.page_index != self.page_index or reference.mcid is None:
            return ()
        return tuple(
            obj for obj in self.objects if obj.stream == reference.stream and obj.mcid == reference.mcid and not obj.artifact
        )


def _read_marks(handles: list) -> list:
    """核验标记接口 ABI 后一次进入 Rust 读取全部对象的标记栈。"""
    obj, mark = raw.FPDF_PAGEOBJECT, raw.FPDF_PAGEOBJECTMARK
    string, used = ct.POINTER(ct.c_ushort), ct.POINTER(ct.c_ulong)
    specs = (
        ("FPDFPageObj_GetMarkedContentID", ct.c_int, (obj,)),
        ("FPDFPageObj_CountMarks", ct.c_int, (obj,)),
        ("FPDFPageObj_GetMark", mark, (obj, ct.c_ulong)),
        ("FPDFPageObjMark_GetName", ct.c_int, (mark, string, ct.c_ulong, used)),
        (
            "FPDFPageObjMark_GetParamBlobValue",
            ct.c_int,
            (mark, ct.POINTER(ct.c_char), ct.POINTER(ct.c_ubyte), ct.c_ulong, used),
        ),
        ("FPDFPageObjMark_GetParamIntValue", ct.c_int, (mark, ct.POINTER(ct.c_char), ct.POINTER(ct.c_int))),
    )
    addresses = []
    for name, result, arguments in specs:
        function = getattr(raw, name)
        if (
            not isinstance(function, ct._CFuncPtr)
            or function.restype is not result
            or tuple(function.argtypes or ()) != arguments
            or getattr(function, "errcheck", None) is not None
        ):
            raise RuntimeError(f"Unsupported marked-content ABI: {name}")
        addresses.append(ct.cast(function, ct.c_void_p).value)
    definitions, records = get_native().read_pdfium_marked_content(
        addresses, [ct.cast(handle, ct.c_void_p).value for handle in handles]
    )
    decoded = [(*item[:2], *(_pdf_string(value) for value in item[2:6]), item[6]) for item in definitions]
    return [(mcid, [decoded[index] for index in marks]) for mcid, marks in records]


def _pdf_string(value) -> str | None:
    """按 PDF 字符串编码解码属性，避免 PDFium 的 UTF-8 字符串接口截断 UTF-16。"""
    if value is None:
        return None
    decoded = create_string_object(bytes(value))
    return str(decoded) if isinstance(decoded, str) else bytes(value).decode("utf-8", errors="replace")


def _form_streams(page) -> dict[tuple[int, ...], str]:
    """沿真实 Do 调用记录 Form 内容流身份，每次调用使用独立实例路径。"""
    streams: dict[tuple[int, ...], str] = {}

    def walk(stream, resources, occurrence, active, depth):
        """有界展开 Form 调用，资源中的未调用对象不进入实例索引。"""
        if stream is None or depth >= 64:
            return
        index = 0
        for operands, operator in ContentStream(stream, page.pdf).operations:
            if operator != b"Do" or not operands:
                continue
            reference = dereference(resources.get("/XObject", {})).get(operands[0])
            child = dereference(reference)
            if not isinstance(child, dict) or child.get("/Subtype") != "/Form":
                continue
            path = (*occurrence, index)
            index += 1
            identity = object_identity(reference, f"form:{path}")
            streams[path] = identity
            if identity not in active:
                walk(child, dereference(child.get("/Resources", resources)), path, active | {identity}, depth + 1)

    walk(page.get_contents(), dereference(page.get("/Resources", {})), (), frozenset(), 0)
    return streams


def read_marked_page(document, page_index: int) -> MarkedPage:
    """在原页及文本页存活期间采集所有标记和原始字符，结束时仅返回自有值。"""
    with document._open_page(page_index) as page:
        source = document._get_form_resource_page(page_index)
        if source is None:
            raise ValueError("Original PDF page object unavailable")
        streams = _form_streams(source)
        page_bbox = _normalize_pdf_page_bbox(page.get_bbox())
        rotation = int(page.get_rotation()) % 360
        size = _drawing_page_size(page_bbox, rotation)
        handles, descriptions = [], []
        path_count = 0

        def walk(container, occurrence, matrix, parent_index, depth, clip=None):
            """按 PDFium 实际对象树展开容器，保留祖先标记及源流作用域。"""
            nonlocal path_count
            if depth >= 64:
                raise ValueError("PDF marked-content Form depth exceeded")
            count = raw.FPDFFormObj_CountObjects(container) if occurrence else raw.FPDFPage_CountObjects(container)
            form_index = 0
            for index in range(max(count, 0)):
                handle = raw.FPDFFormObj_GetObject(container, index) if occurrence else raw.FPDFPage_GetObject(container, index)
                if not handle:
                    continue
                if len(handles) >= 1_000_000:
                    raise ValueError("PDF marked-content object limit exceeded")
                kind = raw.FPDFPageObj_GetType(handle)
                effective_clip = _object_clip_bbox(handle, matrix, clip)
                values = [ct.c_float() for _ in range(4)]
                bounds = None
                if raw.FPDFPageObj_GetBounds(handle, *(ct.byref(value) for value in values)):
                    bounds = _clip_object_visual_bbox(
                        _visual_bbox(tuple(value.value for value in values), matrix, page_bbox, rotation),
                        effective_clip,
                        page_bbox,
                        rotation,
                    )
                if kind == raw.FPDF_PAGEOBJ_IMAGE:
                    transform = _get_raw_object_matrix(handle)
                    bounds = (
                        _image_bbox_from_matrix(_multiply_pdf_matrices(transform, matrix), page_bbox, rotation)
                        if transform
                        else None
                    )
                    if bounds is not None:
                        bounds = _clip_object_visual_bbox(bounds, effective_clip, page_bbox, rotation)
                object_index = len(handles)
                handles.append(handle)
                descriptions.append(
                    (kind, occurrence, parent_index, bounds, path_count if kind == raw.FPDF_PAGEOBJ_PATH else None)
                )
                path_count += kind == raw.FPDF_PAGEOBJ_PATH
                if kind == raw.FPDF_PAGEOBJ_FORM:
                    nested = (*occurrence, form_index)
                    form_index += 1
                    transform = _get_raw_object_matrix(handle) or _IDENTITY
                    walk(handle, nested, _multiply_pdf_matrices(transform, matrix), object_index, depth + 1, effective_clip)

        walk(page.raw, (), _IDENTITY, None, 0)
        marks = _read_marks(handles)
        owner = document._extract_owned_text_snapshot(page_index, page, visible_only=False)
        geometry = owner if isinstance(owner, PDFPageTextGeometry) else owner.materialize_geometry()
        textpage = page.get_textpage()
        try:
            owners = _read_char_form_owners(
                textpage, {ct.cast(handle, ct.c_void_p).value: index for index, handle in enumerate(handles)}
            )
        finally:
            textpage.close()
        object_chars: dict[int, list[dict]] = {}
        previous = None
        for char in geometry.chars:
            indices = char.get("source_indices", (char["char_idx"],))
            target = next((owners[index] for index in indices if 0 <= index < len(owners) and owners[index] is not None), None)
            if target is None and (char.get("is_generated") or not char.get("char", "").strip()):
                target = previous
            if target is not None:
                object_chars.setdefault(target, []).append(char)
            elif char.get("char", "").strip():
                raise ValueError(f"Unmapped native character {char['char_idx']}")
            previous = target
        objects = []
        for index, (kind, occurrence, parent_index, bounds, path_index) in enumerate(descriptions):
            mcid, stack = marks[index]
            parent = objects[parent_index] if parent_index is not None else None
            inherited = tuple(parent.marks) if parent is not None else ()
            inherited_ids = {item[6] for item in inherited}
            stack = [*inherited, *(tuple(item) + (occurrence,) for item in stack if item[6] not in inherited_ids)]
            stream = streams.get(occurrence, f"page:{page_index}")
            if mcid < 0 and parent is not None and parent.mcid >= 0:
                mcid, stream = parent.mcid, parent.stream
            artifact = any(mark[0] == "Artifact" for mark in stack) or bool(parent and parent.artifact)
            subtype = next((mark[5] for mark in stack if mark[0] == "Artifact"), None)
            chars = tuple(object_chars.get(index, ()))
            objects.append(
                MarkedObject(
                    index,
                    kind,
                    stream,
                    occurrence,
                    mcid,
                    bounds,
                    chars,
                    frozenset(raw_index for char in chars for raw_index in char.get("source_indices", (char["char_idx"],))),
                    path_index,
                    artifact,
                    subtype,
                    tuple(stack),
                )
            )
        annotations = {}
        for reference in dereference(source.get("/Annots", [])):
            annotation = dereference(reference)
            if not isinstance(annotation, dict):
                continue
            rect = dereference(annotation.get("/Rect", []))
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                continue
            action = dereference(annotation.get("/A", {}))
            target = action.get("/URI") if isinstance(action, dict) else None
            annotations[object_identity(reference)] = (
                _visual_bbox(tuple(map(float, rect)), _IDENTITY, page_bbox, rotation),
                str(target) if target else None,
            )
        return MarkedPage(page_index, size, tuple(objects), geometry, annotations)


def render_marked_visuals(
    document, page_index: int, blocks: list[dict], background_members: frozenset[int] = frozenset()
) -> None:
    """渲染所属成员和装饰背景，避免组合矢量图的矩形裁图带入邻近正文。"""
    from ...foundation._image import crop_pil_image
    from ...foundation.image_encoding import encode_image
    from .raster import page_to_image

    candidates = [
        block
        for block in blocks
        if block.get("type") in {"image", "equation"} and block.get("_pdf_structure", {}).get("members")
    ]
    if not candidates:
        return
    with document._open_page(page_index) as page:
        objects = []

        def collect(container, is_form=False, depth=0):
            """以采集证据时相同的先序遍历重新定位对象，句柄只在当前页面内使用。"""
            if depth >= 64:
                raise ValueError("PDF marked-content Form depth exceeded")
            count = raw.FPDFFormObj_CountObjects(container) if is_form else raw.FPDFPage_CountObjects(container)
            for index in range(max(count, 0)):
                obj = raw.FPDFFormObj_GetObject(container, index) if is_form else raw.FPDFPage_GetObject(container, index)
                if not obj:
                    continue
                kind = raw.FPDFPageObj_GetType(obj)
                objects.append((obj, kind))
                if kind == raw.FPDF_PAGEOBJ_FORM:
                    collect(obj, True, depth + 1)

        collect(page.raw)
        for block in candidates:
            selected = set(block["_pdf_structure"]["members"]) | background_members
            changed = []
            image = crop = None
            try:
                for index, (obj, kind) in enumerate(objects):
                    if index in selected or kind == raw.FPDF_PAGEOBJ_FORM:
                        continue
                    active = ct.c_int()
                    if not raw.FPDFPageObj_GetIsActive(obj, ct.byref(active)):
                        raise RuntimeError("Cannot inspect PDF object activity")
                    if active.value:
                        if not raw.FPDFPageObj_SetIsActive(obj, 0):
                            raise RuntimeError("Cannot isolate PDF graphic members")
                        changed.append(obj)
                image, _ = page_to_image(page)
                crop = crop_pil_image(tuple(block["bbox"]), image)
                artifact = encode_image(crop, image_format="png")
                block["image_base64"] = "data:image/png;base64," + base64.b64encode(artifact.data).decode("ascii")
                block["_pdf_structure"]["image_ready"] = True
            finally:
                restored = [raw.FPDFPageObj_SetIsActive(obj, 1) for obj in changed]
                if crop is not None:
                    crop.close()
                if image is not None:
                    image.close()
                if not all(restored):
                    raise RuntimeError("Cannot restore PDF object activity")
