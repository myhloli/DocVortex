"""用原生控制语句及邻行确认代码被拆作展示公式的布局冲突。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ....content.spans import inline_span_plain_text
from ....schema import BBox
from .geometry import _bbox_axis_overlap_ratio, _bbox_overlap_in_first

if TYPE_CHECKING:
    from .models import _LineItem

_CONTROL = re.compile(r"^(?:for|if|while|switch)\s*[\[(].*(?:==|\+\+|&&|\|\||;.*;|\]\s*;|\)\s*\{)", re.I)
_STATEMENT = re.compile(
    r"\b(?:if|for|while|switch)\s*[(\[]|\b(?:int|void|float|double|const|constexpr|template|struct|class)\s+[A-Za-z_*&][^。！？]{0,90}[();{=]"
    r"|\breturn\b[^.;。]{0,50};|\b\w+(?:\[[^\]]+\])?\s*=(?!=)[^;。！]{1,85};",
    re.I,
)
_CLOSING = re.compile(r"[}\]);\s]+")


def collect_program_controls(lines: list[_LineItem], page_size: tuple[float, float]) -> tuple[BBox, ...]:
    """只保留同一代码带内有两个其他程序行支撑的控制行框，释放字符后仍可核实原成员。"""
    controls = [
        line
        for line in lines
        if not line.angle
        and line.text.lstrip()[:6].lower().startswith(("if", "for", "while", "switch"))
        and _CONTROL.match(line.text.strip())
    ]
    if not controls:
        return ()
    statements = [line.ink_bbox or line.bbox for line in lines if not line.angle and _STATEMENT.search(line.text)]
    width, height = page_size
    result = []
    for line in controls:
        box = line.ink_bbox or line.bbox
        rows = {
            round(b[1], 1)
            for b in statements
            if abs(b[1] - box[1]) <= 0.07 * height and _bbox_axis_overlap_ratio(box, b, axis="x") >= 0.55
        }
        if len(rows) >= 3:
            result.append((box[0] / width, box[1] / height, box[2] / width, box[3] / height))
    return tuple(result)


def fragmented_program_equations(blocks: list[dict]) -> list[BBox]:
    """取在两个可编辑程序片段之间的公式候选，独立公式和完整代码块均不提供此证据。"""
    equations = [b["bbox"] for b in blocks if b.get("type") == "equation" and not b.get("angle") and b.get("bbox")]
    if not equations:
        return []
    statements, endings = [], []
    for block in blocks:
        if block.get("type") not in {"text", "paragraph_title", "doc_title"} or block.get("angle") or not block.get("bbox"):
            continue
        content = block.get("content")
        text = inline_span_plain_text(content) if isinstance(content, list) else content
        if not isinstance(text, str):
            continue
        text = text.strip()
        if len(text) < 900 and not text.startswith("//") and _STATEMENT.search(text):
            statements.append(block["bbox"])
            endings.append(block["bbox"])
        elif _CLOSING.fullmatch(text):
            endings.append(block["bbox"])
    return [
        eq
        for eq in equations
        if any(
            b[1] < eq[1] and abs(b[3] - eq[1]) <= 0.06 and _bbox_axis_overlap_ratio(b, eq, axis="x") >= 0.5 for b in statements
        )
        and any(
            b[1] >= eq[1] and abs(b[1] - eq[3]) <= 0.06 and _bbox_axis_overlap_ratio(b, eq, axis="x") >= 0.4 for b in endings
        )
    ]


def program_equation_conflict(blocks: list[dict], controls: tuple[BBox, ...]) -> str | None:
    """只有误作公式的就是源控制行自身时才报告，邻近真公式不触发。"""
    for equation in fragmented_program_equations(blocks):
        if any(_bbox_overlap_in_first(box, equation) >= 0.75 for box in controls):
            bounds = ", ".join(f"{value:.3f}" for value in equation)
            return f"公式裁片包含源程序控制行，且前后为分离的程序语句或闭括号，区域 [{bounds}]"
    return None
