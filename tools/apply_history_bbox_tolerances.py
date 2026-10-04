"""Write the bbox tolerance frozen by coordinates to the historical fixture based on the Diagnostic Difference Report, without re-approving the baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from diff_history_geometry import _steps, index_summary, summary_fingerprint

_COORD_NAMES = ("x0", "y0", "x1", "y1")


def _load(path: Path) -> object:
    """Read JSON with explicit UTF-8, tool product independent of platform locale."""

    return json.loads(path.read_text(encoding="utf-8"))


def _verify_reference_matches_fixture(fixture: dict, reference: list[dict]) -> None:
    """Reference capture must be consistent with the fixture's page-by-page fingerprint, preventing borrowed fields from re-approving the baseline."""

    by_name = index_summary(reference)
    assert by_name.keys() == index_summary(fixture["documents"]).keys(), "reference document set differs from fixture"
    for document in fixture["documents"]:
        capture = by_name[document["name"]]
        assert capture["sha256"] == document["sha256"], ("source differs", document["name"])
        assert len(capture["pages"]) == len(document["pages"]), ("page count", document["name"])
        for index, (page, expected) in enumerate(zip(capture["pages"], document["pages"], strict=True)):
            assert page["page_index"] == expected["page_index"] == index, ("reference page index differs", document["name"])
            assert page["fingerprint"] == expected["fingerprint"], (
                "reference replay drift",
                document["name"],
                index + 1,
                "content",
            )
            assert page["bbox_fingerprint"] == expected["bbox_fingerprint"], (
                "reference replay drift",
                document["name"],
                index + 1,
                "bbox",
            )


def _verify_existing_platform(fixture: dict, platform: str) -> None:
    """The current fixture only supports single-platform tolerance; it refuses to overwrite or delete frozen configurations of other platforms."""
    for document in fixture["documents"]:
        for page in document["pages"]:
            tolerance = page.get("bbox_tolerance")
            if tolerance is not None and tolerance["platforms"] != [platform]:
                raise ValueError(
                    f"existing tolerance belongs to another platform: {document['name']} page {page['page_index'] + 1}"
                )


def _verify_reports(reference: list[dict], reports: list[dict], platform: str) -> None:
    """Bind the report source and recalculate coordinate-by-coordinate differences before writing, rejecting stale reports, content changes, and incorrect pages or block indexes."""
    captures = index_summary(reference)
    reference_hash = summary_fingerprint(captures)
    environment = reference[0]["environment"]
    for report in reports:
        assert report.get("schema_version") == 2, "regenerate legacy diff report with the current capture and diff tools"
        assert report["reference_sha256"] == reference_hash, "diff report reference capture differs"
        assert report["reference_environment"] == environment, "diff report reference environment differs"
        assert report["candidate_environment"].get("sys_platform") == platform, (
            "diff report candidate platform differs; recapture if missing"
        )
        documents = index_summary(report["documents"])
        assert documents.keys() == captures.keys(), "diff report document set differs"
        for name, document in documents.items():
            capture = captures[name]
            assert document["sha256"] == capture["sha256"], ("diff report source differs", name)
            assert document["candidate_code_sha256"] == capture["code_sha256"], ("parser code differs across platforms", name)
            assert document["page_count"] == len(capture["pages"]), ("diff report page count differs", name)
            assert document["content_changed_pages"] == 0, ("content changes are regressions", name)
            seen_pages = set()
            for page in document["changed_pages"]:
                number = page["page"]
                assert type(number) is int and 1 <= number <= len(capture["pages"]) and number not in seen_pages, (
                    "invalid diff page",
                    name,
                    number,
                )
                seen_pages.add(number)
                assert page["content_changed"] is False, ("content changes are regressions", name, number)
                reference_blocks = capture["pages"][number - 1]["blocks"]
                seen_blocks = set()
                for block in page["blocks"]:
                    index = block["index"]
                    assert type(index) is int and 0 <= index < len(reference_blocks) and index not in seen_blocks, (
                        "invalid diff block",
                        name,
                        number,
                        index,
                    )
                    seen_blocks.add(index)
                    reference_block = reference_blocks[index]
                    assert block["ref_bbox"] == reference_block["bbox"], ("diff reference bbox differs", name, number, index)
                    assert (block["type"], block["text"]) == (reference_block["type"], reference_block["text"]), (
                        "diff block identity differs",
                        name,
                        number,
                        index,
                    )
                    reference_steps = _steps(block["ref_bbox"])
                    candidate_steps = _steps(block["cand_bbox"])
                    delta = {
                        coordinate: actual - expected
                        for coordinate, actual, expected in zip(_COORD_NAMES, candidate_steps, reference_steps, strict=True)
                    }
                    assert (
                        all(type(value) is int for value in block["delta_steps"].values()) and delta == block["delta_steps"]
                    ), ("diff coordinate delta differs", name, number, index)


