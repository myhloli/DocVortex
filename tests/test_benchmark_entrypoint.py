"""验证迁移后的基线工具可在没有 MinerU/pdftext 的安装环境中生成报告。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


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
    assert all(item["file"].startswith("docvortex/analyzers/native/pdf/") for item in record["profile"])
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
