"""验证迁移后的基线工具可在没有 MinerU/pdftext 的安装环境中生成报告。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.pdf_corpus import corpus_manifest, corpus_paths, discover_demo_pdfs


def test_demo_corpus_discovers_new_subdirectory_pdf(tmp_path: Path) -> None:
    """新 demo PDF 自动进入 demo 与 all 集合，路径只出现一次。"""

    (tmp_path / "demo/pdfs/nested").mkdir(parents=True)
    sample = tmp_path / "demo/pdfs/nested/new.pdf"
    sample.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "tests/fixtures").mkdir(parents=True)
    (tmp_path / "tests/fixtures/flash_layout_geometry_manifest.json").write_text(
        json.dumps({"documents": [{"path": "demo/pdfs/nested/new.pdf"}]}), encoding="utf-8"
    )
    assert discover_demo_pdfs(tmp_path) == [sample]
    assert corpus_paths("demo", tmp_path) == [sample.resolve()]
    assert corpus_paths("all", tmp_path) == [sample.resolve()]


def test_corpus_manifest_counts_encoded_pdf_pages(tmp_path: Path) -> None:
    """冻结 XOR 原始字节指纹，同时按解码后的真实 PDF 统计页数。"""

    import hashlib

    data = (Path(__file__).parents[1] / "demo/pdfs/2407.00079v4_origi-10.pdf").read_bytes()
    key = b"MinerU flash layout fixture"
    encoded = bytes(value ^ key[index % len(key)] for index, value in enumerate(data))
    sample = tmp_path / "encoded.pdf.xor"
    sample.write_bytes(encoded)
    assert corpus_manifest([sample]) == [
        {"path": str(sample.resolve()), "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded), "pages": 1}
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="基线工具使用 POSIX resource 进程统计")
def test_native_benchmark_generates_report_and_portable_profile(tmp_path: Path) -> None:
    """实际运行单页基线与 profiler，检查元数据及安装包路径归一化。"""
    root = Path(__file__).parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "tests/benchmarks/flash_pdf.py"),
            "--path",
            "demo/pdfs/2407.00079v4_origi-10.pdf",
            "--runs",
            "0",
            "--profile",
            "--output",
            str(tmp_path),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert "docvortex" in report["dependencies"]
    assert "pdftext" not in report["dependencies"]
    assert len(report["documents"]) == 1
    record = report["documents"][0]
    assert record["pages"] == 1
    assert record["profile"]
    assert all(
        item["file"].startswith(("docvortex/analyzers/native/pdf/", "docvortex/document/pdf/")) for item in record["profile"]
    )
    assert record["compute"]["backend"] in {"python", "rust"}
    assert record["source_version"]
    assert (tmp_path / record["artifact"] / "output.json").is_file()


def test_public_pipeline_benchmark_freezes_all_outputs(tmp_path: Path) -> None:
    """实际比较两次合成 CSV 的七种输出、源字节和独立进程内存报告。"""
    root = Path(__file__).parents[1]
    for name in ("baseline", "candidate"):
        command = [
            sys.executable,
            str(root / "tests/benchmarks/pipeline.py"),
            "--path",
            "fixture:csv",
            "--runs",
            "0",
            "--output",
            str(tmp_path / name),
        ]
        if name == "candidate":
            command.extend(["--baseline", str(tmp_path / "baseline")])
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "candidate/report.json").read_text(encoding="utf-8"))
    record = report["documents"][0]
    assert record["warm_seconds"] == []
    assert record["memory"]["parent_peak_rss_bytes"] > 0
    output = json.loads((tmp_path / "candidate/00/output.json").read_text(encoding="utf-8"))
    assert set(output["renders"]) == {"markdown", "html", "latex", "docx", "epub", "pdf", "structured_content"}
    assert (tmp_path / "candidate/00/bundle/manifest.json").is_file()
    assert json.loads((tmp_path / "candidate/comparison.json").read_text(encoding="utf-8"))[0]["equal"]


def test_pdf_benchmark_compares_both_layouts_and_python_fallback(tmp_path: Path) -> None:
    """独立进程重放公式和重复图片，检查回退后端、视觉签名及严格基线比较。"""
    root = Path(__file__).parents[1]
    for name in ("baseline", "candidate"):
        command = [
            sys.executable,
            str(root / "tests/benchmarks/pdf_render.py"),
            "--runs",
            "0",
            "--backend",
            "python",
            "--output",
            str(tmp_path / name),
        ]
        if name == "candidate":
            command.extend(["--baseline", str(tmp_path / "baseline")])
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "candidate/report.json").read_text())
    assert {item["layout"] for item in report["records"]} == {"original", "reflow"}
    for record in report["records"]:
        assert record["backend"] == "python"
        assert not record["native_functions"]
        assert record["memory"]["parent_peak_rss_bytes"] > 0
        assert (tmp_path / "candidate" / record["artifact"] / "signature.json").is_file()
    assert all(item["equal"] for item in json.loads((tmp_path / "candidate/comparison.json").read_text()))
