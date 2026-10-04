"""Replay the manual annotation corpus and save the current model, final labeling frame and geometric inspection results one by one."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from loguru import logger

from docvortex.analyzers.native import PdfModel
from docvortex.document.pdf import PDFDocument
from docvortex.postprocess.pages import model_json_to_pages
from docvortex.schema import ModelJson

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests/fixtures/flash_manual_annotations.json"


def visible(value: object) -> str:
    """Recursively extract visible content, styles and link wrapping without changing text assertions."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(visible(item) for item in value)
    if isinstance(value, dict):
        return visible(value.get("content", ""))
    return ""


def contains(outer: list, inner: list, tolerance: float = 0.004) -> bool:
    """Check that the member area is covered by the target block by normalized page coordinates."""
    return all((outer[i] <= inner[i] + tolerance if i < 2 else outer[i] >= inner[i] - tolerance) for i in range(4))


def check_case(case: dict, page: list[dict]) -> dict:
    """Defined structural conditions are checked; items requiring visual confirmation are clearly marked and are not passed by default."""
    operation, anchors = case["operation"], case["anchors"]
    if operation == "table":
        anchors = [anchor for anchor in anchors if anchor["type"] != "caption"]
    if case.get("body_original_indices") is not None:
        anchors = [
            anchor
            for index, anchor in zip(case["original_indices"], case["anchors"], strict=True)
            if index in case["body_original_indices"]
        ]
    required_type = {
        "merge": "text",
        "title": "paragraph_title",
        "equation": "equation",
        "table": "table",
        "caption": "caption",
        "footnote": "page_footnote",
        "code": "code",
        "logo": "image",
    }.get(operation)
    if case.get("expected_regions"):
        regions = case["expected_regions"]
        required_type = case.get("expected_type", "image")
    elif anchors and required_type:
        regions = [
            [
                min(a["bbox"][0] for a in anchors),
                min(a["bbox"][1] for a in anchors),
                max(a["bbox"][2] for a in anchors),
                max(a["bbox"][3] for a in anchors),
            ]
        ]
    else:
        return {"id": case["id"], "status": "visual_review", "operation": operation}
    if operation == "code" and case.get("body_original_indices") is None:
        # The algorithm title is left alone as code_caption; code_body only overrides the instruction line after the title.
        anchors = anchors[1:]
        if anchors:
            regions[0][1] = min(a["bbox"][1] for a in anchors)
    matches = [
        [index for index, block in enumerate(page) if block["type"] == required_type and contains(block["bbox"], region)]
        for region in regions
    ]
    passed = all(matches)
    if passed and case.get("allowed_bounds"):
        passed = all(any(contains(case["allowed_bounds"], page[index]["bbox"]) for index in indices) for indices in matches)
    if operation == "footnote" and not passed:
        note_boxes = [
            block["bbox"] for block in page if block["type"] == "page_footnote" and contains(regions[0], block["bbox"], 0.01)
        ]
        if note_boxes:
            union = [
                min(b[0] for b in note_boxes),
                min(b[1] for b in note_boxes),
                max(b[2] for b in note_boxes),
                max(b[3] for b in note_boxes),
            ]
            passed = contains(union, regions[0])
    if passed and operation == "merge":
        chosen = page[matches[0][0]]
        passed = not any(
            block is not chosen
            and block["type"] in {"paragraph_title", "equation"}
            and contains(chosen["bbox"], block["bbox"], 0.001)
            for block in page
        )
    return {
        "id": case["id"],
        "status": "geometry_pass" if passed else "failed",
        "operation": operation,
        "matches": matches,
        "regions": regions,
    }


def review_document(document: dict, output: Path) -> list[dict]:
    """Completely parse an original, preserve replayable evidence and perform machine checks of manual inventories."""
    source = ROOT / document["path"]
    data = source.read_bytes()
    assert hashlib.sha256(data).hexdigest() == document["sha256"]
    folder = output / document["name"]
    folder.mkdir(parents=True, exist_ok=True)
    with PDFDocument(data) as pdf:
        model = PdfModel().predict(pdf)
        assert pdf.page_count == document["page_count"]
        pages = model_json_to_pages(
            ModelJson(
                pages=model,
                page_index_map=[],
                metadata={"file_suffix": "pdf", "producer": {"name": "docvortex", "version": "review"}},
            )
        )
        pdf.draw_layout_bbox(pages, str(folder / "layout.pdf"))
    (folder / "model.json").write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    (folder / "pages.json").write_text(
        json.dumps([page.model_dump(mode="json") for page in pages], ensure_ascii=False), encoding="utf-8"
    )
    checks = [check_case(case, model[case["page"] - 1]) for case in document["cases"]]
    (folder / "checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    return checks


def main() -> None:
    """Provides a designated document re-entry without automatically modifying any expectations or historical baselines."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/flash-manual/current")
    parser.add_argument("--documents", nargs="*")
    args = parser.parse_args()
    logger.remove()
    for document in json.loads(MANIFEST.read_text(encoding="utf-8"))["documents"]:
        if args.documents and document["name"] not in args.documents:
            continue
        checks = review_document(document, args.output)
        failed = [item["id"] for item in checks if item["status"] == "failed"]
        print(document["name"], f"{len(checks) - len(failed)}/{len(checks)} without geometry failure", failed, flush=True)


if __name__ == "__main__":
    main()
