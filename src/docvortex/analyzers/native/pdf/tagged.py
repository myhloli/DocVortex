"""将可信 PDF 结构子树映射为现有类别，并对剩余原语执行 Flash 分析。"""

from __future__ import annotations

from copy import deepcopy
from collections import deque
from dataclasses import dataclass, field
import html
from itertools import groupby, islice
import math
import re
from typing import Any

from lxml import etree
import pypdfium2.raw as raw

from ....content.mathml import mathml_to_latex
from ....content.markup.formula import strip_formula_delimiters
from ....content.spans import (
    append_hyperlink_span,
    append_text_span,
    extend_inline_spans,
    inline_span_plain_text,
    slice_span_dicts,
    text_spans,
)
from ....foundation._hyperlink import sanitize_hyperlink_target
from ....foundation._text import merge_text_line_contents, resolve_text_line_boundary
from ....document.pdf.marked_content import MarkedObject, MarkedPage, read_marked_page, render_marked_visuals
from ....document.pdf.native_contracts import PDFDrawingLine, PDFPageTextGeometry, _PDFPageSnapshot
from ....document.pdf.text.spacing import is_generated_cjk_space, is_ligature_expansion_space
from ....document.pdf.structure import ContentReference, PDFStructure, StructureNode
from ....result import Diagnostic
from ....schema import BBox
from .._shared.xycut import sort_entries
from .geometry import _bbox_overlap_in_first, _bbox_union_many
from .layout_diagnostics import (
    GlyphRanges,
    LayoutDiagnosticPage,
    glyph_index_ranges,
    glyph_ranges,
    retain_glyph_members,
    suppress_repeated_marginal_visuals,
)
from .native_text import _sanitize_pdf_control_text

STRUCTURE_KEY = "_pdf_structure"
_TEXT_ROLES = {
    "P",
    "Span",
    "H",
    "Title",
    "Caption",
    "Note",
    "FENote",
    "Code",
    "BibEntry",
    "Link",
    "Quote",
    "BlockQuote",
    "Aside",
    "TOCI",
}
_SECTION_HEADING_ROLE = re.compile(r"_?(?:section[ _-]*)?(?:([1-9])?[ _-]*heading|heading[ _-]*([1-9]))", re.I)
_PARAGRAPH_CHILD_ROLES = frozenset({"P", "L", "H", "Title"})
_BULLET = re.compile(r"^([•▪◦‣])\s*\S")
_NUMBER_PREFIX = re.compile(r"\d{1,4}[.)]?")


def _fold_lines(spans: list[dict]) -> list[dict]:
    """复用既有断词和 URL 跨行规则，按可见偏移裁剪并保留样式与链接。"""
    if len(spans) == 1 and spans[0]["type"] == "text":
        return text_spans(merge_text_line_contents(spans[0]["content"].splitlines()), spans[0].get("styles", ()))
    text = inline_span_plain_text(spans)
    output = []
    for match in re.finditer(r"[^\r\n]+", text):
        line = match[0]
        content = line.strip()
        if not content:
            continue
        start = match.start() + len(line) - len(line.lstrip())
        fragment = slice_span_dicts(spans, start, start + len(content))
        if output:
            previous = inline_span_plain_text(output)
            prefix, separator = resolve_text_line_boundary(previous, next_content=content)
            output = slice_span_dicts(output, 0, len(prefix))
            append_text_span(output, separator)
        extend_inline_spans(output, fragment)
    return output


class _RejectedStructure(ValueError):
    """标记当前最小语义子树无法可靠物化，其成员应回到规则分析。"""


def _box(objects) -> tuple[float, float, float, float] | None:
    """合并有限且有面积的成员框，不把不可定位值传播到公共 schema。"""
    boxes = [
        obj.bbox
        for obj in objects
        if obj.bbox
        and all(math.isfinite(value) for value in obj.bbox)
        and obj.bbox[2] > obj.bbox[0]
        and obj.bbox[3] > obj.bbox[1]
    ]
    return _bbox_union_many(boxes) if boxes else None


def _normalized_box(box, size):
    """把 PDF point 框裁剪到页面并转成公共归一化坐标。"""
    if box is None:
        return None
    width, height = size
    values = [max(0.0, min(1.0, value / (width if index % 2 == 0 else height))) for index, value in enumerate(box)]
    return values if values[2] > values[0] and values[3] > values[1] else None


def _font_styles(font: dict) -> list[str]:
    """把已有原生字体证据转换为公共加粗和斜体样式。"""
    flags = int(font.get("flags") or 0)
    styles = []
    if int(font.get("weight") or 0) >= 600 or flags & (1 << 18):
        styles.append("bold")
    if flags & 64:
        styles.append("italic")
    return styles


def _math(node: StructureNode, text_resolver=None) -> str | None:
    """只把明确的 MathML 或 TeX annotation 转换为公式，替代描述单独保留。"""
    for data in node.mathml:
        try:
            root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
            roots = [root] if etree.QName(root).localname == "math" else root.xpath("//*[local-name()='math']")
            for element in roots:
                supported = {
                    "math",
                    "semantics",
                    "annotation",
                    "annotation-xml",
                    "mrow",
                    "mstyle",
                    "mpadded",
                    "mphantom",
                    "mi",
                    "mn",
                    "mo",
                    "mtext",
                    "mspace",
                    "mfrac",
                    "msqrt",
                    "mroot",
                    "msup",
                    "msub",
                    "msubsup",
                    "mover",
                    "munder",
                    "munderover",
                    "mfenced",
                    "mtable",
                    "mtr",
                    "mlabeledtr",
                    "mtd",
                }
                if any(etree.QName(child).localname not in supported for child in element.iter() if isinstance(child.tag, str)):
                    continue
                value = mathml_to_latex(element)
                if value:
                    return strip_formula_delimiters(value) or None
        except (ValueError, etree.XMLSyntaxError):
            continue
    math_children = [child for child in node.children if isinstance(child, StructureNode) and child.role == "math"]
    if math_children:

        def element(item):
            """将 PDF MathML 命名空间节点还原成仅含数学标签的 XML。"""
            value = etree.Element(item.role)
            value.text = item.actual_text
            if value.text is None and text_resolver is not None and item.role in {"mi", "mn", "mo", "mtext"}:
                value.text = text_resolver(item)
            for child in item.children:
                if isinstance(child, StructureNode):
                    value.append(element(child))
            return value

        value = mathml_to_latex(element(math_children[0]))
        return strip_formula_delimiters(value) if value else None
    return None


def _spans_html(spans: list[dict]) -> str:
    """把既有行内类型编码为表格安全 HTML，文字始终进行转义。"""
    parts = []
    for span in spans:
        if span["type"] == "hyperlink":
            parts.append(f'<a href="{html.escape(span["url"], quote=True)}">{_spans_html(span["content"])}</a>')
        elif span["type"] == "equation_inline":
            parts.append(f"<eq>{html.escape(span['content'])}</eq>")
        else:
            text = html.escape(span["content"])
            for style, tag in (("bold", "b"), ("italic", "i"), ("superscript", "sup"), ("subscript", "sub")):
                if style in span.get("styles", []):
                    text = f"<{tag}>{text}</{tag}>"
            parts.append(text)
    return "".join(parts)


