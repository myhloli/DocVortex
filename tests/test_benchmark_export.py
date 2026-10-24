"""验证导出基准实际写盘、完整校验及独立计时/RSS 边界。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from PIL import Image
import pytest

from benchmarks import export_files


@pytest.fixture
def worker_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程内单测使用实际已安装包身份，源码选择合同由独立子进程回归覆盖。"""
    import docvortex
    from docvortex._compute_backend import backend_info

    monkeypatch.syspath_prepend(str(Path(__file__).parent / "benchmarks"))
    from rust_pdf import environment_identity, source_identity

    def current_runtime(_source_root: Path) -> dict:
        """仍读取真实源码、原生扩展及依赖摘要，只隔离本单测无关的目录选择。"""
        package = Path(docvortex.__file__).resolve()
        compute = backend_info()
        return {
            "source_package": str(package),
            **source_identity(package),
            "environment": environment_identity(),
            "compute": {name: compute[name] for name in ("backend", "protocol", "extension", "extension_sha256")},
        }

    monkeypatch.setattr(export_files, "runtime_identity", current_runtime)


def test_export_fixture_is_deterministic_and_decodable() -> None:
    """固定字节预算生成真实 PNG，素材不同且不受进程或随机状态影响。"""
    first = export_files.png_payload(0, 128)
    assert len(first) == 128
    assert first == export_files.png_payload(0, 128)
    assert first != export_files.png_payload(1, 128)
    with Image.open(BytesIO(first)) as image:
        image.load()
        assert image.size == (1, 1)


@pytest.mark.parametrize("entry", export_files.ENTRIES)
def test_export_validation_rejects_last_asset_corruption(tmp_path: Path, entry: str) -> None:
    """逐个入口损坏最后一份素材，确认校验没有只抽查主文件或首个图片。"""
    workload = export_files.build_workload(entry, 3, 128)
    before = export_files.input_snapshot(workload)
    result = export_files.invoke(workload, tmp_path / "data", "new")
    snapshot = export_files.validate_output(workload, tmp_path / "data", result)
    assert len(snapshot["references"]) == 3
    assert export_files.input_snapshot(workload) == before
    (tmp_path / "data/images/page_0_image_2.png").write_bytes(b"corrupted")
    with pytest.raises(AssertionError, match="asset differs"):
        export_files.validate_output(workload, tmp_path / "data", result)


def test_precheck_uses_scale_gradient_without_mutation() -> None:
    """路径预检记录多种规模及全部输入摘要，不混入文件写盘。"""
    small, large = (export_files.precheck(count, 2) for count in (2, 8))
    assert [small["total_paths"], large["total_paths"]] == [3, 9]
    assert small["input_sha256"] != large["input_sha256"]
    assert len(small["seconds"]) == len(large["seconds"]) == 2


def test_export_sampler_reports_background_failure_after_valid_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """起点 RSS 有效也不能掩盖后台查询失败，避免错误低峰值通过验收。"""
    monkeypatch.setattr(export_files, "memory_bytes", lambda pids: {os.getpid(): 1024})
    sampler = export_files.ExportMemorySampler()
    sampler.snapshot()

    def unavailable(pids):
        """模拟采样线程遇到操作系统查询失败。"""
        raise OSError("RSS unavailable")

    monkeypatch.setattr(export_files, "memory_bytes", unavailable)
    sampler.thread.start()
    sampler.thread.join()
    with pytest.raises(RuntimeError, match="sampling failed"):
        sampler.finish()


def test_worker_detects_input_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_runtime: None) -> None:
    """注入导出后原对象污染，保证成功写盘仍不能绕过基准正确性门禁。"""
    original = export_files.invoke

    def mutate(workload, folder, mode):
        """在实际写盘后污染仅属于调用方的输入字段。"""
        result = original(workload, folder, mode)
        workload["artifact"].mime_type = "polluted"
        return result

    monkeypatch.setattr(export_files, "invoke", mutate)
    config = {"entry": "write_artifact", "mode": "new", "files": 1, "payload_bytes": 128, "runs": 1}
    with pytest.raises(AssertionError, match="input"):
        export_files.run_worker(config, tmp_path, False)


