"""隔离参数与恢复分支的影响；只在实验进程内替换对象，不增加产品配置。"""

from __future__ import annotations
import argparse
from copy import deepcopy
from dataclasses import asdict
from importlib.metadata import version
import platform
import subprocess
from contextlib import ExitStack
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata
from time import perf_counter
from typing import Any
from unittest.mock import patch

import docvortex
from docvortex.analyzers.native import PdfModel
from docvortex.analyzers.native.pdf import char_geometry as geometry, line_merging, formulas, line_layout, pipeline
from docvortex.document.pdf import PDFDocument, initialize_pdfium_runtime
from docvortex.schema import ModelJson, Producer
from docvortex.postprocess.document import model_json_to_middle_json
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]

# 门槛消融替身对应冻结版本 b4afb58，不能在实现已变化时冒充单因素对照。
_GATE_REFERENCE_SHA256 = "73939259140dbd056d709a9334d8a7bb1df466ffefed8ce627d44b6f4db8bb56"

VARIANTS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "no_size_gate": {"omit_gate": "size"},
    "canonical_size": {"omit_gate": "canonical_size"},
    "no_baseline_gate": {"omit_gate": "baseline"},
    "no_index_gate": {"omit_gate": "indices"},
    "no_source": {"source": False},
    "no_prose": {"prose": False},
    "no_recovery": {"source": False, "prose": False},
    **{
        f"x_{value}_no_source": {
            "source": False,
            "constants": {
                "X_STRONG_MEDIAN_RATIO": value,
                "X_STRONG_RATIO_THRESHOLD": value + 0.05,
                "X_SIBLING_MEDIAN_RATIO": value - 0.15,
                "X_SIBLING_P90_RATIO": value + 0.2,
            },
        }
        for value in (1.15, 1.5, 1.8, 2.0, 2.2)
    },
    **{
        f"y_{value}_no_recovery": {"source": False, "prose": False, "constants": {"Y_DOCUMENT_RISK_P95_RATIO": value}}
        for value in (1.8, 2.6, 3.0)
    },
    **{f"gap_{value}_no_source": {"source": False, "gap": value} for value in (1.5, 3.0, 6.0, 10.5)},
    **{f"padding_{value}_no_recovery": {"source": False, "prose": False, "padding": value} for value in (0.5, 1.5)},
    "origin_left_no_source": {"source": False, "origin_left": True},
}


