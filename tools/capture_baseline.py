"""捕获指定引擎的真实文档与渲染产物，供跨仓库迁移差分验证。"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import time
from typing import Any


def json_value(value: Any) -> Any:
    """将字符几何的纯数据对象规范化为可独立读取的 JSON。"""
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "bbox"):
        return json_value(value.bbox)
    return value


def main() -> None:
    """按明确输入清单保存中间协议、分类、字符和全部 renderer 结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root))
    from mineru.backend.analyze import doc_analyze
    from mineru.config import config
    from mineru.model.flash.pdf.document import PDFDocument
    from mineru.render import RenderFormat, render

    config.llm_aided.features.title_leveling = False
    config.llm_aided.features.cross_page_table_cell_merge = False
    sources = sorted((args.source_root / "demo/office_docs").glob("*"))
    sources = [path for path in sources if path.suffix.lower() != ".md"]
    sources += [args.source_root / "demo/pdfs" / name for name in (
        "demo1.pdf", "demo2.pdf", "中文论文2.pdf", "mixed_elements_pages_39_40.pdf",
    )]
    args.output_root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"versions": {name: version(name) for name in ("mineru", "pdftext", "pypdfium2")}, "documents": []}
    for path in sources:
        output = args.output_root / (path.stem + "_" + path.suffix[1:])
        output.mkdir(exist_ok=True)
        entry: dict[str, Any] = {"source": str(path.relative_to(args.source_root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        started = time.perf_counter()
        try:
            if path.suffix == ".pdf":
                with PDFDocument(path.read_bytes()) as document:
                    entry["classification"] = document.classify()
                    geometry = [json_value(document.get_page_chars_with_geometry(index)) for index in range(min(document.page_count, 2))]
                    (output / "characters.json").write_text(json.dumps(geometry, ensure_ascii=False, indent=2))
            middle, model = doc_analyze(path.read_bytes(), effort="flash", parse_mode="txt", file_suffix=path.suffix[1:])
            entry["analysis_seconds"] = time.perf_counter() - started
            entry["pages"] = len(middle.pages)
            (output / "model.json").write_text(model.to_json(skip_defaults=False))
            (output / "middle.json").write_text(middle.to_json(skip_defaults=False))
            entry["outputs"] = {}
            for target in RenderFormat:
                try:
                    result = render(middle, target)
                    payload = result if isinstance(result, bytes) else (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, sort_keys=True)).encode()
                    (output / target.value).write_bytes(payload)
                    entry["outputs"][target.value] = hashlib.sha256(payload).hexdigest()
                except Exception as error:
                    entry["outputs"][target.value] = {"error": f"{type(error).__name__}: {error}"}
        except Exception as error:
            entry["error"] = f"{type(error).__name__}: {error}"
        entry["total_seconds"] = time.perf_counter() - started
        manifest["documents"].append(entry)
        (args.output_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(json.dumps(entry, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