class _PageAssembler:
    """按有序结构引用装配单页内容，独立提交或拒绝每个语义子树。"""

    def __init__(
        self,
        page: MarkedPage,
        diagnostics: list[Diagnostic],
        roles: dict[str, str] | None = None,
        node_pages: dict[str, frozenset[int]] | None = None,
        *,
        collect_layout_diagnostics: bool = False,
    ):
        """缓存引用解析和节点成员集合，避免列表、表格反复扫描页面。"""
        self.page = page
        self.collect_layout_diagnostics = collect_layout_diagnostics
        self.suppressed_layout_glyphs: set[int] = set()
        self.explicit_body_glyphs: set[int] = set()
        self.diagnostics = diagnostics
        self.members: dict[str, tuple] = {}
        self.claimed: set[int] = set()
        self.released_link_text: set[int] = set()
        self.roles = roles or {}
        self.node_pages = node_pages or {}
        self.formulas: dict[str, str | None] = {}
        self.actual_groups: set[tuple] = set()
        self.pending_actual_groups: set[tuple] = set()
        self.pending_actual_members: set[int] = set()
        self.suppressed_spaces: set[int] = set()
        chars = page.geometry.chars
        for index, char in enumerate(chars):
            if (
                0 < index < len(chars) - 1
                and char.get("is_generated")
                and (
                    is_generated_cjk_space(chars[index - 1], char, chars[index + 1], origins=page.geometry.origins)
                    or is_ligature_expansion_space(
                        chars, index, tight_bboxes=page.geometry.tight_bboxes, origins=page.geometry.origins
                    )
                )
            ):
                self.suppressed_spaces.add(char["char_idx"])
        self.reference_index: dict[tuple[str, int], list] = {}
        for obj in page.objects:
            if obj.mcid >= 0 and not obj.artifact:
                self.reference_index.setdefault((obj.stream, obj.mcid), []).append(obj)

    def formula(self, node: StructureNode) -> str | None:
        """复用每个公式的转换结果，并为 MathML 结构叶子提供已关联的原始文字。"""
        if node.identity not in self.formulas:
            self.formulas[node.identity] = _math(node, self._math_text)
        return self.formulas[node.identity]

    def _math_text(self, node: StructureNode) -> str:
        """读取数学叶子的字符值，避免把几何推断结果作为 MathML 的内容。"""
        if node.actual_text is not None:
            return node.actual_text
        return inline_span_plain_text(self._object_spans(self.objects(node), actual_groups=set())).strip()

    def objects(self, node: StructureNode) -> tuple:
        """按内容引用顺序汇总节点成员，验证当前页引用及重复认领。"""
        if not node.valid:
            raise _RejectedStructure(f"Invalid structure node {node.identity}")
        if len(self.node_pages.get(node.identity, ())) > 1 and (node.actual_text is not None or node.role == "Formula"):
            raise _RejectedStructure(f"Cross-page replacement cannot be projected for {node.identity}")
        if node.identity in self.members:
            return self.members[node.identity]
        output = []
        seen = set()
        for child in node.children:
            if isinstance(child, StructureNode):
                found = self.objects(child)
            elif child.page_index == self.page.page_index and child.mcid is not None:
                found = self.reference_index.get((child.stream, child.mcid), [])
                if not found:
                    raise _RejectedStructure(f"Unresolved marked content {child.stream}/{child.mcid}")
            else:
                found = []
            for obj in found:
                if obj.index in seen:
                    raise _RejectedStructure(f"Repeated content member in {node.identity}")
                seen.add(obj.index)
                output.append(obj)
        self.members[node.identity] = tuple(output)
        return self.members[node.identity]

    def _replacement(self, obj, actual_groups: set | None) -> tuple[bool, str]:
        """以原始标记实例应用最外层 ActualText，子树失败时由调用方回滚认领。"""
        mark = next((item for item in obj.marks if item[2] is not None), None)
        if mark is None:
            return False, ""
        key = mark[6], mark[7]
        if actual_groups is not None:
            already = key in actual_groups
            actual_groups.add(key)
        else:
            self.pending_actual_members.add(obj.index)
            already = key in self.actual_groups or key in self.pending_actual_groups
            self.pending_actual_groups.add(key)
        return True, "" if already else mark[2]

    def _object_spans(self, objects, *, actual_groups: set | None = None) -> list[dict]:
        """保留 PDFium 的字符顺序和字体样式，生成空白只在所属对象内消费。"""
        spans: list[dict] = []
        for obj in objects:
            replaced, actual = self._replacement(obj, actual_groups)
            if replaced:
                styles = _font_styles(obj.chars[0].get("font") or {}) if obj.chars else []
                append_text_span(spans, actual, styles)
                trailing = []
                for char in reversed(obj.chars):
                    if not char.get("is_generated") or char.get("char", "").strip():
                        break
                    if char["char_idx"] not in self.suppressed_spaces:
                        trailing.append(char.get("char", ""))
                append_text_span(spans, "".join(reversed(trailing)), styles)
                continue
            for char in obj.chars:
                if char["char_idx"] in self.suppressed_spaces:
                    continue
                value = char.get("char", "").replace("\r\n", "\n").replace("\r", "\n")
                if not value or value == "\x00":
                    continue
                append_text_span(spans, value, _font_styles(char.get("font") or {}))
        for span in spans:
            span["content"] = _sanitize_pdf_control_text(span["content"], preserve_newlines=True).replace("\u00ad", "")
        spans = [span for span in spans if span["content"]]
        return spans

    def spans(self, node: StructureNode) -> list[dict]:
        """按 K 交错顺序输出行内内容，结构 ActualText 对整段只替换一次。"""
        objects = self.objects(node)
        if node.role == "Formula":
            latex = self.formula(node)
            if latex:
                return [{"type": "equation_inline", "content": latex}]
        output = []
        if node.actual_text is not None and objects:
            output = text_spans(node.actual_text)
        else:
            for child in node.children:
                if isinstance(child, StructureNode):
                    extend_inline_spans(output, self.spans(child))
                elif child.page_index == self.page.page_index and child.mcid is not None:
                    extend_inline_spans(output, self._object_spans(self.reference_index.get((child.stream, child.mcid), [])))
        if node.role == "Link":
            targets = [
                self.page.annotations.get(child.object_id)
                for child in node.children
                if isinstance(child, ContentReference) and child.object_id
            ]
            target = next((item[1] for item in targets if item and item[1]), None)
            linked = []
            append_hyperlink_span(linked, output, target)
            return linked
        if not output and node.role == "Formula" and objects:
            return text_spans(node.actual_text or node.alt or "")
        styles = []
        if node.role in {"Strong", "Em"}:
            styles.append("bold" if node.role == "Strong" else "italic")
        layout = node.attributes.get("Layout", {})
        decoration = layout.get("TextDecorationType")
        if decoration in {"/Underline", "/LineThrough"}:
            styles.append("underline" if decoration == "/Underline" else "strikethrough")
        shift = layout.get("BaselineShift")
        if isinstance(shift, (int, float)) and shift:
            styles.append("superscript" if shift > 0 else "subscript")
        if styles:
            for span in output:
                if span["type"] == "text":
                    span["styles"] = list(dict.fromkeys([*span.get("styles", []), *styles]))
        return output

    def block(self, node: StructureNode, kind: str, content, *, logical_id=None, member_override=None) -> dict:
        """创建带来源及原始顺序的 raw block，定位缺失时要求上层回退。"""
        members = self.objects(node) if member_override is None else member_override
        if kind in {"image", "table"}:
            annotations = {
                obj.index
                for child in node.children
                if isinstance(child, StructureNode) and child.role in {"Caption", "Note", "FENote"}
                for obj in self.objects(child)
            }
            members = tuple(obj for obj in members if obj.index not in annotations)
        box = _normalized_box(_box(members), self.page.size)
        if box is None:
            raise _RejectedStructure(f"No content geometry for {node.identity}")
        block = {
            "type": kind,
            "content": content,
            "bbox": box,
            STRUCTURE_KEY: {"node_id": node.identity, "logical_id": logical_id or node.identity, "order": node.order},
            "_paragraph_boundary": True,
        }
        if kind in {"text", "paragraph_title", "doc_title", "page_footnote", "ref_text"}:
            block["lines"] = [{"bbox": box}]
        if kind in {"image", "equation"}:
            block[STRUCTURE_KEY]["members"] = [obj.index for obj in members]
        if self.collect_layout_diagnostics:
            if kind not in {"list", "index", "table"}:
                retain_glyph_members(block, glyph_ranges(char for obj in members for char in obj.chars))
            if node.role in {"P", "BlockQuote", "Quote"} and kind == "text":
                self.explicit_body_glyphs.update(char["char_idx"] for obj in members for char in obj.chars)
        return block

    @staticmethod
    def _neighboring_glyphs(left: dict, right: dict) -> bool:
        """通过首尾墨迹的间距和实际字形基线确认同一物理行的近邻。"""
        a, b = (char.get("tight_bbox") or char["bbox"] for char in (left, right))
        height = max(a[3] - a[1], b[3] - b[1])
        gap = b[0] - a[2]
        origin_a, origin_b = left.get("origin"), right.get("origin")
        delta = (
            abs(origin_a[1] - origin_b[1])
            if origin_a is not None and origin_b is not None
            else abs((a[1] + a[3]) - (b[1] + b[3])) / 2
        )
        return -0.15 * height <= gap <= 1.5 * height and delta <= 0.4 * height

    def _is_inline_number(self, paragraph: StructureNode, child: StructureNode) -> bool:
        """短数字紧接同一行的题干或被同一行字形夹住时，保留其原来的行内归属。"""
        if child.role != "P" or child.actual_text is not None:
            return False
        members = self.objects(child)
        chars = list(islice((char for obj in members for char in obj.chars if char["char"].strip()), 6))
        if not _NUMBER_PREFIX.fullmatch("".join(char["char"] for char in chars)):
            return False

        child_objects = {obj.index for obj in members}
        seen_number = False
        previous = None
        for obj in self.objects(paragraph):
            if obj.index in child_objects:
                seen_number = True
                continue
            if not seen_number:
                previous = next((char for char in reversed(obj.chars) if char["char"].strip()), previous)
                continue
            following = next((char for char in obj.chars if char["char"].strip()), None)
            if following is None:
                continue
            if not self._neighboring_glyphs(chars[-1], following):
                return False
            return following["char"].isalpha() if previous is None else self._neighboring_glyphs(previous, chars[0])
        return False

    def _inline_parts(self, node: StructureNode, *, paragraph: StructureNode | None = None):
        """把嵌套行内结构分成文字片段和独立视觉块，保留原始交错顺序。"""
        if node.role == "Artifact":
            return
        if node.role == "Formula" and self.formula(node):
            yield self.spans(node), self.objects(node)
            return
        if node.actual_text is not None:
            yield self.spans(node), self.objects(node)
            return
        if node.role in {"Figure", "Table"}:
            yield from self.emit(node)
            return
        if node.role == "Formula":
            spans = self.spans(node)
            if spans:
                yield spans, self.objects(node)
            else:
                yield from self.emit(node)
            return
        if (
            node.role in {"Link", "Strong", "Em", "Code"}
            or node.attributes.get("Layout", {}).get("TextDecorationType")
            or node.attributes.get("Layout", {}).get("BaselineShift")
        ):
            yield self.spans(node), self.objects(node)
            return
        for child in node.children:
            if isinstance(child, StructureNode):
                is_block = child.role in _PARAGRAPH_CHILD_ROLES or re.fullmatch(r"H[1-9][0-9]*", child.role)
                if paragraph is not None and is_block and not self._is_inline_number(paragraph, child):
                    yield from self.emit(child)
                else:
                    yield from self._inline_parts(child, paragraph=paragraph)
            elif child.page_index == self.page.page_index and child.mcid is not None:
                members = self.reference_index.get((child.stream, child.mcid), [])
                yield self._object_spans(members), members

    def _paragraph(self, node: StructureNode, kind: str) -> list[dict]:
        """在纯图形公式处分段，文字片段框仅覆盖对应字符和对象。"""
        blocks, spans, members = [], [], []
        for part in self._inline_parts(node, paragraph=node if node.role == "P" else None):
            if isinstance(part, dict):
                if spans:
                    blocks.append(self.block(node, kind, _fold_lines(spans), member_override=members))
                    spans, members = [], []
                blocks.append(part)
            else:
                extend_inline_spans(spans, part[0])
                members.extend(part[1])
        if spans:
            content = spans if node.actual_text is not None else _fold_lines(spans)
            blocks.append(self.block(node, kind, content, member_override=members))
        if node.role != "P" or len(blocks) < 2:
            return blocks
        self._restore_mapped_heading(node, blocks)
        return self._group_nested_bullets(node, blocks)

    def _restore_mapped_heading(self, node: StructureNode, blocks: list[dict]) -> None:
        """作者把标题映射成 P 时，只提升位于后续独立正文前的短标题字形段。"""
        role = _SECTION_HEADING_ROLE.fullmatch(node.raw_role)
        first, following = blocks[:2]
        if (
            role is None
            or first[STRUCTURE_KEY]["node_id"] != node.identity
            or following[STRUCTURE_KEY]["node_id"] == node.identity
            or following["type"] != "text"
            or not 0 < len(inline_span_plain_text(first["content"])) <= 160
            or first["bbox"][3] >= following["bbox"][1]
            or first["bbox"][0] > following["bbox"][0] + 0.012
            or first["bbox"][3] - first["bbox"][1] > 0.06
        ):
            return
        first["type"] = "paragraph_title"
        first["level"] = min(int(role[1] or role[2] or 1) + 1, 6)

    def _group_nested_bullets(self, node: StructureNode, blocks: list[dict]) -> list[dict]:
        """相邻 P 子段有项目符号和相同悬挂缩进时，保留其各自文字与来源并组成列表。"""
        indents = {}
        for child in node.children:
            if not isinstance(child, StructureNode) or child.role != "P":
                continue
            layout = child.attributes.get("Layout", {})
            start, first = layout.get("StartIndent"), layout.get("TextIndent")
            if isinstance(start, (int, float)) and isinstance(first, (int, float)) and start > first:
                indents[child.identity] = (start, first)
        if len(indents) < 2:
            return blocks

        def bullet_key(block):
            """仅识别带明确缩进属性和可见项目符号的已分离子段。"""
            if block["type"] == "text" and (indent := indents.get(block[STRUCTURE_KEY]["node_id"])):
                if mark := _BULLET.match(inline_span_plain_text(block["content"]).lstrip()):
                    return mark[1], indent
            return None

        output = []
        for key, grouped in groupby(blocks, key=bullet_key):
            items = list(grouped)
            if key is None or len(items) < 2:
                output.extend(items)
                continue
            first = items[0][STRUCTURE_KEY]
            output.append({
                "type": "list",
                "content": items,
                "bbox": list(_bbox_union_many([item["bbox"] for item in items])),
                STRUCTURE_KEY: {
                    "node_id": node.identity,
                    "logical_id": first["logical_id"],
                    "order": first["order"],
                },
                "_paragraph_boundary": True,
            })
        return output

    def _table(self, node: StructureNode) -> dict:
        """依据标签行和单元格跨度建立占位网格，冲突或过大表格整体回退。"""
        rows = []
        for child in node.children:
            if isinstance(child, StructureNode) and child.role == "TR":
                rows.append(child)
            elif isinstance(child, StructureNode) and child.role in {"THead", "TBody", "TFoot"}:
                rows.extend(item for item in child.children if isinstance(item, StructureNode) and item.role == "TR")
        occupied: set[tuple[int, int]] = set()
        cells_by_row = []
        for row_index, row in enumerate(rows):
            column = 0
            for cell in row.children:
                if not isinstance(cell, StructureNode) or cell.role not in {"TD", "TH"}:
                    continue
                while (row_index, column) in occupied:
                    column += 1
                attributes = cell.attributes.get("Table", {})
                row_span, col_span = attributes.get("RowSpan", 1), attributes.get("ColSpan", 1)
                if (
                    type(row_span) is not int
                    or type(col_span) is not int
                    or not 1 <= row_span <= len(rows) - row_index
                    or not 1 <= col_span <= 1024
                ):
                    raise _RejectedStructure(f"Invalid table span in {cell.identity}")
                slots = {(r, c) for r in range(row_index, row_index + row_span) for c in range(column, column + col_span)}
                if occupied & slots or len(occupied) + len(slots) > 100_000:
                    raise _RejectedStructure(f"Conflicting table cells in {cell.identity}")
                occupied.update(slots)
                column += col_span
                cells_by_row.append((row_index, column - col_span, row_span, col_span, cell))
        columns = max((column for _, column in occupied), default=-1) + 1
        if not columns or len(occupied) != len(rows) * columns:
            raise _RejectedStructure(f"Incomplete table grid in {node.identity}")
        selected_rows = [index for index, row in enumerate(rows) if self.objects(row)]
        projected: dict[int, list[tuple[int, str]]] = {index: [] for index in selected_rows}
        projected_cells = []
        for row_index, column, row_span, col_span, cell in cells_by_row:
            covered = [index for index in selected_rows if row_index <= index < row_index + row_span]
            if not covered:
                continue
            content = _spans_html(_fold_lines(self.spans(cell)))
            scope = {"/Column": "col", "/Row": "row"}.get(cell.attributes.get("Table", {}).get("Scope"))
            extra = f' scope="{scope}"' if scope and cell.role == "TH" else ""
            tag = cell.role.lower()
            value = f'<{tag} rowspan="{len(covered)}" colspan="{col_span}"{extra}>{content}</{tag}>'
            projected[covered[0]].append((column, value))
            if self.collect_layout_diagnostics:
                projected_cells.append(cell)
        rendered = ["<tr>" + "".join(value for _, value in sorted(projected[index])) + "</tr>" for index in selected_rows]
        if not rendered:
            raise _RejectedStructure(f"No table rows for {node.identity}")
        block = self.block(node, "table", "<table>" + "".join(rendered) + "</table>")
        if self.collect_layout_diagnostics:
            retain_glyph_members(block, glyph_ranges(char for cell in projected_cells for obj in self.objects(cell) for char in obj.chars))
        return block

    def _list(self, node: StructureNode) -> list[dict]:
        """按条目次序生成列表，视觉成员分隔列表片段并保留共同逻辑身份。"""
        blocks, items = [], []
        for item in node.children:
            if not isinstance(item, StructureNode):
                continue
            if item.role == "L":
                nested = self._list(item)
                items.extend(nested)
                continue
            if item.role != "LI" or not self.objects(item):
                continue
            pending = []
            for child in item.children:
                if isinstance(child, StructureNode) and child.role in {"Lbl", "LBody"}:
                    pending.extend(child.children if child.role == "LBody" else [child])
                else:
                    pending.append(child)
            spans = []
            for child in pending:
                if isinstance(child, StructureNode) and child.role in {"Figure", "Table", "L"}:
                    if spans:
                        items.append(self.block(item, "text", _fold_lines(spans), logical_id=item.identity))
                        spans = []
                    if child.role == "L":
                        items.extend(self._list(child))
                    else:
                        if items:
                            blocks.append(self.block(node, "list", items))
                            items = []
                        blocks.extend(self.emit(child))
                elif isinstance(child, StructureNode):
                    new_spans = self.spans(child)
                    if spans and new_spans and child.role != "Lbl" and not inline_span_plain_text(spans).endswith((" ", "\n")):
                        append_text_span(spans, " ")
                    spans.extend(new_spans)
                elif child.page_index == self.page.page_index and child.mcid is not None:
                    spans.extend(self._object_spans(self.reference_index.get((child.stream, child.mcid), [])))
            if spans:
                items.append(self.block(item, "text", _fold_lines(spans), logical_id=item.identity))
        if items:
            blocks.append(self.block(node, "list", items))
        return blocks

    def emit(self, node: StructureNode) -> list[dict]:
        """将一个可独立接受的子树投影为现有类别，不在此阶段提交成员认领。"""
        if not node.valid:
            raise _RejectedStructure(f"Invalid structure node {node.identity}")
        objects = self.objects(node)
        if not objects:
            return []
        role = node.role
        if role == "L":
            return self._list(node)
        if role in {"TOC", "Index"}:
            children = [block for child in node.children if isinstance(child, StructureNode) for block in self.emit(child)]
            return [self.block(node, "index", children)] if children else []
        if role == "Table":
            blocks = [self._table(node)]
        elif role == "Figure":
            blocks = [self.block(node, "image", html.escape(node.alt or ""))]
        elif role == "Formula":
            blocks = [self.block(node, "equation", self.formula(node) or "")]
        elif role == "Code":
            blocks = [self.block(node, "code", inline_span_plain_text(self.spans(node)))]
            language = node.attributes.get("Code", {}).get("Language")
            blocks[0]["guess_lang"] = language.lstrip("/") if isinstance(language, str) and language.strip("/") else "txt"
        else:
            kind = {
                "Title": "doc_title",
                "Note": "page_footnote",
                "FENote": "page_footnote",
                "BibEntry": "ref_text",
                "Aside": "aside_text",
            }.get(role, "text")
            level = None
            if role == "H" or re.fullmatch(r"H[1-9][0-9]*", role):
                kind = "paragraph_title"
                level = min((max(1, node.heading_depth) if role == "H" else int(role[1:])) + 1, 6)
            visual_parent = next(
                (reference for reference in node.references if self.roles.get(reference) in {"Figure", "Table"}), None
            )
            if role == "Caption" and visual_parent is not None:
                kind = "image_caption" if self.roles[visual_parent] == "Figure" else "table_caption"
            blocks = self._paragraph(node, kind)
            if not blocks:
                if node.actual_text == "" or all(obj.index in self.pending_actual_members for obj in objects):
                    return []
                raise _RejectedStructure(f"Empty text for {node.identity}")
            for block in blocks:
                if block["type"] == kind and level is not None:
                    block["level"] = level
                if block["type"] == "doc_title":
                    block["level"] = 1
                if role == "Caption" and visual_parent is not None:
                    block[STRUCTURE_KEY]["visual_parent"] = visual_parent
        if role in {"Figure", "Table"}:
            for child in node.children:
                if isinstance(child, StructureNode) and child.role == "Caption" and self.objects(child):
                    caption = self.block(
                        child, "image_caption" if role == "Figure" else "table_caption", _fold_lines(self.spans(child))
                    )
                    caption[STRUCTURE_KEY]["visual_parent"] = node.identity
                    blocks.append(caption)
        return blocks

    def _linked_raster(self, node: StructureNode, members: tuple) -> tuple[list[dict], set[int]] | None:
        """以注解与源位图的双向覆盖验证直接 Figure，非链接范围内的附带字形回归原生归属。"""
        if node.role != "Link" or node.actual_text is not None:
            return None
        figures = [child for child in node.children if isinstance(child, StructureNode) and child.role == "Figure"]
        if len(figures) != 1:
            return None
        figure = figures[0]
        if any(isinstance(child, StructureNode) for child in figure.children) or any(
            isinstance(child, StructureNode) and child is not figure for child in node.children
        ):
            return None
        annotations = [
            self.page.annotations[ref.object_id]
            for ref in node.children
            if isinstance(ref, ContentReference) and ref.object_id in self.page.annotations
        ]
        if len(annotations) != 1 or not annotations[0][1]:
            return None
        link_box, uri = annotations[0]
        target = sanitize_hyperlink_target(uri)
        if not target:
            return None
        visual_members = self.objects(figure)
        visual_ids = {obj.index for obj in visual_members}
        raster = [obj for obj in visual_members if obj.kind == raw.FPDF_PAGEOBJ_IMAGE and obj.bbox]
        if (
            len(members) <= len(visual_ids)
            or len(raster) != 1
            or any(obj.kind not in {raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH} for obj in visual_members)
        ):
            return None
        box = _box(visual_members)
        if box is None or min(_bbox_overlap_in_first(box, link_box), _bbox_overlap_in_first(link_box, box)) < 0.9:
            return None
        # 链外对象必须全是可回退的字形，不跳过第二张图，也不拆分真实混合链接。
        outside = [obj for obj in members if obj.index not in visual_ids]
        if any(
            obj.kind != raw.FPDF_PAGEOBJ_TEXT or obj.bbox is None or _bbox_overlap_in_first(obj.bbox, link_box) > 0
            for obj in outside
        ) or not any(char.get("char", "").strip() for obj in outside for char in obj.chars):
            return None
        if any(
            obj.kind == raw.FPDF_PAGEOBJ_TEXT
            and obj.index not in visual_ids
            and not obj.artifact
            and obj.bbox is not None
            and any(c.get("char", "").strip() for c in obj.chars)
            and _bbox_overlap_in_first(obj.bbox, box) > 0.1
            for obj in self.page.objects
        ):
            return None
        # 仅接管先绘制的未标记底板。其他有竞争归属的源绘制保留原 Link 处理路径。
        underlays = set()
        for obj in self.page.objects:
            if (
                obj.kind not in {raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_SHADING}
                or obj.index in visual_ids
                or obj.artifact
                or obj.bbox is None
                or _bbox_overlap_in_first(obj.bbox, box) < 0.5
            ):
                continue
            raster_coverage = _bbox_overlap_in_first(box, obj.bbox)
            if not (
                obj.kind == raw.FPDF_PAGEOBJ_PATH
                and obj.path_index is not None
                and obj.index < raster[0].index
                and obj.index not in self.claimed
                and not obj.marks
                and obj.mcid < 0
                and _bbox_overlap_in_first(obj.bbox, box) >= 0.95
                and 0.45 <= raster_coverage <= 0.95
            ):
                return None
            underlays.add(obj.index)
        blocks = self.emit(figure)
        blocks[0][STRUCTURE_KEY]["link_target"] = target
        blocks[0][STRUCTURE_KEY]["members"] = sorted(visual_ids | underlays)
        blocks[0]["_linked_raster_underlay"] = bool(underlays)
        return blocks, visual_ids | underlays

    def visit(self, node: StructureNode) -> list[dict]:
        """容器逐个处理子树，语义节点在检查及物化全部成功后原子认领成员。"""
        if node.role == "Artifact":
            try:
                self.claimed.update(obj.index for obj in self.objects(node))
            except _RejectedStructure:
                pass
            return []
        semantic = node.role in _TEXT_ROLES | {"L", "Table", "Figure", "Formula", "TOC", "Index"} or bool(
            re.fullmatch(r"H[1-9][0-9]*", node.role)
        )
        if not semantic:
            output = []
            for child in node.children:
                if isinstance(child, StructureNode):
                    output.extend(self.visit(child))
            return output
        try:
            members = self.objects(node)
            if self.claimed.intersection(obj.index for obj in members):
                raise _RejectedStructure(f"Content already owned before {node.identity}")
            linked_raster = self._linked_raster(node, members)
            if linked_raster is not None:
                blocks, claim = linked_raster
                self.claimed.update(claim)
                self.released_link_text.update(obj.index for obj in members if obj.index not in claim)
                return blocks
            blocks = self.emit(node)
            if blocks or node.actual_text == "" or all(obj.index in self.pending_actual_members for obj in members):
                self.claimed.update(obj.index for obj in members)
                self.actual_groups.update(self.pending_actual_groups)
                if self.collect_layout_diagnostics and not blocks:
                    self.suppressed_layout_glyphs.update(char["char_idx"] for obj in members for char in obj.chars)
            return blocks
        except (ValueError, TypeError, KeyError) as error:
            self.diagnostics.append(
                Diagnostic("pdf_structure_fallback", f"node={node.identity}: {error}", self.page.page_index)
            )
            return []
        finally:
            self.pending_actual_groups.clear()
            self.pending_actual_members.clear()


