"""局部修复 CJK 对象断行，并在普通段落上复用测量，保持 ReportLab 的拆分与绘制契约。"""

from __future__ import annotations

from collections.abc import Callable
from copy import copy, deepcopy
from types import FunctionType
from unicodedata import category

from reportlab.platypus import Paragraph
from reportlab.platypus import paragraph as rl_paragraph
from reportlab.platypus.paraparser import ParaFrag

from .diagnostics import report_pdf_diagnostic
from .formula import InlineFormulaImage


def _natural_cjk_unit(fragment: ParaFrag, text: str) -> rl_paragraph.cjkU:
    """分页后的公式从未缩放的回调恢复几何，试排不修改原始片段或共享公式。"""
    definition = getattr(fragment, "cbDefn", None)
    original = getattr(definition, "_pdf_cjk_original", None)
    if original is not None:
        fragment = fragment.clone(cbDefn=original)
    return rl_paragraph.cjkU(text, fragment, "utf8")


def _fit_cjk_formula(unit: rl_paragraph.cjkU, width: float) -> rl_paragraph.cjkU:
    """仅在空行仍容不下时缩小自有公式，保留原始回调供后续宽窄试排和分页复用。"""
    definition = getattr(unit.frag, "cbDefn", None)
    if not isinstance(getattr(definition, "image", None), InlineFormulaImage) or not 0 < width < unit.width:
        return unit
    scale = width / unit.width
    fitted = copy(definition)
    fitted._pdf_cjk_original = definition
    fitted.width = width
    fitted.height = definition.height * scale
    fitted.valign = definition.valign * scale
    return rl_paragraph.cjkU(str(unit), unit.frag.clone(cbDefn=fitted), "utf8")


def _safe_cjk_split(frags: list[ParaFrag], widths: list[float], calc_bounds: bool) -> rl_paragraph.ParaLines:
    """沿用上游 CJK 断行规则，但把空文本回调当作完整对象，安全移行并保留零宽标记。"""
    # Adapted from ReportLab 4.4.4/5.0.1 cjkFragSplit (BSD); see licenses/REPORTLAB.txt.
    units = []
    for fragment in frags:
        text = fragment.text
        if isinstance(text, bytes):
            text = text.decode("utf8")
        units.extend(_natural_cjk_unit(fragment, character) for character in text or [""])
    lines = []
    index = start = 0
    used = 0.0
    width = widths[0]
    while index < len(units):
        unit = units[index]
        if used == 0:
            unit = units[index] = _fit_cjk_formula(unit, width)
        index += 1
        advance = unit.width
        if hasattr(advance, "normalizedValue"):
            advance = advance.normalizedValue(width)
        used += advance
        forced = hasattr(unit.frag, "lineBreak")
        if (used > width + rl_paragraph._FUZZ and used > 0) or forced:
            extra = width - used
            if not forced:
                if unit and ord(unit) < 0x3000:
                    limit = (start + index) >> 1
                    for candidate in range(index - 1, limit, -1):
                        previous = units[candidate]
                        if previous and (category(previous) == "Zs" or ord(previous) >= 0x3000):
                            following = candidate + 1
                            if following < index:
                                next_index = following + 1
                                extra += sum(item.width for item in units[next_index:index])
                                advance = units[following].width
                                unit = units[following]
                                index = next_index
                                break
                # 空串属于任意字符串的子串，必须显式排除，不能当作悬挂的禁则标点。
                if (not unit or unit not in rl_paragraph.ALL_CANNOT_START) and index > start + 1:
                    index -= 1
                    extra += advance
            lines.append(rl_paragraph.makeCJKParaLine(units[start:index], width, width - extra, extra, forced, calc_bounds))
            width = widths[min(len(lines), len(widths) - 1)]
            start = index
            used = 0.0
    # 零宽 anchor 仍有绘制副作用，不能按累计宽度决定是否丢弃尾部片段。
    if start < len(units):
        lines.append(rl_paragraph.makeCJKParaLine(units[start:], width, used, width - used, False, calc_bounds))
    return rl_paragraph.ParaLines(kind=1, lines=lines)


def _split_cjk_lines(lines: rl_paragraph.ParaLines, start: int, stop: int) -> list[ParaFrag]:
    """直接复制已分行的 CJK 片段，避免上游给公式补空格或修改原段落的绘制状态。"""
    return [fragment.clone() for line in lines.lines[start:stop] for fragment in line.words]


class CJKParagraph(Paragraph):
    """为自有 CJK 特殊片段提供局部安全断行，其余内容继续遵守原生 Paragraph 契约。"""

    def breakLinesCJK(self, maxWidths: float | list[float] | tuple[float, ...]) -> rl_paragraph.ParaLines:
        """只接管含空文本的片段；保留首行缩进、项目符号及分页首片的既有布局。"""
        self._pdf_safe_cjk = False
        if not any(getattr(fragment, "text", None) == "" for fragment in self.frags):
            return super().breakLinesCJK(maxWidths)
        self._pdf_safe_cjk = True
        if hasattr(self, "blPara") and getattr(self, "_splitpara", False):
            return self.blPara
        widths = list(maxWidths) if isinstance(maxWidths, (list, tuple)) else [maxWidths]
        self.height = 0
        rl_paragraph._handleBulletWidth(self.bulletText, self.style, widths)
        auto_leading = getattr(self, "autoLeading", getattr(self.style, "autoLeading", ""))
        return _safe_cjk_split(self.frags, widths, auto_leading not in ("", "off"))

    def _get_split_blParaFunc(self) -> Callable[[rl_paragraph.ParaLines, int, int], list[ParaFrag]]:
        """只对安全组行的富片段使用无补空格拆分，普通文本仍走原生拆分入口。"""
        if getattr(self, "_pdf_safe_cjk", False):
            return _split_cjk_lines
        return super()._get_split_blParaFunc()

    def draw(self) -> None:
        """仅对最终绘制的超小公式报告诊断，避免试排产生过时告警。"""
        if getattr(self, "_pdf_safe_cjk", False):
            for line in self.blPara.lines:
                for fragment in line.words:
                    definition = getattr(fragment, "cbDefn", None)
                    original = getattr(definition, "_pdf_cjk_original", None)
                    if original is not None and fragment.fontSize * definition.width / original.width < 6:
                        report_pdf_diagnostic(
                            "pdf_layout_small_text",
                            f"Inline formula fitted below 6 pt ({getattr(fragment, '_pdf_location', '')})",
                            getattr(fragment, "_pdf_page_idx", None),
                        )
        super().draw()


