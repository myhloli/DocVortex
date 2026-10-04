"""Capture historical corpus under explicit version, compare semantics, geometry and framing on a page-by-page basis without automatic baseline approval."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import html
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

_PAGE_IDENTITY_FIELDS = ("fingerprint", "bbox_fingerprint")


def _capture_environment(root: Path) -> dict:
    """Document commit, interpreter, PDFium and font runtime identities for cross-platform geometric difference attribution."""
    import pypdfium2
    from importlib.metadata import version

    from docvortex.document.pdf import initialize_pdfium_runtime

    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=root, check=False)
    return {
        "commit": commit.stdout.strip() or "unknown",
        "platform": platform.platform(),
        "sys_platform": sys.platform,
        "python": platform.python_version(),
        "pypdfium2": version("pypdfium2"),
        "pdfium_build": str(pypdfium2.PDFIUM_INFO),
        "font_runtime": asdict(initialize_pdfium_runtime()),
    }


def build_comparison(output: Path, baseline: Path) -> None:
    """Generate front and rear labels of the same scale for each change page, and the page content and model JSON can be directly reviewed."""
    from docvortex.document.pdf import PDFDocument

    documents = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    sections = []
    for document in documents:
        name = document["name"]
        if not document.get("changed_pages"):
            continue
        sections.append(f"<h2>{html.escape(name)}</h2>")
        folder = output / name
        with PDFDocument(str(baseline / name / "layout.pdf")) as old, PDFDocument(str(folder / "layout.pdf")) as new:
            for page in document["changed_pages"]:
                for label, pdf in (("before", old), ("after", new)):
                    pdf.render_page(page - 1, scale=1.35).pil_image.save(folder / f"page-{page:02d}-{label}.png")
                sections.append(
                    f'<section><h3>Page {page}</h3><div class="pair">'
                    f'<figure><figcaption>Before</figcaption><img src="{html.escape(name)}/page-{page:02d}-before.png"></figure>'
                    f'<figure><figcaption>After</figcaption><img src="{html.escape(name)}/page-{page:02d}-after.png"></figure>'
                    "</div></section>"
                )
    (output / "comparison.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Flash regression comparison</title>'
        "<style>body{font-family:system-ui;margin:24px;background:#eee}.pair{display:flex;gap:12px}"
        "figure{margin:0;width:50%}img{width:100%}section{margin-bottom:32px}</style>"
        "<h1>Flash regression: source layout before / after</h1>" + "".join(sections),
        encoding="utf-8",
    )


def main() -> None:
    """Isolate import specified checkout, save complete historical output and change page list."""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-to", type=Path)
    parser.add_argument("--documents", nargs="*")
    args = parser.parse_args()
    root = args.source_root.resolve()
    sys.path[:0] = [str(root / "src"), str(root / "tests/unittest")]
    from loguru import logger
    from _flash_pdf_test_utils import _page_bbox_fingerprint, _page_fingerprint, _visible_text, formula_detection_evidence
    from test_flash_pdf_char_geometry import _read_pdf_fixture
    from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
    from docvortex.document.pdf import PDFDocument
    from docvortex.postprocess.pages import model_json_to_pages
    from docvortex.schema import ModelJson

    logger.remove()
    manifest = json.loads((root / "tests/fixtures/flash_layout_geometry_manifest.json").read_text(encoding="utf-8"))
    environment = _capture_environment(root)
    summaries = []
    source_hash = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        source_hash.update(str(path.relative_to(root)).encode())
        source_hash.update(path.read_bytes())
    for document in manifest["documents"]:
        name = Path(document["path"]).name.removesuffix(".xor").removesuffix(".pdf")
        if args.documents and name not in args.documents:
            continue
        folder = args.output / name
        folder.mkdir(parents=True, exist_ok=True)
        source = _read_pdf_fixture(root / document["path"])
        start = time.monotonic()
        geometry = []
        with PDFDocument(source) as pdf, formula_detection_evidence():
            model = _analyze_native_document(pdf, geometry_diagnostics=geometry)
            pages = model_json_to_pages(
                ModelJson(
                    pages=model,
                    page_index_map=[],
                    metadata={"file_suffix": "pdf", "producer": {"name": "docvortex", "version": "regression"}},
                )
            )
            pdf.draw_layout_bbox(pages, str(folder / "layout.pdf"))
            page_sizes = [list(pdf.page_size(index)) for index in range(len(model))]
        (folder / "model.json").write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
        (folder / "geometry.json").write_text(json.dumps(geometry, ensure_ascii=False), encoding="utf-8")
        summary = {
            "name": name,
            "path": document["path"],
            "sha256": hashlib.sha256(source).hexdigest(),
            "code_sha256": source_hash.hexdigest(),
            "environment": environment,
            "seconds": round(time.monotonic() - start, 3),
            "pages": [
                {
                    "page_index": index,
                    "fingerprint": _page_fingerprint(page),
                    "bbox_fingerprint": _page_bbox_fingerprint(page),
                    "page_size": page_sizes[index],
                    "blocks": [
                        {
                            "type": block.get("type"),
                            "text": " ".join(_visible_text(block.get("content")).split())[:40],
                            "bbox": [round(value, 3) for value in block["bbox"]],
                        }
                        for block in page
                    ],
                }
                for index, page in enumerate(model)
            ],
        }
        if args.compare_to:
            previous = json.loads((args.compare_to / name / "summary.json").read_text(encoding="utf-8"))
            summary["changed_pages"] = [
                index + 1
                for index, (old, new) in enumerate(zip(previous["pages"], summary["pages"], strict=True))
                if any(old.get(field) != new.get(field) for field in _PAGE_IDENTITY_FIELDS)
            ]
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summaries.append(summary)
        print(name, len(model), summary.get("changed_pages", []), flush=True)
    (args.output / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.compare_to:
        build_comparison(args.output, args.compare_to)


if __name__ == "__main__":
    main()