def _without_marked_drawings(drawings: list[PDFDrawingLine], removed: list[MarkedObject]) -> list[PDFDrawingLine]:
    """移除由已排除的标记路径绘制的横竖线，保留其他路径的几何。"""
    removed_boxes = [obj.bbox for obj in removed if obj.bbox and obj.kind == raw.FPDF_PAGEOBJ_PATH]
    if not removed_boxes:
        return drawings
    return [
        line
        for line in drawings
        if not any(
            box[0] <= line.bbox[0] and box[1] <= line.bbox[1] and box[2] >= line.bbox[2] and box[3] >= line.bbox[3]
            for box in removed_boxes
        )
    ]


class _ResidualSource:
    """向现有 Flash 编排提供仅含未认领原语的页面快照。"""

    def __init__(
        self,
        document,
        pages: list[MarkedPage],
        claimed: list[set[int]] | None,
        trusted_blocks: list[list[dict]],
        *,
        layout_body_glyphs: dict[int, GlyphRanges] | None = None,
    ):
        """保留选页映射及自有成员集合，原件仍由上层输入生命周期管理。"""
        self.document, self.pages, self.claimed = document, pages, claimed
        self.trusted_blocks = trusted_blocks
        self.page_count = len(pages)
        self.layout_body_glyphs = layout_body_glyphs
        self.full_layout_pages: dict[int, LayoutDiagnosticPage] = {}
        self.reference_header_pages: set[int] = set()

    def layout_evidence(self, page_idx: int, snapshot: _PDFPageSnapshot | None = None) -> LayoutDiagnosticPage:
        """残余几何读取前冻结全页的可见原字和图件，完整结构页按需读取一次证据。"""
        if page_idx not in self.full_layout_pages:
            page = self.pages[page_idx]
            if snapshot is None:
                snapshot = self.document._extract_native_page(page.page_index)
            owner_ids = self.claimed[page_idx] if self.claimed is not None else set()
            rendered = {
                member
                for block in self.trusted_blocks[page_idx]
                for member in block.get(STRUCTURE_KEY, {}).get("members", ())
            }
            unrendered_paths = {
                obj.path_index for obj in page.objects
                if obj.path_index is not None and obj.index in owner_ids and obj.index not in rendered and not obj.artifact
            }
            self.full_layout_pages[page_idx] = _tagged_layout_evidence(
                snapshot, page, (self.layout_body_glyphs or {}).get(page_idx, b""), unrendered_paths, self.page_count >= 3
            )
        return self.full_layout_pages[page_idx]

    def native_trusted_text_bboxes(self, page_idx: int):
        """供残余金额表避开已由结构认领的文字，保持原生回退字流的既有成员。"""
        if self.claimed is None:
            return ()
        width, height = self.pages[page_idx].size
        return tuple(
            (box[0] * width, box[1] * height, box[2] * width, box[3] * height)
            for block in self.trusted_blocks[page_idx]
            if block.get("type") in {"text", "paragraph_title", "doc_title"} and (box := block.get("bbox"))
        )

    def _extract_native_page(self, page_idx):
        """在完整可见快照上移除已认领成员，原始字符索引及其几何映射保持对应。"""
        page = self.pages[page_idx]
        snapshot = self.document._extract_native_page(page.page_index)
        if self.claimed is None:
            return snapshot
        removed = [obj for obj in page.objects if obj.index in self.claimed[page_idx] or obj.artifact]
        if not removed:
            return snapshot
        from .text_assembly.continuity import _is_reference_heading

        if any(
            obj.artifact and obj.chars and obj.bbox and obj.bbox[3] <= 0.10 * page.size[1]
            and _is_reference_heading("".join(c.get("char", "") for c in obj.chars))
            for obj in removed
        ):
            self.reference_header_pages.add(page_idx)
        if self.layout_body_glyphs is not None:
            self.layout_evidence(page_idx, snapshot)
        indices = {index for obj in removed for index in obj.raw_indices}
        paths = {obj.path_index for obj in removed if obj.path_index is not None}
        image_instances: dict[tuple, deque] = {}
        for obj in page.objects:
            if obj.kind == raw.FPDF_PAGEOBJ_IMAGE and obj.bbox is not None:
                image_instances.setdefault(obj.bbox, deque()).append(obj.index)
        removed_ids = {obj.index for obj in removed}
        images = []
        for image in snapshot.image_infos:
            instances = image_instances.get(image.bbox)
            if not instances or instances.popleft() not in removed_ids:
                images.append(image)
        source = snapshot.text_geometry
        geometry = PDFPageTextGeometry(
            [char for char in source.chars if not indices.intersection(char.get("source_indices", (char["char_idx"],)))],
            {key: box for key, box in source.tight_bboxes.items() if key not in indices},
            {key: origin for key, origin in source.origins.items() if key not in indices},
            {key: box for key, box in source.loose_bboxes.items() if key not in indices},
        )
        kept_paths = [path for path in snapshot.path_infos if path.source_index not in paths]
        return _PDFPageSnapshot(
            snapshot.page_size,
            snapshot.rotation,
            geometry,
            _without_marked_drawings(snapshot.drawing_lines, removed),
            kept_paths,
            images,
            [],
            snapshot.signature_bboxes,
            snapshot.link_annotations,
        )


