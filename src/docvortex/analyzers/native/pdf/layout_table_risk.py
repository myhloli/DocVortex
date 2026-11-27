"""从 Flash 输出的表格内容中发现与正文或图形的角色冲突。"""

from __future__ import annotations

import re

from lxml import etree, html

from ....content.spans import inline_span_plain_text
from ....schema import BBox
from .annotation_text import _caption_identifier, _is_strong_caption_text, _normalize_annotation_text
from .geometry import _coerce_bbox
from .table_constants import _TABLE_CAPTION_RE
from .text_assembly.common import _FIGURE_CAPTION_MARKER_RE

_SOURCE_LABEL = re.compile(r"(?:data sources?|sources?|数据来源|资料来源|来源)\s*[:：]\s*\S", re.IGNORECASE)
_COLUMN_GAP = re.compile(r"[ \t]{2,}")
_SENTENCE_END = re.compile(r"[。！？]|[.!?](?=\s|$)")
_AMOUNT = re.compile(r"[-+−(]?\s*[$€£¥]?\s*(?:\d{1,3}(?:[, .’']\d{3})+|\d+)(?:[.,]\d{1,2})?\s*[%‰)]?")
_PROJECTED_CAPTION = re.compile(r"^\s*(?:tab|表)", re.IGNORECASE | re.MULTILINE)
_STRONG = 60


def _figure_caption_rows(content: str) -> int:
    """确认并排图题、空图面行和来源行被物化成没有图像的单元格。"""
    if not _SOURCE_LABEL.search(content):
        return 0
    try:
        table = html.fromstring(content)
    except (ValueError, etree.ParserError):
        return 0
    if table.tag != "table" or table.xpath(".//img | .//svg | .//object"):
        return 0
    rows = table.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    if len(rows) < 3:
        return 0
    # 中间行必须全部为空，保留真正用表格组织图件和其他单元内容的情况。
    if any("".join(row.itertext()).strip() for row in rows[1:-1]):
        return 0
    figures = {
        _caption_identifier(label)
        for cell in rows[0].iterchildren("td", "th")
        if (label := " ".join(cell.text_content().split()))
        and _FIGURE_CAPTION_MARKER_RE.match(label)
        and _is_strong_caption_text(label)
    }
    sources = sum(bool(_SOURCE_LABEL.match(cell.text_content().strip())) for cell in rows[-1].iterchildren("td", "th"))
    count = len(figures - {None})
    return count if count >= 2 and sources >= count else 0


def _embedded_prose(rows: list[str]) -> tuple[int, int] | None:
    """找出独立表题之前的大段正文和表题之后的重复多列行。"""
    fields = [_COLUMN_GAP.split(row) for row in rows]
    for index, caption_cells in enumerate(fields):
        if len(caption_cells) != 1 or index < 5:
            continue
        title = caption_cells[0]
        if not _TABLE_CAPTION_RE.fullmatch(title) or not _is_strong_caption_text(title):
            continue
        before = [cells[0] for cells in fields[:index] if len(cells) == 1 and sum(char.isalpha() for char in cells[0]) >= 20]
        if len(before) < 2 or sum(len(_SENTENCE_END.findall(line)) for line in before) < 2:
            continue
        after = sum(len(cells) >= 3 for cells in fields[index + 1 : index + 7])
        if after >= 3:
            return len(before), after
    return None


def _region(box: BBox) -> str:
    """把表框转为可在源页和标框中定位的归一化坐标。"""
    return "[" + ", ".join(f"{coord:.3f}" for coord in box) + "]"