def _collect_allowances(reports: list[dict]) -> dict[tuple[str, int], dict[str, dict[str, int]]]:
    """Summarize the maximum measured scale difference of block-by-block coordinates of each diagnostic matrix; reject immediately if content changes."""

    collected: dict[tuple[str, int], dict[int, dict[str, int]]] = {}
    for report in reports:
        for document in report["documents"]:
            if document["content_changed_pages"]:
                raise SystemExit(f"content changes are regressions, not tolerance material: {document['name']}")
            for page in document["changed_pages"]:
                page_allowances = collected.setdefault((document["name"], page["page"]), {})
                for block in page["blocks"]:
                    coords = page_allowances.setdefault(block["index"], {})
                    for name in _COORD_NAMES:
                        delta = abs(block["delta_steps"].get(name, 0))
                        if delta:
                            coords[name] = max(coords.get(name, 0), delta)
    return {key: {str(block): coords for block, coords in sorted(blocks.items())} for key, blocks in collected.items()}


def main() -> None:
    """Write the frozen tolerance into the fixture and upgrade it to schema; the tolerance is based on the maximum measured scale difference, without adding any margin."""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--reference", type=Path, required=True, help="参考平台（如 macOS）的捕获 summary.json")
    parser.add_argument("--diff", type=Path, action="append", required=True, help="候选平台差异报告，可多次提供")
    parser.add_argument("--platform", required=True, help="容差生效的平台标识（sys.platform 值，如 linux）")
    parser.add_argument("--reason", required=True, help="例外原因，说明字体回退等归因")
    parser.add_argument("--source", required=True, help="诊断来源，如 PR 与差异产物名称")
    args = parser.parse_args()

    fixture = _load(args.fixture)
    reference = _load(args.reference)
    reports = [_load(path) for path in args.diff]
    _verify_existing_platform(fixture, args.platform)
    _verify_reference_matches_fixture(fixture, reference)
    _verify_reports(reference, reports, args.platform)
    allowances = _collect_allowances(reports)
    assert allowances, "no bbox differences found in the provided diff reports"

    capture_by_name = {document["name"]: document for document in reference}
    applied = []
    for document in fixture["documents"]:
        for page in document["pages"]:
            page_allowances = allowances.get((document["name"], page["page_index"] + 1))
            if page_allowances is None:
                page.pop("bbox_tolerance", None)
                continue
            capture_page = capture_by_name[document["name"]]["pages"][page["page_index"]]
            page["bbox_tolerance"] = {
                "platforms": [args.platform],
                "reason": args.reason,
                "source": args.source,
                "reference_blocks": [block["bbox"] for block in capture_page["blocks"]],
                "allowances": page_allowances,
            }
            applied.append((document["name"], page["page_index"] + 1, page_allowances))
    fixture["schema_version"] = 2
    fixture["bbox_tolerance_policy"] = "未配置坐标容差为零；容差按 0.001 输出网格的整数刻度差冻结，取诊断矩阵最大实测值"
    args.fixture.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"pages_with_tolerance": len(applied), "platform": args.platform}, ensure_ascii=False))
    for name, page_number, page_allowances in applied:
        print(name, f"page {page_number}", json.dumps(page_allowances, ensure_ascii=False))


if __name__ == "__main__":
    main()
