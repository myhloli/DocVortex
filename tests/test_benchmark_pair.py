"""验证配对基准完整输出、输入与实际运行身份的门禁及失败证据。"""

from __future__ import annotations

from copy import deepcopy
import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pair(monkeypatch):
    """独立载入工具模块，并为已有基准辅助模块提供测试导入路径。"""
    from loguru import logger

    def preserve_logging(_name):
        """测试直接调用子进程入口时，阻止其关闭整个 pytest 进程的日志。"""

    monkeypatch.setattr(logger, "disable", preserve_logging)
    monkeypatch.setenv("LOGURU_LEVEL", os.environ.get("LOGURU_LEVEL", "DEBUG"))
    monkeypatch.syspath_prepend(str(ROOT / "tests" / "benchmarks"))
    spec = importlib.util.spec_from_file_location("benchmark_native_pair", ROOT / "tools" / "benchmark_native_pair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evidence(pair, tmp_path):
    """建立两个各自身份独立但公开输出相同的最小可审阅运行记录。"""
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"%PDF fixture")
    manifest = pair._input_manifest(tmp_path)
    helpers = pair._helpers()
    sources = {
        kind: {
            "source_root": f"/{kind}",
            "source_package": f"/{kind}/src/docvortex/__init__.py",
            "source_code_sha256": kind,
            "source_commit": kind,
            "extension": f"/{kind}/src/docvortex/_native.so",
            "extension_sha256": f"native-{kind}",
        }
        for kind in ("before", "after")
    }
    output = {
        "model": {"pages": [{"text": "same"}]},
        "middle": {"pages": [{"text": "same"}]},
        "assets": {"images/one.png": "image-digest"},
        "diagnostics": [],
    }
    runs = {"before": [], "after": []}
    for kind in runs:
        artifact = f"{kind}-output.json"
        pair._write_json(tmp_path / artifact, output)
        runs[kind].append(
            {
                **sources[kind],
                "status": "complete",
                "inputs": deepcopy(manifest),
                "python": sys.executable,
                "environment": {"dependencies": {"pydantic": "fixed"}},
                "compute": {
                    "backend": "rust",
                    "protocol": 36,
                    "extension": sources[kind]["extension"],
                    "extension_sha256": sources[kind]["extension_sha256"],
                },
                "seconds": 1.0,
                "peak_tree_rss_bytes": 100,
                "documents": [
                    {**manifest[0], "seconds": 1.0, "artifact": artifact, "full_output_sha256": helpers.digest(output)}
                ],
            }
        )
    return SimpleNamespace(folder=tmp_path, manifest=manifest, helpers=helpers, sources=sources, runs=runs, output=output)


def test_pair_accepts_stable_complete_outputs_with_different_builds(pair, evidence):
    """不同源码和原生二进制可比较，但同一源码各轮及跨版本输出必须完整相等。"""
    for records in evidence.runs.values():
        records.append(deepcopy(records[0]))
    pair._validate_runs(evidence.runs, evidence.manifest, evidence.sources, evidence.folder, evidence.helpers)


@pytest.mark.parametrize("field", ["model", "middle", "assets", "diagnostics"])
def test_pair_rejects_stable_cross_version_difference(pair, evidence, field):
    """任一公开输出分量改变，即使候选各轮自身稳定也必须拒绝。"""
    changed = deepcopy(evidence.output)
    changed[field] = [{"changed": True}] if field == "diagnostics" else {"changed": True}
    document = evidence.runs["after"][0]["documents"][0]
    pair._write_json(evidence.folder / document["artifact"], changed)
    document["full_output_sha256"] = evidence.helpers.digest(changed)
    for records in evidence.runs.values():
        records.append(deepcopy(records[0]))
    with pytest.raises(AssertionError, match="Full public output differs"):
        pair._validate_runs(evidence.runs, evidence.manifest, evidence.sources, evidence.folder, evidence.helpers)


def test_pair_rejects_same_version_instability(pair, evidence):
    """基线自身第二轮发生变化时，跨版本恰好相同也不能掩盖不稳定。"""
    later = deepcopy(evidence.runs["before"][0])
    changed = deepcopy(evidence.output)
    changed["middle"]["pages"].append({"text": "unstable"})
    later["documents"][0].update(artifact="later.json", full_output_sha256=evidence.helpers.digest(changed))
    pair._write_json(evidence.folder / "later.json", changed)
    evidence.runs["before"].append(later)
    with pytest.raises(AssertionError, match="Full public output differs"):
        pair._validate_runs(evidence.runs, evidence.manifest, evidence.sources, evidence.folder, evidence.helpers)


