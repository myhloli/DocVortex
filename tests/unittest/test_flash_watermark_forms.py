"""验证已过滤水印的 Form 不会遮住原生正文及独立图形。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from _flash_pdf_test_utils import _text_line
from docvortex import parse
from docvortex.analyzers.native import PdfModel
from docvortex.analyzers.native.pdf import code_blocks
from docvortex.analyzers.native.pdf.title_analysis.structural import _classify_bold_numbered_heading_rows
from docvortex.document.pdf import PDFDocument


def _names(**entries):
    """构造键带斜杠的 PDF 字典。"""
    return DictionaryObject({NameObject("/" + key): item for key, item in entries.items()})


def _stream(data, **entries):
    """把独立的绘图或字节流写入合成原始 PDF 对象。"""
    stream = DecodedStreamObject()
    stream.set_data(data)
    stream.update(_names(**entries))
    return stream


def _image(writer, size, shaded):
    """生成尺寸固定的平滑底板或有明显纹理的位图素材。"""
    ink = bytes((42 if (x // 4 + y // 4) % 2 else 243) if shaded else 255 for y in range(size) for x in range(size))
    return writer._add_object(
        _stream(
            ink,
            Type=NameObject("/XObject"),
            Subtype=NameObject("/Image"),
            Width=NumberObject(size),
            Height=NumberObject(size),
            BitsPerComponent=NumberObject(8),
            ColorSpace=NameObject("/DeviceGray"),
        )
    )


def _document(path: Path, variant: str):
    """重复使用多层水印，也能合成无标记真扫描及误带水印声明的矢量反例。"""
    writer = PdfWriter()
    front = _image(writer, 72, True)
    flat = _image(writer, 96, False)
    independent = _image(writer, 49, True)
    font = writer._add_object(_names(Type=NameObject("/Font"), Subtype=NameObject("/Type1"), BaseFont=NameObject("/Helvetica")))
    bounds = ArrayObject(NumberObject(item) for item in (0, 0, 500, 700))
    inner = writer._add_object(
        _stream(
            b"q 500 0 0 700 0 0 cm /W Do Q",
            Type=NameObject("/XObject"),
            Subtype=NameObject("/Form"),
            BBox=bounds,
            Resources=_names(XObject=_names(W=front if variant == "scan" else flat)),
        )
    )
    layers = b"q /Inner Do Q"
    if variant != "scan":
        layers += b"\nq 355 0 0 390 76 138 cm /Ink Do Q"
    if variant == "poster":
        layers += b"\nq 0.1 0.2 0.8 rg 130 160 120 260 re f Q"
    outer = _stream(
        layers,
        Type=NameObject("/XObject"),
        Subtype=NameObject("/Form"),
        BBox=bounds,
        Resources=_names(XObject=_names(Inner=inner, Ink=front)),
    )
    if variant in {"declared", "poster"}:
        outer.update(_names(PieceInfo=_names(ADBE_CompoundType=_names(Private=NameObject("/Watermark")))))
    root = writer._add_object(outer)
    for offset in range(3):
        page = writer.add_blank_page(width=500, height=700)
        page[NameObject("/Resources")] = _names(Font=_names(F=font), XObject=_names(Root=root, Local=independent))
        text = (
            "\n".join(
                f"BT /F 12 Tf 1 0 0 1 55 {620 - 25 * row} Tm (Native body item {offset + 1}-{row + 1} contains recognizable words.) Tj ET"
                for row in range(6)
            )
            if variant != "scan"
            else ""
        )
        # 多条本页线在原版会错误地成为满页 Form 的图形证据。
        rules = "\n".join(f"35 {y} m 445 {y} l S" for y in (100, 128, 155, 215))
        local_image = "q 54 0 0 54 380 615 cm /Local Do Q" if offset == 0 and variant != "scan" else ""
        grid = _outline_grid() if offset == 1 and variant != "scan" else ""
        content = f"{rules}\n{text}\n{local_image}\n{grid}\nq /Root Do Q"
        page[NameObject("/Contents")] = writer._add_object(_stream(content.encode("ascii")))
    writer.write(path)


def _outline_grid():
    """绘制完全由 Path 表示的闭合表格单元及分布在多行列的轮廓字形。"""
    cols, rows = (80, 155, 230, 305, 380), (245, 275, 305, 335, 365)
    rules = [f"{x} {rows[0]} m {x} {rows[-1]} l S" for x in cols]
    rules.extend(f"{cols[0]} {y} m {cols[-1]} {y} l S" for y in rows)
    rules.extend(f"{x + dx} {y + 11} 2 8 re f" for y in rows[:-1] for x in cols[:-1] for dx in (10, 15, 20))
    return "\n".join(rules)


@pytest.mark.parametrize("variant", ["declared", "unmarked"])
def test_discarded_watermark_form_keeps_independent_outputs(tmp_path, variant):
    """带标记及由重复与平滑底板独立印证的无标记水印均不再交付整页图。"""
    source = tmp_path / "watermark.pdf"
    _document(source, variant)
    result = parse(source, parse_mode="txt", keep_model_json=True)
    pages = result.model_json.pages
    images = [[item for item in page if item["type"] == "image"] for page in pages]
    assert len(images[0]) == len(images[1]) == 1
    assert images[0][0]["bbox"] == [0.76, 0.044, 0.868, 0.121]
    assert images[1][0]["bbox"][1] > 0.4 and images[1][0]["bbox"][3] < 0.7
    assert images[2] == []
    assert all("Native body item" in str(page) for page in pages)
    exported = tmp_path / "page-content.html"
    result.export(exported, output_format="html")
    text = exported.read_text()
    assert text.count("<img") == 2
    assert len(list((tmp_path / "images").glob("*.jpg"))) == 2


@pytest.mark.parametrize("variant", ["scan", "poster"])
def test_real_full_page_assets_keep_their_owner(tmp_path, variant):
    """重复的真实扫描及被误贴角色但仍有自有矢量内容的海报保留满页图。"""
    source = tmp_path / "whole.pdf"
    _document(source, variant)
    with PDFDocument(source.read_bytes()) as document:
        model = PdfModel().predict(document)
    assert all(any(block["type"] == "image" and block["bbox"] == [0, 0, 1, 1] for block in page) for page in model)


def test_framed_wrapped_prose_versus_proportional_pseudocode(monkeypatch):
    """有真实悬挂段落的比例字正文拒绝升级，另有程序结构的比例字清单仍保留。"""
    prose, pseudocode = [], []
    for row in range(12):
        item, part = divmod(row, 3)
        text = (
            f"{item + 1}. In the usual case the readers return for another look at the result",
            "when the following examples apply to the physical situation",
            "and all quantities have been converted to a common scale.",
        )[part]
        top = 24 + 10 * row
        prose.append(_text_line(text, (24 + (0, 10, 19)[part], top, 132, top + 8), row, visual_row_id=row, effective_height=8))
        text = ("if destination is ready then", "for each entry in current input", "return accumulated_result")[part]
        pseudocode.append(
            _text_line(text, (24 + (0, 10, 19)[part], top, 132, top + 8), row, visual_row_id=row, effective_height=8)
        )
    candidate = (10, 10, 160, 158)
    assert code_blocks._rule_delimited_code_members_are_structured(prose, candidate, 8)
    assert not code_blocks._rule_delimited_code_members_are_structured(prose, candidate, 8, protect_wrapped_prose=True)
    assert code_blocks._rule_delimited_code_members_are_structured(pseudocode, candidate, 8, protect_wrapped_prose=True)
    monkeypatch.setattr(code_blocks, "_rule_window_wraps_numbered_prose", lambda *args: False)
    assert code_blocks._rule_delimited_code_members_are_structured(prose, candidate, 8, protect_wrapped_prose=True)


def test_numbered_exercise_wrap_does_not_suppress_real_title():
    """同组编号自然题干保持续行，正常的粗体章节名仍会晋升。"""
    metadata = dict(effective_height=10, font_signature=("Helvetica-Bold", 262144), font_coverage=1, dominant_font_weight=700)
    previous = _text_line(
        "2.1 Calculate the units for each quantity using these definitions:", (40, 30, 380, 40), 0, **metadata
    )
    question = _text_line("2.2 State whether the following operations are true for", (40, 65, 380, 75), 1, **metadata)
    continuation = _text_line("physical quantities under these local conditions.", (57, 77, 329, 87), 2, **metadata)
    heading = _text_line("3 Material Models", (40, 135, 180, 145), 3, **metadata)
    _classify_bold_numbered_heading_rows(
        [previous, question, continuation, heading], (500, 700), [], respect_protected_roles=True
    )
    assert question.semantic_type is None
    assert continuation.semantic_type is None
    assert heading.semantic_type == "paragraph_title"


def test_filled_prose_panel_keeps_live_text_and_protects_vector_art():
    """同一真实矩形里的齐边段落提供正文反证，加入独立矢量图形就保留图候选。"""
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.watermark_forms import carries_filled_prose_panel
    from docvortex.document.pdf.native_contracts import PDFPathInfo

    box = (20.0, 20.0, 205.0, 89.0)
    body = [
        "The sample begins with three explicit measurements",
        "and their reported ranges remain easy to verify",
        "from these parallel rows of editable body text.",
    ]
    lines = [
        _text_line(line, (27, 30 + 12 * i, 196, 40 + 12 * i), i, font_signature=("Body", 0), effective_height=10)
        for i, line in enumerate(body)
    ]
    source = _PageSource(
        page_size=(500, 700),
        lines=lines,
        chars=[],
        drawing_lines=[],
        path_infos=[PDFPathInfo(box, 5, True, False, 0, 0, (210, 232, 248, 255), (box,))],
    )
    candidate = (20.0, 16.0, 205.0, 90.0)
    source.path_infos.append(PDFPathInfo((19.8, 19.8, 205.2, 89.2), 5, False, True, 0, 2))
    assert carries_filled_prose_panel(source, candidate, lines, 10)
    source.path_infos.append(PDFPathInfo((145, 42, 181, 76), 6, False, True, 0, 1))
    assert not carries_filled_prose_panel(source, candidate, lines, 10)


def test_adjacent_equation_value_does_not_override_real_numbered_or_quantity_titles():
    """同基线等号保护数值右项，数字小节和另一行独立量值仍可作为标题。"""
    bold = dict(effective_height=8, font_signature=("Helvetica-Bold", 262144), font_coverage=1, dominant_font_weight=700)
    equals = _text_line("=", (115, 29, 120, 41), 0, effective_height=12)
    value = _text_line("10.0 m", (122.2, 31, 157, 39), 1, **bold)
    quantity_title = _text_line("15.0 m", (122.2, 70, 157, 78), 2, **bold)
    numbered_title = _text_line("3.10 Circular Motion", (122.2, 115, 220, 123), 3, **bold)
    _classify_bold_numbered_heading_rows(
        [equals, value, quantity_title, numbered_title], (500, 700), [], respect_protected_roles=True
    )
    assert value.semantic_type is None
    assert quantity_title.semantic_type == numbered_title.semantic_type == "paragraph_title"


def test_numbered_prose_sidebar_and_formula_keep_native_roles():
    """通过实际窄侧栏、连续悬挂编号或同排运算符识别非标题，保留独立数字小节。"""
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.watermark_forms import protect_native_non_titles
    from docvortex.document.pdf.native_contracts import PDFPathInfo

    metadata = dict(effective_height=10, font_signature=("Helvetica", 0), font_coverage=1, dominant_font_weight=600)
    sidebar = (15, 20, 145, 480)
    topics = [
        _text_line(f"3.{index + 1} Sidebar topic", (20, 35 + 18 * index, 124, 45 + 18 * index), index, **metadata)
        for index in range(5)
    ]
    stems = []
    tails = []
    for item in range(3):
        top = 170 + 30 * item
        stems.append(
            _text_line(f"{10 + item}. In this case the vector can be defined", (185, top, 416, top + 10), 10 + item, **metadata)
        )
        tails.append(
            _text_line(
                "by these explicit rules for component magnitudes.", (209, top + 13, 426, top + 23), 20 + item, **metadata
            )
        )
    equation = _text_line("u =", (205, 285, 230, 295), 35, **metadata)
    symbol = _text_line("A x", (233, 285, 252, 295), 36, **metadata)
    heading = _text_line("3.9 PROJECTILE MOTION", (185, 411, 316, 421), 38, **metadata)
    lines = [*topics, *stems, *tails, equation, symbol, heading]
    source = _PageSource(
        page_size=(500, 700),
        lines=lines,
        chars=[],
        drawing_lines=[],
        path_infos=[PDFPathInfo(sidebar, 5, True, True, 0, 0, (214, 232, 252, 255), (sidebar,))],
    )
    protect_native_non_titles(source)
    assert all(line.title_suppressed for line in [*topics, *stems, symbol])
    assert not heading.title_suppressed
    _classify_bold_numbered_heading_rows(lines, source.page_size, [], respect_protected_roles=True)
    assert all(line.semantic_type is None for line in [*topics, *stems, symbol])
    assert heading.semantic_type == "paragraph_title"
