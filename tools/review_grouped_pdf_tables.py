"""重放实际 layout 表格框及公共导出，保存原生恢复和整本解析的独立证据。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys

from loguru import logger

import docvortex
from docvortex._compute_backend import backend_info
from docvortex.analyzers.pdf import prepare_table_page, recover_table_region
from docvortex.analyzers.native.pdf._table_recovery import NativeTableInput
from docvortex.analyzers.native.pdf._table_recovery.engine import diagnose_native_pdf_table
from docvortex.analyzers.native.pdf.spatial_text import project_pdf_table_text
from docvortex.document.pdf import PDFDocument
from docvortex.content.spans import text_spans
from docvortex.postprocess.pages import model_json_to_pages
from docvortex.schema import ModelJson


def main() -> None:
    """通过固定源指纹和真实导入路径生成一次不可覆盖的验收输出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--layout-records", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise ValueError("验收输出目录必须为空，不能覆盖已有证据")
    logger.remove()
    source = args.source.read_bytes()
    code_root = Path(docvortex.__file__).resolve().parent
    fingerprint = hashlib.sha256()
    for path in sorted(code_root.rglob("*.py")):
        fingerprint.update(str(path.relative_to(code_root)).encode())
        fingerprint.update(path.read_bytes())
    env = {"python": sys.version, "docvortex_path": str(code_root), "compute_backend": backend_info()}
    for name in ("docvortex", "pypdfium2", "numpy"):
        env[name] = metadata.version(name)
    meta = {"source_sha256": hashlib.sha256(source).hexdigest(), "code_sha256": fingerprint.hexdigest(), "environment": env}
    public = docvortex.parse(source, file_suffix="pdf", keep_model_json=True)
    (args.output / "model.json").write_text(json.dumps(public.model_json.pages, ensure_ascii=False))
    (args.output / "middle.json").write_text(json.dumps(public.to_dict(), ensure_ascii=False))
    public.export(args.output / "document.html", output_format="html")
    public.save_bundle(args.output / "document.zip")
    regions, pages = [], [[] for _ in public.model_json.pages]
    with PDFDocument(source) as document:
        document.draw_layout_bbox(model_json_to_pages(public.model_json), str(args.output / "layout.pdf"))
        for record in json.loads(args.layout_records.read_text()):
            page_number = record["page"]
            prepared = prepare_table_page(document[page_number - 1])
            for block in record["initial_blocks"]:
                bbox = tuple(value * prepared.page_size[index % 2] for index, value in enumerate(block["bbox"]))
                if block["type"] != "table":
                    # 保留实际相邻正文和表注，避免裸表导出产生伪跨页合并。
                    contextual = deepcopy(block)
                    projected = project_pdf_table_text(prepared.geometry.chars, bbox)
                    contextual["content"] = (
                        projected
                        if block["type"] in {"image", "chart", "equation", "formula_number"}
                        else text_spans(projected)
                    )
                    pages[page_number - 1].append(contextual)
                    continue
                result = recover_table_region(prepared, bbox)
                content = result.html if result else project_pdf_table_text(prepared.geometry.chars, bbox)
                pages[page_number - 1].append({"type": "table", "bbox": block["bbox"], "angle": 0, "content": content})
                table = NativeTableInput(
                    bbox, prepared.page_size, 0, tuple(prepared.geometry.chars), prepared.drawing_lines, prepared.rectangles
                )
                diagnostics = diagnose_native_pdf_table(table)
                regions.append(
                    {
                        "page": page_number,
                        "bbox": block["bbox"],
                        "accepted": result is not None,
                        "html": result.html if result else None,
                        "diagnostics": diagnostics,
                    }
                )
        regional_model = ModelJson(
            pages=pages,
            page_index_map=[],
            metadata={"file_suffix": "pdf", "producer": {"name": "docvortex", "version": "review"}},
        )
        document.draw_layout_bbox(model_json_to_pages(regional_model), str(args.output / "regions-layout.pdf"))
    regional = docvortex.postprocess_document(regional_model, keep_model_json=True)
    regional.export(args.output / "tables.html", output_format="html")
    regional.export(args.output / "tables.md", output_format="markdown")
    (args.output / "regions-model.json").write_text(json.dumps(pages, ensure_ascii=False))
    (args.output / "regions-middle.json").write_text(json.dumps(regional.to_dict(), ensure_ascii=False))
    (args.output / "regions.json").write_text(json.dumps(regions, ensure_ascii=False, indent=2))
    meta.update(pages=len(pages), regions=len(regions), accepted=sum(row["accepted"] for row in regions))
    (args.output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps({key: meta[key] for key in ("pages", "regions", "accepted", "code_sha256")}))


if __name__ == "__main__":
    main()
