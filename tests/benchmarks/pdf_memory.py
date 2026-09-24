"""独立进程采样入口 RSS，避免完整 JSON 校验的大块临时分配污染内存门禁。"""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import psutil

from rust_pdf import digest, shared_once, source_identity, write_json
from pipeline import MemorySampler


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
    os.environ["DOCVORTEX_COMPUTE_BACKEND"] = config["compute"]["backend"]
    os.environ["LOGURU_LEVEL"] = "WARNING"
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    import docvortex
    from docvortex._compute_backend import backend_info
    from loguru import logger

    logger.disable("docvortex")
    identity = source_identity(Path(docvortex.__file__))
    assert identity["source_code_sha256"] == config["source_code_sha256"]
    assert backend_info()["extension_sha256"] == config["compute"]["extension_sha256"]
    path = Path(config["path"])
    payload = path.read_bytes()
    import hashlib

    assert hashlib.sha256(payload).hexdigest() == config["source_sha256"]
    if path.suffix == ".xor":
        key = b"MinerU flash layout fixture"
        payload = bytes(value ^ key[index % len(key)] for index, value in enumerate(payload))
    model = None
    if config["suite"] == "shared":
        baseline = Path(config["flash_baseline"])
        entry = next(
            item
            for item in json.loads((baseline / "report.json").read_text())["documents"]
            if item["source_sha256"] == config["source_sha256"]
        )
        model = json.loads((baseline / entry["artifact"] / "output.json").read_text())["model_list"]
        assert digest(model) == config["region_input_sha256"]
    folder.mkdir(parents=True, exist_ok=False)
    if model is None:
        warmed = docvortex.parse(payload, file_suffix="pdf", keep_model_json=True)
    else:
        warmed = shared_once(payload, model)
    del warmed
    gc.collect()
    sampler = EntryMemorySampler()
    sampler.thread.start()
    try:
        result = (
            docvortex.parse(payload, file_suffix="pdf", keep_model_json=True) if model is None else shared_once(payload, model)
        )
    finally:
        memory = sampler.finish()
    if model is None:
        from dataclasses import asdict

        output = {
            "model": result.model_json.model_dump(mode="json"),
            "middle": result.middle_json.model_dump(mode="json"),
            "assets": {key: hashlib.sha256(value).hexdigest() for key, value in result.assets.items()},
            "diagnostics": [asdict(item) for item in result.diagnostics],
        }
    else:
        output = result[1]
    actual = digest(output)
    record = {
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
    if not (folder / "result.json").exists():
        config_path = folder.with_suffix(".config.json")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(config_path, config)
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(config_path), "--output", str(folder)],
            check=True,
            env=dict(os.environ, PYTHONPATH=str(Path(config["source_package"]).parents[1]), ORT_DISABLE_TELEMETRY="1"),
        )
    record = json.loads((folder / "result.json").read_text())
    assert record["equal"] and record["source_code_sha256"] == config["source_code_sha256"]
    assert record["full_output_sha256"] == config["full_output_sha256"]
    return record


def ratios(records: dict) -> dict:
    """按同一时刻的进程树 RSS 比较三组关系，不将各进程独立峰值直接相加。"""
    pairs = {
        "backends": ("python-current", "rust-current"),
        "rust-revision": ("rust-reference", "rust-current"),
        "python-revision": ("python-reference", "python-current"),
    }
    return {
        name: records[new]["memory"]["sampled_tree_peak_rss_bytes"] / records[old]["memory"]["sampled_tree_peak_rss_bytes"]
        for name, (old, new) in pairs.items()
    }


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
        memory_worker(json.loads(args.worker.read_text()), args.output)
        return
    timed = json.loads(args.timing_report.read_text())
    report = {"timing_report": str(args.timing_report.resolve()), "method": "isolated-entry-after-one-warmup", "documents": []}
    args.output.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(timed["documents"]):
        if args.path and Path(item["path"]).resolve() != args.path.resolve():
            continue
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
        pairs = {
            "backends": ("python-current", "rust-current"),
            "rust-revision": ("rust-reference", "rust-current"),
            "python-revision": ("python-reference", "python-current"),
        }
        triggered = [name for name, ratio in compared.items() if ratio > 1.05]
        if triggered:
            required = {label for name in triggered for label in pairs[name]}
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