def _detached_table_columns(box: BBox, content: str, blocks: list[dict]) -> int:
    """一行表格的文字列名下方如有同一基线的独立数据块，检查表体是否漏在框外。"""
    if content.count("<tr>") != 1:
        return 0
    left, _, right, bottom = box
    width, height = right - left, bottom - box[1]
    row = sorted(
        (
            (b["bbox"], b.get("content"))
            for b in blocks
            if b.get("type") in {"text", "paragraph_title", "doc_title"}
            and not b.get("angle")
            and (rect := b.get("bbox"))
            and left - 0.015 * width <= rect[0] < rect[2] <= right + 0.015 * width
            and rect[2] - rect[0] < 0.55 * width
            and -0.15 * height <= rect[1] - bottom <= min(0.025, 0.75 * height)
        ),
        key=lambda pair: pair[0][0],
    )
    if len(row) < 3 or row[-1][0][2] - row[0][0][0] < 0.65 * width:
        return 0
    boxes = [rect for rect, _ in row]
    if max(b[1] for b in boxes) - min(b[1] for b in boxes) > 0.5 * height or any(
        a[2] > b[0] + 0.005 * width for a, b in zip(boxes, boxes[1:])
    ):
        return 0
    texts = [inline_span_plain_text(value) if isinstance(value, list) else (value or "") for _, value in row]
    if sum(sum(c.isalpha() for c in text) >= 2 for text in texts) < 2:
        return 0
    try:
        cells = html.fromstring(content).xpath("./tr/* | ./tbody/tr/* | ./thead/tr/*")
    except (ValueError, etree.ParserError):
        return 0
    if len(cells) < len(row) or any(
        cell.tag not in {"th", "td"}
        or cell.get("colspan", "1") != "1"
        or cell.get("rowspan", "1") != "1"
        or not any(c.isalpha() for c in cell.text_content())
        for cell in cells
    ):
        return 0
    return len(row)


def _caption_between_data(rows: list[str]) -> str | None:
    """查找投影中被两组多列数据夹住的独立编号表题，排除首格仅为类别名 table 的数据行。"""
    fields = [_COLUMN_GAP.split(row) for row in rows]
    data = [len(cells) >= 3 and sum(any(char.isdigit() for char in cell) for cell in cells) >= 2 for cells in fields]
    before, total = 0, sum(data)
    for cells, is_data in zip(fields, data):
        title = cells[0]
        if (
            before >= 2
            and total - before - is_data >= 2
            and _TABLE_CAPTION_RE.fullmatch(title)
            and _is_strong_caption_text(title)
        ):
            return _caption_identifier(title)
        before += is_data
    return None


def _has_numeric_body(content: str) -> bool:
    """确认截断前已有两行可分别读出名称和至少两个数值的表体，排除说明框和已串值的网格。"""
    try:
        table = html.fromstring(content)
    except (ValueError, etree.ParserError):
        return False
    count = 0
    for row in reversed(table.xpath("./tr[td] | ./tbody/tr[td] | ./tfoot/tr[td]")):
        values = [" ".join(cell.text_content().split()) for cell in row.iterchildren("td", "th")]
        if (
            any(any(ch.isalpha() for ch in value) for value in values)
            and sum(bool(_AMOUNT.fullmatch(value)) for value in values) >= 2
        ):
            count += 1
            if count == 2:
                return True
    return False


def _continued_numeric_rows(box: BBox, blocks: list[dict]) -> int:
    """在表底后的同行文字和独立金额块中，查找连续复用两条数值列线的多行数据。"""
    left, _, right, bottom = box
    width = right - left
    stop = min(1.0, bottom + 0.48)
    cells = []
    for block in blocks:
        if block.get("angle") or not (rect := _coerce_bbox(block.get("bbox"))):
            continue
        x, y, end, base = rect
        if y < bottom - 0.002 or y >= stop or x < left - 0.012 * width or end > right + 0.012 * width:
            continue
        kind = block.get("type")
        if kind in {"caption", "table", "image", "chart", "code", "doc_title", "paragraph_title"} and end - x > 0.45 * width:
            stop = min(stop, y)
        if kind == "text" and base - y <= 0.045 and end - x < 0.55 * width:
            content = block.get("content")
            text = inline_span_plain_text(content) if isinstance(content, list) else content
            if isinstance(text, str) and text.strip():
                cells.append((y, x, end, base, text.strip()))
    if len(cells) < 9:
        return 0
    rows = []
    for cell in sorted(cells):
        y, _, _, base, _ = cell
        if y >= stop:
            break
        if not rows or y - rows[-1][0][0] > 0.40 * min(0.025, base - y, rows[-1][0][3] - rows[-1][0][0]):
            rows.append([])
        rows[-1].append(cell)
    alignments = []
    for row in rows:
        if len(row) < 3 or not any(sum(c.isalpha() for c in cell[4]) >= 2 for cell in row):
            continue
        numbers = sorted((x, end) for _, x, end, _, s in row if end - x < 0.22 * width and _AMOUNT.fullmatch(s))
        if max(cell[2] for cell in row) - min(cell[1] for cell in row) < 0.32 * width:
            continue
        # 某年可以留空或和邻行粘连；其余相邻两列的右缘仍可由各行独立证实。
        for (_, first_end), (next_left, next_end) in zip(numbers, numbers[1:]):
            if next_left - first_end >= 0.025 * width:
                alignments.append((row[0][0], first_end, next_end))
    for y, first, second in alignments:
        if y - bottom > 0.05:
            break
        heights = {cy for cy, a, b in alignments if abs(a - first) <= 0.018 * width and abs(b - second) <= 0.018 * width}
        if len(heights) >= 3 and max(heights) - min(heights) >= 0.038:
            return len(heights)
    return 0


