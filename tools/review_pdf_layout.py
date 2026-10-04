"""Generate original pages, block-level restorations, overlays and diagnostics of real PDF for page-by-page visual acceptance."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import html
import json
from pathlib import Path
import re
import shutil

from loguru import logger
from PIL import Image
from pypdf import PdfReader

from docvortex import load_bundle, parse, render_artifact
from docvortex.document.pdf import PDFDocument
from docvortex.render import PdfLayout, PdfRenderOptions
from docvortex.render._internal.pdf.font_plan import collect_font_plans
from docvortex.document.pdf.layout import read_layout_geometry
from docvortex.schema import CodeInlineSpan, HyperlinkSpan, InlineContentBlock, TextSpan


def review_pdf(source: Path, output: Path) -> dict:
    """Completely parse real documents and generate page-by-page acceptance materials that can be viewed and replayed independently."""
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output / "source.pdf")
    result = parse(source, file_suffix="pdf")
    result.save_bundle(output / "bundle", overwrite=True)
    restored = load_bundle(output / "bundle")
    with collect_font_plans() as font_plans:
        artifact = render_artifact(
            restored.middle_json,
            "pdf",
            assets=restored.assets,
            options=PdfRenderOptions(layout=PdfLayout.ORIGINAL),
        )
    artifact.write(output / "original.pdf", overwrite=True)
    render_artifact(
        restored.middle_json,
        "pdf",
        assets=restored.assets,
        options=PdfRenderOptions(layout=PdfLayout.REFLOW),
    ).write(output / "reflow.pdf", overwrite=True)
    sections = []
    contact_rows = []
    with PDFDocument(str(source)) as original, PDFDocument(artifact.content) as rebuilt:
        assert original.page_count == rebuilt.page_count
        for page_idx in range(original.page_count):
            source_size, result_size = original.page_size(page_idx), rebuilt.page_size(page_idx)
            assert all(abs(a - b) <= 0.5 for a, b in zip(source_size, result_size, strict=True))
            left = original.render_page(page_idx, scale=1.5).pil_image.convert("RGB")
            right = rebuilt.render_page(page_idx, scale=1.5).pil_image.convert("RGB")
            assert left.size == right.size
            stem = f"page-{page_idx + 1:03d}"
            left.save(output / f"{stem}-source.png")
            right.save(output / f"{stem}-original.png")
            Image.blend(left, right, 0.5).save(output / f"{stem}-overlay.png")
            pair = Image.new("RGB", (left.width * 2, left.height), "white")
            pair.paste(left, (0, 0))
            pair.paste(right, (left.width, 0))
            pair.thumbnail((900, 650))
            contact_rows.append(pair)
            sections.append(
                f'<section><h2>Page {page_idx + 1}</h2><div class="pair">'
                f'<img src="{stem}-source.png"><img src="{stem}-original.png"></div>'
                f'<details><summary>Overlay</summary><img src="{stem}-overlay.png"></details></section>'
            )
        for start in range(0, len(contact_rows), 5):
            rows = contact_rows[start : start + 5]
            sheet = Image.new("RGB", (max(row.width for row in rows), sum(row.height for row in rows)), "#ddd")
            top = 0
            for row in rows:
                sheet.paste(row, (0, top))
                top += row.height
            sheet.save(output / f"contact-{start + 1:03d}.jpg")
    title = html.escape(source.name)
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>' + title + "</title>"
        "<style>body{font-family:sans-serif;background:#eee;margin:24px}section{margin-bottom:32px}"
        ".pair{display:flex;gap:8px}.pair img{width:49%;object-fit:contain;align-self:start}"
        "details img{max-width:95%}</style><h1>" + title + "</h1><p>Source / original block layout</p>" + "".join(sections),
        encoding="utf-8",
    )
    summary = {
        "source": str(source),
        "pages": len(PdfReader(output / "original.pdf").pages),
        "page_geometry_checked": True,
        "bundle_replay_checked": True,
        "title_font_groups": [
            {**asdict(plan), "target_coverage": plan.fitting_count / plan.block_count} for plan in font_plans
        ],
        "title_layout_violations": _check_title_geometry(restored.middle_json, font_plans),
        "diagnostic_counts": dict(Counter(item.code for item in artifact.diagnostics)),
        "diagnostics": [asdict(item) for item in artifact.diagnostics],
    }
    checked = 0
    missing = []
    pdf = PdfReader(output / "original.pdf")
    for page, rendered in zip(restored.middle_json.pages, pdf.pages, strict=True):
        extracted = re.sub(r"\s+", "", rendered.extract_text())
        pending = list(page.blocks)
        while pending:
            block = pending.pop()
            if isinstance(block, InlineContentBlock):
                for text in _text_spans(block.content):
                    expected = re.sub(r"\s+", "", text)
                    if len(expected) >= 8:
                        checked += 1
                        if expected not in extracted:
                            missing.append({"page_idx": page.page_idx, "block_index": block.index, "text": expected[:80]})
            elif isinstance(getattr(block, "content", None), list):
                pending.extend(block.content)
    summary["text_spans_checked"] = checked
    summary["text_spans_missing"] = missing
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    compact = {key: value for key, value in summary.items() if key != "diagnostics"}
    compact["title_font_groups"] = [
        {key: value for key, value in asdict(plan).items() if key != "titles"} for plan in font_plans
    ]
    return compact


def _layout_leaves(blocks):
    """Independently traverse the leaves with coordinates, and the combination of missing child coordinates uses the entire parent box as the occupied area."""
    for block in blocks:
        if block.type in {"image", "table", "chart", "code", "list", "index"} and all(
            child.bbox is not None for child in block.content
        ):
            yield from _layout_leaves(block.content)
        else:
            yield block


def _check_title_geometry(middle, plans):
    """Independently check the spacing between the final title box and other original boxes or the final title box, without reusing the layout algorithm to determine success."""
    sizes = read_layout_geometry(middle)
    records = [title for plan in plans for title in plan.titles]
    violations = []
    for page in middle.pages:
        width, height = sizes[page.page_idx]
        titles = [title for title in records if title["page_idx"] == page.page_idx]
        occupied = []
        for block in _layout_leaves(page.blocks):
            if block.bbox is None:
                continue
            x0, y0, x1, y1 = block.bbox
            rect = (x0 * width, y0 * height, x1 * width, y1 * height)
            title = next(
                (
                    item
                    for item in titles
                    if item["block_index"] == block.index
                    and all(abs(a - b) < 0.001 for a, b in zip(item["original_bbox_pt"], rect, strict=True))
                ),
                None,
            )
            occupied.append((block, title["draw_bbox_pt"] if title is not None else rect, title))
        for title in titles:
            rect = title["draw_bbox_pt"]
            if rect[0] < -0.001 or rect[1] < -0.001 or rect[2] > width + 0.001 or rect[3] > height + 0.001:
                violations.append({"page_idx": page.page_idx, "block_index": title["block_index"], "reason": "outside_page"})
            if title["geometry_conflict"] or title["clearance_unavailable"]:
                original = title["original_bbox_pt"]
                if (
                    rect[0] < original[0] - 0.001
                    or rect[1] < original[1] - 0.001
                    or rect[2] > original[2] + 0.001
                    or rect[3] > original[3] + 0.001
                ):
                    violations.append(
                        {"page_idx": page.page_idx, "block_index": title["block_index"], "reason": "expanded_conflicting_input"}
                    )
                continue
            for block, other, reference in occupied:
                if reference is title:
                    continue
                if not (
                    rect[2] + 2 <= other[0] + 0.001
                    or other[2] + 2 <= rect[0] + 0.001
                    or rect[3] + 2 <= other[1] + 0.001
                    or other[3] + 2 <= rect[1] + 0.001
                ):
                    violations.append(
                        {
                            "page_idx": page.page_idx,
                            "block_index": title["block_index"],
                            "other_block_index": block.index,
                            "reason": "insufficient_clearance",
                        }
                    )
            if "insufficient_space" not in title["reasons"] and title["final_font_size"] + 0.01 < title["body_font_size"] + 2:
                violations.append(
                    {"page_idx": page.page_idx, "block_index": title["block_index"], "reason": "title_not_larger_than_body"}
                )
    return violations


def _text_spans(spans):
    """Enumerations should remain copyable plain text, excluding vector formulas and area plot text."""
    for span in spans:
        if isinstance(span, HyperlinkSpan):
            yield from _text_spans(span.content)
        elif isinstance(span, (TextSpan, CodeInlineSpan)):
            yield span.content


def main() -> None:
    """Runs from an explicit file entry for compatibility with the macOS PDF rendered spawn worker process."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(args.output / "review.log", level="WARNING")
    summaries = []
    for source in args.sources:
        summary = review_pdf(source, args.output / source.stem)
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    (args.output / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
