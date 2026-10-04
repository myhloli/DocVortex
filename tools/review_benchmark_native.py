"""Freeze the model, export and frame of the specified corpus through the public parsing entrance without modifying the original evaluation document."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path


def main() -> None:
    """Isolate the source code entry and save the reviewable product one by one to avoid repeated execution of the rendering sub-process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids", type=int, nargs="*")
    args = parser.parse_args()
    root = args.source_root.resolve()
    sys.path.insert(0, str(root / "src"))
    from importlib.metadata import version

    from loguru import logger

    from docvortex.api import parse
    from docvortex._compute_backend import backend_info
    from docvortex.visualization import render_layout_pdf

    logger.remove()
    args.output.mkdir(parents=True, exist_ok=False)
    code = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        code.update(str(path.relative_to(root)).encode() + path.read_bytes())
    documents = []
    for source in sorted(args.input_dir.glob("*.pdf")):
        number = int(source.stem[-3:]) if source.stem[-3:].isdigit() else None
        if args.ids and number not in args.ids:
            continue
        folder = args.output / source.stem
        folder.mkdir()
        result = parse(source, keep_model_json=True)
        (folder / "model.json").write_text(json.dumps(result.model_json.to_dict(), ensure_ascii=False), encoding="utf-8")
        (folder / "middle.json").write_text(json.dumps(result.to_dict(), ensure_ascii=False), encoding="utf-8")
        result.export(folder / "document.md")
        result.export(folder / "document.html", output_format="html")
        from bs4 import BeautifulSoup
        from PIL import Image

        exported = BeautifulSoup((folder / "document.html").read_text(), "html.parser")
        for asset in exported.find_all("img", src=True):
            if asset["src"].startswith(("data:", "http:", "https:")):
                continue
            with Image.open(folder / asset["src"]) as original:
                original.load()
                assert min(original.size) > 0
        (folder / "layout.pdf").write_bytes(render_layout_pdf(source.read_bytes(), result.middle_json.pages))
        documents.append(
            {
                "name": source.stem,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "pages": len(result.middle_json.pages),
                "code_sha256": code.hexdigest(),
            }
        )
        print(source.stem, flush=True)
    (args.output / "summary.json").write_text(
        json.dumps(
            {
                "documents": documents,
                "python": sys.executable,
                "platform": platform.platform(),
                "pypdfium2": version("pypdfium2"),
                "environment": {
                    "python_version": sys.version,
                    "pypdfium2": version("pypdfium2"),
                    "compute_backend": backend_info(),
                },
                "code_sha256": code.hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
