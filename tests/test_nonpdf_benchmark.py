"""验证多格式配对工具的固定输入、解析参数、完整输出与独立内存边界。"""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def corpus(monkeypatch):
    """按工具入口搜索路径加载冻结器，不改变当前已导入的产品源码。"""
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    monkeypatch.syspath_prepend(str(ROOT / "tests/benchmarks"))
    return importlib.import_module("prepare_nonpdf_corpus")


@pytest.fixture
def frozen(corpus, tmp_path):
    """冻结小型 CSV 和带显式来源的 HTML，供字节及上下文漂移测试使用。"""
    html = tmp_path / "sample.html"
    html.write_text('<html><h1 id="title">Title</h1><p><a href="next.html">Next</a></p></html>', encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "csv",
                        "category": "fixture",
                        "file_suffix": "csv",
                        "generator": "csv",
                        "parameters": {"rows": 2, "columns": 2},
                    },
                    {
                        "name": "html",
                        "category": "real",
                        "file_suffix": "html",
                        "path": str(html),
                        "source_context": {"source_uri": "https://example.test/guide/sample.html"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return corpus.freeze_corpus(spec, tmp_path / "frozen")


@pytest.mark.parametrize(
    ("kind", "parameters", "suffix"),
    [
        ("csv", {"rows": 3, "columns": 2}, "csv"),
        ("tsv", {"rows": 3, "columns": 2}, "tsv"),
        ("ods", {"rows": 3, "columns": 2}, "ods"),
        ("xlsx", {"rows": 3, "columns": 2}, "xlsx"),
        ("rtf", {"paragraphs": 3}, "rtf"),
        ("docx_paragraphs", {"paragraphs": 3}, "docx"),
        ("docx_tables", {"tables": 2, "rows": 2, "columns": 2}, "docx"),
    ],
)
def test_pressure_inputs_are_deterministic_real_documents(corpus, kind, parameters, suffix):
    """压力生成器产生可由公开入口解析的实际格式，重复生成字节完全相同。"""
    from docvortex import parse

    payload = corpus.generate_pressure(kind, parameters)
    assert corpus.generate_pressure(kind, parameters) == payload
    result = parse(payload, file_suffix=suffix, keep_model_json=True)
    assert result.model_json is not None and result.model_json.pages
    assert result.middle_json.pages


def test_manifest_freezes_bytes_suffix_and_html_context(corpus, frozen):
    """解析实际冻结 HTML 时来源参数保持稳定，不能被临时目录或工作区路径替代。"""
    from rust_pdf import public_once

    entries = corpus.load_frozen_manifest(frozen)
    html = entries[1]
    _, output = public_once(
        Path(html["path"]).read_bytes(), file_suffix=html["file_suffix"], source_context=html["source_context"]
    )
    assert set(output) == {"model", "middle", "assets", "diagnostics"}
    assert "https://example.test/guide/next.html" in json.dumps(output)
    assert all(Path(entry["path"]).parent == frozen.parent for entry in entries)


def test_manifest_rejects_changed_source_bytes(corpus, frozen):
    """冻结后改写任一输入必须失败，不能把新内容沿用旧摘要加入配对。"""
    entry = corpus.load_frozen_manifest(frozen)[0]
    Path(entry["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="bytes changed"):
        corpus.load_frozen_manifest(frozen)


@pytest.mark.parametrize("change", ["duplicate", "local_resource_root", "suffix", "relative_path"])
def test_manifest_rejects_ambiguous_or_hidden_inputs(corpus, frozen, change):
    """拒绝重复案例、未冻结本地依赖、未知格式或随 cwd 变化的输入路径。"""
    value = json.loads(frozen.read_text(encoding="utf-8"))
    if change == "duplicate":
        value["inputs"].append(deepcopy(value["inputs"][0]))
    elif change == "local_resource_root":
        value["inputs"][0]["source_context"] = {"local_resource_root": "/unfrozen"}
    elif change == "suffix":
        value["inputs"][0]["file_suffix"] = "unknown"
    else:
        value["inputs"][0]["path"] = "relative.csv"
    frozen.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        corpus.load_frozen_manifest(frozen)


@pytest.mark.parametrize("field", ["file_suffix", "source_context"])
def test_memory_identity_includes_parse_parameters(corpus, field):
    """相同源字节与输出摘要也不能掩盖 RSS 重放使用另一种格式或 HTML 来源。"""
    from pdf_memory import validate_identity

    record = {
        "path": "/input.csv",
        "source_sha256": "input",
        "source_package": "/source/src/docvortex/__init__.py",
        "source_commit": "commit",
        "source_code_sha256": "source",
        "suite": "public",
        "region_input_sha256": None,
        "environment": {},
        "compute": {"backend": "rust", "protocol": 36, "extension": "/source/native.so", "extension_sha256": "native"},
        "file_suffix": "csv",
        "source_context": None,
    }
    changed = deepcopy(record)
    changed[field] = "tsv" if field == "file_suffix" else {"source_uri": "https://example.test/changed"}
    with pytest.raises(ValueError, match=field):
        validate_identity(changed, record)


def test_nonpdf_pair_runs_ab_ba_with_independent_rss(corpus, frozen, tmp_path):
    """真实运行两轮 CSV/HTML 配对，核对全部输出及独立 RSS 的格式、来源和摘要。"""
    import docvortex

    source = tmp_path / "selected-source"
    shutil.copytree(Path(docvortex.__file__).parent, source / "src/docvortex", ignore=shutil.ignore_patterns("__pycache__"))
    output = tmp_path / "paired"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/benchmark_native_pair.py"),
            "--baseline-root",
            str(source),
            "--candidate-root",
            str(source),
            "--manifest",
            str(frozen),
            "--rounds",
            "2",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(source / "src")},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "complete" and report["full_output_equal"]
    assert report["order"] == "AB/BA alternating"
    assert len(report["documents"]) == 2
    assert report["runs"] == {"before": ["1-before.json", "2-before.json"], "after": ["1-after.json", "2-after.json"]}
    for files in report["runs"].values():
        for name in files:
            run = json.loads((output / name).read_text(encoding="utf-8"))
            assert run["warmup_documents"] == 2
            for item in run["documents"]:
                captured = json.loads((output / item["artifact"]).read_text(encoding="utf-8"))
                memory = json.loads((output / item["memory_artifact"]).read_text(encoding="utf-8"))
                assert set(captured) == {"model", "middle", "assets", "diagnostics"}
                assert memory["equal"] and memory["file_suffix"] == item["file_suffix"]
                assert memory["source_context"] == item["source_context"]
                assert memory["full_output_sha256"] == item["full_output_sha256"]
                assert memory["capture_scope"] == "parse_before_serialization"
                assert memory["memory"]["sampled_tree_peak_rss_bytes"] > 0


def test_pdf_profile_preserves_owned_inline_fast_path(tmp_path):
    """实际探针不能因包装 detector 改变函数身份而关闭已实现的 owned 样式批次。"""
    import docvortex

    source = tmp_path / "selected-source"
    shutil.copytree(Path(docvortex.__file__).parent, source / "src/docvortex", ignore=shutil.ignore_patterns("__pycache__"))
    output = tmp_path / "profile"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/pdf_public_profile.py"),
            "--source",
            str(source),
            "--path",
            str(ROOT / "demo/pdfs/2407.00079v4_origi-10.pdf"),
            "--output",
            str(output),
            "--runs",
            "1",
            "--render-backend",
            "session",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(source / "src")},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["full_output_sha256"] if "full_output_sha256" in report else report["complete_output_sha256"]
    assert report["native_inline_style_batches"] > 0
    assert (
        report["compute_after"]["native_inline_style_batches"] - report["compute_before"]["native_inline_style_batches"]
        == report["native_inline_style_batches"]
    )
    assert not {"detect_pdf_text_style_lines", "detect_pdf_text_link_lines", "detect_pdf_text_script_lines"}.intersection(
        report["stages"]
    )
