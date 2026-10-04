"""Isolate PDF export timing, freeze input, diagnostics, PDF bytes and page-by-page visual signatures."""

from __future__ import annotations

import argparse
import cProfile
from dataclasses import asdict
import gc
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

from pipeline import MemorySampler, artifact_signature, digest, write_json

ROOT = Path(__file__).resolve().parents[2]


def source_case(source: str):
    """Read fixed Bundle, or generate portable corpora containing vector formulas, links and repeat material."""
    from docvortex import load_bundle
    from docvortex.assets import AssetStore
    from docvortex.result import DocumentResult
    from docvortex.schema import MiddleJson, PageInfo
    from PIL import Image

    if source != "fixture:rich":
        return load_bundle(source)
    image = BytesIO()
    Image.new("RGB", (240, 120), "#3678a8").save(image, "PNG")
    pages = []
    for page_idx in range(3):
        blocks = []
        for index in range(8):
            y = 0.06 + index * 0.085
            blocks.append(
                {
                    "type": "text",
                    "index": index,
                    "bbox": [0.08, y, 0.92, y + 0.075],
                    "content": [
                        {"type": "text", "content": "中文混排 Formula H₂O: "},
                        {"type": "equation_inline", "content": r"\frac{x^2+1}{\sqrt{y}}"},
                        {"type": "hyperlink", "url": "https://example.com", "content": [{"type": "text", "content": " LINK"}]},
                    ],
                }
            )
        for index in range(2):
            bbox = [0.08 + index * 0.45, 0.78, 0.47 + index * 0.45, 0.94]
            blocks.append(
                {
                    "type": "image",
                    "index": 10 + index,
                    "bbox": bbox,
                    "content": [
                        {
                            "type": "image_body",
                            "index": 10 + index,
                            "bbox": bbox,
                            "content": "",
                            "image_path": "images/repeated.png",
                        }
                    ],
                }
            )
        pages.append(PageInfo.model_validate({"page_idx": page_idx, "blocks": blocks}))
    middle = MiddleJson(
        pages=pages,
        is_full_document=True,
        metadata={"file_suffix": "pdf", "producer": {"name": "pdf-benchmark", "version": "1"}},
        extensions={
            "docvortex_layout": {"version": 1, "pages": [{"page_idx": i, "width_pt": 400, "height_pt": 600} for i in range(3)]}
        },
    )
    return DocumentResult(middle, AssetStore({"images/repeated.png": image.getvalue()}))