def _tagged_layout_evidence(
    snapshot: _PDFPageSnapshot,
    page: MarkedPage,
    explicit_body: GlyphRanges,
    unrendered_paths: set[int],
    collect_margins: bool,
) -> LayoutDiagnosticPage:
    """结构页仍按可见原字和独立图片检查，跳过 Artifact 与已过滤的不可见绘制。"""
    from .annotation_text import _is_strong_caption_text
    from .graphics import _detect_strong_graphic_bboxes, _raster_is_native_text_background
    from .models import _PageSource
    from .native_text import _build_native_line_items_from_chars, _coerce_pdf_drawing_lines, _filter_native_direction_chars
    from .raster_backdrops import filter_panel_backdrops
    from .raster_composites import occluded_raster_boxes

    artifacts = [obj for obj in page.objects if obj.artifact]
    artifact_indices = {index for obj in artifacts for index in obj.raw_indices}
    chars = [
        char for char in snapshot.text_geometry.chars
        if not artifact_indices.intersection(char.get("source_indices", (char["char_idx"],)))
    ]
    chars = _filter_native_direction_chars(chars, page_rotation=snapshot.rotation)
    lines = _build_native_line_items_from_chars(chars, snapshot.page_size, page_rotation=snapshot.rotation)
    evidence = LayoutDiagnosticPage(snapshot.page_size)
    evidence.record_rows([line for line in lines if not _is_strong_caption_text(line.text)], explicit_body=explicit_body)
    drawing_lines = _coerce_pdf_drawing_lines(snapshot.drawing_lines)
    visible_drawings = _without_marked_drawings(snapshot.drawing_lines, artifacts)
    artifact_paths = {obj.path_index for obj in artifacts if obj.path_index is not None}
    evidence.record_visual_tables(
        lines,
        drawing_lines if visible_drawings is snapshot.drawing_lines else _coerce_pdf_drawing_lines(visible_drawings),
        [path for path in snapshot.path_infos if path.source_index not in artifact_paths]
        if artifact_paths
        else snapshot.path_infos,
    )
    source = _PageSource(snapshot.page_size, lines, chars, [])
    art_images = {obj.bbox for obj in artifacts if obj.kind == raw.FPDF_PAGEOBJ_IMAGE}
    images = filter_panel_backdrops(
        source,
        [info for info in snapshot.image_infos if info.bbox not in art_images],
        {},
    )
    have_prose = len(lines) >= 3 and sum(len(line.text.strip()) for line in lines) >= 60
    raster_boxes = [
        info.bbox for info in images
        if not (info.smooth_background and have_prose) and not _raster_is_native_text_background(source, info.bbox)
    ]
    source.raster_composite_infos = tuple(info for info in images if info.occluded_inner_bboxes)
    hidden = occluded_raster_boxes(source, set(raster_boxes)) if source.raster_composite_infos else set()
    evidence.record_visuals("位图", (box for box in raster_boxes if box not in hidden))
    if collect_margins:
        evidence.record_marginal_rasters({info.bbox: info.fingerprint for info in images if info.fingerprint})
    if unrendered_paths:
        from .table_detection import _detect_aligned_numeric_tables

        # 已交付的结构图由自身成员验证，几何候选只补查被结构文字或表格认领的路径。
        source.path_infos = [info for info in snapshot.path_infos if info.source_index in unrendered_paths]
        source.drawing_lines = [
            line for line in drawing_lines
            if any(
                path.bbox[0] <= line.bbox[0] <= line.bbox[2] <= path.bbox[2]
                and path.bbox[1] <= line.bbox[1] <= line.bbox[3] <= path.bbox[3]
                for path in source.path_infos
            )
        ]
        strong_graphics = _detect_strong_graphic_bboxes(source)
        if strong_graphics:
            from .geometry import _bbox_area, _bbox_overlap_in_smaller

            numeric_tables = _detect_aligned_numeric_tables(source, [])
            evidence.record_vector_visuals(
                box for box in strong_graphics if not any(
                    _bbox_overlap_in_smaller(box, table.bbox) >= 0.5 and _bbox_area(box) >= 0.25 * _bbox_area(table.bbox)
                    for table in numeric_tables
                )
            )
            if collect_margins:
                evidence.record_marginal_vectors(source.path_infos)
    return evidence


