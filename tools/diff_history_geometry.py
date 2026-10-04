"""Compares block-by-block bbox of two historical geometry captures, reporting coordinate scale differences and content fingerprint differences without modifying any baselines."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

_COORD_NAMES = ("x0", "y0", "x1", "y1")
_GRID = 1000


def _read_summary(directory: Path) -> dict[str, dict]:
    """Capture summary by document name index. If it is missing, the capture is considered incomplete."""
    documents = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    return index_summary(documents)


def index_summary(documents: list[dict]) -> dict[str, dict]:
    """Reject empty captures and duplicate document names to avoid silent loss of diagnostic records during indexing."""
    indexed = {document["name"]: document for document in documents}
    assert indexed and len(indexed) == len(documents), "empty capture or duplicate document names"
    return indexed


def summary_fingerprint(documents: dict[str, dict]) -> str:
    """Bind to complete reference capture, ignore JSON typesetting and document arrangement, retain version, fingerprint and coordinate identity."""
    payload = json.dumps(documents, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _steps(bbox: list[float]) -> list[int]:
    """The output quantization grid is an integer scale based on 0.001 to avoid boundary misjudgments caused by floating point representation differences."""
    assert isinstance(bbox, (list, tuple)) and len(bbox) == 4, "bbox must hold four coordinates"
    assert all(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 for value in bbox), "invalid bbox"
    assert all(math.isclose(value * _GRID, round(value * _GRID), rel_tol=0, abs_tol=1e-8) for value in bbox), "bbox is off grid"
    return [round(value * _GRID) for value in bbox]


def _coordinate_points(delta_steps: dict[str, int], page_size: list[float]) -> dict[str, float]:
    """To convert the scale difference to PDF point, use the page width in the horizontal direction and the page height in the vertical direction."""
    return {name: round(delta_steps[name] / _GRID * page_size[0 if name.startswith("x") else 1], 3) for name in _COORD_NAMES}


def compare_document(reference: dict, candidate: dict) -> dict:
    """Page-by-page comparison of content fingerprints with integer scale bbox; content differences are attributed separately from geometric differences."""
    assert reference["sha256"] == candidate["sha256"], ("source differs", reference["name"])
    pages = []
    max_steps = 0
    content_changed = 0
    for index, (ref_page, cand_page) in enumerate(zip(reference["pages"], candidate["pages"], strict=True)):
        assert ref_page["page_index"] == cand_page["page_index"] == index, ("page index differs", reference["name"])
        assert ref_page["page_size"] == cand_page["page_size"], ("page size differs", reference["name"], index + 1)
        ref_blocks, cand_blocks = ref_page["blocks"], cand_page["blocks"]
        page_content_changed = ref_page["fingerprint"] != cand_page["fingerprint"] or len(ref_blocks) != len(cand_blocks)
        content_changed += page_content_changed
        entries = []
        if not page_content_changed:
            for block_index, (ref_block, cand_block) in enumerate(zip(ref_blocks, cand_blocks, strict=True)):
                delta = {
                    name: cand - ref
                    for name, ref, cand in zip(_COORD_NAMES, _steps(ref_block["bbox"]), _steps(cand_block["bbox"]), strict=True)
                }
                if any(delta.values()):
                    max_steps = max(max_steps, max(abs(value) for value in delta.values()))
                    entries.append(
                        {
                            "index": block_index,
                            "type": cand_block.get("type"),
                            "text": cand_block.get("text"),
                            "ref_bbox": ref_block["bbox"],
                            "cand_bbox": cand_block["bbox"],
                            "delta_steps": delta,
                            "delta_points": _coordinate_points(delta, ref_page["page_size"]),
                        }
                    )
        if page_content_changed or entries:
            pages.append({"page": index + 1, "content_changed": page_content_changed, "blocks": entries})
    return {
        "name": reference["name"],
        "sha256": reference["sha256"],
        "page_count": len(reference["pages"]),
        "candidate_code_sha256": candidate["code_sha256"],
        "max_abs_steps": max_steps,
        "content_changed_pages": content_changed,
        "changed_pages": pages,
    }


def build_report(reference: dict[str, dict], candidate: dict[str, dict]) -> dict:
    """Generate reports that trace back to the complete reference capture and check that each document comes from the same runtime environment."""
    assert reference and reference.keys() == candidate.keys(), "captured document sets differ"
    reference_environment = next(iter(reference.values()))["environment"]
    candidate_environment = next(iter(candidate.values()))["environment"]
    assert all(document["environment"] == reference_environment for document in reference.values()), (
        "mixed reference environments"
    )
    assert all(document["environment"] == candidate_environment for document in candidate.values()), (
        "mixed candidate environments"
    )
    return {
        "schema_version": 2,
        "reference_sha256": summary_fingerprint(reference),
        "reference_environment": reference_environment,
        "candidate_environment": candidate_environment,
        "documents": [compare_document(reference[name], candidate[name]) for name in sorted(reference)],
    }


def main() -> None:
    """Output a block-by-block diff report for references/candidates; only content fingerprint changes are considered regressions and exit with a non-zero code."""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference, candidate = _read_summary(args.reference), _read_summary(args.candidate)
    report = build_report(reference, candidate)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    total_content = sum(document["content_changed_pages"] for document in report["documents"])
    affected = [document["name"] for document in report["documents"] if document["changed_pages"]]
    maximum = max((document["max_abs_steps"] for document in report["documents"]), default=0)
    print(
        json.dumps(
            {"max_abs_steps": maximum, "documents_with_diff": affected, "content_changed_pages": total_content},
            ensure_ascii=False,
        )
    )
    for document in report["documents"]:
        for page in document["changed_pages"]:
            print(document["name"], f"page {page['page']}", "content-changed" if page["content_changed"] else "", flush=True)
    raise SystemExit(1 if total_content else 0)


if __name__ == "__main__":
    main()