_STYLE_FIELDS = ("fontName", "fontSize", "textColor", "rise", "us_lines", "link", "backColor", "nobr")
_MISSING = object()


def _same_plain_style(first, second) -> bool:
    """普通片段按身份或样式字典比较，避免逐字符触发动态属性查找和缺失属性异常。"""
    if first is second:
        return True
    left, right = first.__dict__, second.__dict__
    # 保留原生比较中「属性不存在」与「属性值为 None」的区别。
    return [left.get(name, _MISSING) for name in _STYLE_FIELDS] == [right.get(name, _MISSING) for name in _STYLE_FIELDS]


def _plain_cjk_breaker():
    """仅为自有段落绑定局部比较函数，不复制断行算法，也不修改 ReportLab 模块全局。"""
    replacement = _same_plain_style
    for function, dependency in (
        (getattr(rl_paragraph, "makeCJKParaLine", None), "sameFrag"),
        (getattr(rl_paragraph, "cjkFragSplit", None), "makeCJKParaLine"),
        (Paragraph.breakLinesCJK, "cjkFragSplit"),
    ):
        # ReportLab 内部实现变化或已被外部包装时，保守回到其原有入口。
        if not isinstance(function, FunctionType) or dependency not in function.__code__.co_names:
            return None
        namespace = function.__globals__.copy()
        namespace[dependency] = replacement
        replacement = FunctionType(function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__)
        replacement.__kwdefaults__ = function.__kwdefaults__
    return replacement


_PLAIN_CJK_BREAK = _plain_cjk_breaker()


class PlainCJKParagraph(CJKParagraph):
    """仅普通 CJK 组行使用局部比较，复杂内容和拆分后的段落交给安全断行层。"""

    def breakLinesCJK(self, maxWidths):
        """每次检查当前片段，内容变更后不沿用旧资格或样式比较缓存。"""
        self._pdf_safe_cjk = False
        if (
            _PLAIN_CJK_BREAK is not None
            and len(self.frags) > 1
            and not self.bulletText
            and not self.style.endDots
            and not getattr(self, "_splitpara", False)
            and all(
                type(fragment) is ParaFrag
                and type(getattr(fragment, "text", None)) is str
                and bool(fragment.text)
                and "cbDefn" not in fragment.__dict__
                and "lineBreak" not in fragment.__dict__
                and not any(fragment.__dict__.get(name) for name in ("link", "us_lines", "rise", "nobr"))
                for fragment in self.frags
            )
        ):
            return _PLAIN_CJK_BREAK(self, maxWidths)
        return super().breakLinesCJK(maxWidths)


class MeasuredParagraph(CJKParagraph):
    """缓存仍保存在本对象中的完整排版状态，不跨段落分享 blPara 或 fragments。"""

    def _measurement_key(self, width: float) -> tuple | None:
        """检查内容、样式及已保存几何；复杂回调和处理后的词列表保守重新测量。"""
        if any(not hasattr(fragment, "__dict__") or hasattr(fragment, "cbDefn") for fragment in self.frags):
            return None
        style = vars(self.style).copy()
        # ReportLab 已把继承值复制到当前样式，parent 本身不参与 wrap，不能按对象身份比较其深副本。
        style.pop("parent", None)
        return (
            width,
            style,
            getattr(self, "autoLeading", None),
            tuple(vars(fragment) for fragment in self.frags),
            id(getattr(self, "blPara", None)),
            getattr(self, "width", None),
            getattr(self, "height", None),
            tuple(getattr(self, "_wrapWidths", ())),
        )

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        """同宽且状态未变时复用现有换行；Paragraph 本身不使用可用高度决定换行。"""
        key = self._measurement_key(availWidth)
        cached = getattr(self, "_measurement_cache", None)
        if key is not None and cached is not None and key == cached[0]:
            return cached[1]
        self._measurement_cache = None
        result = super().wrap(availWidth, availHeight)
        key = self._measurement_key(availWidth)
        if key is not None:
            # 只在真实换行后保存独立快照；命中时用字典比较，避免逐次序列化样式和长文本。
            self._measurement_cache = (deepcopy(key), result)
        return result

    def split(self, availWidth: float, availHeight: float) -> list:
        """拆分可能修改行布局，返回的新段落独立缓存，原对象随后重新测量。"""
        self._measurement_cache = None
        try:
            return super().split(availWidth, availHeight)
        finally:
            self._measurement_cache = None

    def draw(self) -> None:
        """绘制可能更新行内状态，绘制后不再把旧结果当作未改变的试排状态。"""
        try:
            super().draw()
        finally:
            self._measurement_cache = None


class MeasuredCJKParagraph(MeasuredParagraph, PlainCJKParagraph):
    """组合普通 CJK 比较与原有测量复用，拆分段落继续拥有独立排版状态。"""