def _inner_grid_caption(content: str) -> str | None:
    """确认一个跨列的独立表题夹在两套数据行之间，题后重新出现多列文字表头。"""
    if "colspan" not in content or not re.search(r">\s*(?:table|tab\.|表)\s*[A-Z0-9一二三四五六七八九十]", content, re.I):
        return None
    try:
        rows = html.fromstring(content).xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    except (ValueError, etree.ParserError):
        return None
    values = [[cell.text_content().strip() for cell in row.iterchildren("td", "th")] for row in rows]
    data = [sum(any(c.isdigit() for c in v) for v in cells) >= 2 for cells in values]
    for i in range(2, len(rows) - 2):
        cells = values[i]
        if len(cells) != 1 or not _TABLE_CAPTION_RE.fullmatch(cells[0]) or not _is_strong_caption_text(cells[0]):
            continue
        if not rows[i].xpath("./td[number(@colspan)>=3] | ./th[number(@colspan)>=3]"):
            continue
        if sum(data[:i]) >= 2 and sum(data[i + 1 :]) >= 2 and sum(any(ch.isalpha() for ch in v) for v in values[i + 1]) >= 2:
            return _caption_identifier(cells[0])
    return None


def table_structure_risk(blocks: list[dict]) -> tuple[int, str] | None:
    """从同一页识别表格错吞图文、相邻表混并及表内数据留在框外的证据。"""
    detached = None
    for block in blocks:
        if block.get("type") != "table" or block.get("angle") or not (box := _coerce_bbox(block.get("bbox"))):
            continue
        content = block.get("content")
        if not isinstance(content, str) or not content:
            continue
        if detached is None and (fields := _detached_table_columns(box, content, blocks)):
            detached = 30, f"表区仅有一行列名，下方紧邻的 {fields} 列数据落在独立文本框，区域 {_region(box)}"
        height = box[3] - box[1]
        if (
            detached is None
            and content.startswith("<table")
            and content.count("<tr") >= 3
            and height >= 0.035
            and box[2] - box[0] >= 0.22
        ):
            if (count := _continued_numeric_rows(box, blocks)) and _has_numeric_body(content):
                detached = 30, f"表内已有独立数值行，表外 {count} 行文字和数值仍沿两条列线对齐，区域 {_region(box)}"
        if detached is None and not content.lstrip().startswith("<") and _PROJECTED_CAPTION.search(content):
            rows = [_normalize_annotation_text(row) for row in content.splitlines() if row.strip()]
            if identifier := _caption_between_data(rows):
                detached = 30, f"表体内的编号表题 {identifier} 前后均有独立多列数据，区域 {_region(box)}"
        if height < 0.10:
            continue
        if content.lstrip().lower().startswith("<table"):
            if identifier := _inner_grid_caption(content):
                return _STRONG, f"已有网格内的表题 {identifier} 跨越多列，其后重启独立表头与数据行，区域 {_region(box)}"
            if count := _figure_caption_rows(content):
                return _STRONG, f"表框内 {count} 个编号图题对应空图面行及来源行，区域 {_region(box)}"
            continue

        if height < 0.20 or box[2] - box[0] < 0.50:
            continue
        rows = [_normalize_annotation_text(row) for row in content.splitlines() if row.strip()]
        if prose := _embedded_prose(rows):
            before, after = prose
            return _STRONG, f"表框内独立表题前有 {before} 行长正文，题后才出现 {after} 行多列数据，区域 {_region(box)}"
    return detached
