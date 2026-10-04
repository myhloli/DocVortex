"""Generate structural differences and original page comparisons for the frozen baseline page by page, without modifying the test gold standard or automatically approving changes."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import unicodedata

from docvortex.document.pdf import PDFDocument
from review_flash_annotations import visible

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprint() -> str:
    """Record the current production source code to ensure that the replayed product is consistent with the actual delivered code."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def text_inventory(blocks: list[dict]) -> Counter:
    """Alphanumeric statistics are visible, excluding differences in line breaks, word breaks, and spaces; the original content is still saved separately for verbatim review."""
    return Counter(c for block in blocks for c in unicodedata.normalize("NFKC", visible(block["content"])) if c.isalnum())


def apply_review_decisions(changes: list[dict], path: Path | None) -> None:
    """Only visual rulings that are explicitly provided and for which the front and rear difference fingerprints exactly match are reused, and new differences remain pending."""
    if path is None:
        return
    decisions = json.loads(path.read_text(encoding="utf-8"))
    for change in changes:
        for decision in decisions:
            if (change["document"], change["page"]) != (decision["document"], decision["page"]):
                continue
            matches = all(
                hashlib.sha256(json.dumps(change[key], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                == decision[f"{key}_sha256"]
                for key in ("before", "after")
            )
            if matches:
                change["decision"] = decision["decision"]
                change["reason"] = decision["reason"]


def compare_manual(root: Path, decisions_path: Path | None = None) -> list[dict]:
    """Compare the original model of artificial samples page by page, and save the original page and front and back frames for all changed pages."""
    manifest = json.loads((ROOT / "tests/fixtures/flash_manual_annotations.json").read_text(encoding="utf-8"))
    review = root / "visual-review"
    review.mkdir(parents=True, exist_ok=True)
    changes = []
    sections = []
    for document in manifest["documents"]:
        name = document["name"]
        before = root / "baseline-manual" / name
        after = root / "current-manual" / name
        original = json.loads((before / "model.json").read_text(encoding="utf-8"))
        current = json.loads((after / "model.json").read_text(encoding="utf-8"))
        assert len(original) == len(current) == document["page_count"]
        with (
            PDFDocument(str(ROOT / document["path"])) as source,
            PDFDocument(str(before / "layout.pdf")) as old,
            PDFDocument(str(after / "layout.pdf")) as new,
        ):
            for index, (first, second) in enumerate(zip(original, current, strict=True)):
                if first == second:
                    continue
                entry = {
                    "document": name,
                    "page": index + 1,
                    "before": [block for block in first if block not in second],
                    "after": [block for block in second if block not in first],
                    "alphanumeric_inventory_equal": text_inventory(first) == text_inventory(second),
                    "decision": "pending_visual_review",
                }
                changes.append(entry)
                apply_review_decisions([entry], decisions_path)
                images = []
                for label, pdf in [("original", source), ("before", old), ("after", new)]:
                    filename = f"{name}-{index + 1:02d}-{label}.png"
                    pdf.render_page(index, scale=1.5).pil_image.save(review / filename)
                    images.append(f'<figure><figcaption>{label}</figcaption><img src="{filename}"></figure>')
                sections.append(
                    f'<h2>{html.escape(name)} / {index + 1}</h2><div class="pages">'
                    + "".join(images)
                    + "</div>"
                    + "<pre>"
                    + html.escape(json.dumps(entry, ensure_ascii=False, indent=2))
                    + "</pre>"
                )
    (review / "changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
    (review / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Flash rule generalization review</title>'
        "<style>body{font:14px system-ui;margin:20px}.pages{display:flex}figure{margin:5px;width:33%}img{width:100%}pre{white-space:pre-wrap}</style>"
        "<h1>Flash 规则抽象：全部变化页</h1>" + "".join(sections),
        encoding="utf-8",
    )
    return changes


def main() -> None:
    """Summarize baselines, changes and source code fingerprints; visual adjudication is a separate report and cannot be passed by default by the script."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/pdf/flash-rule-generalization")
    parser.add_argument("--decisions", type=Path)
    args = parser.parse_args()
    changes = compare_manual(args.output, args.decisions)
    histories = json.loads((args.output / "current-history/summary.json").read_text(encoding="utf-8"))
    result = {
        "baseline_commit": "3acfed223b548c5b6476917d65ce1dd728b0c269",
        "source_sha256": source_fingerprint(),
        "manual_changed_pages": [{k: v for k, v in entry.items() if k not in {"before", "after"}} for entry in changes],
        "history_changed_pages": {item["name"]: item["changed_pages"] for item in histories if item["changed_pages"]},
    }
    (args.output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
