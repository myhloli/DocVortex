"""验证原生基准的恢复身份、独立 RSS 汇总和超过阈值后的反序复测。"""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


_TIMED_OUTPUT = {"model": {"pages": [[]]}, "middle": {"pages": []}, "assets": {}, "diagnostics": []}


@pytest.fixture
def benchmarks(monkeypatch: pytest.MonkeyPatch):
    """按命令行脚本的模块布局导入基准工具，测试结束后恢复搜索路径。"""
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "benchmarks"))
    return importlib.import_module("pdf_memory"), importlib.import_module("compare_pdf_revisions")


@pytest.fixture
def sample(tmp_path: Path, benchmarks) -> dict:
    """创建可独立探测身份的微型源码和扩展替身，不编译或解析真实 PDF。"""
    memory, _ = benchmarks
    package = tmp_path / "source/src/docvortex"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""基准身份测试包。"""\n', encoding="utf-8")
    (package / "_native.so").write_bytes(b"native-binary-one")
    (package / "_compute_backend.py").write_text(
        '"""提供磁盘扩展替身身份，用于验证独立进程探测。"""\n'
        "import hashlib\nfrom pathlib import Path\n"
        "def backend_info():\n"
        '    """返回当前磁盘内容，避免父进程模块缓存遮蔽替换。"""\n'
        "    path = Path(__file__).with_name('_native.so')\n"
        "    return {'backend': 'rust', 'extension': str(path), "
        "'extension_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'protocol': 36}\n",
        encoding="utf-8",
    )
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"frozen-pdf-input")
    return {
        **memory.benchmark_identity(pdf, package / "__init__.py", "public", None),
        "full_output_sha256": memory.digest(_TIMED_OUTPUT),
        "flash_baseline": None,
    }


def _memory_record(config: dict, rss: int = 100) -> dict:
    """构造完整独立审计记录，保留输入及运行身份供恢复校验。"""
    return {
        **deepcopy(config),
        "equal": True,
        "method": "isolated-entry-after-one-warmup",
        "memory": {"sampled_tree_peak_rss_bytes": rss},
    }


def _timing_record(config: dict, rss: int = 100) -> dict:
    """构造有效五次计时记录，使回归只改变待验证的身份或配置。"""
    return {
        **_memory_record(config, rss),
        "page_count": 1,
        "seconds": {"parse": [1.0] * 5},
        "median_seconds": {"parse": 1.0},
        "memory_method": "isolated-entry-after-one-warmup",
        "artifact": "00-sample",
    }


def _save(path: Path, value: dict) -> None:
    """在 pytest 临时目录保存小型报告，避免污染仓库及正式基线。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _save_timing(folder: Path, report: dict) -> None:
    """保存计时元数据和对应完整输出，恢复验证必须同时读取二者。"""
    _save(folder / "report.json", report)
    for record in report["documents"]:
        _save(folder / record["artifact"] / "output.json", _TIMED_OUTPUT)


def test_native_rss_ratios_use_only_current_two_labels(benchmarks) -> None:
    """当前报告无需旧 Python 标签即可得到正确的原生内存比例。"""
    memory, _ = benchmarks
    assert memory.ratios(
        {
            "rust-reference": {"memory": {"sampled_tree_peak_rss_bytes": 100}},
            "rust-current": {"memory": {"sampled_tree_peak_rss_bytes": 90}},
        }
    ) == {"rust-revision": 0.9}


@pytest.mark.parametrize("labels", [[], ["rust-current"], ["rust-reference", "rust-current", "python-current"]])
def test_native_comparisons_reject_incomplete_or_legacy_labels(benchmarks, labels: list[str]) -> None:
    """空报告、少标签或混入旧后端不能产生成功比例。"""
    memory, comparison = benchmarks
    records = {label: {} for label in labels}
    for compare in (memory.ratios, comparison.compare):
        with pytest.raises(ValueError, match="measurement labels"):
            compare(records)


