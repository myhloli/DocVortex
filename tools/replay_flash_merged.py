"""通过公共 Flash API 重放视觉复核原件，并保存全部页及变化页证据；不读取GT。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import platform
import sys
import time

from bs4 import BeautifulSoup
from loguru import logger
from PIL import Image

from docvortex import parse, load_bundle
from docvortex.document.pdf import PDFDocument
from docvortex._compute_backend import backend_info

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    """忽略资产保存路径，保留所有内容、成员、顺序、样式和几何参与比较。"""
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items() if key not in {"image_path", "image_url", "img_path"}}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def validate_public_artifacts(middle, folder):
    """检查公开叶子成员唯一性、图片解码、公式裁图非空及包校验，不以结果生成视觉期望。"""
    errors = []
    images, equations = 0, 0
    for page_number, page in enumerate(middle["pages"], 1):
        pending = list(page["blocks"])
        leaves = []
        while pending:
            block = pending.pop()
            children = (
                [item for item in block.get("content", []) if isinstance(item, dict) and "bbox" in item]
                if isinstance(block.get("content"), list)
                else []
            )
            if children:
                pending.extend(children)
            else:
                leaves.append(block)
        identities = [block["index"] for block in leaves]
        if len(identities) != len(set(identities)):
            errors.append(f"页{page_number}:公开叶子块编号重复")
        for block in leaves:
            path = block.get("image_path")
            if not path:
                continue
            asset = folder / "bundle" / path
            if not asset.is_file():
                errors.append(f"页{page_number}:缺少资产{path}")
                continue
            try:
                with Image.open(asset) as original:
                    original.load()
                    images += 1
                    if min(original.size) <= 0:
                        errors.append(f"页{page_number}:空图片{path}")
                    if block["type"] == "equation":
                        equations += 1
                        with original.convert("L") as gray:
                            if gray.getextrema()[0] >= 250:
                                errors.append(f"页{page_number}:公式裁图无墨迹{path}")
            except Exception as error:
                errors.append(f"页{page_number}:资产无法解码{path}:{error}")
    restored = load_bundle(folder / "bundle")
    if canonical(restored.to_dict()) != canonical(middle):
        errors.append("导出包回读内容与原 MiddleJson 不一致")
    return {
        "errors": errors,
        "decoded_images": images,
        "formula_crops": equations,
        "leaf_indices_unique": not any("编号重复" in error for error in errors),
    }


def replay(manifest, output, selected=None, compare_to=None):
    """校验源指纹后解析完整文档，变化仅进入待视觉裁决队列。"""
    logger.remove()
    data = json.loads(manifest.read_text())
    code = hashlib.sha256()
    inputs = [*(ROOT / "src").rglob("*.py"), *(ROOT / "rust").rglob("*.rs"), ROOT / "Cargo.toml", ROOT / "Cargo.lock"]
    for path in sorted(inputs):
        code.update(str(path.relative_to(ROOT)).encode())
        code.update(path.read_bytes())
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "code_sha256": code.hexdigest(),
        "code_fingerprint_scope": "Python源码、Rust源码及Cargo清单与锁文件",
        "compute_backend": backend_info(),
        "python": sys.executable,
        "platform": platform.platform(),
        "compare_to": str(compare_to or manifest.parent / "baseline"),
        "documents": [],
    }
    sections = []
    for document in data["documents"]:
        name = document["name"]
        if selected and name not in selected:
            continue
        source = Path(document["path"])
        assert hashlib.sha256(source.read_bytes()).hexdigest() == document["sha256"]
        folder = output / name
        folder.mkdir()
        started = time.monotonic()
        result = parse(source, keep_model_json=True)
        assert len(result.middle_json.pages) == document["page_count"]
        result.export(folder / "document.html", output_format="html", overwrite=False)
        result.save_bundle(folder / "bundle", overwrite=False)
        middle = result.to_dict()
        model = result.model_json.model_dump(mode="json")
        (folder / "middle.json").write_text(json.dumps(middle, ensure_ascii=False))
        (folder / "model.json").write_text(json.dumps(model, ensure_ascii=False))
        rendered = BeautifulSoup((folder / "document.html").read_text(), "html.parser")
        missing = [
            image["src"]
            for image in rendered.find_all("img", src=True)
            if not image["src"].startswith(("data:", "http:", "https:")) and not (folder / image["src"]).is_file()
        ]
        assert not missing, (name, missing)
        checks = validate_public_artifacts(middle, folder)
        old_folder = (compare_to or manifest.parent / "baseline") / name
        before = json.loads((old_folder / "middle.json").read_text())
        changed = [
            i + 1
            for i, (old, new) in enumerate(zip(before["pages"], middle["pages"], strict=True))
            if canonical(old) != canonical(new)
        ]
        with PDFDocument(str(source)) as original:
            original.draw_layout_bbox(result.middle_json.pages, str(folder / "layout.pdf"))
            with PDFDocument(str(folder / "layout.pdf")) as after, PDFDocument(str(old_folder / "layout.pdf")) as previous:
                for i in range(original.page_count):
                    after.render_page(i, scale=1.6).pil_image.save(folder / f"layout-{i + 1:03d}.png")
                    if i + 1 in changed:
                        original.render_page(i, scale=1.6).pil_image.save(folder / f"source-{i + 1:03d}.png")
                        previous.render_page(i, scale=1.6).pil_image.save(folder / f"before-{i + 1:03d}.png")
                        sections.append(
                            f"<h2>{html.escape(name)} · 页{i + 1}</h2><section>"
                            + "".join(
                                f'<figure><figcaption>{label}</figcaption><img src="{html.escape(name)}/{kind}-{i + 1:03d}.png"></figure>'
                                for label, kind in [("原页", "source"), ("旧布局", "before"), ("新布局", "layout")]
                            )
                            + "</section>"
                        )
        item = {
            "name": name,
            "sha256": document["sha256"],
            "pages": document["page_count"],
            "changed_pages": changed,
            "visual_review": "pending",
            "seconds": round(time.monotonic() - started, 2),
            "assets": len(result.assets),
            "missing_images": missing,
            "public_checks": checks,
        }
        report["documents"].append(item)
        (output / "run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(item, ensure_ascii=False), flush=True)
    (output / "comparison.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Flash逐页变化待审</title><style>body{font:16px system-ui;margin:20px;background:#eee}section{display:flex;gap:8px}figure{width:33%;margin:0}img{width:100%}</style><h1>原页／基线／候选布局</h1><p>此队列不代表视觉验收通过。</p>'
        + "".join(sections)
    )


def main():
    """接受显式合并清单和全新目录，避免覆盖已冻结证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--documents", nargs="*")
    parser.add_argument("--compare-to", type=Path, help="显式指定已冻结的比较基线目录")
    args = parser.parse_args()
    replay(args.manifest, args.output, args.documents, args.compare_to)


if __name__ == "__main__":
    main()
