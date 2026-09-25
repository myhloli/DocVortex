"""统一 Flash 与共享 PDF 基准的语料发现和输入指纹。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def discover_demo_pdfs(root: Path = ROOT) -> list[Path]:
    """递归发现 demo PDF，使新增子目录样本自动进入下一次基准。"""

    return sorted((root / "demo/pdfs").rglob("*.pdf"))


def corpus_paths(kind: str, root: Path = ROOT) -> list[Path]:
    """按固定顺序合并既有布局、全部 demo 和原生表格语料。"""

    if kind not in {"demo", "all"}:
        raise ValueError(f"Unknown PDF corpus: {kind}")
    demo = discover_demo_pdfs(root)
    if kind == "demo":
        return [path.resolve() for path in demo]
    manifest = json.loads((root / "tests/fixtures/flash_layout_geometry_manifest.json").read_text(encoding="utf-8"))
    paths = [
        *(root / item["path"] for item in manifest["documents"]),
        *demo,
        *sorted((root / "tests/unittest/pdfs/native_pdf_tables").rglob("*.pdf")),
    ]
    return list(dict.fromkeys(path.resolve() for path in paths))


def corpus_manifest(paths: list[Path]) -> list[dict[str, str | int]]:
    """冻结正式输入的路径、源字节和页数，阻止测量中途替换。"""

    from pypdfium2 import PdfDocument

    records = []
    for path in paths:
        data = path.read_bytes()
        payload = data
        if path.suffix == ".xor":
            key = b"MinerU flash layout fixture"
            payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(data))
        with PdfDocument(payload) as document:
            pages = len(document)
        records.append({"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "pages": pages})
    return records