@pytest.mark.parametrize("rss", [0, -1, None, float("nan"), float("inf")])
def test_native_rss_rejects_empty_or_invalid_sampling(benchmarks, rss) -> None:
    """无有效 RSS 观察值必须失败，不能变成虚假的低内存或无穷比例。"""
    memory, _ = benchmarks
    with pytest.raises(ValueError, match="positive finite"):
        memory.ratios(
            {
                "rust-reference": {"memory": {"sampled_tree_peak_rss_bytes": rss}},
                "rust-current": {"memory": {"sampled_tree_peak_rss_bytes": 100}},
            }
        )


def test_matching_memory_audit_can_resume(benchmarks, sample: dict, tmp_path: Path) -> None:
    """同源码、输入、扩展和环境的已完成审计可复用，不启动采样 worker。"""
    memory, _ = benchmarks
    record = _memory_record(sample)
    folder = tmp_path / "audit"
    _save(folder / "result.json", record)
    assert memory.measure(sample, folder) == record
    assert not folder.with_suffix(".config.json").exists()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", "/different/sample.pdf"),
        ("source_sha256", "different-input"),
        ("source_package", "/different/src/docvortex/__init__.py"),
        ("source_code_sha256", "different-code"),
        ("source_commit", "different-commit"),
        ("region_input_sha256", "different-region"),
        ("environment", {"python_version": "different-runtime"}),
        ("compute.extension", "/different/_native.so"),
        ("compute.extension_sha256", "different-extension"),
        ("compute.protocol", 35),
    ],
)
def test_memory_resume_rejects_stale_identity(benchmarks, sample: dict, tmp_path: Path, field: str, replacement) -> None:
    """即使输出摘要相同，其他样本、源码、二进制或环境的旧审计也不能复用。"""
    memory, _ = benchmarks
    record = _memory_record(sample)
    target = record
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = replacement
    folder = tmp_path / "audit"
    _save(folder / "result.json", record)
    with pytest.raises(ValueError, match=field):
        memory.measure(sample, folder)
    assert json.loads((folder / "result.json").read_text(encoding="utf-8")) == record


@pytest.mark.parametrize("change", ["input", "source", "extension"])
def test_memory_resume_rechecks_current_files(benchmarks, sample: dict, tmp_path: Path, change: str) -> None:
    """配置和缓存同时过期时仍重新核对磁盘，不能只比较两个旧报告彼此一致。"""
    memory, _ = benchmarks
    folder = tmp_path / "audit"
    _save(folder / "result.json", _memory_record(sample))
    paths = {
        "input": Path(sample["path"]),
        "source": Path(sample["source_package"]),
        "extension": Path(sample["compute"]["extension"]),
    }
    paths[change].write_bytes(b"# changed-source-or-binary\n")
    expected = {"input": "source_sha256", "source": "source_code_sha256", "extension": "extension_sha256"}
    with pytest.raises(ValueError, match=expected[change]):
        memory.measure(sample, folder)


def test_old_memory_audit_without_input_identity_is_rejected(benchmarks, sample: dict, tmp_path: Path) -> None:
    """升级前缺少输入身份的缓存必须重新采集，不能假定同一输出摘要就是同一输入。"""
    memory, _ = benchmarks
    record = _memory_record(sample)
    del record["source_sha256"]
    folder = tmp_path / "audit"
    _save(folder / "result.json", record)
    with pytest.raises(ValueError, match="missing fields.*source_sha256"):
        memory.measure(sample, folder)


@pytest.mark.parametrize("change", ["suite", "runs", "empty_seconds", "input", "extension", "environment"])
def test_timing_resume_rejects_stale_report(benchmarks, sample: dict, tmp_path: Path, change: str) -> None:
    """比较驱动不能复用其他套件、计时次数、样本、扩展或环境生成的旧计时。"""
    _, comparison = benchmarks
    record = _timing_record(sample)
    report = {"suite": "public", "documents": [record]}
    if change == "suite":
        report["suite"] = "shared"
    elif change == "runs":
        record["seconds"]["parse"].pop()
    elif change == "empty_seconds":
        record["seconds"] = {}
    elif change == "input":
        record["source_sha256"] = "another-input"
    elif change == "extension":
        record["compute"]["extension_sha256"] = "another-extension"
    else:
        record["environment"] = {"python_version": "another-runtime"}
    folder = tmp_path / "timing"
    _save_timing(folder, report)
    args = SimpleNamespace(suite="public", runs=5, flash_baseline=None)
    with pytest.raises(ValueError):
        comparison.measure(Path(sample["path"]), folder, Path(sample["source_package"]).parents[2], args)


