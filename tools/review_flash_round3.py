"""Replay the third round of originals and generate a page-by-page acceptance gallery of the original page, the modified pre-frame, and the current frame."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import subprocess
import sys

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests/unittest"))

from tools.review_flash_annotations import review_document  # noqa: E402
from tools.review_flash_regression import _capture_environment  # noqa: E402
from _flash_pdf_test_utils import _page_bbox_fingerprint, _page_fingerprint  # noqa: E402


def main() -> None:
    """Reviewable image evidence is saved; the report does not automatically modify or approve any test baselines."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/flash-round3/final")
    parser.add_argument("--baseline", type=Path, default=ROOT / "output/pdf/flash-round3-analysis")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "tests/fixtures/flash_round3_annotations.json").read_text(encoding="utf-8"))
    logger.remove()
    sections, summaries = [], []
    code_hash = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        code_hash.update(str(path.relative_to(ROOT)).encode())
        code_hash.update(path.read_bytes())
    for document in manifest["documents"]:
        name = document["name"]
        baseline_meta = json.loads((args.baseline / name / "meta.json").read_text(encoding="utf-8"))
        assert baseline_meta["sha256"] == document["sha256"]
        review_document(document, args.output)
        folder = args.output / name
        before = json.loads((args.baseline / name / "model.json").read_text(encoding="utf-8"))
        after = json.loads((folder / "model.json").read_text(encoding="utf-8"))
        changed = {
            index + 1
            for index, (old, new) in enumerate(zip(before, after, strict=True))
            if _page_fingerprint(old) != _page_fingerprint(new) or _page_bbox_fingerprint(old) != _page_bbox_fingerprint(new)
        }
        focus = {case["page"] for case in document["cases"]}
        pages = sorted(focus | changed)
        sections.append(f"<h2>{html.escape(name)}</h2><p><a href='{name}/layout.pdf'>完整标框 PDF</a></p>")
        for page in pages:
            images = []
            for label, source in [
                ("source", ROOT / document["path"]),
                ("before", args.baseline / name / "layout.pdf"),
                ("after", folder / "layout.pdf"),
            ]:
                stem = f"page-{page:02d}-{label}"
                subprocess.run(
                    [
                        "pdftoppm",
                        "-f",
                        str(page),
                        "-l",
                        str(page),
                        "-scale-to",
                        "1350",
                        "-png",
                        "-singlefile",
                        str(source),
                        str(folder / stem),
                    ],
                    check=True,
                    capture_output=True,
                )
                images.append(
                    f"<figure><figcaption>{label}</figcaption><a href='{name}/{stem}.png'>"
                    f"<img loading='lazy' src='{name}/{stem}.png'></a></figure>"
                )
            cases = [case for case in document["cases"] if case["page"] == page]
            text = "；".join(f"{case['id']}: {case['expected']}" for case in cases)
            if not cases:
                text = "同类规则产生的额外变化页，需与原文一起裁决。"
            sections.append(
                f"<section><h3>{name} · 第 {page} 页</h3><p>{html.escape(text)}</p>"
                f"<div class='pages'>{''.join(images)}</div></section>"
            )
        summaries.append(
            {
                "name": name,
                "sha256": document["sha256"],
                "pages": document["page_count"],
                "review_pages": pages,
                "annotated_pages": sorted(focus),
                "changed_pages": sorted(changed),
                "cases": len(document["cases"]),
            }
        )
        print(name, document["page_count"], "pages;", len(pages), "review pages", flush=True)
    (args.output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Flash 空间布局逐页验收</title>'
        "<style>body{font-family:system-ui;margin:24px;background:#eee;color:#222}"
        ".pages{display:flex;gap:8px}figure{margin:0;flex:1;min-width:0}img{width:100%}"
        "section{margin-bottom:32px}figcaption{padding:8px;background:white}</style>"
        "<h1>Flash 空间布局：原文 / 修改前 / 修改后</h1>" + "".join(sections),
        encoding="utf-8",
    )
    (args.output / "summary.json").write_text(
        json.dumps(
            {"documents": summaries, "environment": _capture_environment(ROOT), "code_sha256": code_hash.hexdigest()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
