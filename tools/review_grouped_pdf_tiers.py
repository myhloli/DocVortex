"""用保存的真实 layout 重放 MinerU 的原生优先决策，不加载后续表格模型。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image
from loguru import logger

from docvortex._compute_backend import backend_info
from docvortex.document.pdf import PDFDocument


def main() -> None:
    """记录两档任务数、未修改的外围块及复杂内容拦截，供真实入口验收。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--layout-records", type=Path, required=True)
    parser.add_argument("--mineru-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("不能覆盖已有决策证据")
    sys.path.insert(0, str(args.mineru_root.resolve()))
    from mineru.backend.analysis.pdf import tables

    logger.remove()
    source = args.source.read_bytes()
    records = json.loads(args.layout_records.read_text())
    outputs = {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "layout_sha256": hashlib.sha256(args.layout_records.read_bytes()).hexdigest(),
        "mineru_tables_path": tables.__file__,
        "mineru_tables_sha256": hashlib.sha256(Path(tables.__file__).read_bytes()).hexdigest(),
    }
    with PDFDocument(source) as document:
        pages = [document[record["page"] - 1] for record in records]
        images = [{"img_pil": Image.new("RGB", tuple(record["image_size"])), "scale": record["scale"]} for record in records]
        try:
            for effort, tier in (("medium", "basic"), ("high", "standard")):
                blocks = [deepcopy(record["initial_blocks"]) for record in records]
                layouts = [deepcopy(record["layout"]) for record in records]
                summary = tables._apply_native_txt_table_priority(blocks, layouts, pages, images, effort=effort)
                if effort == "medium":
                    inputs = [
                        np.zeros((image["img_pil"].height, image["img_pil"].width, 3), dtype=np.uint8) for image in images
                    ]
                    pending = len(tables._collect_medium_table_tasks(blocks, layouts, inputs))
                else:
                    vlm, _accepted = tables._split_native_high_table_blocks(deepcopy(blocks))
                    pending = sum(block["type"] == "table" for page in vlm for block in page)
                outputs[tier] = {"summary": asdict(summary), "pending_table_inputs": pending, "blocks": blocks}
            complex_cases = []
            # 优先验证新命中的跨列区域；基线没有该结果时检查已有表格的前置拦截。
            choices = [
                (index, block)
                for index, page in enumerate(outputs["basic"]["blocks"])
                for block in page
                if block["type"] == "table"
            ]
            probe_index, probe_block = next((item for item in choices if "colspan=" in item[1].get("content", "")), choices[0])
            for block_type in sorted(tables._NATIVE_TABLE_ALWAYS_COMPLEX_BLOCK_TYPES):
                block = deepcopy(probe_block)
                block.pop("content", None)
                x0, y0, x1, y1 = block["bbox"]
                blocks = [[block, {"type": block_type, "bbox": [x0 + 0.01, y0 + 0.01, x1 - 0.01, y1 - 0.01]}]]
                summary = tables._apply_native_txt_table_priority(
                    blocks, [[]], [pages[probe_index]], [images[probe_index]], effort="medium"
                )
                assert summary.complex_fallbacks == 1 and summary.accepted == 0
                complex_cases.append({"block_type": block_type, "summary": asdict(summary)})
            outputs["complex_cases"] = complex_cases
        finally:
            for image in images:
                image["img_pil"].close()
    outputs["backend"] = backend_info()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outputs, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {tier: {key: value for key, value in outputs[tier].items() if key != "blocks"} for tier in ("basic", "standard")}
        )
    )


if __name__ == "__main__":
    main()
