"""按实际排版高度保护短语义块，并保留长表格的分页能力。"""

from __future__ import annotations

from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, Image, KeepTogether, Paragraph, Table
from reportlab.platypus.flowables import _Container, _listWrapOn

PARAGRAPH_KEEP_RATIO = 0.25
TABLE_KEEP_RATIO = 0.5


class _FlowableGroup(Flowable):
    """提供可测量的容器，同时允许 ReportLab 将前面的标题与容器绑定。"""

    def __init__(self, content: list[Flowable]) -> None:
        """保存有序子项，不继承会被标题绑定逻辑排除的内部容器基类。"""
        super().__init__()
        self._content = content

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        """测量真实高度，使外层标题绑定逻辑可以正确判断剩余空间。"""
        self.width, self.height = _listWrapOn(self._content, availWidth, self.canv)
        return self.width, self.height

    def getSpaceBefore(self) -> float:
        """把第一个子项的外侧间距交给内容框处理。"""
        return self._content[0].getSpaceBefore()

    def getSpaceAfter(self) -> float:
        """把最后一个子项的外侧间距交给内容框处理。"""
        return self._content[-1].getSpaceAfter()

    def drawOn(self, canv: Canvas, x: float, y: float, _sW: float = 0) -> None:
        """复用 ReportLab 容器绘制，保持子项对齐、链接及合并间距。"""
        _Container.drawOn(self, canv, x, y, _sW=_sW)


class _MeasuredKeepTogether(_FlowableGroup):
    """以真实高度参与排版，并在空间不足时委托 KeepTogether 换页。"""

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:
        """保留 KeepTogether 对超高内容的降级，避免保护规则导致无限换页。"""
        group = KeepTogether(self._content)
        group._frame = getattr(self, "_frame", None)
        return group.splitOn(self.canv, availWidth, availHeight)


def _measure(content: list[Flowable], width: float, canvas: Canvas) -> float:
    """测量合并相邻间距后的高度，不包含整个容器的外侧间距。"""
    return _listWrapOn(content, width, canvas)[1] if content else 0.0


def _total_height(content: list[Flowable], width: float, canvas: Canvas) -> float:
    """计算用于保护阈值判断的完整高度，包含首尾外侧间距。"""
    if not content:
        return 0.0
    return _measure(content, width, canvas) + content[0].getSpaceBefore() + content[-1].getSpaceAfter()


def protect_paragraphs(content: list[Flowable], *, width: float, height: float, canvas: Canvas) -> list[Flowable]:
    """逐段保护短文本，避免把整个列表绑定为不可分页的大块。"""
    return [
        _MeasuredKeepTogether([item])
        if isinstance(item, Paragraph) and _total_height([item], width, canvas) <= height * PARAGRAPH_KEEP_RATIO
        else item
        for item in content
    ]


def protect_images(content: list[Flowable], *, width: float, height: float, canvas: Canvas) -> list[Flowable]:
    """绑定图片和简短说明，多图超过整页时按图片主体边界降级分页。"""
    if not content:
        return []
    images = [item for item in content if isinstance(item, Image)]
    notes = [item for item in content if not isinstance(item, Image)]
    short_notes = _total_height(notes, width, canvas) <= height * PARAGRAPH_KEEP_RATIO
    if len(images) == 1 and short_notes:
        image = images[0]
        overhead = _total_height(content, width, canvas) - image.drawHeight
        limit = max(1.0, height - overhead)
        if image.drawHeight > limit:
            image.drawWidth *= limit / image.drawHeight
            image.drawHeight = limit
    if short_notes and _total_height(content, width, canvas) <= height + 1e-6:
        return [_MeasuredKeepTogether(content)]
    if len(images) > 1:
        result: list[Flowable] = []
        group: list[Flowable] = []
        has_image = False
        for item in content:
            if isinstance(item, Image) and has_image:
                result.extend(protect_images(group, width=width, height=height, canvas=canvas))
                group = []
            group.append(item)
            has_image = has_image or isinstance(item, Image)
        result.extend(protect_images(group, width=width, height=height, canvas=canvas))
        return result
    if len(images) == 1:
        # 超长脚注不应解除图片与相邻短图注的绑定；只把超出预算的说明放回文本流。
        start = content.index(images[0])
        end = start + 1
        selected_notes: list[Flowable] = []
        while start > 0:
            candidate = [content[start - 1], *selected_notes]
            if _total_height(candidate, width, canvas) > height * PARAGRAPH_KEEP_RATIO:
                break
            selected_notes = candidate
            start -= 1
        while end < len(content):
            candidate = [*selected_notes, content[end]]
            if _total_height(candidate, width, canvas) > height * PARAGRAPH_KEEP_RATIO:
                break
            selected_notes = candidate
            end += 1
        return [
            *protect_paragraphs(content[:start], width=width, height=height, canvas=canvas),
            *protect_images(content[start:end], width=width, height=height, canvas=canvas),
            *protect_paragraphs(content[end:], width=width, height=height, canvas=canvas),
        ]
    return protect_paragraphs(content, width=width, height=height, canvas=canvas)


