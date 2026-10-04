"""Compare the platform products with fixed PDFium/font and strictly verify the geometry and ordering semantics of CJK."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

_TOLERANCE = 1e-3


def _read(path: Path) -> Any:
    """Read artifacts in an explicit encoding that does not depend on the current platform locale."""
    return json.loads(path.read_text(encoding="utf-8"))


def compare_geometry(reference: dict[str, Any], candidate: dict[str, Any], font_hash: str) -> dict[str, Any]:
    """All character indexes/Unicode must be consistent; the loose/tight/origin of the taken over glyph uses the PDF point tolerance."""
    assert reference["source_sha256"] == candidate["source_sha256"], "Source PDF differs"
    assert len(reference["pages"]) == len(candidate["pages"]), "Page count differs"
    count = 0
    maximum = 0.0
    other_changes = 0
    for page_index, (before_page, after_page) in enumerate(zip(reference["pages"], candidate["pages"])):
        assert before_page["size"] == after_page["size"] and before_page["rotation"] == after_page["rotation"]
        assert len(before_page["chars"]) == len(after_page["chars"]), (page_index, "Char count differs")
        for before, after in zip(before_page["chars"], after_page["chars"]):
            assert (before["index"], before["unicode"]) == (after["index"], after["unicode"]), (page_index, before["index"])
            source_font = reference["fonts"][before["font"]]
            target_font = candidate["fonts"][after["font"]]
            selected = source_font["data_sha256"] == font_hash
            assert selected == (target_font["data_sha256"] == font_hash), "Font policy differs"
            if not selected:
                other_changes += any(before[field] != after[field] for field in ("loose", "tight", "origin"))
                continue
            assert source_font["embedded"] == target_font["embedded"] == 0
            count += 1
            for field in ("loose", "tight", "origin"):
                left, right = before[field], after[field]
                assert (left is None) == (right is None), (page_index, before["index"], field)
                if left is None:
                    continue
                assert len(left) == len(right)
                difference = max(abs(a - b) for a, b in zip(left, right))
                assert math.isfinite(difference) and difference <= _TOLERANCE, (page_index, before["index"], field, difference)
                maximum = max(maximum, difference)
    assert count > 0, "The fixture did not exercise the bundled CJK font"
    return {"cjk_chars": count, "max_cjk_delta_points": maximum, "other_font_geometry_changes": other_changes}


def _semantics(model: dict[str, Any]) -> list[list[tuple[Any, ...]]]:
    """Block type, text/table structure and angles are compared in original reading order, with material pixels separated for visual review."""
    return [[(block["type"], block.get("content"), block.get("angle")) for block in page] for page in model["pages"]]


def main() -> None:
    """Using one platform as a reference, all other platforms are checked and the maximum auditable geometric difference is saved."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert len(args.directories) >= 2
    reference = args.directories[0]
    metadata = _read(reference / "metadata.json")
    report = {"runtime": metadata["runtime"], "tolerance_points": _TOLERANCE, "comparisons": []}
    for candidate in args.directories[1:]:
        target = _read(candidate / "metadata.json")
        assert target["runtime"] == metadata["runtime"], "PDFium or font identity differs"
        assert target["documents"].keys() == metadata["documents"].keys()
        for key in metadata["documents"]:
            result = compare_geometry(
                _read(reference / key / "geometry.json"),
                _read(candidate / key / "geometry.json"),
                metadata["runtime"]["font_sha256"],
            )
            assert _semantics(_read(reference / key / "model.json")) == _semantics(_read(candidate / key / "model.json")), (
                key,
                "Semantic order differs",
            )
            report["comparisons"].append(
                {
                    "reference": metadata["system"],
                    "candidate": target["system"],
                    "document": key,
                    **result,
                    "semantics_equal": True,
                }
            )
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
