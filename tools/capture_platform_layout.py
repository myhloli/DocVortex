"""在实际运行平台导出未嵌入字体 PDF 的布局标注和语义 HTML，供跨平台审阅。"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from html import escape
from importlib.metadata import version
import json
from pathlib import Path
import platform
import shutil
import sys
import subprocess
from typing import Any

from PIL import Image, ImageDraw

from docgale.api import parse
from docgale.document.pdf import PDFDocument, initialize_pdfium_runtime
from font_geometry import capture_geometry

_COLORS = {
    "text": "#2563eb",
    "doc_title": "#be185d",
    "paragraph_title": "#7c3aed",
    "caption": "#16a34a",
    "table": "#ea580c",
    "image": "#0891b2",
    "equation": "#dc2626",
    "header": "#64748b",
    "footer": "#64748b",
    "page_number": "#64748b",
    "footnote": "#92400e",
}
_DOCUMENTS = (
    ("paper-3", "demo/pdfs/中文论文3.pdf"),
    ("paper-4", "demo/pdfs/中文论文4.pdf"),
    ("cjk-synthetic", "tests/unittest/pdfs/native_cjk_layout_synthetic.pdf"),
)


def block_type(block: dict[str, Any]) -> str:
    """将原始字符串或枚举统一为可读的布局类型。"""
    value = block.get("type", "unknown")
    return str(getattr(value, "value", value))


def draw_layout(source: Image.Image, blocks: list[dict[str, Any]], destination: Path) -> None:
    """按归一化 bbox 在原页像素上叠加类型和序号，保留源页面尺寸。"""
    canvas = source.convert("RGB")
    try:
        painter = ImageDraw.Draw(canvas)
        width, height = canvas.size
        for index, block in enumerate(blocks):
            bbox = block.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            left, top, right, bottom = [float(value) for value in bbox]
            box = (left * width, top * height, right * width, bottom * height)
            kind = block_type(block)
            color = _COLORS.get(kind, "#475569")
            painter.rectangle(box, outline=color, width=2)
            label = f"{index}: {kind}"
            position = (max(0, box[0]), max(0, box[1] - 12))
            label_box = painter.textbbox(position, label)
            painter.rectangle(label_box, fill="white")
            painter.text(position, label, fill=color)
        canvas.save(destination, format="PNG")
    finally:
        canvas.close()


def capture_document(source: Path, destination: Path) -> dict[str, Any]:
    """一次分析生成原始/中间协议、真实页面标注图和带素材的渲染 HTML。"""
    destination.mkdir(parents=True, exist_ok=True)
    # 单独的全新进程保留默认提供器对照，不能在当前 PDFium 上切换字体策略。
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("font_geometry.py")),
            str(source),
            str(destination / "system-geometry.json"),
            "--system-fonts",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    (destination / "geometry.json").write_text(json.dumps(capture_geometry(source), ensure_ascii=False), encoding="utf-8")
    result = parse(source, keep_model_json=True)
    assert result.model_json is not None
    result.export(destination / "render.html", output_format="html")
    result.export(destination / "render.md", output_format="markdown")
    (destination / "model.json").write_text(result.model_json.to_json(skip_defaults=False), encoding="utf-8")
    (destination / "middle.json").write_text(result.middle_json.to_json(skip_defaults=False), encoding="utf-8")
    records = []
    with PDFDocument(str(source)) as document:
        for index, blocks in enumerate(result.model_json.pages):
            image = document.render_page(index, scale=1.5).pil_image
            try:
                image.save(destination / f"source-{index + 1:02}.png", format="PNG")
                draw_layout(image, blocks, destination / f"layout-{index + 1:02}.png")
            finally:
                image.close()
            records.append({"page": index + 1, "counts": dict(Counter(block_type(block) for block in blocks))})
    figures = "".join(
        f'<section><h2>Page {item["page"]}</h2><img loading="lazy" src="layout-{item["page"]:02}.png"></section>'
        for item in records
    )
    legend = " ".join(f'<span style="color:{color}">{escape(kind)}</span>' for kind, color in _COLORS.items())
    viewer = (
        '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<style>body{font-family:system-ui;background:#f1f5f9;margin:24px}img{max-width:100%;background:white}"
        "section{max-width:1100px;margin:auto}a{color:#2563eb}</style>"
        f"<h1>{escape(source.name)} · {escape(platform.system())}</h1><p>{legend}</p>"
        f'<p><a href="render.html">Render HTML</a> · <a href="model.json">Model JSON</a></p>{figures}</html>'
    )
    (destination / "layout.html").write_text(viewer, encoding="utf-8")
    return {"name": source.name, "sha256": sha256(source.read_bytes()).hexdigest(), "pages": records}


def main() -> None:
    """从源仓库语料生成可下载的审阅产物，并记录实际平台及依赖版本。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"Choose a new artifact directory: {args.output}")
    args.output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    metadata = {
        "platform": platform.platform(),
        "system": platform.system(),
        "python": sys.version,
        "runtime": asdict(initialize_pdfium_runtime()),
        "pdf_text_normalization": "fullwidth-alphanumeric-v1",
        "dependencies": {name: version(name) for name in ("docgale", "pypdfium2", "pydantic", "numpy", "pillow")},
        "documents": {},
    }
    if platform.system() == "Linux" and shutil.which("fc-list"):
        font_list = subprocess.run(["fc-list", ":lang=zh", "family"], capture_output=True, text=True, check=True)
        metadata["system_cjk_font_families"] = sorted(set(font_list.stdout.splitlines()))
    for key, name in _DOCUMENTS:
        metadata["documents"][key] = capture_document(root / name, args.output / key)
    (args.output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Platform review artifacts: {args.output}")


if __name__ == "__main__":
    main()
