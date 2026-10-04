"""Corpus discovery and input fingerprinting of the unified Flash and shared PDF benchmarks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def discover_demo_pdfs(root: Path = ROOT) -> list[Path]:
    """Recursively discover demo PDF, so that newly added subdirectory samples automatically enter the next baseline."""

    return sorted((root / "demo/pdfs").rglob("*.pdf"))


def corpus_paths(kind: str, root: Path = ROOT) -> list[Path]:
    """Merge existing layouts, all demo and native table corpus in fixed order."""

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
    """Freezes the formal input path, source bytes, and page numbers to prevent mid-measurement replacement."""

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
        records.append(
            {"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "pages": pages}
        )
    return records