def _ordered_merge(trusted: list[dict], fallback: list[dict], page_size: tuple[float, float]) -> list[dict]:
    """结构和规则序列分别保序，仅用 PDF point 几何决定两者的插入位置。"""
    if not trusted:
        return fallback
    moved, kept = [], []
    for block in trusted:
        (moved if block.pop("_linked_raster_underlay", False) else kept).append(block)
    trusted = kept
    if not fallback and not moved:
        return trusted
    if not trusted and not moved:
        return fallback
    candidates = sort_entries(
        [
            {"bbox": [value * page_size[index % 2] for index, value in enumerate(block["bbox"])], "block": block}
            for block in [*trusted, *fallback, *moved]
            if block.get("bbox")
        ]
    )
    ranks = {id(item["block"]): index for index, item in enumerate(candidates)}
    pending = deque(fallback)
    for block in moved:
        index = next((i for i, item in enumerate(pending) if ranks.get(id(item), len(ranks)) > ranks[id(block)]), len(pending))
        pending.insert(index, block)
    output = []
    for block in trusted:
        rank = ranks.get(id(block), len(ranks))
        while pending and ranks.get(id(pending[0]), len(ranks)) < rank:
            output.append(pending.popleft())
        output.append(block)
    output.extend(pending)
    return output


@dataclass
class TaggedAnalysis:
    """在分类前准备结构输出，分类通过后才运行必要的规则回退。"""

    document: Any
    structure: PDFStructure
    pages: list[MarkedPage]
    blocks: list[list[dict]]
    claimed: list[set[int]]
    diagnostics: list[Diagnostic]
    trusted_order: bool = True
    orphan_caption_roles: dict[str, str] = field(default_factory=dict)
    released_link_text: dict[int, set[int]] = field(default_factory=dict)

    frame_parents: dict[str, str] = field(default_factory=dict)
    raster_sibling_parents: dict[str, str] = field(default_factory=dict)
    collect_layout_diagnostics: bool = False
    layout_body_glyphs: dict[int, GlyphRanges] = field(default_factory=dict)
    suppressed_layout_glyphs: dict[int, GlyphRanges] = field(default_factory=dict)
    layout_diagnostics: tuple[Diagnostic, ...] = ()
    layout_program_controls: dict[int, tuple[BBox, ...]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """确认可读结构覆盖；已从错误链接释放的原生字形仍有可信引用。"""
        return (
            self.trusted_order
            and bool(self.structure.roots)
            and all(page.available for page in self.pages)
            and all(
                obj.artifact
                or obj.index in claimed
                or obj.index in released
                or (
                    not any(char.get("char", "").strip() for char in obj.chars)
                    and obj.kind not in {raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH}
                )
                for page, claimed in zip(self.pages, self.claimed, strict=True)
                for released in (self.released_link_text.get(page.page_index, ()),)
                for obj in page.objects
            )
        )

    def predict(self) -> list[list[dict]]:
        """合并结构输出与剩余原语的 Flash 结果，每次返回独立 raw block。"""
        from .pipeline import _analyze_native_document

        residual = _ResidualSource(
            self.document, self.pages, self.claimed if self.trusted_order else None, self.blocks,
            layout_body_glyphs=self.layout_body_glyphs if self.collect_layout_diagnostics else None,
        )
        layout_pages = [] if self.collect_layout_diagnostics else None
        # 链外文本可证明是原生内容，但仍需由残余管线重建其归属和阅读位置。
        fallback = (
            [[] for _ in self.pages]
            if self.complete and not self.released_link_text
            else _analyze_native_document(residual, **({"layout_pages": layout_pages} if layout_pages is not None else {}))
        )
        result = [
            _ordered_merge(deepcopy(tagged), rest, page.size)
            for tagged, rest, page in zip(self.blocks, fallback, self.pages, strict=True)
        ]
        from .tagged_captions import bind_orphan_figure_captions
        from .tagged_order import prefer_geometric_tagged_order

        prefer_geometric_tagged_order(result, self.pages, self.orphan_caption_roles, self.document)
        final_layout_pages = []
        for page_idx, (page, blocks) in enumerate(zip(self.pages, result, strict=True)):
            if self.orphan_caption_roles:
                bind_orphan_figure_captions(blocks, self.orphan_caption_roles)
            if self.frame_parents:
                from .tagged_frames import join_tagged_frames

                join_tagged_frames(self.document, page, blocks, self.frame_parents)
            backgrounds = frozenset(obj.index for obj in page.objects if obj.artifact and obj.kind != raw.FPDF_PAGEOBJ_TEXT)
            render_marked_visuals(
                self.document,
                page.page_index,
                blocks,
                backgrounds,
                marked_page=page,
                frame_parents=self.frame_parents,
                raster_sibling_parents=self.raster_sibling_parents,
            )
            for block in blocks:
                if block.get("type") == "code":
                    block.setdefault("guess_lang", "txt")
            if layout_pages is not None:
                fallback_evidence = layout_pages[page_idx] if layout_pages else None
                evidence = residual.full_layout_pages.get(page_idx)
                if evidence is None and fallback_evidence is None:
                    evidence = residual.layout_evidence(page_idx)
                if evidence is None:
                    evidence = fallback_evidence
                elif fallback_evidence is not None:
                    evidence.hard_breaks.extend(fallback_evidence.hard_breaks)
                    evidence.visual_sources.extend(fallback_evidence.visual_sources)
                    evidence.marginal_visual_keys.update(fallback_evidence.marginal_visual_keys)
                    evidence.visual_tables = tuple(dict.fromkeys((*evidence.visual_tables, *fallback_evidence.visual_tables)))
                    evidence.program_controls = tuple(dict.fromkeys((*evidence.program_controls, *fallback_evidence.program_controls)))
                evidence.accepted_suppressed_glyphs = self.suppressed_layout_glyphs.get(page_idx, b"")
                objects = {obj.index: obj for obj in page.objects if not obj.artifact}
                for block in blocks:
                    members = block.get(STRUCTURE_KEY, {}).get("members", ())
                    if members:
                        retain_glyph_members(
                            block,
                            glyph_ranges(char for index in members if index in objects for char in objects[index].chars),
                        )
                final_layout_pages.append(evidence)
        if layout_pages is not None:
            suppress_repeated_marginal_visuals(final_layout_pages)
            self.layout_program_controls = {i: ev.program_controls for i, ev in enumerate(final_layout_pages) if ev.program_controls}
            self.layout_diagnostics = tuple(
                diagnostic
                for page_idx, (evidence, blocks) in enumerate(zip(final_layout_pages, result, strict=True))
                for diagnostic in evidence.diagnose(blocks, page_idx)
            )
        return result

    def extension(self) -> dict:
        """保存源节点信息及源页身份，最终块路径由后处理重新生成。"""
        nodes = {}
        visual_links = {
            marker["node_id"]: marker["link_target"]
            for blocks in self.blocks
            for block in blocks
            if "link_target" in (marker := block.get(STRUCTURE_KEY, {}))
        }
        selected = {page.page_index for page in self.pages}
        membership = self.structure.page_membership()
        included = {identity for identity, pages in membership.items() if pages & selected}
        for node in self.structure.nodes():
            if node.identity not in included:
                continue
            partial = bool(membership[node.identity] - selected)
            record = {"role": node.role, "raw_role": node.raw_role, "order": node.order, "page_idx": node.page_index}
            if partial:
                record["partial_selection"] = True
            record["children"] = [
                {"node_id": child.identity}
                if isinstance(child, StructureNode)
                else {
                    "page_idx": child.page_index,
                    "stream": child.stream,
                    "mcid": child.mcid,
                    "object_id": child.object_id,
                }
                for child in node.children
                if (isinstance(child, StructureNode) and child.identity in included)
                or (isinstance(child, ContentReference) and child.page_index in selected)
            ]
            for key, value in (
                ("language", node.language),
                ("alt", node.alt),
                ("actual_text", node.actual_text),
                ("namespace", node.namespace),
                ("attributes", node.attributes),
                ("references", list(node.references)),
            ):
                if value is not None and value != {}:
                    if partial and key in {"alt", "actual_text", "attributes"}:
                        continue
                    record[key] = deepcopy(value)
            if node.identity in visual_links:
                record["link_target"] = visual_links[node.identity]
            if re.fullmatch(r"H[1-9][0-9]*", node.role):
                record["heading_level"] = int(node.role[1:])
            elif node.role == "H":
                record["heading_level"] = max(1, node.heading_depth)
            nodes[node.identity] = record
        return {
            "version": 1,
            "used": any(self.blocks),
            "declared_ua": self.structure.declared_ua,
            "nodes": nodes,
            "pages": [page.page_index for page in self.pages],
            "members": {
                str(page.page_index): [
                    {
                        "object_index": obj.index,
                        "stream": obj.stream,
                        "mcid": obj.mcid,
                        "form_instance": list(obj.occurrence),
                        "char_ranges": _index_ranges(obj.raw_indices),
                        "language": next((mark[3] for mark in reversed(obj.marks) if mark[3] is not None), None),
                        "actual_text": next((mark[2] for mark in obj.marks if mark[2] is not None), None),
                    }
                    for obj in page.objects
                    if obj.index in claimed
                ]
                for page, claimed in zip(self.pages, self.claimed, strict=True)
            },
        }


def _index_ranges(indices: frozenset[int]) -> list[list[int]]:
    """将原始字符索引压成半开连续区间，便于保存可追溯成员而控制扩展体积。"""
    ranges = []
    for index in sorted(indices):
        if ranges and ranges[-1][1] == index:
            ranges[-1][1] = index + 1
        else:
            ranges.append([index, index + 1])
    return ranges


def _unverified_order_conflict(structure: PDFStructure, node_pages: dict[str, frozenset[int]]) -> int | None:
    """识别普通标签树把早期正文挂到多页后的位置，检查始终基于完整原件。"""
    nodes = list(structure.nodes())
    if any(node.role in {"H", "Title"} or re.fullmatch(r"H[1-9][0-9]*", node.role) for node in nodes):
        return None
    furthest = -1
    for node in nodes:
        pages = node_pages[node.identity]
        if node.role != "P" or len(pages) != 1:
            continue
        page_index = next(iter(pages))
        if furthest - page_index > 1:
            return page_index
        furthest = max(furthest, page_index)
    return None


def prepare_tagged_analysis(document, *, collect_layout_diagnostics: bool = False) -> TaggedAnalysis | None:
    """优先从原件读取结构，选页副本只通过原页映射参与结构投影。"""
    source = getattr(document, "_structure_source", None) or document
    structure = source._get_structure()
    if not isinstance(structure, PDFStructure):
        return None
    indices = document._structure_page_indices
    if indices is None:
        indices = list(range(source.page_count))
    pages, blocks, claims = [], [], []
    released_link_text = {}
    layout_body_glyphs = {}
    suppressed_layout_glyphs = {}
    diagnostics = list(structure.diagnostics)
    nodes = list(structure.nodes())
    roles = {node.identity: node.role for node in nodes}
    node_pages = structure.page_membership()
    conflict = _unverified_order_conflict(structure, node_pages)
    if conflict is not None or not structure.roots:
        if conflict is not None:
            diagnostics.append(
                Diagnostic(
                    "pdf_structure_order_conflict",
                    "Unverified tag tree returns to earlier body pages after later-page prose; using native layout",
                    conflict,
                )
            )
        pages = [
            MarkedPage(index, source.page_size(index), (), PDFPageTextGeometry([], {}, {}), available=False)
            for index in indices
        ]
        return TaggedAnalysis(
            source, structure, pages, [[] for _ in indices], [set() for _ in indices], diagnostics, False,
            collect_layout_diagnostics=collect_layout_diagnostics,
        )
    for index in indices:
        try:
            page = read_marked_page(source, index)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            diagnostics.append(Diagnostic("pdf_structure_fallback", f"Cannot link page content: {error}", index))
            page = MarkedPage(index, source.page_size(index), (), PDFPageTextGeometry([], {}, {}), available=False)
        assembler = _PageAssembler(page, diagnostics, roles, node_pages, collect_layout_diagnostics=collect_layout_diagnostics)
        output = []
        for root in structure.roots:
            output.extend(assembler.visit(root))
        pages.append(page)
        blocks.append(output)
        claims.append(assembler.claimed)
        if assembler.released_link_text:
            released_link_text[index] = assembler.released_link_text
        if collect_layout_diagnostics:
            layout_body_glyphs[len(pages) - 1] = glyph_index_ranges(assembler.explicit_body_glyphs)
            suppressed_layout_glyphs[len(pages) - 1] = glyph_index_ranges(assembler.suppressed_layout_glyphs)
    from .tagged_frames import figure_siblings

    orphan_roles = {
        node.identity: node.role
        for node in nodes
        if node.role in {"P", "Caption"} and not node.references and len(node_pages[node.identity]) == 1
    }
    return TaggedAnalysis(
        source,
        structure,
        pages,
        blocks,
        claims,
        diagnostics,
        orphan_caption_roles=orphan_roles,
        released_link_text=released_link_text,
        frame_parents=figure_siblings(nodes, node_pages),
        raster_sibling_parents=figure_siblings(nodes, node_pages, allow_alt=True),
        collect_layout_diagnostics=collect_layout_diagnostics,
        layout_body_glyphs=layout_body_glyphs,
        suppressed_layout_glyphs=suppressed_layout_glyphs,
    )