def input_digest(result) -> str:
    """Verify the complete semantic tree and material content, and confirm that renderer has not modified the input."""
    return digest(
        json.dumps(
            {
                "middle": result.middle_json.model_dump(mode="json"),
                "assets": {name: digest(data) for name, data in sorted(result.assets.items())},
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    )


def pdf_signature(payload: bytes) -> dict:
    """Freezing pixels, text, page frames, and link targets allows comparison of actual output outside the container across accelerated backends."""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(payload))
    pages = []
    for page in reader.pages:
        links = []
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            links.append(
                {
                    "rect": list(annotation.get("/Rect", [])),
                    "action": str(annotation.get("/A", {})),
                    "destination": str(annotation.get("/Dest", "")),
                }
            )
        pages.append({"box": list(page.mediabox), "links": links})
    return {"visual": artifact_signature("pdf", payload), "pages": pages}


def worker(args) -> None:
    """Individual inputs and layouts are measured in separate processes, pure Python fallback is only simulated within this test process."""
    if args.backend == "python":
        sys.modules["_rl_accel"] = None
    import docvortex
    from docvortex import render_artifact
    from docvortex.render import PdfLayout, PdfRenderOptions
    from loguru import logger

    logger.remove()
    result = source_case(args.source[0])
    before = input_digest(result)
    options = PdfRenderOptions(layout=PdfLayout(args.layout))
    sampler = MemorySampler()
    sampler.thread.start()
    times = []
    expected = None
    try:
        for index in range(args.runs + 1):
            gc.collect()
            start = time.perf_counter()
            artifact = render_artifact(result.middle_json, "pdf", assets=result.assets, options=options)
            elapsed = time.perf_counter() - start
            signature = (digest(artifact.content), artifact.diagnostics)
            if expected is not None and signature != expected:
                raise AssertionError("Non-deterministic PDF or diagnostics")
            expected = signature
            if index == 0:
                first = elapsed
            else:
                times.append(elapsed)
        memory = sampler.finish()
    finally:
        if sampler.thread.is_alive():
            sampler.finish()
    assert input_digest(result) == before, "Renderer mutated its input"
    from reportlab.lib import rl_accel

    backend = "accel" if rl_accel._c_funcs else "python"
    if args.backend == "accel" and backend != "accel":
        raise AssertionError("ReportLab native accelerator is not loaded")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    (output / "output.pdf").write_bytes(artifact.content)
    write_json(output / "signature.json", pdf_signature(artifact.content))
    write_json(output / "diagnostics.json", [asdict(d) for d in artifact.diagnostics])
    dependencies = {}
    for name in ("docvortex", "reportlab", "rl_accel", "ziamath", "pypdfium2", "pypdf", "pillow"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    implementation = Path(docvortex.__file__).resolve()
    revision = subprocess.run(["git", "-C", str(implementation.parent), "rev-parse", "HEAD"], capture_output=True, text=True)
    record = {
        "source": args.source[0],
        "layout": args.layout,
        "input_sha256": before,
        "backend": backend,
        "native_functions": sorted(rl_accel._c_funcs),
        "dependencies": dependencies,
        "implementation": str(implementation),
        "implementation_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "first_seconds": first,
        "warm_seconds": times,
        "median_seconds": statistics.median(times) if times else None,
        "memory": memory,
        "bytes": len(artifact.content),
        "sha256": digest(artifact.content),
        "diagnostics": len(artifact.diagnostics),
        "source_pages": len(result.middle_json.pages),
    }
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(render_artifact, result.middle_json, "pdf", assets=result.assets, options=options)
        profiler.dump_stats(str(output / "render.prof"))
    write_json(output / "record.json", record)


def main() -> None:
    """Run documents and layouts one by one and refuse to overwrite the baseline or ignore output differences."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", help="Bundle directory or fixture:rich")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--backend", choices=("auto", "accel", "python"), default="auto")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--layout", choices=("original", "reflow"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.runs < 0:
        parser.error("--runs must be non-negative")
    args.source = args.source or ["fixture:rich"]
    if args.worker:
        worker(args)
        return
    if args.output.exists():
        parser.error("Output directory already exists")
    args.output.mkdir(parents=True)
    records = []
    for index, source in enumerate(args.source):
        for layout in ("original", "reflow"):
            relative = f"{index:02d}-{layout}"
            print(f"Rendering {source} [{layout}]", flush=True)
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--source",
                source,
                "--output",
                str(args.output / relative),
                "--layout",
                layout,
                "--backend",
                args.backend,
                "--runs",
                str(args.runs),
            ]
            if args.profile:
                command.append("--profile")
            subprocess.run(command, check=True, cwd=ROOT)
            record = json.loads((args.output / relative / "record.json").read_text())
            records.append({**record, "artifact": relative})
            print(f"  median={record['median_seconds']} backend={record['backend']}", flush=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    write_json(
        args.output / "report.json",
        {"revision": revision, "worktree": dirty, "python": sys.version, "platform": platform.platform(), "records": records},
    )
    if args.baseline:
        baseline = json.loads((args.baseline / "report.json").read_text())
        if len(baseline["records"]) != len(records):
            raise AssertionError("Different corpus length")
        comparisons = []
        for old, new in zip(baseline["records"], records, strict=True):
            same_backend = old["backend"] == new["backend"]
            left, right = args.baseline / old["artifact"], args.output / new["artifact"]
            equal = old["input_sha256"] == new["input_sha256"] and old["layout"] == new["layout"]
            for name in ("signature.json", "diagnostics.json"):
                equal &= json.loads((left / name).read_text()) == json.loads((right / name).read_text())
            if same_backend:
                equal &= old["sha256"] == new["sha256"]
            comparisons.append(
                {
                    "artifact": new["artifact"],
                    "equal": equal,
                    "byte_equal": old["sha256"] == new["sha256"],
                    "seconds_ratio": new["median_seconds"] / old["median_seconds"]
                    if new["median_seconds"] and old["median_seconds"]
                    else None,
                    "rss_ratio": new["memory"]["parent_peak_rss_bytes"] / max(1, old["memory"]["parent_peak_rss_bytes"]),
                }
            )
        write_json(args.output / "comparison.json", comparisons)
        if not all(item["equal"] for item in comparisons):
            raise AssertionError("PDF output differs; inspect comparison.json")


if __name__ == "__main__":
    main()
