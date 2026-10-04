"""Use real CLI to verify the source of historical geometry reports and fixture writes and do not change the file when verification fails."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.diff_history_geometry import build_report, index_summary

ROOT = Path(__file__).resolve().parents[1]


def _capture(platform: str) -> list[dict]:
    """Construct a complete capture of two documents, two pages each, covering unaffected pages and document collection verification."""
    return [
        {
            "name": name,
            "sha256": f"pdf-{name}",
            "code_sha256": "same-parser-code",
            "environment": {"commit": "reviewed-commit", "sys_platform": platform},
            "pages": [
                {
                    "page_index": index,
                    "page_size": [600, 800],
                    "fingerprint": f"content-{name}-{index}",
                    "bbox_fingerprint": f"geometry-{name}-{index}",
                    "blocks": [{"type": "text", "text": "中文正文", "bbox": [0.1, 0.2, 0.3, 0.4]}],
                }
                for index in range(2)
            ],
        }
        for name in ("article", "other")
    ]


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, list[dict], list[dict]]:
    """Generates an unrelaxed clamp and platform capture, with only the first page of the first document moved up one tick."""
    reference = _capture("darwin")
    candidate = _capture("linux")
    candidate[0]["pages"][0]["blocks"][0]["bbox"][1] = 0.199
    fixture = {
        "schema_version": 1,
        "documents": [
            {
                "name": document["name"],
                "sha256": document["sha256"],
                "pages": [
                    {key: page[key] for key in ("page_index", "fingerprint", "bbox_fingerprint")} for page in document["pages"]
                ],
            }
            for document in reference
        ],
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    return path, reference, candidate


def _apply(path: Path, reference: list[dict], report: dict, platform: str = "linux") -> subprocess.CompletedProcess:
    """The actual freeze entry is called, and the test files are only located in the pytest temporary directory."""
    reference_path = path.parent / "reference.json"
    report_path = path.parent / "report.json"
    reference_path.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/apply_history_bbox_tolerances.py"),
            str(path),
            "--reference",
            str(reference_path),
            "--diff",
            str(report_path),
            "--platform",
            platform,
            "--reason",
            "已审阅的字体差异",
            "--source",
            "test capture",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_diff_cli_report_can_freeze_only_measured_coordinate(inputs) -> None:
    """From capturing the difference CLI to freezing CLI, check that the original gold mark remains unchanged and the execution is idempotent repeatedly on the same platform."""
    path, reference, candidate = inputs
    original = json.loads(path.read_text(encoding="utf-8"))
    for name, capture in (("reference", reference), ("candidate", candidate)):
        folder = path.parent / name
        folder.mkdir()
        (folder / "summary.json").write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")
    output = path.parent / "diff.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/diff_history_geometry.py"),
            str(path.parent / "reference"),
            str(path.parent / "candidate"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    result = _apply(path, reference, report)
    assert result.returncode == 0, result.stderr
    frozen = json.loads(path.read_text(encoding="utf-8"))
    tolerance = frozen["documents"][0]["pages"][0].pop("bbox_tolerance")
    assert tolerance["platforms"] == ["linux"]
    assert tolerance["allowances"] == {"0": {"y0": 1}}
    assert tolerance["reference_blocks"] == [[0.1, 0.2, 0.3, 0.4]]
    assert frozen["documents"] == original["documents"]
    before = path.read_bytes()
    assert _apply(path, list(reversed(reference)), report).returncode == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("target", ["win32", "darwin"])
def test_applying_another_platform_preserves_existing_fixture(inputs, target: str) -> None:
    """Subsequent platform reports must not delete or overwrite the existing Linux configuration, and the files before and after the failure must be consistent byte-for-byte."""
    path, reference, candidate = inputs
    report = build_report(index_summary(reference), index_summary(candidate))
    assert _apply(path, reference, report).returncode == 0
    before = path.read_bytes()
    for document in candidate:
        document["environment"]["sys_platform"] = target
    # Let the new platform change to another page, while covering the two risks of deleting and replacing the old configuration.
    candidate[0]["pages"][0]["blocks"][0]["bbox"][1] = 0.2
    candidate[1]["pages"][1]["blocks"][0]["bbox"][3] = 0.401
    result = _apply(path, reference, build_report(index_summary(reference), index_summary(candidate)), target)
    assert result.returncode != 0 and "another platform" in result.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("location", "value", "message"),
    [
        (("schema_version",), 1, "regenerate legacy"),
        (("reference_sha256",), "stale", "reference capture differs"),
        (("reference_environment", "commit"), "other", "reference environment differs"),
        (("candidate_environment", "sys_platform"), "win32", "candidate platform differs"),
        (("documents", 0, "sha256"), "other-source", "source differs"),
        (("documents", 0, "candidate_code_sha256"), "other-parser", "parser code differs"),
        (("documents", 0, "page_count"), 3, "page count differs"),
        (("documents", 0, "content_changed_pages"), 1, "content changes"),
        (("documents", 0, "changed_pages", 0, "content_changed"), True, "content changes"),
        (("documents", 0, "changed_pages", 0, "page"), 0, "invalid diff page"),
        (("documents", 0, "changed_pages", 0, "blocks", 0, "index"), -1, "invalid diff block"),
        (("documents", 0, "changed_pages", 0, "blocks", 0, "ref_bbox"), [0.1, 0.3, 0.3, 0.4], "reference bbox differs"),
        (("documents", 0, "changed_pages", 0, "blocks", 0, "text"), "wrong block", "block identity differs"),
        (("documents", 0, "changed_pages", 0, "blocks", 0, "delta_steps", "y0"), -20, "coordinate delta differs"),
        (("documents", 0, "changed_pages", 0, "blocks", 0, "cand_bbox"), [0.1, 0.1994, 0.3, 0.4], "off grid"),
    ],
)
def test_invalid_report_never_changes_fixture(inputs, location, value, message: str) -> None:
    """Error source, semantic or coordinate reports must fail before writing, bad reports cannot be turned into gold standard."""
    path, reference, candidate = inputs
    report = deepcopy(build_report(index_summary(reference), index_summary(candidate)))
    container = report
    for key in location[:-1]:
        container = container[key]
    container[location[-1]] = value
    before = path.read_bytes()
    result = _apply(path, reference, report)
    assert result.returncode != 0 and message in result.stderr, result.stderr
    assert path.read_bytes() == before


def test_report_from_different_reference_is_rejected(inputs) -> None:
    """Real reports generated by another capture round of the corpus of the same name cannot be mixed with the current reference, even if the content fingerprint is the same."""
    path, reference, candidate = inputs
    other_reference = deepcopy(reference)
    other_reference[0]["pages"][0]["blocks"][0]["bbox"][1] = 0.19
    report = build_report(index_summary(other_reference), index_summary(candidate))
    before = path.read_bytes()
    result = _apply(path, reference, report)
    assert result.returncode != 0 and "reference capture differs" in result.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["missing", "duplicate"])
def test_incomplete_or_duplicate_report_documents_are_rejected(inputs, change: str) -> None:
    """The report must completely cover the captured document, duplicate names cannot be silently covered via dictionary index."""
    path, reference, candidate = inputs
    report = build_report(index_summary(reference), index_summary(candidate))
    if change == "missing":
        report["documents"].pop()
    else:
        report["documents"].append(deepcopy(report["documents"][0]))
    before = path.read_bytes()
    assert _apply(path, reference, report).returncode != 0
    assert path.read_bytes() == before


def test_content_failure_cli_still_writes_diagnostic_report(inputs) -> None:
    """The real content is degraded and returned to a non-zero status, but the report is uploaded when CI fails."""
    path, reference, candidate = inputs
    candidate[0]["pages"][0]["fingerprint"] = "changed-content"
    for name, capture in (("reference", reference), ("candidate", candidate)):
        folder = path.parent / name
        folder.mkdir()
        (folder / "summary.json").write_text(json.dumps(capture), encoding="utf-8")
    output = path.parent / "diff.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/diff_history_geometry.py"),
            str(path.parent / "reference"),
            str(path.parent / "candidate"),
            "--output",
            str(output),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["documents"][0]["content_changed_pages"] == 1


def test_page_size_changes_are_not_font_tolerance(inputs) -> None:
    """Page size changes cannot be considered equivalent font geometries simply by the same normalized coordinates."""
    _, reference, candidate = inputs
    candidate[0]["pages"][0]["page_size"][0] = 700
    with pytest.raises(AssertionError, match="page size differs"):
        build_report(index_summary(reference), index_summary(candidate))