def test_matching_timing_report_can_resume(benchmarks, sample: dict, tmp_path: Path) -> None:
    """完整身份及五次采样配置相同的计时可恢复，保持既有独立内存报告。"""
    _, comparison = benchmarks
    record = _timing_record(sample)
    folder = tmp_path / "timing"
    _save_timing(folder, {"suite": "public", "documents": [record]})
    args = SimpleNamespace(suite="public", runs=5, flash_baseline=None)
    assert comparison.measure(Path(sample["path"]), folder, Path(sample["source_package"]).parents[2], args) == record


@pytest.mark.parametrize("missing", [False, True])
def test_timing_resume_requires_unchanged_complete_artifact(benchmarks, sample: dict, tmp_path: Path, missing: bool) -> None:
    """报告中的摘要不能代替实际输出；输出被删除或修改后都禁止恢复旧计时。"""
    _, comparison = benchmarks
    record = _timing_record(sample)
    folder = tmp_path / "timing"
    _save_timing(folder, {"suite": "public", "documents": [record]})
    artifact = folder / record["artifact"] / "output.json"
    if missing:
        artifact.unlink()
    else:
        _save(artifact, {**_TIMED_OUTPUT, "diagnostics": ["changed"]})
    args = SimpleNamespace(suite="public", runs=5, flash_baseline=None)
    with pytest.raises((ValueError, FileNotFoundError)):
        comparison.measure(Path(sample["path"]), folder, Path(sample["source_package"]).parents[2], args)


def test_revision_comparison_requires_same_runtime_environment(benchmarks, sample: dict) -> None:
    """不同依赖环境生成的两组计时不能直接当作同环境的源码优化结论。"""
    _, comparison = benchmarks
    reference, candidate = _timing_record(sample), _timing_record(sample)
    candidate["environment"]["dependencies"]["pydantic"] = "different-version"
    with pytest.raises(ValueError, match="environment"):
        comparison.compare({"rust-reference": reference, "rust-current": candidate})


def test_shared_resume_rejects_changed_frozen_region(benchmarks, sample: dict, tmp_path: Path) -> None:
    """PDF 字节不变而区域基线改变时，旧 shared 计时仍应失效。"""
    memory, comparison = benchmarks
    baseline = tmp_path / "flash"
    _save(baseline / "report.json", {"documents": [{"source_sha256": sample["source_sha256"], "artifact": "document"}]})
    _save(baseline / "document/output.json", {"model_list": [[{"type": "text", "bbox": [0, 0, 1, 1]}]]})
    config = {**sample, "suite": "shared", "region_input_sha256": "old-frozen-region"}
    record = _timing_record(config)
    folder = tmp_path / "timing"
    _save_timing(folder, {"suite": "shared", "documents": [record]})
    args = SimpleNamespace(suite="shared", runs=5, flash_baseline=baseline)
    with pytest.raises(ValueError, match="region_input_sha256"):
        comparison.measure(Path(sample["path"]), folder, Path(sample["source_package"]).parents[2], args)
    with pytest.raises(ValueError, match="exactly one"):
        memory.region_model("missing-input", baseline)


