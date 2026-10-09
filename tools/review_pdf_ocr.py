"""生成真实 PDF OCR 审阅产物：原页、标框、模型结果、Markdown、HTML 和结果包。"""

from __future__ import annotations

import argparse
from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import time

from PIL import ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

import docvortex
from docvortex.document.page_range import parse_page_range
from docvortex.document.pdf import PDFDocument


def rasterize_pdf(source: Path, page_range: str, target: Path) -> Path:
    """把真实文档选页转为仅含页面图像的 PDF，模拟扫描输入并保持物理页尺寸。"""
    output = BytesIO()
    canvas = Canvas(output)
    with PDFDocument(source.read_bytes()) as document:
        for index in parse_page_range(page_range, document.page_count):
            image = document.render_page(index, scale=200 / 72).pil_image
            try:
                width, height = document.page_size(index)
                canvas.setPageSize((width, height))
                canvas.drawImage(ImageReader(image), 0, 0, width, height)
                canvas.showPage()
            finally:
                image.close()
    canvas.save()
    target.write_bytes(output.getvalue())
    return target


def review(source: Path, output: Path, page_range: str, rasterize: bool, parse_mode: str = "auto") -> dict:
    """保存可重复的输入及导出结果，按原文页面坐标绘制所有模型框供人工复核。"""
    output.mkdir(parents=True, exist_ok=True)
    if rasterize:
        source = rasterize_pdf(source, page_range, output / "scanned.pdf")
        page_range = ""
    started = time.perf_counter()
    result = docvortex.parse(source, page_range=page_range, keep_model_json=True, parse_mode=parse_mode)
    elapsed = time.perf_counter() - started
    result.export(output / "result.md")
    result.export(output / "result.html", output_format="html")
    result.save_bundle(output / "bundle")
    (output / "model.json").write_text(result.model_json.to_json(), encoding="utf-8")
    with PDFDocument(source.read_bytes()) as document:
        classification = document.classify()
        page_indices = result.model_json.resolved_page_indices
        for page_index, blocks in zip(page_indices, result.model_json.pages, strict=True):
            image = document.render_page(page_index, scale=200 / 72).pil_image
            try:
                image.save(output / f"page-{page_index + 1}-source.png")
                overlay = image.convert("RGB")
                try:
                    draw = ImageDraw.Draw(overlay)
                    width, height = overlay.size
                    for index, block in enumerate(blocks):
                        bbox = block.get("bbox")
                        if not bbox:
                            continue
                        rectangle = tuple(value * (width if axis % 2 == 0 else height) for axis, value in enumerate(bbox))
                        color = "blue" if block["type"] in {"table", "equation", "image", "chart"} else "red"
                        draw.rectangle(rectangle, outline=color, width=3)
                        draw.text(rectangle[:2], f"{index}: {block['type']}", fill=color, stroke_width=1, stroke_fill="white")
                    overlay.save(output / f"page-{page_index + 1}-overlay.png")
                finally:
                    overlay.close()
            finally:
                image.close()
    summary = {
        "source": str(source.resolve()),
        "classification": classification,
        "parse_mode": parse_mode,
        "pages": len(result.model_json.pages),
        "elapsed_seconds": round(elapsed, 3),
        "blocks": dict(Counter(block["type"] for page in result.model_json.pages for block in page)),
        "assets": len(result.assets),
        "diagnostics": [
            {"code": item.code, "message": item.message, "page_index": item.page_index} for item in result.diagnostics
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    """提供选页及扫描模拟选项，真实模型按正常公开 API 下载和加载。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path("output/pdf/ocr-review"))
    parser.add_argument("--pages", default="1-2")
    parser.add_argument("--rasterize", action="store_true")
    parser.add_argument("--parse-mode", choices=["auto", "txt", "ocr"], default="auto")
    args = parser.parse_args()
    summaries = [
        review(source, args.output / source.stem, args.pages, args.rasterize, args.parse_mode) for source in args.sources
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