def visible_text(value: Any) -> str:
    """按原生语义金标的约定提取嵌套文本，不引入符号归一化后的比较偏差。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(visible_text(item) for item in value)
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, (str, list, dict)):
            return visible_text(content)
        for key in ("text", "latex", "html"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def digest(value: Any) -> str:
    """为完整公开输出记录稳定摘要。"""
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def gold_errors(pages: list[list[dict[str, Any]]], expectation: dict[str, Any] | None) -> list[str]:
    """不修改既有金标，记录库存、精确文本、分组与禁止类型的全部失败。"""
    if expectation is None:
        return []

    def norm(value: Any) -> str:
        """保持原金标的空白与可选 NFKC 比较规则。"""
        text = re.sub(r"\s+", "", visible_text(value))
        return unicodedata.normalize("NFKC", text) if expectation.get("normalize_nfkc") is True else text

    errors = []
    actual = Counter(b["type"] for p in pages for b in p)
    if actual != Counter(expectation["type_counts"]):
        errors.append("inventory: " + str(dict(actual)))
    for item in expectation.get("exact_typed_text", []):
        matches = [
            b for b in pages[item["page_index"]] if b["type"] == item["type"] and norm(b["content"]) == norm(item["text"])
        ]
        if len(matches) != 1:
            errors.append("exact: " + item["text"])
    for kind in ("same_block_groups", "different_block_groups"):
        for group in expectation.get(kind, []):
            matches = [
                [
                    i
                    for i, b in enumerate(pages[group["page_index"]])
                    if (kind != "same_block_groups" or b["type"] == group["type"]) and norm(f) in norm(b["content"])
                ]
                for f in group["fragments"]
            ]
            valid = all(len(m) == 1 for m in matches)
            if valid:
                valid = len({m[0] for m in matches}) == (1 if kind == "same_block_groups" else len(matches))
            if not valid:
                errors.append(kind + ": " + str(group["fragments"]))
    for item in expectation.get("forbidden_type_fragments", []):
        if any(b["type"] == item["type"] and norm(item["fragment"]) in norm(b["content"]) for b in pages[item["page_index"]]):
            errors.append("forbidden: " + item["fragment"])
    return errors


def style_errors(pages: list[list[dict[str, Any]]], path: str) -> list[str]:
    """补查原文本金标无法发现的作者名下伸部误判，允许机构编号的真实上标。"""
    if path != "demo/pdfs/中文论文4.pdf":
        return []
    authors = [block for block in pages[4] if "TangJiaping" in re.sub(r"\s+", "", visible_text(block))]
    if len(authors) != 1:
        return ["author text missing or duplicated"]
    spans = authors[0]["content"]
    if not isinstance(spans, list):
        return ["author inline structure missing"]
    bad = [
        span["content"] for span in spans if "subscript" in span.get("styles", []) and any(c.isalpha() for c in span["content"])
    ]
    errors = ["author descenders marked subscript: " + repr(bad)] if bad else []
    if not any("superscript" in span.get("styles", []) and any(c.isdigit() for c in span["content"]) for span in spans):
        errors.append("author affiliation superscripts missing")
    return errors


def configure(stack: ExitStack, config: dict[str, Any]) -> None:
    """在显式实验配置内控制一组参数，离开上下文后恢复所有函数与常量。"""
    if config.get("source") is False:
        stack.enter_context(patch.object(line_merging, "_consecutive_source_row", return_value=False))
    if config.get("prose") is False:
        stack.enter_context(patch.object(formulas, "_fragmented_left_prose", return_value=False))
    for key, value in config.get("constants", {}).items():
        stack.enter_context(patch.object(geometry, key, value))
    if "padding" in config:
        stack.enter_context(patch.object(line_layout, "_TIGHT_OUTPUT_PADDING", config["padding"]))
    if "gap" in config:

        def touching(first_bbox: Any, first_height: float, second_bbox: Any, second_height: float) -> bool:
            """仅改变原紧贴分支的最大水平距离，其余原始条件逐项保留。"""
            height = max(first_height, second_height)
            overlap = max(0.0, min(first_bbox[3], second_bbox[3]) - max(first_bbox[1], second_bbox[1]))
            smaller = max(0.1, min(first_bbox[3] - first_bbox[1], second_bbox[3] - second_bbox[1]))
            if overlap / smaller < 0.7:
                return False
            if abs((first_bbox[1] + first_bbox[3] - second_bbox[1] - second_bbox[3]) / 2) > 0.5 * height:
                return False
            left, right = sorted((first_bbox, second_bbox), key=lambda b: b[0])
            return -0.15 * height <= right[0] - left[2] <= config["gap"]

        stack.enter_context(patch.object(line_merging, "_touching_same_baseline_geometry", touching))
    if "omit_gate" in config:

        def source_row(first: Any, second: Any) -> bool:
            """逐一去除恢复路径中的门槛，其余条件保持原样。"""
            import statistics

            if first.angle != 0 or second.angle != 0 or first.baseline is None or second.baseline is None:
                return False
            if first.preserve_split_boundary or second.preserve_split_boundary or not first.chars or not second.chars:
                return False
            a, b = first.source_bbox, second.source_bbox
            if a is None or b is None:
                return False
            ah, bh = a[3] - a[1], b[3] - b[1]
            if config["omit_gate"] != "baseline" and abs(first.baseline - second.baseline) > 0.25 * min(ah, bh):
                return False
            if not line_merging._same_baseline_geometry(a, ah, b, bh):
                return False
            if config["omit_gate"] != "size":
                sizes = [
                    [
                        float(size)
                        for c in line.chars
                        if isinstance(size := (c.get("font") or {}).get("size"), (int, float)) and size > 0
                    ]
                    for line in (first, second)
                ]
                if not all(sizes):
                    return False
                av, bv = (statistics.median(v) for v in sizes)
                if config["omit_gate"] == "canonical_size":
                    av, bv = line_merging._line_effective_height(first, a), line_merging._line_effective_height(second, b)
                if max(av, bv) > 1.2 * min(av, bv):
                    return False
            left, right = sorted((first, second), key=lambda line: line.bbox[0])
            li = [i for c in left.chars for i in c.get("source_indices", (c.get("char_idx"),))]
            ri = [i for c in right.chars for i in c.get("source_indices", (c.get("char_idx"),))]
            if not li or not ri or not all(isinstance(i, int) for i in li + ri):
                return False
            return config["omit_gate"] == "indices" or 1 <= min(ri) - max(li) <= 2

        stack.enter_context(patch.object(line_merging, "_consecutive_source_row", source_row))
    if config.get("origin_left"):
        original = geometry._build_x_char_repair

        def cell(*args: Any, **kwargs: Any) -> Any:
            """实验保留字符 origin 左边界，而不采用正向字形侧边距。"""
            kwargs["left_bearing"] = min(kwargs["left_bearing"], 0.0)
            return original(*args, **kwargs)

        stack.enter_context(patch.object(geometry, "_build_x_char_repair", cell))


def package_source_digest() -> str:
    """对整个解析包取源码指纹，发现实验期间其它任务的源码变动。"""
    package = Path(docvortex.__file__).parent
    return digest(
        {str(path.relative_to(package)): sha256(path.read_bytes()).hexdigest() for path in sorted(package.rglob("*.py"))}
    )


def read_pdf(path: Path) -> bytes:
    """读取完整版本化语料，XOR 样例只在内存中解码。"""
    data = path.read_bytes()
    if path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(data))
    return data


def main() -> None:
    """冻结运行信息并执行可复现实验，不修改生产代码、默认参数或既有金标。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--variants", nargs="+", choices=sorted(VARIANTS), default=list(VARIANTS))
    parser.add_argument("--corpus", choices=("screen", "full"), default="screen")
    parser.add_argument("--baseline", type=Path, help="flash_pdf.py 生成的冻结报告目录")
    args = parser.parse_args()
    if any("omit_gate" in VARIANTS[name] for name in args.variants):
        if sha256(Path(line_merging.__file__).read_bytes()).hexdigest() != _GATE_REFERENCE_SHA256:
            parser.error("Gate ablations require the b4afb58 line_merging.py reference; use an isolated reference checkout")
    logger.disable("docvortex")
    if args.output.exists():
        parser.error("Choose a new output directory")
    args.output.mkdir(parents=True)
    expectations = {
        x["path"]: x
        for x in json.loads((ROOT / "tests/fixtures/flash_layout_block_expectations.json").read_text(encoding="utf-8"))[
            "documents"
        ]
    }
    paths = ["demo/pdfs/中文论文3.pdf", "demo/pdfs/中文论文4.pdf", "demo/pdfs/mixed_elements_pages_03_06.pdf"]
    if args.corpus == "full":
        manifest = json.loads((ROOT / "tests/fixtures/flash_layout_geometry_manifest.json").read_text(encoding="utf-8"))
        paths = [x["path"] for x in manifest["documents"]]
        paths.extend(str(p.relative_to(ROOT)) for p in sorted((ROOT / "tests/unittest/pdfs/native_pdf_tables").glob("*.pdf")))
    references = {}
    if args.baseline:
        references = {
            r["path"]: r for r in json.loads((args.baseline / "report.json").read_text(encoding="utf-8"))["documents"]
        }
        if args.corpus == "full" and set(paths) != set(references):
            parser.error("Baseline corpus differs")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    report = {
        "reference_commit": revision.stdout.strip() if revision.returncode == 0 else None,
        "runtime": asdict(initialize_pdfium_runtime()),
        "python": platform.python_version(),
        "dependencies": {name: version(name) for name in ("docvortex", "pypdfium2", "pydantic", "numpy")},
        "engine_modules": {
            module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (geometry, line_merging, formulas, line_layout)
        },
        "package_source_sha256": package_source_digest(),
        "corpus": args.corpus,
        "variants": {name: VARIANTS[name] for name in args.variants},
        "records": [],
    }
    for name in args.variants:
        with ExitStack() as stack:
            configure(stack, VARIANTS[name])
            for index, path in enumerate(dict.fromkeys(paths)):
                source = ROOT / path
                source_hash = sha256(source.read_bytes()).hexdigest()
                if path in references and references[path]["source_sha256"] != source_hash:
                    raise ValueError(f"Input changed: {path}")
                started = perf_counter()
                raw_checks: list[str] = []
                original_analyze = pipeline._analyze_native_document

                def analyze_with_checks(document: PDFDocument) -> list[list[dict[str, Any]]]:
                    """在公共符号归一化之前检查原生金标，公开输出仍完整经过 PdfModel。"""
                    raw_pages = original_analyze(document)
                    raw_checks.extend(gold_errors(raw_pages, expectations.get(path)))
                    return raw_pages

                with patch.object(pipeline, "_analyze_native_document", side_effect=analyze_with_checks):
                    with PDFDocument(read_pdf(source)) as document:
                        pages = PdfModel().predict(document)
                middle = model_json_to_middle_json(
                    ModelJson(
                        pages=deepcopy(pages),
                        page_index_map=[],
                        file_suffix="pdf",
                        producer=Producer(name="docvortex", version="refactor-baseline"),
                    )
                ).model_dump(mode="json")
                output = {"model_list": pages, "middle_json": middle}
                record = {
                    "variant": name,
                    "path": path,
                    "source_sha256": source_hash,
                    "full_output_sha256": digest(output),
                    "seconds": perf_counter() - started,
                    "counts": dict(Counter(b["type"] for p in pages for b in p)),
                    "gold_checked": path in expectations,
                    "gold_errors": raw_checks,
                    "style_errors": style_errors(pages, path),
                }
                if path in references:
                    record["equal_to_baseline"] = record["full_output_sha256"] == references[path]["full_output_sha256"]
                folder = args.output / name / f"{index:02d}-{source.stem}"
                folder.mkdir(parents=True)
                (folder / "output.json").write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
                record["artifact"] = str(folder.relative_to(args.output))
                report["records"].append(record)
                (args.output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    name, path, "gold_errors", len(record["gold_errors"]), "equal", record.get("equal_to_baseline"), flush=True
                )
    report["source_unchanged"] = package_source_digest() == report["package_source_sha256"]
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not report["source_unchanged"]:
        raise RuntimeError("Engine source changed during calibration; discard this run and use an isolated checkout")


__all__ = ["main"]

if __name__ == "__main__":
    main()
