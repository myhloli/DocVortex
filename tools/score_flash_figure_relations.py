"""在固定源页真值上分别计量图体、图题父关系、可复制锚点和跨栏顺序。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

if __package__:
    from .score_flash_layout_gt import flat_blocks, geom, inner_text, norm
else:
    from score_flash_layout_gt import flat_blocks, geom, inner_text, norm


DEFAULT_GT = Path(__file__).resolve().parents[1] / "tests/fixtures/flash_layout_gt_r11_r13_v1.json.gz"


def load_page(roots: list[Path], sha: str, physical: int) -> dict:
    """按原件哈希和物理页定位完整公开输出，并核对状态及页身份。"""
    for root in roots:
        for doc_id in (sha, f"d-{sha[:16]}"):
            folder = root / "documents" / doc_id
            state_path = folder / "status.json"
            if not state_path.is_file():
                continue
            state = json.loads(state_path.read_bytes())
            if state.get("sha256") != sha:
                raise ValueError(f"来源不符 {state_path}")
            if state.get("status") != "complete":
                raise ValueError(f"整本解析未成功 {state_path}")
            with gzip.open(folder / "middle.json.gz", "rt", encoding="utf-8") as handle:
                page = json.load(handle)["pages"][physical - 1]
            if page["page_idx"] != physical - 1:
                raise ValueError(f"物理页不符 {sha} p{physical}")
            return page
    raise FileNotFoundError(f"找不到原件的公开输出 {sha} p{physical}")


def score(page: dict, truth: dict) -> dict:
    """图题须唯一且归入真正图体；同时独立计图体是否存在和两个文字锚点的先后。"""
    stream = " ".join(block["text"] for block in flat_blocks(page))
    parents = [block for block in page["blocks"] if block["type"] in {"image", "chart"}]
    figures = []
    for label in truth["figures"]:
        phrase = norm(label["caption"])
        unique = stream.count(phrase) == 1
        owners = [
            parent
            for parent in parents
            if any(
                child["type"].endswith("caption") and phrase in norm(inner_text(child.get("content")))
                for child in parent["content"]
            )
        ]
        body = label["body"]
        figures.append(
            {
                "anchor": label["caption"],
                "unique": unique,
                "body": any(geom(parent["bbox"], body) >= 0.5 for parent in parents),
                "parent": unique and len(owners) == 1 and geom(owners[0]["bbox"], body) >= 0.5,
            }
        )
    order = None
    if truth.get("order"):
        first, second = map(norm, truth["order"])
        order = stream.count(first) == stream.count(second) == 1 and stream.index(first) < stream.index(second)
    return {"figures": figures, "order": order}


def summarize(pages: list[dict], side: str) -> dict:
    """分别汇总区域数和顺序边数，两个任务保持各自分母。"""
    figures = [figure for row in pages for figure in row[side]["figures"]]
    order = [row[side]["order"] for row in pages if row[side]["order"] is not None]
    return {
        "figures": len(figures),
        **{field: sum(f[field] for f in figures) for field in ("body", "unique", "parent")},
        "order_correct": sum(order),
        "order_total": len(order),
    }


def main() -> None:
    """按封存子集比较两个公共回放目录，并输出逐页可复查的结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT)
    parser.add_argument("--suite", choices=["doclaynet", "tagged"], required=True)
    parser.add_argument("--baseline-dir", type=Path, action="append", required=True)
    parser.add_argument("--candidate-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = args.gt.read_bytes()
    tables = json.loads(gzip.decompress(raw))
    pages = []
    for truth in tables["challenges"][args.suite]:
        sha, physical = truth["sha256"], truth["physical_page"]
        pages.append(
            {
                "sha256": sha,
                "physical_page": physical,
                "A": score(load_page(args.baseline_dir, sha, physical), truth),
                "D": score(load_page(args.candidate_dir, sha, physical), truth),
            }
        )
    result = {
        "gt_sha256": hashlib.sha256(raw).hexdigest(),
        "suite": args.suite,
        "A": summarize(pages, "A"),
        "D": summarize(pages, "D"),
        "pages": pages,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
