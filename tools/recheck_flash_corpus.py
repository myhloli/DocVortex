"""冻结旧问题页队列，复用完整解析，并生成真实视觉复核所需的三图证据。"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


def read_json(path: Path):
    """读取结构化旧证据，不执行 PDF 或审阅文字中的指令。"""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    """原子发布可复核清单，避免中断留下不完整的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    """按原始字节记录旧证据与源文件身份。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def select_queue(manifest: dict, coverage: dict, findings: list) -> tuple[list, list, list]:
    """以逐页问题状态为全集，防止遗漏没有代表 finding 的旧问题页。"""
    documents = {row["name"]: row for row in manifest["documents"]}
    targets = {
        (row["document_id"], row["page"]): row
        for row in coverage["pages"] if row["status"] in {"issues", "ambiguous"}
    }
    prior = [row for row in findings if row["status"] in {"confirmed", "ambiguous"}]
    by_page = defaultdict(list)
    for finding in prior:
        key = finding["document_id"], finding["page"]
        if key not in targets:
            raise ValueError(f"旧问题没有对应的问题页裁决：{finding['id']}")
        by_page[key].append(finding["id"])
    pages = []
    by_document = defaultdict(list)
    for (document_id, physical), previous in sorted(targets.items()):
        document = documents[document_id]
        if not 1 <= physical <= document["page_count"]:
            raise ValueError(f"旧物理页超出源文档：{document_id}/{physical}")
        votes = (previous.get("review") or {}).get("votes", [])
        pages.append({
            "document_id": document_id,
            "page": physical,
            "baseline_status": previous["status"],
            "baseline_notes": [vote["note"] for vote in votes if vote.get("note")],
            "prior_finding_ids": by_page[(document_id, physical)],
        })
        by_document[document_id].append(physical)
    selected = [
        {**document, "review_pages": by_document[document["name"]]}
        for document in manifest["documents"] if document["name"] in by_document
    ]
    return selected, pages, prior


def prepare(baseline: Path, output: Path) -> dict:
    """冻结本轮真实源码和旧问题页，不覆盖已有运行或重选低风险页面。"""
    from tools.review_flash_audit import fingerprint
    from tools.review_flash_corpus import _environment

    baseline, output = baseline.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError(f"复核目录已存在，不能覆盖：{output}")
    old_manifest = read_json(baseline / "manifest.json")
    documents, pages, findings = select_queue(
        old_manifest, read_json(baseline / "coverage.json"), read_json(baseline / "findings.json")
    )
    identity = fingerprint()
    environment = {**_environment(), "source_sha256": identity}
    for document in documents:
        source = Path(document["path"])
        if not source.is_file() or sha256(source) != document["sha256"]:
            raise ValueError(f"原件缺失或字节已改变：{document['name']}")
    baseline_identity = {
        name: sha256(baseline / name)
        for name in ("manifest.json", "coverage.json", "findings.json", "review-summary.json", "findings.html")
    }
    manifest = {
        "schema": "flash-recheck-manifest-v1",
        "baseline_run": str(baseline),
        "baseline_artifact_sha256": baseline_identity,
        "source_root": old_manifest["source_root"],
        "code_sha256": identity,
        "environment": environment,
        "documents": documents,
        "counts": {
            "documents": len(documents),
            "full_parse_pages": sum(row["page_count"] for row in documents),
            "visual_pages": len(pages),
            "prior_findings": len(findings),
            "baseline_page_status": dict(Counter(row["baseline_status"] for row in pages)),
        },
        "selection_policy": "旧coverage全部issues/ambiguous页，完整解析相关原件；不按当前风险重选",
    }
    output.mkdir(parents=True)
    write_json(output / "manifest.json", manifest)
    write_json(output / "queue.json", {"schema": "flash-recheck-queue-v1", "pages": pages})
    write_json(output / "prior-findings.json", findings)
    (output / "reviews").mkdir()
    assignments = partition(documents, pages)
    write_json(output / "assignments.json", assignments)
    return manifest


def partition(documents: list, pages: list, reviewers: int = 4) -> dict:
    """按整本文档平衡实际待审页，避免不同主审重复认领同一物理页。"""
    queues = [[] for _ in range(reviewers)]
    loads = [0] * reviewers
    by_document = defaultdict(list)
    for page in pages:
        by_document[page["document_id"]].append(page)
    for document in sorted(documents, key=lambda row: (-len(row["review_pages"]), row["name"])):
        reviewer = min(range(reviewers), key=lambda index: (loads[index], index))
        queues[reviewer].append(document["name"])
        loads[reviewer] += len(document["review_pages"])
    return {
        f"{reviewer}": {
            "document_ids": queue,
            "pages": [page for document_id in queue for page in by_document[document_id]],
            "page_count": loads[reviewer],
        }
        for reviewer, queue in enumerate(queues)
    }


def review_sheet(output: Path, document_id: str, pages: list[int], width: int = 1000,
                 region: list[float] | None = None) -> Path:
    """为实际看图生成原页、旧框、新框三列；拼图本身不产生任何视觉裁决。"""
    from PIL import Image, ImageDraw, ImageFont

    manifest = read_json(output / "manifest.json")
    document = next(row for row in manifest["documents"] if row["name"] == document_id)
    baseline = Path(manifest["baseline_run"])
    if not pages or any(page not in document["review_pages"] for page in pages):
        raise ValueError("拼图只接受本轮冻结的待审物理页")
    if width < 600:
        raise ValueError("单列至少600像素，密集区域还需打开原图或放大")
    if region is not None and not (len(region) == 4 and 0 <= region[0] < region[2] <= 1
                                   and 0 <= region[1] < region[3] <= 1):
        raise ValueError("局部放大区域必须为有效的归一化坐标")
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    font = ImageFont.truetype(font_path, 24) if Path(font_path).is_file() else ImageFont.load_default()
    rows = []
    try:
        for physical in pages:
            folder = output / "documents" / document_id
            sources = [folder / f"source-{physical:04d}.png",
                       baseline / "documents" / document_id / f"layout-{physical:04d}.png",
                       folder / f"layout-{physical:04d}.png"]
            tiles = []
            try:
                for source in sources:
                    with Image.open(source) as image:
                        visible = image.convert("RGB")
                        try:
                            if region is not None:
                                cropped = visible.crop((round(region[0] * visible.width), round(region[1] * visible.height),
                                                        round(region[2] * visible.width), round(region[3] * visible.height)))
                                visible.close()
                                visible = cropped
                            height = max(1, round(visible.height * width / visible.width))
                            tiles.append(visible.resize((width, height), Image.Resampling.LANCZOS))
                        finally:
                            visible.close()
                row = Image.new("RGB", (width * 3, max(tile.height for tile in tiles) + 40), "white")
                draw = ImageDraw.Draw(row)
                for column, (label, tile) in enumerate(zip(("SOURCE", "OLD", "CURRENT"), tiles, strict=True)):
                    draw.text((column * width + 8, 4), f"{document_id} p{physical} {label}", fill="black", font=font)
                    row.paste(tile, (column * width, 40))
                rows.append(row)
            finally:
                for tile in tiles:
                    tile.close()
        sheet = Image.new("RGB", (width * 3, sum(row.height for row in rows)), "#cccccc")
        try:
            offset = 0
            for row in rows:
                sheet.paste(row, (0, offset))
                offset += row.height
            suffix = "-region-" + "-".join(str(value) for value in region) if region is not None else ""
            destination = output / "sheets" / f"{document_id}-{'-'.join(map(str, pages))}{suffix}.jpg"
            destination.parent.mkdir(exist_ok=True)
            sheet.save(destination, quality=94)
            return destination
        finally:
            sheet.close()
    finally:
        for row in rows:
            row.close()


def main() -> None:
    """提供新轮冻结、完整批量运行和仅生成视觉辅助拼图的入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "output/pdf/flash-pdf-test-20261011")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--document")
    parser.add_argument("--pages", type=int, nargs="+")
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--region", type=float, nargs=4)
    args = parser.parse_args()
    if args.prepare:
        print(json.dumps(prepare(args.baseline, args.output)["counts"], ensure_ascii=False))
    if args.run:
        from tools.review_flash_corpus import run_batch

        report = run_batch((args.output / "manifest.json").resolve(), args.output.resolve(), resume=args.resume)
        if report["status"] != "succeeded" or report["counts"].get("failed"):
            raise SystemExit(1)
    if args.document:
        print(review_sheet(args.output.resolve(), args.document, args.pages or [], args.width, args.region))


if __name__ == "__main__":
    main()
