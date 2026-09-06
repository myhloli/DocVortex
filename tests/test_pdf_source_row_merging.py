"""保持混合字体同行文本，同时保护分式、上下标及独立行的结构。"""

from __future__ import annotations

from dataclasses import replace

from docvortex.analyzers.native.pdf.line_merging import _can_merge_same_baseline_pair
from docvortex.analyzers.native.pdf.models import _LineItem
from docvortex.document.pdf.text import Bbox


def _fragment(
    text: str, index: int, source: tuple[float, float, float, float], ink: tuple[float, float, float, float]
) -> _LineItem:
    """构造相邻源字符但字形框距离更大的混合字体片段。"""
    return _LineItem(
        text=text,
        bbox=ink,
        source_bbox=source,
        angle=0,
        source_index=index,
        baseline=391.9,
        font_coverage=0.5,
        effective_height=7.8,
        geometry_state="repair_x",
        chars=[
            {
                "char": text,
                "char_idx": index,
                "source_indices": (index,),
                "font": {"size": 8.25},
                "bbox": Bbox(list(source)),
                "rotation": 0,
            }
        ],
    )


def test_source_row_survives_fallback_ink_geometry() -> None:
    """图号与正文来自不同字体时，紧致字形框的间隔不能打断同一物理行。"""
    prefix = _fragment("图２", 228, (100.5, 384.4, 117.8, 392.3), (100.5, 384.4, 115.2, 392.3))
    body = _fragment("２００５年逆温厚度", 229, (123.0, 384.4, 243.6, 392.3), (125.4, 384.4, 243.6, 392.3))
    assert _can_merge_same_baseline_pair(prefix, prefix.bbox, body, body.bbox, [])
    # 表格隔断、缺失源索引及上下标偏移仍阻止普通同行合并。
    assert not _can_merge_same_baseline_pair(prefix, prefix.bbox, body, body.bbox, [(118, 380, 122, 396)])
    assert not _can_merge_same_baseline_pair(prefix, prefix.bbox, replace(body, chars=[]), body.bbox, [])
    assert not _can_merge_same_baseline_pair(prefix, prefix.bbox, replace(body, baseline=380), body.bbox, [])


def test_overlapping_fraction_is_not_an_ordinary_source_row() -> None:
    """公式大框可覆盖相邻正文，但真实基线不同，必须继续由二维公式逻辑处理。"""
    fraction = _fragment("(1/2)", 100, (214.8, 528.9, 272.1, 548.1), (214.8, 528.9, 272.1, 548.1))
    prose = _fragment("and σ0 = 3e2γph", 99, (141.7, 534.6, 208.0, 549.5), (141.7, 534.6, 208.0, 549.5))
    fraction.baseline, prose.baseline = 533.8, 546.9
    assert not _can_merge_same_baseline_pair(prose, prose.bbox, fraction, fraction.bbox, [])


def test_source_row_uses_display_geometry_across_text_matrix_scales() -> None:
    """8pt 与经过 0.5 倍矩阵缩放的 16pt 文本显示大小相同，不能被名义字号比拆开。"""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    from docvortex.document.pdf import PDFDocument

    payload = BytesIO()
    canvas = Canvas(payload)
    for letter, size, scale, left in (("A", 8, 1, 60), ("B", 16, 0.5, 69)):
        text = canvas.beginText()
        text.setTextTransform(scale, 0, 0, scale, left, 700)
        text.setFont("Helvetica", size)
        text.textOut(letter)
        canvas.drawText(text)
    canvas.save()
    with PDFDocument(payload.getvalue()) as document:
        chars = [char for char in document.get_page_chars_with_geometry(0).chars if char["char"] in {"A", "B"}]
    assert [char["font"]["size"] for char in chars] == [8, 16]
    lines = []
    for char in chars:
        box, origin = char["loose_bbox"], char["origin"]
        assert box is not None and origin is not None
        lines.append(
            _LineItem(
                text=char["char"],
                bbox=box,
                source_bbox=box,
                angle=0,
                source_index=char["char_idx"],
                baseline=origin[1],
                chars=[char],
                effective_height=box[3] - box[1],
            )
        )
    first, second = lines
    assert abs(first.effective_height - second.effective_height) < 1e-3
    assert second.bbox[0] - first.bbox[2] > 0.75
    assert _can_merge_same_baseline_pair(first, first.bbox, second, second.bbox, [])
    assert not _can_merge_same_baseline_pair(first, first.bbox, replace(second, baseline=second.baseline + 20), second.bbox, [])