@pytest.mark.parametrize(
    "change", ["collection", "input_hash", "source", "extension", "loaded_extension", "python", "environment"]
)
def test_pair_rejects_identity_drift(pair, evidence, change):
    """相同输出不能掩盖输入集合、文件内容、源码、扩展或解释器身份漂移。"""
    record = evidence.runs["after"][0]
    if change == "collection":
        record["documents"] = []
    elif change == "input_hash":
        record["documents"][0]["sha256"] = "different"
    elif change == "source":
        record["source_code_sha256"] = "different"
    elif change == "extension":
        record["extension_sha256"] = "different"
    elif change == "loaded_extension":
        record["compute"]["extension_sha256"] = "different"
    elif change == "python":
        record["python"] = "/different/python"
    else:
        record["environment"]["dependencies"]["pydantic"] = "changed"
    with pytest.raises(AssertionError):
        pair._validate_runs(evidence.runs, evidence.manifest, evidence.sources, evidence.folder, evidence.helpers)


def test_pair_rejects_artifact_tampering(pair, evidence):
    """落盘证据发生变化时不能只信任旧摘要。"""
    pair._write_json(evidence.folder / "after-output.json", {**evidence.output, "diagnostics": ["changed"]})
    with pytest.raises(AssertionError, match="artifact differs"):
        pair._validate_runs(evidence.runs, evidence.manifest, evidence.sources, evidence.folder, evidence.helpers)


def test_source_snapshot_requires_local_extension(pair, tmp_path):
    """冻结源码必须自带扩展，不能悄悄使用运行环境已安装的二进制。"""
    package = tmp_path / "src/docvortex"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="Native extension is missing"):
        pair._source_snapshot(tmp_path, pair._helpers())


def test_source_snapshot_rejects_extension_outside_selected_source(pair, tmp_path):
    """源码中的扩展软链指向另一构建目录时不能被视为本次冻结二进制。"""
    root = tmp_path / "source"
    package = root / "src/docvortex"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    binary = tmp_path / "external-native.so"
    binary.write_bytes(b"native fixture")
    (package / f"_native{importlib.machinery.EXTENSION_SUFFIXES[0]}").symlink_to(binary)
    with pytest.raises(ValueError, match="Native extension is outside"):
        pair._source_snapshot(root, pair._helpers())


@pytest.mark.parametrize("wrong", ["source_package", "extension", "extension_sha256"])
def test_runtime_identity_checks_real_imports(pair, monkeypatch, evidence, wrong):
    """实际已导入模块或扩展与指定源码不符时立即失败。"""
    expected = evidence.sources["before"]
    package = SimpleNamespace(__file__=expected["source_package"])
    compute = deepcopy(evidence.runs["before"][0]["compute"])
    if wrong == "source_package":
        package.__file__ = "/installed/docvortex/__init__.py"
    else:
        compute[wrong] = "/installed/_native.so" if wrong == "extension" else "wrong"
    monkeypatch.setitem(sys.modules, "docvortex", package)
    monkeypatch.setitem(sys.modules, "docvortex._compute_backend", SimpleNamespace(backend_info=lambda: compute))
    helpers = SimpleNamespace(
        source_identity=lambda path: {key: expected[key] for key in ("source_commit", "source_code_sha256")}
    )
    with pytest.raises(AssertionError, match="Imported source or extension differs"):
        pair._runtime_identity(Path("/before"), expected, helpers)


@pytest.mark.parametrize(
    "field", ["source_sha256", "source_code_sha256", "full_output_sha256", "extension_sha256", "environment"]
)
def test_memory_replay_identity_is_not_optional(pair, field):
    """内存重放的输入、源码、扩展和公开输出必须与计时记录一致。"""
    config = {
        "path": "/a.pdf",
        "source_sha256": "input",
        "region_input_sha256": None,
        "suite": "public",
        "source_package": "/src/docvortex/__init__.py",
        "source_code_sha256": "code",
        "source_commit": "commit",
        "full_output_sha256": "output",
        "environment": {},
        "compute": {"backend": "rust", "protocol": 36, "extension": "/src/_native.so", "extension_sha256": "native"},
    }
    record = deepcopy(config)
    record.update(equal=True, memory={"sampled_tree_peak_rss_bytes": 10})
    if field == "extension_sha256":
        record["compute"][field] = "wrong"
    else:
        record[field] = "wrong"
    with pytest.raises((AssertionError, ValueError), match="[Mm]ismatch|differs"):
        pair._check_memory(record, config)


