"""用真实 PDF 和冻结布局验证 MinerU 的 medium/high 原生复用；不运行模型推理。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch


def capture(args: argparse.Namespace) -> dict:
    """回放实际宿主的表格优先与文本回填阶段，并冻结回退模型输入及调用计数。"""
    import onnxruntime

    # 回放不运行模型推理，禁用无关后台遥测以稳定独立进程的正常退出。
    onnxruntime.disable_telemetry_events()
    sys.path.insert(0, str(args.mineru_source.resolve()))
    from loguru import logger
    from docvortex._compute_backend import backend_info
    from docvortex.document.pdf import PDFDocument
    from mineru.backend.analysis.pdf import tables
    from mineru.backend.analysis.pdf.text import content

    logger.disable("docvortex")
    logger.disable("mineru")
    root = Path(__file__).resolve().parents[1]
    frozen = json.loads((args.flash_baseline / "report.json").read_text())
    output = {}
    original_geometry = PDFDocument.get_page_chars_with_geometry

    paths = args.path or [root / "demo/pdfs" / f"{name}.pdf" for name in ("caibao1", "demo1", "demo2")]
    for path in paths:
        name = path.stem
        payload = path.read_bytes()
        source_hash = hashlib.sha256(payload).hexdigest()
        entry = next(item for item in frozen["documents"] if item["source_sha256"] == source_hash)
        baseline = json.loads((args.flash_baseline / entry["artifact"] / "output.json").read_text())["model_list"]
        for effort in ("medium", "high"):
            calls = []
            geometry_calls = [0] * len(baseline)

            def geometry_once(document, page_index):
                """统计真实提取调用，检查表格和文本阶段是否复用同一份字符几何。"""
                geometry_calls[page_index] += 1
                return original_geometry(document, page_index)

            def fixed_ocr(_model, images, **_kwargs):
                """冻结低置信 OCR 响应，并记录实际图像输入以验证宿主回退不变。"""
                calls.append(
                    [
                        {
                            "shape": list(image.shape),
                            "dtype": str(image.dtype),
                            "sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                        }
                        for image in images
                    ]
                )
                return [[("", 0.0) for _ in images]]

            with (
                PDFDocument(payload) as document,
                patch.object(PDFDocument, "get_page_chars_with_geometry", geometry_once),
                patch.object(content, "run_ocr_inference", fixed_ocr),
            ):
                pages = [document[index] for index in range(len(document))]
                images = [
                    {"img_pil": document.render_page(index, scale=1.0).pil_image, "scale": 1.0}
                    for index in range(len(document))
                ]
                try:
                    model = deepcopy(baseline)
                    layout = []
                    for index, blocks in enumerate(model):
                        size = pages[index].size
                        layout.append(
                            [
                                {
                                    "label": "display_formula" if block["type"] == "equation" else block["type"],
                                    "bbox": [v * size[i % 2] for i, v in enumerate(block["bbox"])],
                                }
                                for block in blocks
                            ]
                        )
                        for block in blocks:
                            if block["type"] == "table":
                                block.pop("content", None)
                    geometries = [None] * len(pages)
                    vectors = [None] * len(pages)
                    summary = tables._apply_native_txt_table_priority(
                        model,
                        layout,
                        pages,
                        images,
                        effort=effort,
                        page_text_geometries=geometries,
                        page_vector_geometries=vectors,
                    )
                    if effort == "high":
                        model_inputs, accepted = tables._split_native_high_table_blocks(deepcopy(model))
                    else:
                        model_inputs = [
                            [b for b in blocks if b["type"] == "table" and not b.get("content")] for blocks in model
                        ]
                        accepted = None
                    context = SimpleNamespace(ocr_model=SimpleNamespace(ocr=object()))
                    filled = content._fill_window_block_content_and_lines(
                        images,
                        pages,
                        model,
                        [[] for _ in pages],
                        [[] for _ in pages],
                        "txt",
                        effort,
                        {"text"},
                        context,
                        geometries,
                        page_vector_geometries=vectors,
                    )
                    assert max(geometry_calls, default=0) <= 1, (name, effort, geometry_calls)
                    output[f"{name}/{effort}"] = {
                        "source_sha256": source_hash,
                        "summary": asdict(summary),
                        "model": filled,
                        "layout": layout,
                        "remaining_model_inputs": model_inputs,
                        "accepted_high_tables": accepted,
                        "post_ocr_calls": calls,
                        "geometry_calls": geometry_calls,
                    }
                finally:
                    for image in images:
                        image["img_pil"].close()
            print(name, effort, asdict(summary), "OCR calls", len(calls), flush=True)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "output.json").write_text(json.dumps(output, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (args.output / "report.json").write_text(
        json.dumps(
            {
                "compute": backend_info(),
                "mineru_source": str(args.mineru_source.resolve()),
                "scope": "Frozen Flash-derived layout, real PDF primitives, fixed low-confidence post-OCR; no model inference",
            },
            indent=2,
        )
    )
    return output


def main() -> None:
    """显式指定宿主 checkout 和后端，不向 DocVortex 的运行依赖加入 MinerU。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mineru-source", type=Path, required=True)
    parser.add_argument("--flash-baseline", type=Path, required=True)
    parser.add_argument("--path", type=Path, action="append", help="额外或指定 PDF，必须存在于冻结区域基线")
    parser.add_argument("--backend", choices=("python", "rust"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    os.environ["DOCVORTEX_COMPUTE_BACKEND"] = args.backend
    output = capture(args)
    if args.baseline:
        assert output == json.loads((args.baseline / "output.json").read_text()), "MinerU native-path replay differs"


if __name__ == "__main__":
    main()
