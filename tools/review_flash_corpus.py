"""隔离重放完整 Flash 文档，并生成有明确待审覆盖记录的原页与布局证据。"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    # 仅直接运行源码工具时选择 checkout；导入不能改变 spawn 子进程使用的已安装 wheel。
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from tools.review_flash_audit import fingerprint  # noqa: E402

STAGES = ("parse", "export", "overlay", "render")
RENDER_SCALE = 160 / 72


def _now() -> str:
    """返回带时区的真实运行时间，供阶段和失败记录定位。"""
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    """先写同目录临时文件再替换，避免中断留下半份状态或汇总。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    """分块核对原件字节身份，不额外长期持有整份大 PDF。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _environment() -> dict:
    """记录实际源码导入、PDFium、字体及计算扩展身份。"""
    import docvortex
    from docvortex._compute_backend import backend_info
    from tools.review_flash_regression import _capture_environment

    return {
        **_capture_environment(ROOT),
        "executable": sys.executable,
        "docvortex_source": str(Path(docvortex.__file__).resolve()),
        "runner_sha256": _sha256(Path(__file__).resolve()),
        "backend": backend_info(),
        "runtime_overrides": {key: os.environ[key] for key in ("DOCVORTEX_PDF_RENDER_THREADS",) if key in os.environ},
    }


def _visible(value: Any) -> str:
    """递归提取文字锚点，表格 HTML 只保留可见文字。"""
    if isinstance(value, str):
        if "<table" in value or "<td" in value:
            from bs4 import BeautifulSoup

            return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
        return value
    if isinstance(value, list):
        return "".join(_visible(item) for item in value)
    if isinstance(value, dict):
        return _visible(value.get("content", ""))
    return ""


def flatten_blocks(blocks: list[dict], parent: dict | None = None, prefix: str = "blocks", offset_base: int = 0) -> list[dict]:
    """保留父子路径、真实编号和文字首尾，不能把编号当作数组下标。"""
    result = []
    for offset, block in enumerate(blocks):
        path = f"{prefix}.{offset + offset_base}"
        text = " ".join(_visible(block.get("content", "")).split())
        item = {
            "tree_path": path,
            "type": block.get("type"),
            "index": block.get("index"),
            "bbox": block.get("bbox"),
            "text_start": text[:120],
            "text_end": text[-120:],
            "parent": parent,
        }
        result.append(item)
        content = block.get("content")
        if isinstance(content, list):
            identity = {key: item[key] for key in ("tree_path", "type", "index")}
            for child_offset, child in enumerate(content):
                if isinstance(child, dict) and "bbox" in child and "type" in child:
                    result.extend(flatten_blocks([child], identity, f"{path}.content", child_offset))
    return result


def _page_risk(page: dict, mixed_native: bool) -> tuple[float, dict]:
    """仅用类型和几何复杂度排序审阅优先级，不自动裁决布局是否错误。"""
    counts = Counter(block["type"] for block in page["blocks"])
    positioned = []
    outside = invalid = overlaps = 0
    for block in page["blocks"]:
        box = block["bbox"]
        if box is None:
            continue
        if len(box) != 4 or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in box):
            invalid += 1
            continue
        if box[0] >= box[2] or box[1] >= box[3]:
            invalid += 1
            continue
        outside += int(any(v < 0 or v > 1 for v in box))
        positioned.append(block)
    for offset, first in enumerate(positioned):
        for second in positioned[offset + 1 :]:
            paths = first["tree_path"], second["tree_path"]
            if paths[0].startswith(paths[1] + ".") or paths[1].startswith(paths[0] + "."):
                continue
            a, b = first["bbox"], second["bbox"]
            intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
            overlaps += int(intersection / smaller > 0.35)
    zero_native = mixed_native and page["native_char_count"] == 0 and bool(page["blocks"])
    score = (
        min(len(page["blocks"]), 200) * 0.05
        + min(counts["equation"], 30) * 2
        + min(counts["table_body"], 20) * 4
        + min(counts["image_body"] + counts["chart_body"], 20)
        + min(overlaps, 50) * 3
        + (outside + invalid) * 10
        + bool(page["rotation"]) * 5
        + zero_native * 10
    )
    return round(score, 3), {
        "type_counts": dict(counts),
        "overlap_pairs": overlaps,
        "outside_boxes": outside,
        "invalid_boxes": invalid,
        "mixed_zero_native_chars": bool(zero_native),
    }


def select_review_pages(page_count: int, pages: list[dict]) -> list[int]:
    """短文档审阅全部页；长文档合并首尾、均匀与风险页后确定性补齐一百页。"""
    if page_count <= 100:
        return list(range(1, page_count + 1))
    selected = set(range(1, 6)) | set(range(page_count - 4, page_count + 1))
    selected.update(6 + round(offset * (page_count - 11) / 69) for offset in range(70))
    risks = {}
    for page in pages:
        physical = page["page"]
        if isinstance(physical, int) and 1 <= physical <= page_count:
            risks[physical] = max(risks.get(physical, 0), float(page.get("risk", 0)))
    selected.update(sorted(risks, key=lambda physical: (-risks[physical], physical))[:20])
    while len(selected) < 100:
        ordered = sorted(selected)
        distances = []
        for physical in range(1, page_count + 1):
            if physical in selected:
                continue
            offset = bisect_left(ordered, physical)
            distance = min(physical - ordered[offset - 1], ordered[offset] - physical)
            distances.append((distance, -physical))
        selected.add(-max(distances)[1])
    return sorted(selected)


def build_page_index(document: dict, middle: dict, pdf: Any, code_sha256: str) -> dict:
    """按真实物理页建立索引，乱序 PageInfo 不改变原件页号映射。"""
    count = pdf.page_count
    by_index = {page["page_idx"]: page for page in middle["pages"]}
    if len(by_index) != count or set(by_index) != set(range(count)):
        raise ValueError("完整解析输出的 page_idx 必须唯一且覆盖原件全部物理页")
    pages = [
        {
            "page": index + 1,
            "page_idx": index,
            "native_char_count": pdf.page_char_count(index),
            "rotation": pdf.page_rotation(index),
            "blocks": flatten_blocks(by_index[index].get("blocks", [])),
        }
        for index in range(count)
    ]
    mixed_native = any(page["native_char_count"] > 0 for page in pages)
    for page in pages:
        page["risk"], page["risk_evidence"] = _page_risk(page, mixed_native)
    selected = document.get("review_pages")
    if selected is None:
        selected = select_review_pages(count, pages)
    elif (
        not isinstance(selected, list)
        or not selected
        or any(type(physical) is not int or not 1 <= physical <= count for physical in selected)
        or selected != sorted(set(selected))
    ):
        raise ValueError("固定审阅页必须是升序、唯一且有效的物理页列表")
    for page in pages:
        page["selected"] = page["page"] in selected
    return {
        "document_id": document["name"],
        "source_sha256": document["sha256"],
        "code_sha256": code_sha256,
        "page_count": count,
        "review_pages": selected,
        "selection": "frozen" if "review_pages" in document else "risk_sample",
        "pages": pages,
    }


def _new_status(document: dict, code: str) -> dict:
    """初始化执行状态，视觉覆盖保持待审，不能由执行成功自动接受。"""
    return {
        "document_id": document["name"],
        "source_sha256": document["sha256"],
        "code_sha256": code,
        "source": document["path"],
        "aliases": document.get("aliases", []),
        "page_count": document["page_count"],
        "status": "pending",
        "stages": dict.fromkeys(STAGES, "pending"),
        "errors": [],
        "review_pages": [],
        "started_at": _now(),
    }


def _save_status(folder: Path, state: dict) -> None:
    """原子更新每文档状态，并保持兼容明确命名的阶段记录。"""
    state["updated_at"] = _now()
    _atomic_json(folder / "status.json", state)
    _atomic_json(folder / "perdocstatus.json", state)


def _stage(folder: Path, state: dict, stage: str, status: str) -> None:
    """记录阶段转换并立即打印进度，使长文档处理可被外部观察。"""
    state["stages"][stage] = status
    state["status"] = "running"
    _save_status(folder, state)
    print(f"[{state['document_id']}] {stage}: {status}", flush=True)


def _gallery(folder: Path, page_index: dict) -> None:
    """生成无框原页和布局框并排的完整待审页画廊。"""
    sections = []
    for page in page_index["pages"]:
        if not page["selected"]:
            continue
        physical = page["page"]
        rows = "".join(
            f"<tr><td>{html.escape(block['tree_path'])}</td><td>{html.escape(str(block['type']))}</td>"
            f"<td>{block['index']}</td><td>{html.escape(block['text_start'])}</td></tr>"
            for block in page["blocks"]
        )
        sections.append(
            f'<section id="page-{physical}"><h2>物理页 {physical} · 风险 {page["risk"]}</h2><div class="pair">'
            f'<figure><figcaption>原页</figcaption><a href="source-{physical:04d}.png">'
            f'<img loading="lazy" src="source-{physical:04d}.png"></a></figure>'
            f'<figure><figcaption>布局</figcaption><a href="layout-{physical:04d}.png">'
            f'<img loading="lazy" src="layout-{physical:04d}.png"></a></figure></div>'
            f"<details><summary>块结构与原始编号</summary><table>{rows}</table></details></section>"
        )
    title = html.escape(page_index["document_id"])
    (folder / "gallery.html").write_text(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{title}</title><style>body{{font:14px system-ui;margin:20px;background:#eee}}"
        ".pair{display:flex;gap:12px}figure{width:50%;margin:0}img{width:100%;display:block}"
        "section{margin:24px 0}table{border-collapse:collapse}td{border:1px solid #ccc;padding:4px}"
        "@media(max-width:700px){.pair{display:block}figure{width:100%}}</style>"
        f"<h1>{title}</h1><p><a href='document.html'>HTML</a> · <a href='layout.pdf'>layout PDF</a> · "
        f"{len(page_index['review_pages'])} / {page_index['page_count']} 页待视觉审阅</p>" + "".join(sections),
        encoding="utf-8",
    )


def _save_page_png(document: Any, page_idx: int, destination: Path) -> None:
    """完整写出并关闭页面图后原子发布，防止视觉 agent 读取半写 PNG。"""
    temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.png")
    picture = document.render_page(page_idx, scale=RENDER_SCALE).pil_image
    try:
        picture.save(temporary, format="PNG")
        temporary.replace(destination)
    finally:
        picture.close()


def process_document(document: dict, folder: Path, code: str) -> int:
    """完整解析并逐阶段保存证据；异常只使当前文档失败。"""
    from bs4 import BeautifulSoup
    from loguru import logger
    from docvortex import parse
    from docvortex._compute_backend import backend_info
    from docvortex.document.pdf import PDFDocument

    state = _new_status(document, code)
    folder.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(folder / "parse.log", level="INFO")
    started = time.monotonic()
    current = "parse"
    try:
        state["environment"] = _environment()
        if fingerprint() != code:
            raise ValueError("启动 worker 时源码身份与整轮记录不一致")
        source = Path(document["path"])
        if _sha256(source) != document["sha256"]:
            raise ValueError("解析前源文件 SHA256 与清单不一致")
        _stage(folder, state, current, "running")
        result = parse(source, parse_mode="txt", keep_model_json=True)
        if len(result.middle_json.pages) != document["page_count"]:
            raise ValueError("公开解析页数与清单不一致")
        if result.model_json is None:
            raise ValueError("公开解析未保留 ModelJson")
        middle = result.to_dict()
        _atomic_json(folder / "model.json", result.model_json.pages)
        _atomic_json(folder / "middle.json", middle)
        state["diagnostics"] = [asdict(item) for item in result.diagnostics]
        state["backend"] = backend_info()
        _stage(folder, state, current, "succeeded")

        current = "export"
        _stage(folder, state, current, "running")
        try:
            result.export(folder / "document.html", output_format="html")
            result.save_bundle(folder / "bundle")
            soup = BeautifulSoup((folder / "document.html").read_text(encoding="utf-8"), "html.parser")
            missing = [
                item["src"]
                for item in soup.find_all("img", src=True)
                if not item["src"].startswith(("data:", "http:", "https:")) and not (folder / item["src"]).is_file()
            ]
            state["missing_assets"] = missing
            state["assets"] = len(result.assets)
            if missing:
                raise ValueError(f"公共 HTML 缺失资产：{missing}")
            _stage(folder, state, current, "succeeded")
        except Exception as error:
            state["errors"].append({"stage": current, "type": type(error).__name__, "message": str(error), "at": _now()})
            (folder / "export-error.log").write_text(traceback.format_exc(), encoding="utf-8")
            _stage(folder, state, current, "failed")

        current = "overlay"
        _stage(folder, state, current, "running")
        with PDFDocument(str(source)) as pdf:
            if pdf.page_count != document["page_count"]:
                raise ValueError("源文档页数与清单不一致")
            page_index = build_page_index(document, middle, pdf, code)
            _atomic_json(folder / "page-index.json", page_index)
            state["review_pages"] = page_index["review_pages"]
            _atomic_json(folder / "review.json", {"pages": [{"page": p, "status": "pending"} for p in state["review_pages"]]})
            pdf.draw_layout_bbox(result.middle_json.pages, str(folder / "layout.pdf"))
            _stage(folder, state, current, "succeeded")

            current = "render"
            _stage(folder, state, current, "running")
            with PDFDocument(str(folder / "layout.pdf")) as layout:
                if layout.page_count != pdf.page_count:
                    raise ValueError("layout PDF 页数与原件不一致")
                state["rendered_pages"] = []
                for physical in state["review_pages"]:
                    for kind, document_pdf in (("source", pdf), ("layout", layout)):
                        _save_page_png(document_pdf, physical - 1, folder / f"{kind}-{physical:04d}.png")
                    state["rendered_pages"].append(physical)
                    _save_status(folder, state)
                    if len(state["rendered_pages"]) % 10 == 0:
                        print(
                            f"[{document['name']}] rendered {len(state['rendered_pages'])}/{len(state['review_pages'])}",
                            flush=True,
                        )
        _gallery(folder, page_index)
        if _sha256(source) != document["sha256"] or fingerprint() != code:
            raise ValueError("生成证据期间原件或源码改变，产物不能作为当前版本证据")
        _stage(folder, state, current, "succeeded")
        state["status"] = "failed" if state["errors"] else "succeeded"
        state["evidence_ready"] = True
        state["visual_review"] = "pending"
        state["seconds"] = round(time.monotonic() - started, 3)
        state["finished_at"] = _now()
        _save_status(folder, state)
        print(f"[{document['name']}] completed {document['page_count']} pages; review {len(state['review_pages'])}", flush=True)
        return 1 if state["errors"] else 0
    except Exception as error:
        state["stages"][current] = "failed"
        state["status"] = "failed"
        state["errors"].append({"stage": current, "type": type(error).__name__, "message": str(error), "at": _now()})
        state["seconds"] = round(time.monotonic() - started, 3)
        state["finished_at"] = _now()
        _save_status(folder, state)
        (folder / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        print(f"[{document['name']}] failed {current}: {error}", flush=True)
        return 1


def _load_documents(manifest: Path, selected: list[str] | None) -> list[dict]:
    """校验清单身份和安全目录名，选择文档不能隐式扩展为页截取。"""
    documents = json.loads(manifest.read_text(encoding="utf-8"))["documents"]
    names = set()
    for document in documents:
        name = document["name"]
        if not name or Path(name).name != name or name in {".", ".."} or name in names:
            raise ValueError(f"重复或不安全的 document name：{name}")
        names.add(name)
        if not Path(document["path"]).is_absolute():
            raise ValueError(f"源路径必须为绝对路径：{name}")
        if type(document["page_count"]) is not int or document["page_count"] < 1:
            raise ValueError(f"非法页数：{name}")
        if len(document["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in document["sha256"]):
            raise ValueError(f"非法源 SHA256：{name}")
    if selected and not set(selected) <= names:
        raise ValueError(f"未知 --only ID：{sorted(set(selected) - names)}")
    return [document for document in documents if not selected or document["name"] in selected]


def _archive_attempt(folder: Path) -> None:
    """重试前保存现有失败产物，成功产物只能通过 resume 复用。"""
    if not folder.exists():
        folder.mkdir(parents=True)
        return
    previous = [path for path in folder.iterdir() if path.name != "history"]
    if not previous:
        return
    history = folder / "history"
    history.mkdir(exist_ok=True)
    number = 1
    while (history / f"attempt-{number:04d}").exists():
        number += 1
    archived = history / f"attempt-{number:04d}"
    archived.mkdir()
    for path in previous:
        path.rename(archived / path.name)


def _resumable(folder: Path, document: dict, code: str) -> bool:
    """仅复用同源同源码且证据完整的成功文档；身份不符保留原产物并拒绝。"""
    status = folder / "status.json"
    if not status.is_file():
        return False
    state = json.loads(status.read_text(encoding="utf-8"))
    if state["source_sha256"] != document["sha256"] or state["code_sha256"] != code:
        raise ValueError("resume 产物源或源码身份不匹配；原证据保留")
    if _sha256(Path(document["path"])) != document["sha256"]:
        raise ValueError("resume 时源文件已改变；原证据保留")
    if state["status"] != "succeeded":
        return False
    required = (
        "model.json",
        "middle.json",
        "bundle",
        "document.html",
        "layout.pdf",
        "page-index.json",
        "gallery.html",
        "review.json",
    )
    return all((folder / name).exists() for name in required) and all(
        (folder / f"{kind}-{physical:04d}.png").is_file() for physical in state["review_pages"] for kind in ("source", "layout")
    )


def _worker_command(manifest: Path, output: Path, name: str, code: str) -> list[str]:
    """每份文档启动全新 Python 进程，避免原生崩溃波及整个批次。"""
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--manifest",
        str(manifest),
        "--output",
        str(output),
        "--worker",
        name,
        "--code-sha256",
        code,
    ]


def _failed_process(folder: Path, document: dict, code: str, message: str, returncode: int | None = None) -> dict:
    """即使 worker 原生崩溃或超时，也由父进程保存可继续的阶段失败记录。"""
    state = (
        json.loads((folder / "status.json").read_text(encoding="utf-8"))
        if (folder / "status.json").is_file()
        else _new_status(document, code)
    )
    stage = next((name for name in STAGES if state["stages"][name] == "running"), None)
    if stage is None:
        stage = next((name for name in STAGES if state["stages"][name] != "succeeded"), "render")
    state["stages"][stage] = "failed"
    state["status"] = "failed"
    state["returncode"] = returncode
    state["errors"].append({"stage": stage, "type": "WorkerProcessError", "message": message, "at": _now()})
    state["finished_at"] = _now()
    _save_status(folder, state)
    return state


def _run_one(manifest: Path, output: Path, document: dict, code: str, resume: bool, timeout: float) -> dict:
    """启动并回收独立 worker，拒绝覆盖成功证据并隔离异常退出。"""
    folder = output / "documents" / document["name"]
    try:
        if folder.exists() and resume and _resumable(folder, document, code):
            state = json.loads((folder / "status.json").read_text(encoding="utf-8"))
            return {**state, "resumed": True}
        if folder.exists() and not resume and any(folder.iterdir()):
            raise FileExistsError("文档输出已存在；使用 --resume 或选择新运行目录")
        _archive_attempt(folder)
        _save_status(folder, _new_status(document, code))
        print(f"[{document['name']}] start {document['page_count']} pages", flush=True)
        with (folder / "worker.log").open("a", encoding="utf-8") as errors:
            process = subprocess.Popen(
                _worker_command(manifest, output, document["name"], code), stderr=errors, start_new_session=True
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    # Windows 没有 killpg；终止整棵进程树，包含 worker 启动的渲染子进程。
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=errors,
                        stderr=errors,
                        check=False,
                    )
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if sys.platform == "win32":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                return _failed_process(folder, document, code, f"worker 超过 {timeout:g} 秒超时")
        state = json.loads((folder / "status.json").read_text(encoding="utf-8"))
        if returncode != 0 and state["status"] != "failed":
            return _failed_process(folder, document, code, f"worker 异常退出：{returncode}", returncode)
        if returncode == 0 and state["status"] != "succeeded":
            return _failed_process(folder, document, code, "worker 返回成功但阶段证据未完成", returncode)
        return {**state, "returncode": returncode}
    except Exception as error:
        # 身份不匹配或输出冲突不改写既有证据，失败仍进入本轮汇总。
        return {
            **_new_status(document, code),
            "status": "failed",
            "errors": [{"type": type(error).__name__, "message": str(error)}],
        }


def _write_summary(output: Path, report: dict) -> None:
    """实时汇总已结束的文档；执行完成不改变任何视觉审阅状态。"""
    report["counts"] = dict(Counter(document["status"] for document in report["documents"]))
    report["completed_documents"] = sum(document["status"] == "succeeded" for document in report["documents"])
    report["review_pages"] = sum(
        len(document.get("review_pages", [])) for document in report["documents"] if document["status"] == "succeeded"
    )
    report["updated_at"] = _now()
    _atomic_json(output / "summary.json", report)
    rows = []
    for document in report["documents"]:
        name = html.escape(document["document_id"])
        label = f'<a href="documents/{name}/gallery.html">{name}</a>' if document["status"] == "succeeded" else name
        rows.append(
            f"<tr><td>{label}</td><td>{document['status']}</td><td>{document['page_count']}</td>"
            f"<td>{len(document.get('review_pages', []))}</td></tr>"
        )
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Flash 目录布局审阅</title>'
        "<style>body{font:15px system-ui;margin:24px}td,th{padding:6px 12px;text-align:left;border-bottom:1px solid #ddd}"
        "table{border-collapse:collapse}</style><h1>Flash 目录布局审阅</h1>"
        f"<p>{report['completed_documents']} 份解析完成，{report['review_pages']} 页待视觉审阅。</p>"
        "<table><tr><th>文档</th><th>执行状态</th><th>原件页数</th><th>待审页数</th></tr>" + "".join(rows) + "</table>",
        encoding="utf-8",
    )


def run_batch(
    manifest: Path,
    output: Path,
    *,
    workers: int = 2,
    resume: bool = False,
    selected: list[str] | None = None,
    timeout: float = 1800,
) -> dict:
    """短文档最多双进程；长文档在队列排空后串行，完成后核对全轮源码身份。"""
    if workers not in (1, 2) or timeout <= 0:
        raise ValueError("workers 只能为 1 或 2，timeout 必须大于零")
    documents = _load_documents(manifest, selected)
    output.mkdir(parents=True, exist_ok=True)
    code = fingerprint()
    inventory_code = json.loads(manifest.read_text(encoding="utf-8")).get("environment", {}).get("source_sha256")
    if inventory_code is not None and inventory_code != code:
        raise ValueError("清单生成时的源码身份与当前 fingerprint 不一致，禁止混用基线")
    report = {
        "manifest": str(manifest),
        "manifest_sha256": _sha256(manifest),
        "code_sha256": code,
        "parse_mode": "txt",
        "environment": _environment(),
        "started_at": _now(),
        "requested_documents": len(documents),
        "documents": [],
        "workers": workers,
        "long_document_policy": "完整解析；超过100页串行执行，审阅100页；风险分数只用于选择",
    }
    _write_summary(output, report)
    short = iter(document for document in documents if document["page_count"] <= 100)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {}
        for _ in range(workers):
            document = next(short, None)
            if document is not None:
                future = executor.submit(_run_one, manifest, output, document, code, resume, timeout)
                pending[future] = document
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                pending.pop(future)
                report["documents"].append(future.result())
                _write_summary(output, report)
                document = next(short, None)
                if document is not None:
                    future = executor.submit(_run_one, manifest, output, document, code, resume, timeout)
                    pending[future] = document
    for document in documents:
        if document["page_count"] > 100:
            report["documents"].append(_run_one(manifest, output, document, code, resume, timeout))
            _write_summary(output, report)
    report["code_unchanged"] = fingerprint() == code
    report["manifest_unchanged"] = _sha256(manifest) == report["manifest_sha256"]
    report["finished_at"] = _now()
    report["status"] = "succeeded" if report["code_unchanged"] and report["manifest_unchanged"] else "failed"
    _write_summary(output, report)
    print(f"Batch ended: {report['counts']}; {report['review_pages']} pages pending visual review", flush=True)
    return report


def main() -> None:
    """提供批次、恢复和单文档预检入口，内部 worker 仍完整解析所选文档。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--workers2", dest="workers", action="store_const", const=2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--code-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args()
    manifest, output = args.manifest.resolve(), args.output.resolve()
    if args.worker:
        document = _load_documents(manifest, [args.worker])[0]
        raise SystemExit(process_document(document, output / "documents" / document["name"], args.code_sha256))
    report = run_batch(manifest, output, workers=args.workers, resume=args.resume, selected=args.only, timeout=args.timeout)
    raise SystemExit(0 if report["status"] == "succeeded" and not report["counts"].get("failed") else 1)


if __name__ == "__main__":
    main()
