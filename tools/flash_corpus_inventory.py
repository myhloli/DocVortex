"""冻结 Flash TXT 审阅语料清单，不启动解析或 OCR 模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys

import docvortex
from docvortex._compute_backend import backend_info
from docvortex.document.pdf import PDFDocument, PdfiumFontError
from docvortex.document.pdf.classification_bridge import NativeClassificationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def source_fingerprint(root: Path) -> str:
    """复用审阅工具的路径加源码字节算法，冻结实际导入的 Python 和 Rust 源码。"""
    value = hashlib.sha256()
    for path in sorted([*(root / "src").rglob("*.py"), *(root / "rust").rglob("*.rs")]):
        value.update(str(path.relative_to(root)).encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def capture_environment(root: Path) -> dict:
    """记录实际解释器、导入路径、源码和原生运行时，保留工作树变化证据。"""
    import pypdfium2
    from tools.review_flash_regression import _capture_environment

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        cwd=root,
        check=True,
    )
    return {
        **_capture_environment(root),
        "python_executable": sys.executable,
        "docvortex_import": str(Path(docvortex.__file__).resolve()),
        "pdfium_import": str(Path(pypdfium2.__file__).resolve()),
        "checkout_root": str(root),
        "dirty": bool(status.stdout),
        "git_status": status.stdout,
        "backend": backend_info(),
        "source_sha256": source_fingerprint(root),
        "inventory_tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def build_manifest(source_root: Path) -> dict:
    """遍历真实 PDF 并按内容去重分类，为每个原始路径保存可复核的归属。"""
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    checkout = Path(docvortex.__file__).resolve().parents[2]
    frozen_source = source_fingerprint(checkout)
    paths = sorted(path for path in source_root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf")
    excluded = []
    groups: dict[str, list[Path]] = {}
    for path in paths:
        if path.name.startswith("._"):
            excluded.append({"path": str(path), "aliases": [str(path)], "reason": "apple_double"})
            continue
        try:
            with path.open("rb") as source:
                # 使用标准库流式计算摘要，避免为大文件一次分配全部字节。
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError as exc:
            excluded.append(
                {
                    "path": str(path),
                    "aliases": [str(path)],
                    "reason": "read_error",
                    "classification": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        groups.setdefault(digest, []).append(path)

    documents = []
    names = set()
    for digest, aliases in groups.items():
        name = f"d-{digest[:16]}"
        if name in names:
            raise RuntimeError(f"文档名称哈希前缀冲突: {name}")
        names.add(name)
        item = {
            "name": name,
            "path": str(aliases[0]),
            "sha256": digest,
            "aliases": [str(path) for path in aliases],
        }
        try:
            data = aliases[0].read_bytes()
        except OSError as exc:
            excluded.append(
                {**item, "reason": "read_error", "classification": "failed", "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError(f"统计期间源文件变化: {aliases[0]}")
        try:
            with PDFDocument(data) as pdf:
                item["page_count"] = pdf.page_count
                item["classification"] = pdf.classify()
        except (PdfiumFontError, NativeClassificationError):
            raise
        except Exception as exc:
            excluded.append(
                {**item, "reason": "pdf_error", "classification": "failed", "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if item["classification"] == "txt":
            item["visual_pages"] = min(item["page_count"], 100)
            documents.append(item)
        elif item["classification"] == "ocr":
            excluded.append({**item, "reason": "ocr"})
        else:
            raise RuntimeError(f"未知 PDF 分类: {item['classification']}")

    sidecars = [item for item in excluded if item["reason"] == "apple_double"]
    ocr = [item for item in excluded if item.get("classification") == "ocr"]
    failed = [item for item in excluded if item.get("classification") == "failed"]
    unidentified = [item for item in failed if "sha256" not in item]
    short = [item for item in documents if item["page_count"] <= 100]
    long = [item for item in documents if item["page_count"] > 100]
    counts = {
        "pdf_named_paths": len(paths),
        "apple_double_paths": len(sidecars),
        "real_pdf_paths": len(paths) - len(sidecars),
        "unique_pdf_documents": len(groups),
        "duplicate_paths": sum(len(aliases) - 1 for aliases in groups.values()),
        "txt_documents": len(documents),
        "txt_paths": sum(len(item["aliases"]) for item in documents),
        "txt_pages": sum(item["page_count"] for item in documents),
        "ocr_documents": len(ocr),
        "ocr_paths": sum(len(item["aliases"]) for item in ocr),
        "ocr_pages": sum(item["page_count"] for item in ocr),
        "failed_documents": len(failed) - len(unidentified),
        "failed_paths": sum(len(item["aliases"]) for item in failed),
        "unidentified_paths": len(unidentified),
        "short_documents": len(short),
        "short_pages": sum(item["page_count"] for item in short),
        "long_documents": len(long),
        "long_pages": sum(item["page_count"] for item in long),
        "visual_pages": sum(item["visual_pages"] for item in documents),
    }
    recorded_paths = [alias for item in [*documents, *excluded] for alias in item["aliases"]]
    if len(recorded_paths) != len(set(recorded_paths)) or set(recorded_paths) != {str(path) for path in paths}:
        raise RuntimeError("清单未完整且唯一地记录原始 PDF 路径")
    if counts["txt_documents"] + counts["ocr_documents"] + counts["failed_documents"] != len(groups):
        raise RuntimeError("去重文档分类统计不一致")
    environment = capture_environment(checkout)
    if environment["source_sha256"] != frozen_source:
        raise RuntimeError("统计期间源码变化，清单不能作为本轮版本证据")
    return {
        "source_root": str(source_root),
        "environment": environment,
        "documents": documents,
        "excluded": excluded,
        "counts": counts,
    }


def write_manifest(source_root: Path, output: Path) -> dict:
    """先完成分类和校验，再排他创建清单文件，拒绝覆盖任何既有清单。"""
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    manifest = build_manifest(source_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2)
        target.write("\n")
    return manifest


def main() -> None:
    """从命令行生成审阅清单并打印动态统计，不调用 Flash 解析入口。"""
    from loguru import logger

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    manifest = write_manifest(args.source_root, args.output)
    print(json.dumps({"manifest": str(args.output.resolve()), "counts": manifest["counts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