@pytest.mark.parametrize("persistent", [False, True])
def test_memory_main_repeats_native_regression_in_reverse_order(
    benchmarks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    """超过 5% 时反序复测两个原生版本，仅持续增长使最终入口失败。"""
    memory, _ = benchmarks
    path = tmp_path / "sample.pdf"
    timing = tmp_path / "timing.json"
    records = {label: {"path": str(path)} for label in ("rust-reference", "rust-current")}
    _save(timing, {"suite": "public", "documents": [{"path": str(path), "measurements": records}]})
    calls = []

    def measure(config: dict, folder: Path) -> dict:
        """返回可控的首次增长与复测结果，同时记录两个版本的执行顺序。"""
        calls.append(folder.name)
        rss = 100
        if folder.name == "rust-current":
            rss = 120
        elif folder.name == "repeat-rust-current":
            rss = 120 if persistent else 105
        return _memory_record(config, rss)

    monkeypatch.setattr(memory, "measure", measure)
    output = tmp_path / "audit"
    monkeypatch.setattr(sys, "argv", ["pdf_memory.py", "--timing-report", str(timing), "--output", str(output)])
    if persistent:
        with pytest.raises(SystemExit, match="Persistent isolated RSS regression"):
            memory.main()
    else:
        memory.main()
    assert calls == ["rust-reference", "rust-current", "repeat-rust-current", "repeat-rust-reference"]
    record = json.loads((output / "report.json").read_text(encoding="utf-8"))["documents"][0]
    assert record["persistent_regression"] is persistent


@pytest.mark.parametrize("filtered", [False, True])
def test_memory_main_rejects_empty_selection(
    benchmarks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    """空计时报告和未命中任何样本的筛选都明确失败，不生成空成功报告。"""
    memory, _ = benchmarks
    timing = tmp_path / "timing.json"
    _save(timing, {"suite": "public", "documents": [{"path": str(tmp_path / "one.pdf")}] if filtered else []})
    command = ["pdf_memory.py", "--timing-report", str(timing), "--output", str(tmp_path / "output")]
    if filtered:
        command.extend(["--path", str(tmp_path / "missing.pdf")])
    monkeypatch.setattr(sys, "argv", command)
    with pytest.raises(SystemExit, match="2"):
        memory.main()


@pytest.mark.parametrize("persistent", [False, True])
def test_comparison_main_repeats_native_regression_in_reverse_order(
    benchmarks, sample: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    """配对比较驱动也使用同一原生比较对，并在计时增长后按反序确认是否持续。"""
    _, comparison = benchmarks
    calls = []

    def measure(path: Path, folder: Path, source: Path, args) -> dict:
        """模拟首次解析退化和可控复测，不启动真实 PDF 分析。"""
        calls.append(folder.name)
        record = _timing_record(sample)
        elapsed = 1.0
        if folder.name == "rust-current":
            elapsed = 1.2
        elif folder.name == "repeat-rust-current":
            elapsed = 1.2 if persistent else 1.05
        record["median_seconds"]["parse"] = elapsed
        record["seconds"]["parse"] = [elapsed] * 5
        return record

    def manifest(paths: list[Path]) -> list:
        """保留真实源字节指纹，只替换需要打开 PDF 的页数发现。"""
        return [{"path": str(paths[0]), "sha256": sample["source_sha256"], "pages": 1}]

    monkeypatch.setattr(comparison, "measure", measure)
    monkeypatch.setattr(comparison, "corpus_manifest", manifest)
    output = tmp_path / "comparison"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compare_pdf_revisions.py",
            "--reference-source",
            str(tmp_path / "before"),
            "--candidate-source",
            str(tmp_path / "after"),
            "--path",
            sample["path"],
            "--output",
            str(output),
        ],
    )
    if persistent:
        with pytest.raises(SystemExit, match="persistent regression"):
            comparison.main()
    else:
        comparison.main()
    assert calls == ["rust-reference", "rust-current", "repeat-rust-current", "repeat-rust-reference"]
    record = json.loads((output / "report.json").read_text(encoding="utf-8"))["documents"][0]
    assert record["persistent_regression"] is persistent


def test_comparison_main_rejects_empty_corpus(benchmarks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认发现为空时比较驱动立即失败，不输出没有样本的通过结论。"""
    _, comparison = benchmarks

    def empty_corpus(kind: str) -> list:
        """模拟缺失的语料集合，不打开 PDF 或触发正式计时。"""
        return []

    monkeypatch.setattr(comparison, "corpus_paths", empty_corpus)
    monkeypatch.setattr(
        sys, "argv", ["compare_pdf_revisions.py", "--reference-source", str(tmp_path), "--output", str(tmp_path / "output")]
    )
    with pytest.raises(SystemExit, match="2"):
        comparison.main()
