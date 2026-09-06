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
    assert "docgale" in report["dependencies"]
    assert "pdftext" not in report["dependencies"]
    assert len(report["documents"]) == 1
    record = report["documents"][0]
    assert record["pages"] == 1
    assert record["profile"]
    assert all(item["file"].startswith("docgale/analyzers/native/pdf/") for item in record["profile"])
    assert (tmp_path / record["artifact"] / "output.json").is_file()