@pytest.mark.parametrize("memory_differs", [False, True])
def test_worker_separates_memory_replay_and_preserves_full_output(pair, monkeypatch, evidence, memory_differs):
    """计时输出先落盘，独立内存进程收到同一身份；内存不等价时完整证据仍保留。"""
    monkeypatch.setattr(sys, "path", list(sys.path))
    identity = {
        key: value
        for key, value in evidence.runs["before"][0].items()
        if key not in {"status", "inputs", "seconds", "peak_tree_rss_bytes", "documents"}
    }
    events = []

    def parse(payload, **kwargs):
        """记录不涉及输出序列化的预热调用。"""
        events.append("warmup")
        assert payload == Path(evidence.manifest[0]["path"]).read_bytes()

    def public_once(payload):
        """返回既有完整输出协议，模拟正式计时与序列化已经分开的入口。"""
        events.append("public_once")
        return {"parse": 0.1}, deepcopy(evidence.output)

    def memory_worker(command, folder, *, env=None):
        """模拟独立 RSS 子进程并检查落盘证据先于内存重放。"""
        events.append("memory_subprocess")
        assert json.loads((folder / "output.json").read_text(encoding="utf-8")) == evidence.output
        assert env["PYTHONPATH"] == str(args.source_root.resolve() / "src")
        config = json.loads(Path(command[command.index("--worker") + 1]).read_text(encoding="utf-8"))
        assert config["environment"] == identity["environment"]
        target = Path(command[command.index("--output") + 1])
        target.mkdir()
        result = {**config, "equal": not memory_differs, "memory": {"sampled_tree_peak_rss_bytes": 123}}
        pair._write_json(target / "result.json", result)

    monkeypatch.setitem(sys.modules, "docvortex", SimpleNamespace(parse=parse))
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(disable_telemetry_events=lambda: None))
    monkeypatch.setattr(pair, "_helpers", lambda: SimpleNamespace(public_once=public_once, digest=evidence.helpers.digest))
    monkeypatch.setattr(pair, "_runtime_identity", lambda root, expected, helpers: deepcopy(identity))
    monkeypatch.setattr(pair, "_source_snapshot", lambda root, helpers: evidence.sources["before"])
    monkeypatch.setattr(pair, "_run_command", memory_worker)
    config = evidence.folder / "config.json"
    pair._write_json(config, {"sources": evidence.sources, "inputs": evidence.manifest})
    output = evidence.folder / "worker.json"
    args = argparse.Namespace(
        source_root=Path("/before"), output=output, config=config, kind="before", input_dir=evidence.folder
    )
    if memory_differs:
        with pytest.raises(AssertionError, match="Memory replay differs"):
            pair._worker(args)
    else:
        pair._worker(args)
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["status"] == ("failed" if memory_differs else "complete")
    assert events == ["warmup", "public_once", "memory_subprocess"]
    assert json.loads((evidence.folder / record["documents"][0]["artifact"]).read_text(encoding="utf-8")) == evidence.output


@pytest.mark.parametrize(
    "rounds,empty,message",
    [(0, False, "rounds must be positive"), (-1, False, "rounds must be positive"), (1, True, "No PDF inputs")],
)
def test_cli_rejects_invalid_workload_with_failure_record(tmp_path, rounds, empty, message):
    """无效轮数或空输入以非零状态退出，并留下不含性能比例的失败报告。"""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    if not empty:
        (inputs / "sample.pdf").write_bytes(b"%PDF fixture")
    output = tmp_path / "report"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/benchmark_native_pair.py"),
            "--baseline-root",
            str(ROOT),
            "--candidate-root",
            str(ROOT),
            "--input-dir",
            str(inputs),
            "--output",
            str(output),
            "--rounds",
            str(rounds),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed" and message in report["error"]
    assert "time_ratio" not in report and "rss_ratio" not in report


def test_main_retains_different_outputs_without_performance_summary(pair, monkeypatch, evidence):
    """主入口遇到跨版本差异时保留双方完整证据，禁止落盘成功性能结论。"""
    output = evidence.folder / "report"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_native_pair",
            "--baseline-root",
            "/before",
            "--candidate-root",
            "/after",
            "--input-dir",
            str(evidence.folder),
            "--output",
            str(output),
            "--rounds",
            "2",
        ],
    )
    monkeypatch.setattr(pair, "_source_snapshot", lambda root, helpers, suffixes=None: evidence.sources[root.name])

    def fake_worker(command, folder, *, env=None):
        """模拟不同版本各自稳定的已完成子进程，真实执行主入口门禁与报告写入。"""
        kind = command[command.index("--kind") + 1]
        path = Path(command[command.index("--output") + 1])
        record = deepcopy(evidence.runs[kind][0])
        changed = deepcopy(evidence.output)
        if kind == "after":
            changed["middle"]["pages"].append({"text": "candidate-only"})
        artifact = f"{kind}-output.json"
        pair._write_json(output / artifact, changed)
        record["documents"][0].update(artifact=artifact, full_output_sha256=evidence.helpers.digest(changed))
        pair._write_json(path, record)

    monkeypatch.setattr(pair, "_run_command", fake_worker)
    with pytest.raises(AssertionError, match="Full public output differs"):
        pair.main()
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert "time_ratio" not in report and "rss_ratio" not in report
    assert (output / "before-output.json").is_file() and (output / "after-output.json").is_file()