class _AnnotatedTable(_FlowableGroup):
    """让前置说明跟随表格首段，后置说明跟随最后一段。"""

    def __init__(self, table: Table, before: list[Flowable], after: list[Flowable]) -> None:
        """保存原生表格及说明，继续由 ReportLab 处理表头与合并单元格。"""
        super().__init__([*before, table, *after])
        self.table = table
        self.before = before
        self.after = after

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:
        """分页时只拆表格主体，并为末段保留简短说明所需的高度。"""
        table_height = self.table.wrapOn(self.canv, availWidth, 0xFFFFFF)[1]
        before_height = _measure([*self.before, self.table], availWidth, self.canv) - table_height
        after_height = _measure([self.table, *self.after], availWidth, self.canv) - table_height
        budget = availHeight - before_height
        if table_height <= budget + 1e-6:
            budget -= after_height
        if budget <= 0:
            return []
        # 将容器所在页的位置传给表格，避免在页尾提前拆开普通单元格。
        previous_frame = getattr(self.table, "_frame", None)
        self.table._frame = getattr(self, "_frame", None)
        try:
            parts = self.table.splitOn(self.canv, availWidth, budget)
        finally:
            if previous_frame is None:
                del self.table._frame
            else:
                self.table._frame = previous_frame
        if len(parts) < 2:
            return []
        return [
            _MeasuredKeepTogether([*self.before, *parts[:-1]]),
            _AnnotatedTable(parts[-1], [], self.after),
        ]


def protect_tables(content: list[Flowable], *, width: float, height: float, canvas: Canvas) -> list[Flowable]:
    """保护短表整体，并按表格主体分组处理长表的前后说明。"""
    if not content:
        return []
    if _total_height(content, width, canvas) <= height * TABLE_KEEP_RATIO:
        return [_MeasuredKeepTogether(content)]
    result: list[Flowable] = []
    pending: list[Flowable] = []
    table: Table | None = None
    before: list[Flowable] = []
    for item in [*content, None]:
        if isinstance(item, Table) or item is None:
            if table is not None:
                after = pending
                if _total_height(after, width, canvas) > height * PARAGRAPH_KEEP_RATIO:
                    result.append(_AnnotatedTable(table, before, []) if before else table)
                    result.extend(protect_paragraphs(after, width=width, height=height, canvas=canvas))
                else:
                    result.append(_AnnotatedTable(table, before, after) if before or after else table)
                before = []
            else:
                before = pending
                if _total_height(before, width, canvas) > height * PARAGRAPH_KEEP_RATIO:
                    result.extend(protect_paragraphs(before, width=width, height=height, canvas=canvas))
                    before = []
            table = item
            pending = []
        else:
            pending.append(item)
    if table is None and before:
        result.extend(protect_paragraphs(before, width=width, height=height, canvas=canvas))
    return result