def test_export_benchmark_round_trip_and_baseline(tmp_path: Path) -> None:
    """运行九种小型实际事务并比较基线，检查所有文件和独立 RSS 进程记录。"""
    root = Path(__file__).resolve().parents[1]
    import docvortex

    selected = tmp_path / "installed-source"
    shutil.copytree(Path(docvortex.__file__).parent, selected / "src/docvortex", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("baseline", "candidate"):
        command = [
            sys.executable,
            str(root / "tests/benchmarks/export_files.py"),
            "--source-root",
            str(selected),
            "--files",
            "2",
            "--payload-bytes",
            "128",
            "--runs",
            "1",
            "--output",
            str(tmp_path / name),
        ]
        if name == "candidate":
            command += ["--baseline", str(tmp_path / "baseline")]
        completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((tmp_path / "candidate/report.json").read_text(encoding="utf-8"))
    assert len(report["records"]) == 9
    assert report["source_root"] == str(selected.resolve())
    assert Path(report["source_package"]) == (selected / "src/docvortex/__init__.py").resolve()
    assert report["source_code_sha256"] and report["compute"]["extension_sha256"]
    assert report["environment"]["dependencies"]["pydantic"]
    assert all(item["equal"] for item in report["comparison"])
    assert {(row["config"]["entry"], row["config"]["mode"]) for row in report["records"]} == {
        (entry, mode) for entry in export_files.ENTRIES for mode in export_files.MODES
    }
    for row in report["records"]:
        assert len(row["seconds"]) == 1
        assert row["pid"] != row["memory_pid"]
        assert row["memory"]["parent_peak_rss_bytes"] > 0
        assert row["memory"]["sample_count"] >= 2
        assert row["input_unchanged"]
        assert row["identity"]["source_package"] == report["source_package"]
        assert row["identity"]["source_code_sha256"] == report["source_code_sha256"]
        assert row["identity"]["compute"] == report["compute"]
        assert row["output_sha256"] == export_files.fingerprint(row["output"])
        stages = row["stage_diagnostic"]["stages"]
        assert stages["path_relationships"]["calls"] == 1
        assert stages["commit_compare_stage_replace"]["calls"] == 1
        assert stages["path_resolution"]["calls"] >= 3
        for name, identity in row["output"]["files"].items():
            data = (tmp_path / "candidate" / row["artifact"] / name).read_bytes()
            assert len(data) == identity["bytes"]
            assert hashlib.sha256(data).hexdigest() == identity["sha256"]
    changed = deepcopy(report)
    changed["records"][-1]["output"]["references"] = []
    roots = {"report_root": tmp_path / "candidate", "baseline_root": tmp_path / "candidate"}
    with pytest.raises(AssertionError, match="manifest digest differs"):
        export_files.compare_baseline(changed, report, **roots)
    changed = deepcopy(report)
    changed["workload"]["payload_bytes"] += 1
    with pytest.raises(ValueError, match="not equivalent"):
        export_files.compare_baseline(changed, report, **roots)
    changed = deepcopy(report)
    changed["environment"]["python"] = "other interpreter"
    with pytest.raises(ValueError, match="protocol or environment"):
        export_files.compare_baseline(changed, report, **roots)
    changed = deepcopy(report)
    changed["measurement"]["runs"] += 1
    with pytest.raises(ValueError, match="protocol or environment"):
        export_files.compare_baseline(changed, report, **roots)
    changed = deepcopy(report)
    changed["source_code_sha256"] = "candidate source differs"
    changed["compute"]["extension_sha256"] = "candidate extension differs"
    assert all(row["equal"] for row in export_files.compare_baseline(changed, report, **roots))


def test_selected_source_overrides_ambient_worktree(tmp_path: Path) -> None:
    """在独立解释器中选择另一份最小源码，保证环境 PYTHONPATH 不把请求导回工作树。"""
    frozen = tmp_path / "frozen"
    package = frozen / "src/docvortex"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("MARKER = 'selected-frozen-source'\n")
    code = """
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from export_files import select_source
selected = select_source(Path(sys.argv[2]))
import docvortex
print(json.dumps({'selected': str(selected), 'package': str(Path(docvortex.__file__).resolve()), 'marker': docvortex.MARKER}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(Path(export_files.__file__).parent), str(frozen)],
        env=dict(os.environ, PYTHONPATH=str(export_files.ROOT / "src")),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    selected = json.loads(completed.stdout)
    assert selected == {
        "selected": str(frozen.resolve()),
        "package": str((package / "__init__.py").resolve()),
        "marker": "selected-frozen-source",
    }


@pytest.mark.parametrize("field", ["source_code_sha256", "compute", "environment"])
def test_export_rejects_identity_drift(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """源码资源、扩展或依赖环境变化必须阻断报告，不能继续沿用开始时身份。"""
    expected = {"source_code_sha256": "source", "compute": {"extension_sha256": "native"}, "environment": {"python": "current"}}
    changed = deepcopy(expected)
    changed[field] = "changed"
    monkeypatch.setattr(export_files, "runtime_identity", lambda _root: changed)
    with pytest.raises(RuntimeError, match="changed during export benchmark"):
        export_files.assert_runtime_unchanged(expected, export_files.ROOT)


def test_memory_reuses_timed_expectation_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_runtime: None
) -> None:
    """RSS 重放复用已校验结果，只在停止采样后进行一次完整 bundle 回读校验。"""
    config = {"entry": "save_bundle", "mode": "same", "files": 1, "payload_bytes": 128, "runs": 1}
    timing = tmp_path / "timing"
    timed = export_files.run_worker(config, timing, False)
    export_files.write_json(timing / "result.json", timed)
    original = export_files.validate_output
    validations = []

    def checked(workload, folder, result):
        """记录完整校验次数，避免在采样前重复读取结果包和所有素材。"""
        validations.append(folder)
        return original(workload, folder, result)

    monkeypatch.setattr(export_files, "validate_output", checked)
    memory = export_files.run_worker(config, tmp_path / "memory", True, timing / "result.json")
    assert validations == [tmp_path / "memory/data"]
    assert memory["output"] == timed["output"]
    assert (timing / "data/manifest.json").is_file()


def _file_report(root: Path) -> dict:
    """构造带三个真实素材和全部文件摘要的小报告，供篡改及路径边界回归使用。"""
    artifact = "sample/timing/data"
    payloads = {f"images/page_0_image_{index}.png": export_files.png_payload(index, 128) for index in range(3)}
    references = list(payloads)
    payloads["document.md"] = "".join(f"![]({name})\n" for name in references).encode()
    for name, payload in payloads.items():
        path = root / artifact / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    output = {
        "files": {
            name: {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in payloads.items()
        },
        "references": references,
    }
    return {
        "schema": export_files.SCHEMA,
        "workload": {"files": [3]},
        "measurement": {"runs": 5},
        "environment": {"python": "fixture"},
        "precheck": [],
        "records": [
            {
                "case": "sample",
                "artifact": artifact,
                "output": output,
                "output_sha256": export_files.fingerprint(output),
                "input_sha256": "deterministic",
                "median_seconds": 1.0,
                "memory": {"parent_peak_rss_bytes": 1024},
            }
        ],
    }


@pytest.mark.parametrize("change", ["delete", "modify", "extra", "digest"])
def test_baseline_checks_real_files_and_its_manifest_digest(tmp_path: Path, change: str) -> None:
    """报告声明仍相同也必须拒绝被删、被改、额外文件或自相矛盾的摘要。"""
    candidate = _file_report(tmp_path / "candidate")
    baseline = _file_report(tmp_path / "baseline")
    last = tmp_path / "baseline/sample/timing/data/images/page_0_image_2.png"
    if change == "delete":
        last.unlink()
    elif change == "modify":
        last.write_bytes(b"x" * 128)
    elif change == "extra":
        (last.parent / "unexpected.png").write_bytes(b"extra")
    else:
        # 两份记录使用同一错误摘要时，旧的报告间相等比较仍会接受。
        baseline["records"][0]["output_sha256"] = candidate["records"][0]["output_sha256"] = "0" * 64
    with pytest.raises(AssertionError, match="differs"):
        export_files.compare_baseline(
            candidate, baseline, report_root=tmp_path / "candidate", baseline_root=tmp_path / "baseline"
        )


@pytest.mark.parametrize("change", ["delete", "modify", "digest"])
def test_controller_rejects_corrupt_overwrite_seed(tmp_path: Path, change: str) -> None:
    """overwrite 的计时种子在启动 RSS 子进程前必须校验，后续重写不能掩盖损坏。"""
    report = _file_report(tmp_path)
    record = report["records"][0]
    record["config"] = {"mode": "overwrite"}
    timing = tmp_path / "sample/timing/result.json"
    export_files.write_json(timing, record)
    last = timing.parent / "data/images/page_0_image_2.png"
    if change == "delete":
        last.unlink()
    elif change == "modify":
        last.write_bytes(b"x" * 128)
    else:
        record["output_sha256"] = "0" * 64
        export_files.write_json(timing, record)
    with pytest.raises(AssertionError, match="differs"):
        export_files.validate_timing_seed(timing, report_root=tmp_path)


@pytest.mark.parametrize("artifact", ["../outside", "/absolute/data", "C:/outside/data", "sample/../../outside"])
def test_artifact_paths_cannot_escape_report(tmp_path: Path, artifact: str) -> None:
    """产物相对路径不得通过父级、绝对路径或 Windows 盘符跳出报告根目录。"""
    row = _file_report(tmp_path)["records"][0]
    with pytest.raises(ValueError, match="safe relative"):
        export_files.validate_artifact_files(tmp_path, artifact, row["output"], row["output_sha256"])


@pytest.mark.skipif(sys.platform == "win32", reason="创建符号链接可能需要额外 Windows 权限")
@pytest.mark.parametrize("kind", ["file", "directory", "seed_parent"])
def test_artifact_validation_rejects_symlinks(tmp_path: Path, kind: str) -> None:
    """拒绝文件、目录和计时种子祖先的符号链接，即使链接目标字节完全一致。"""
    row = _file_report(tmp_path)["records"][0]
    data = tmp_path / row["artifact"]
    if kind == "file":
        target = data / "images/page_0_image_2.png"
        external = tmp_path / "external.png"
    elif kind == "directory":
        target = data / "images"
        external = tmp_path / "external_images"
    else:
        export_files.write_json(tmp_path / "sample/timing/result.json", row)
        target = tmp_path / "sample"
        external = tmp_path / "external_sample"
    target.rename(external)
    target.symlink_to(external, target_is_directory=kind != "file")
    with pytest.raises(ValueError, match="symlink"):
        if kind == "seed_parent":
            export_files.validate_timing_seed(tmp_path / "sample/timing/result.json", report_root=tmp_path)
        else:
            export_files.validate_artifact_files(tmp_path, row["artifact"], row["output"], row["output_sha256"])


@pytest.mark.parametrize(
    "field,value", [("median_seconds", 0), ("median_seconds", float("nan")), ("RSS", -1), ("RSS", float("inf")), ("RSS", True)]
)
def test_baseline_rejects_invalid_measurement_values(tmp_path: Path, field: str, value: float) -> None:
    """损坏指标不能生成零除、NaN 或无穷性能比率。"""
    report = _file_report(tmp_path)
    baseline = deepcopy(report)
    row = baseline["records"][0]
    if field == "RSS":
        row["memory"]["parent_peak_rss_bytes"] = value
    else:
        row[field] = value
    with pytest.raises(ValueError, match="positive finite"):
        export_files.compare_baseline(report, baseline, report_root=tmp_path, baseline_root=tmp_path)


@pytest.mark.parametrize("argument", ["--runs=0", "--files=0", "--payload-bytes=1"])
def test_export_benchmark_rejects_invalid_bounds(tmp_path: Path, argument: str) -> None:
    """拒绝无效规模，避免把空计时、空文件或非目标载荷作为有效基线。"""
    command = [sys.executable, str(Path(export_files.__file__)), "--output", str(tmp_path / "result"), argument]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode != 0
    assert not (tmp_path / "result").exists()
