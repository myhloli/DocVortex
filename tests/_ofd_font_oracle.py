"""冻结R4B字体指标实现，只用于逐位差分，禁止跟随生产实现修改。"""

from dataclasses import dataclass
from io import BytesIO

from fontTools.pens.boundsPen import BoundsPen
from loguru import logger

from docvortex.schema import BBox
from docvortex.analyzers.native.ofd.constants import MAX_FONT_BYTES
from docvortex.analyzers.native.ofd.models import FontResource
from docvortex.analyzers.native.ofd.package import OfdPackage


@dataclass(slots=True)
class _LoadedFont:
    """缓存 FontTools 中与字形几何相关的只读表。"""

    font: object
    glyph_order: list[str]
    glyph_set: object
    units_per_em: float
    advances: dict[str, tuple[int, int]]
    char_to_name: dict[int, str]
    name_to_char: dict[str, str]


class FontMetricResolver:
    """按 OFD 字体资源惰性读取内嵌 OpenType 指标。"""

    def __init__(self, package: OfdPackage) -> None:
        """绑定当前包并创建字体解析缓存。"""
        self.package = package
        self._cache: dict[str, _LoadedFont | None] = {}

    def _load(self, resource: FontResource | None) -> _LoadedFont | None:
        """读取一个受限字体成员，失败时缓存空结果。"""
        if resource is None or resource.font_part is None:
            return None
        if resource.font_part in self._cache:
            return self._cache[resource.font_part]
        data = self.package.read_part(resource.font_part, asset=True)
        if data is None or len(data) > MAX_FONT_BYTES:
            logger.warning(
                f"OFD_FONT_UNAVAILABLE: part={resource.font_part!r}, reason={'missing' if data is None else 'too_large'}"
            )
            self._cache[resource.font_part] = None
            return None
        try:
            from fontTools.ttLib import TTFont

            font = TTFont(BytesIO(data), lazy=True)
            glyph_order = list(font.getGlyphOrder())
            glyph_set = font.getGlyphSet()
            units_per_em = float(font["head"].unitsPerEm) if "head" in font else 1000.0
            advances = dict(font["hmtx"].metrics) if "hmtx" in font else {}
            char_to_name: dict[int, str] = {}
            if "cmap" in font:
                for table in font["cmap"].tables:
                    char_to_name.update(table.cmap)
            name_to_char: dict[str, str] = {}
            for codepoint, glyph_name in char_to_name.items():
                if glyph_name not in name_to_char and 0 <= codepoint <= 0x10FFFF:
                    name_to_char[glyph_name] = chr(codepoint)
            loaded = _LoadedFont(
                font=font,
                glyph_order=glyph_order,
                glyph_set=glyph_set,
                units_per_em=max(units_per_em, 1.0),
                advances=advances,
                char_to_name=char_to_name,
                name_to_char=name_to_char,
            )
        except Exception as exc:
            logger.warning(f"OFD_FONT_INVALID: part={resource.font_part!r}, error={type(exc).__name__}")
            loaded = None
        self._cache[resource.font_part] = loaded
        return loaded

    def resolve_character(self, resource: FontResource | None, glyph_id: int | None, fallback: str) -> str:
        """在 TextCode 使用占位符时尝试由 glyph cmap 恢复字符。"""
        if fallback != "¤" or glyph_id is None:
            return fallback
        loaded = self._load(resource)
        if loaded is None or not (0 <= glyph_id < len(loaded.glyph_order)):
            return fallback
        return loaded.name_to_char.get(loaded.glyph_order[glyph_id], fallback)

    def glyph_bbox(
        self,
        resource: FontResource | None,
        glyph_id: int | None,
        character: str,
        *,
        size: float,
        hscale: float,
        advance_hint: float | None,
    ) -> BBox:
        """返回以 glyph origin 为基准的确定性局部字形框。"""
        loaded = self._load(resource)
        glyph_name: str | None = None
        if loaded is not None:
            if glyph_id is not None and 0 <= glyph_id < len(loaded.glyph_order):
                glyph_name = loaded.glyph_order[glyph_id]
            elif character:
                glyph_name = loaded.char_to_name.get(ord(character[0]))
        if loaded is not None and glyph_name is not None and glyph_name in loaded.glyph_set:
            scale = size / loaded.units_per_em
            try:
                pen = BoundsPen(loaded.glyph_set)
                loaded.glyph_set[glyph_name].draw(pen)
                bounds = pen.bounds
            except Exception:
                bounds = None
            advance = float(loaded.advances.get(glyph_name, (loaded.units_per_em, 0))[0]) * scale * hscale
            if advance_hint is not None and advance_hint > 0:
                advance = advance_hint
            if bounds is not None:
                x0, y0, x1, y1 = bounds
                bbox = (x0 * scale * hscale, -y1 * scale, x1 * scale * hscale, -y0 * scale)
                if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                    return bbox
            return (0.0, -0.85 * size, max(advance, 0.2 * size), 0.2 * size)
        fallback_advance = (
            advance_hint if advance_hint is not None and advance_hint > 0 else max(0.5 * size * hscale, 0.2 * size)
        )
        return (0.0, -0.85 * size, fallback_advance, 0.2 * size)

    def close(self) -> None:
        """关闭已经打开的 FontTools 字体对象。"""
        for loaded in self._cache.values():
            if loaded is None:
                continue
            close = getattr(loaded.font, "close", None)
            if callable(close):
                close()
