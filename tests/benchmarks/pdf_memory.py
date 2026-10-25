"""独立进程采样入口 RSS，避免完整 JSON 校验的大块临时分配污染内存门禁。"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import psutil

from rust_pdf import digest, environment_identity, public_output, public_parse_options, shared_once, source_identity, write_json
from pipeline import MemorySampler


NATIVE_PAIRS = {"rust-revision": ("rust-reference", "rust-current")}
_IDENTITY_FIELDS = (
    "path",
    "source_sha256",
    "source_package",
    "source_commit",
    "source_code_sha256",
    "suite",
    "region_input_sha256",
    "environment",
)
_COMPUTE_FIELDS = ("backend", "extension", "extension_sha256", "protocol")


def validate_measurements(records: dict) -> None:
    """拒绝空测量、缺失原生标签及遗留后端，避免不完整报告被误判为通过。"""
    required = {label for pair in NATIVE_PAIRS.values() for label in pair}
    if not records or set(records) != required:
        raise ValueError(f"Native measurement labels must be {sorted(required)}; received {sorted(records)}")


def _require_identity(record: dict) -> None:
    """要求报告显式携带可审计身份；旧报告缺字段时不能静默复用。"""
    missing = [field for field in (*_IDENTITY_FIELDS, "compute") if field not in record]
    if missing:
        raise ValueError(f"Benchmark identity is missing fields: {missing}")
    compute = record["compute"]
    if not isinstance(compute, dict) or any(field not in compute or compute[field] is None for field in _COMPUTE_FIELDS):
        raise ValueError(f"Benchmark compute identity must contain {_COMPUTE_FIELDS}")
    if compute["backend"] != "rust":
        raise ValueError("Only native benchmark records are supported")
    if record["suite"] not in {"public", "shared"}:
        raise ValueError(f"Unsupported benchmark suite: {record['suite']}")
    if record["suite"] == "shared" and not record["region_input_sha256"]:
        raise ValueError("Shared benchmark identity requires region_input_sha256")


def validate_identity(record: dict, expected: dict) -> None:
    """比较输入、源码、原生二进制和区域身份，不把运行计数等动态字段当成构建身份。"""
    _require_identity(record)
    _require_identity(expected)
    for field, default in (("file_suffix", "pdf"), ("source_context", None)):
        if record.get(field, default) != expected.get(field, default):
            raise ValueError(f"Benchmark identity mismatch: {field}")
    for field in _IDENTITY_FIELDS:
        actual, wanted = record[field], expected[field]
        if field in {"path", "source_package"}:
            actual, wanted = Path(actual).resolve(), Path(wanted).resolve()
        if actual != wanted:
            raise ValueError(f"Benchmark identity mismatch: {field}")
    for field in _COMPUTE_FIELDS:
        actual, wanted = record["compute"][field], expected["compute"][field]
        if field == "extension":
            actual, wanted = Path(actual).resolve(), Path(wanted).resolve()
        if actual != wanted:
            raise ValueError(f"Benchmark identity mismatch: compute.{field}")


def runtime_identity(package: Path) -> dict:
    """用独立解释器探测选定源码实际加载的扩展，避免父进程导入缓存遮蔽二进制替换。"""
    package = package.resolve()
    code = (
        "import json, pathlib, sys; "
        "sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parents[1])); "
        "import docvortex; from docvortex._compute_backend import backend_info; "
        "sys.path.insert(1, sys.argv[2]); from rust_pdf import environment_identity; "
        "print(json.dumps({'source_package': str(pathlib.Path(docvortex.__file__).resolve()), "
        "'compute': backend_info(), 'environment': environment_identity()}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(package), str(Path(__file__).resolve().parent)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    identity = json.loads(completed.stdout)
    if Path(identity["source_package"]).resolve() != package:
        raise ValueError("Benchmark identity probe imported a different source_package")
    return {**identity, **source_identity(package)}


def region_model(source_sha256: str, flash_baseline: Path | None) -> list:
    """从冻结区域清单按源字节身份精确选中一份模型，缺失或重复输入均失败。"""
    if flash_baseline is None:
        raise ValueError("Shared benchmarks require a frozen flash_baseline")
    report = json.loads((flash_baseline / "report.json").read_text(encoding="utf-8"))
    entries = [item for item in report["documents"] if item["source_sha256"] == source_sha256]
    if len(entries) != 1:
        raise ValueError("Source PDF must match exactly one frozen Flash baseline record")
    return json.loads((flash_baseline / entries[0]["artifact"] / "output.json").read_text(encoding="utf-8"))["model_list"]


def benchmark_identity(
    path: Path,
    package: Path,
    suite: str,
    flash_baseline: Path | None,
    *,
    file_suffix: str = "pdf",
    source_context: dict | None = None,
) -> dict:
    """重新读取当前输入和运行身份，已存在报告也必须通过同一恢复校验。"""
    source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        **runtime_identity(package),
        "path": str(path.resolve()),
        "source_sha256": source_sha256,
        "suite": suite,
        "file_suffix": file_suffix,
        "source_context": source_context,
        "region_input_sha256": digest(region_model(source_sha256, flash_baseline)) if suite == "shared" else None,
    }


def peak_rss(record: dict) -> float:
    """拒绝缺失、零值或非有限 RSS，避免空采样和损坏报告产生虚假比例。"""
    value = record.get("memory", {}).get("sampled_tree_peak_rss_bytes")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Benchmark memory requires positive finite sampled_tree_peak_rss_bytes")
    return value


class EntryMemorySampler(MemorySampler):
    """读取完整后代树，包含渲染 worker、forkserver 和资源跟踪进程。"""

    def __init__(self) -> None:
        """在采样前准备查询对象；采样失败必须传播，不能产生虚假的低峰值。"""
        self.parent = psutil.Process()
        self.error = None
        super().__init__()

    def collect(self) -> None:
        """直接读取进程 RSS，避免把轮询命令自身算入被测进程树。"""
        try:
            while not self.stop.is_set():
                values = {}
                for process in [self.parent, *self.parent.children(recursive=True)]:
                    try:
                        values[process.pid] = process.memory_info().rss
                    except psutil.NoSuchProcess:
                        continue
                for pid, value in values.items():
                    self.peaks[pid] = max(value, self.peaks.get(pid, 0))
                self.total_peak = max(self.total_peak, sum(values.values()))
                self.samples += 1
                self.stop.wait(0.05)
        except Exception as error:
            self.error = error
            self.stop.set()

    def finish(self) -> dict:
        """拒绝采样异常或空记录，保持内存门禁失败可见。"""
        result = super().finish()
        if self.error is not None:
            raise RuntimeError("Process-tree RSS sampling failed") from self.error
        if not self.samples:
            raise RuntimeError("Process-tree RSS sampling produced no observations")
        return result


def memory_worker(config: dict, folder: Path) -> None:
    """只预热实际入口，在采样停止后序列化并与原计时输出做完整差分。"""
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    _require_identity(config)
    os.environ["LOGURU_LEVEL"] = "WARNING"
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    import docvortex
    from docvortex._compute_backend import backend_info
    from loguru import logger

    logger.disable("docvortex")
    identity = source_identity(Path(docvortex.__file__))
    path = Path(config["path"])
    payload = path.read_bytes()
    source_sha256 = hashlib.sha256(payload).hexdigest()
    if path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(payload))
    model = None
    if config["suite"] == "shared":
        model = region_model(source_sha256, Path(config["flash_baseline"]) if config.get("flash_baseline") else None)
    current_identity = {
        **identity,
        "path": str(path.resolve()),
        "source_sha256": source_sha256,
        "source_package": str(Path(docvortex.__file__).resolve()),
        "compute": backend_info(),
        "environment": environment_identity(),
        "suite": config["suite"],
        "file_suffix": config.get("file_suffix", "pdf"),
        "source_context": config.get("source_context"),
        "region_input_sha256": digest(model) if model is not None else None,
    }
    validate_identity(config, current_identity)
    folder.mkdir(parents=True, exist_ok=False)
    options = public_parse_options(config.get("file_suffix", "pdf"), config.get("source_context"))
    if model is None:
        warmed = docvortex.parse(payload, **options)
    else:
        warmed = shared_once(payload, model)
    del warmed
    gc.collect()
    sampler = EntryMemorySampler()
    sampler.thread.start()
    try:
        result = docvortex.parse(payload, **options) if model is None else shared_once(payload, model)
    finally:
        memory = sampler.finish()
    if model is None:
        output = public_output(result)
    else:
        output = result[1]
    actual = digest(output)
    record = {
        **current_identity,
        "method": "isolated-entry-after-one-warmup",
        "tree_scope": "all_descendants_including_render_helpers",
        "capture_scope": "parse_before_serialization"
        if model is None
        else "shared_entries_with_evidence_snapshots_before_json",
        "suite": config["suite"],
        "onnxruntime_telemetry": "disabled_before_import",
        "memory": memory,
        "full_output_sha256": actual,
        "equal": actual == config["full_output_sha256"],
        "source_package": str(Path(docvortex.__file__).resolve()),
        "compute": backend_info(),
        **identity,
    }
    write_json(folder / "result.json", record)
    if not record["equal"]:
        write_json(folder / "output.json", output)
        raise AssertionError("Memory replay differs from full timed output")


def measure(config: dict, folder: Path) -> dict:
    """结束上一独立进程后再采样下一组，已完成审计可恢复而不重跑计时。"""
    _require_identity(config)
    current = benchmark_identity(
        Path(config["path"]),
        Path(config["source_package"]),
        config["suite"],
        Path(config["flash_baseline"]) if config.get("flash_baseline") else None,
        file_suffix=config.get("file_suffix", "pdf"),
        source_context=config.get("source_context"),
    )
    validate_identity(config, current)
    if not (folder / "result.json").exists():
        config_path = folder.with_suffix(".config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(config_path, config)
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(config_path), "--output", str(folder)],
            check=True,
            env=dict(os.environ, PYTHONPATH=str(Path(config["source_package"]).parents[1]), ORT_DISABLE_TELEMETRY="1"),
        )
    record = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    validate_identity(record, current)
    if record.get("method") != "isolated-entry-after-one-warmup":
        raise ValueError("Memory replay method differs from isolated-entry-after-one-warmup")
    if record.get("equal") is not True or record.get("full_output_sha256") != config["full_output_sha256"]:
        raise ValueError("Memory replay differs from full timed output")
    peak_rss(record)
    return record


def ratios(records: dict) -> dict:
    """只比较两个原生版本的进程树 RSS，比较定义同时供反序复测使用。"""
    validate_measurements(records)
    return {name: peak_rss(records[new]) / peak_rss(records[old]) for name, (old, new) in NATIVE_PAIRS.items()}


def main() -> None:
    """复用已完成计时的源码及输入身份，统一重测 RSS，并反序确认超过 5% 的增长。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timing-report", type=Path)
    parser.add_argument("--flash-baseline", type=Path)
    parser.add_argument("--path", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        memory_worker(json.loads(args.worker.read_text(encoding="utf-8")), args.output)
        return
    if args.timing_report is None:
        parser.error("--timing-report is required outside worker mode")
    timed = json.loads(args.timing_report.read_text(encoding="utf-8"))
    if timed.get("suite") == "shared" and args.flash_baseline is None:
        parser.error("shared reports require --flash-baseline")
    selected = [
        (index, item)
        for index, item in enumerate(timed["documents"])
        if args.path is None or Path(item["path"]).resolve() == args.path.resolve()
    ]
    if not selected:
        parser.error("timing report contains no selected documents")
    report = {"timing_report": str(args.timing_report.resolve()), "method": "isolated-entry-after-one-warmup", "documents": []}
    args.output.mkdir(parents=True, exist_ok=True)
    for index, item in selected:
        validate_measurements(item["measurements"])
        if any(Path(record["path"]).resolve() != Path(item["path"]).resolve() for record in item["measurements"].values()):
            raise ValueError("Timing report document path differs from its measurements")
        folder = args.output / f"{index:02d}-{Path(item['path']).stem}"
        configs = {
            label: {
                **record,
                "suite": timed["suite"],
                "flash_baseline": str(args.flash_baseline.resolve()) if args.flash_baseline else None,
            }
            for label, record in item["measurements"].items()
        }
        measurements = {label: measure(config, folder / label) for label, config in configs.items()}
        compared = ratios(measurements)
        record = {"path": item["path"], "measurements": measurements, "ratios": compared}
        triggered = [name for name, ratio in compared.items() if ratio > 1.05]
        if triggered:
            required = {label for name in triggered for label in NATIVE_PAIRS[name]}
            repeated = {
                label: measure(configs[label], folder / f"repeat-{label}") for label in reversed(configs) if label in required
            }
            record["repeat_measurements"] = repeated
            record["repeat_ratios"] = ratios({**measurements, **repeated})
            record["persistent_regression"] = any(record["repeat_ratios"][name] > 1.05 for name in triggered)
        report["documents"].append(record)
        write_json(args.output / "progress.json", report)
        print(Path(item["path"]).name, compared, "persistent", record.get("persistent_regression", False), flush=True)
    write_json(args.output / "report.json", report)
    if any(item.get("persistent_regression", False) for item in report["documents"]):
        raise SystemExit("Persistent isolated RSS regression exceeds 5%")


if __name__ == "__main__":
    main()
