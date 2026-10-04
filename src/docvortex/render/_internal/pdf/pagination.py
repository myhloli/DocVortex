"""Protect short semantic blocks according to the actual typesetting height, and retain the paging capabilities of long tables."""

from __future__ import annotations

from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, Image, KeepTogether, Paragraph, Table
from reportlab.platypus.flowables import _Container, _listWrapOn

PARAGRAPH_KEEP_RATIO = 0.25
TABLE_KEEP_RATIO = 0.5


class _FlowableGroup(Flowable):
    """Provides a measurable container while allowing ReportLab to bind the preceding header to the container."""

    def __init__(self, content: list[Flowable]) -> None:
        """Saves ordered children without inheriting the inner container base class which would be excluded by title binding logic."""
        super().__init__()
        self._content = content

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        """Measure the true height so that the outer title binding logic can correctly determine the remaining space."""
        self.width, self.height = _listWrapOn(self._content, availWidth, self.canv)
        return self.width, self.height

    def getSpaceBefore(self) -> float:
        """Leave the outer spacing of the first child to the content box."""
        return self._content[0].getSpaceBefore()

    def getSpaceAfter(self) -> float:
        """Leave the outer spacing of the last child to the content box."""
        return self._content[-1].getSpaceAfter()

    def drawOn(self, canv: Canvas, x: float, y: float, _sW: float = 0) -> None:
        """Reuse ReportLab container drawing to maintain alignment, linking and merging spacing of sub-items."""
        _Container.drawOn(self, canv, x, y, _sW=_sW)


class _MeasuredKeepTogether(_FlowableGroup):
    """Engage in typesetting at true height and entrust KeepTogether to page change when space is insufficient."""

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:
        """Preserve KeepTogether downgrade for ultra-high content to avoid infinite paging caused by protection rules."""
        group = KeepTogether(self._content)
        group._frame = getattr(self, "_frame", None)
        return group.splitOn(self.canv, availWidth, availHeight)


def _measure(content: list[Flowable], width: float, canvas: Canvas) -> float:
    """Measure the height combined with adjacent spacing, excluding the outside spacing of the entire container."""
    return _listWrapOn(content, width, canvas)[1] if content else 0.0


def _total_height(content: list[Flowable], width: float, canvas: Canvas) -> float:
    """Calculate the complete height used for protection threshold judgment, including the distance between the front and rear outer sides."""
    if not content:
        return 0.0
    return _measure(content, width, canvas) + content[0].getSpaceBefore() + content[-1].getSpaceAfter()


def protect_paragraphs(content: list[Flowable], *, width: float, height: float, canvas: Canvas) -> list[Flowable]:
    """Protect short text paragraph by paragraph and avoid binding the entire list into one non-pageable chunk."""
    return [
        _MeasuredKeepTogether([item])
        if isinstance(item, Paragraph) and _total_height([item], width, canvas) <= height * PARAGRAPH_KEEP_RATIO
        else item
        for item in content
    ]


def protect_images(content: list[Flowable], *, width: float, height: float, canvas: Canvas) -> list[Flowable]:
    """Bind pictures and short descriptions. When multiple pictures exceed the entire page, they will be downgraded and paginated according to the boundaries of the main body of the picture."""
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
        # Extra-long footnotes should not unbind the figure from the adjacent short figure legend; only put the over-budget caption back into the text flow.
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
    """Let the pre-description follow the first paragraph of the table, and the post-description follow the last paragraph."""

    def __init__(self, table: Table, before: list[Flowable], after: list[Flowable]) -> None:
        """Save the original table and description, and continue to use ReportLab to process headers and merged cells."""
        super().__init__([*before, table, *after])
        self.table = table
        self.before = before
        self.after = after

    def split(self, availWidth: float, availHeight: float) -> list[Flowable]:
        """When paginating, split only the main body of the table and leave the height required for the short description at the end."""
        table_height = self.table.wrapOn(self.canv, availWidth, 0xFFFFFF)[1]
        before_height = _measure([*self.before, self.table], availWidth, self.canv) - table_height
        after_height = _measure([self.table, *self.after], availWidth, self.canv) - table_height
        budget = availHeight - before_height
        if table_height <= budget + 1e-6:
            budget -= after_height
        if budget <= 0:
            return []
        # Pass the position of the page where the container is located to the table to avoid opening ordinary cells in advance at the end of the page.
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
    """Protect the short table as a whole and process the before and after instructions of the long table by grouping the main body of the table."""
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
