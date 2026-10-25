"""交替比较冻结源码；PDF 目录或多格式字节清单的完整输出与身份通过后才汇总性能。"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys


BENCHMARKS = Path(__file__).resolve().parents[1] / "tests" / "benchmarks"


def _helpers():
    """复用已有完整输出协议；调用方须先选择生产源码再进入此处。"""
    sys.path.insert(1, str(BENCHMARKS))
    import rust_pdf

    return rust_pdf


def _write_json(path: Path, value: object) -> None:
    """及时保存成功或失败证据，已完成的记录不会因后续异常丢失。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _input_manifest(folder: Path) -> list[dict]:
    """冻结有序 PDF 集合及原始字节身份，拒绝空集合。"""
    manifest = []
    for source in sorted(folder.glob("*.pdf")):
        if not source.is_file():
            raise ValueError(f"PDF input is not a file: {source}")
        payload = source.read_bytes()
        manifest.append(
            {
                "name": source.name,
                "path": str(source.resolve()),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if not manifest:
        raise ValueError(f"No PDF inputs: {folder}")
    return manifest


def _workload_manifest(args: argparse.Namespace) -> list[dict]:
    """兼容既有 PDF 目录，并让非 PDF 两版本共享显式冻结的字节和解析参数。"""
    manifest = getattr(args, "manifest", None)
    if manifest is not None:
        from prepare_nonpdf_corpus import load_frozen_manifest

        return load_frozen_manifest(manifest)
    return _input_manifest(args.input_dir)


def _source_snapshot(root: Path, helpers, extension_suffixes: list[str] | None = None) -> dict:
    """按 Python 扩展查找规则冻结指定源码和二进制，拒绝借用安装包或外部软链。"""
    root = root.resolve()
    package = root / "src" / "docvortex" / "__init__.py"
    if not package.is_file() or not package.resolve().is_relative_to(root):
        raise ValueError(f"Source package is missing or outside source root: {package}")
    finder = importlib.machinery.FileFinder(
        str(package.parent),
        (importlib.machinery.ExtensionFileLoader, extension_suffixes or importlib.machinery.EXTENSION_SUFFIXES),
    )
    spec = finder.find_spec("docvortex._native")
    if spec is None or spec.origin is None:
        raise ValueError(f"Native extension is missing from source root: {root}")
    extension = Path(spec.origin).resolve()
    if not extension.is_relative_to(root):
        raise ValueError(f"Native extension is outside source root: {extension}")
    return {
        "source_root": str(root),
        "source_package": str(package.resolve()),
        **helpers.source_identity(package),
        "extension": str(extension),
        "extension_sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
    }


def _runtime_identity(root: Path, expected: dict, helpers) -> dict:
    """核对实际导入路径、源码摘要与原生扩展，避免环境串用形成错误结论。"""
    import docvortex
    from docvortex._compute_backend import backend_info

    package = Path(docvortex.__file__).resolve()
    compute = backend_info()
    actual = {
        "source_root": str(root.resolve()),
        "source_package": str(package),
        **helpers.source_identity(package),
        "extension": str(Path(compute["extension"]).resolve()),
        "extension_sha256": compute["extension_sha256"],
    }
    if actual != expected:
        raise AssertionError(f"Imported source or extension differs: expected={expected!r}, actual={actual!r}")
    if compute["backend"] != "rust" or not isinstance(compute.get("protocol"), int):
        raise AssertionError(f"Invalid native backend identity: {compute!r}")
    return {
        **actual,
        "compute": compute,
        "python": str(Path(sys.executable).resolve()),
        "environment": helpers.environment_identity(),
    }


def _run_command(command: list[str], folder: Path, *, env: dict | None = None) -> None:
    """保留子进程日志，失败时由调用方记录报告并以非零状态退出。"""
    with (folder / "stdout.log").open("w", encoding="utf-8") as stdout:
        with (folder / "stderr.log").open("w", encoding="utf-8") as stderr:
            subprocess.run(command, env=env, stdout=stdout, stderr=stderr, check=True)


def _check_memory(record: dict, config: dict) -> None:
    """独立内存重放必须与计时输出和全部身份一致。"""
    from pdf_memory import peak_rss, validate_identity

    validate_identity(record, config)
    peak_rss(record)
    if record.get("equal") is not True or record["full_output_sha256"] != config["full_output_sha256"]:
        raise AssertionError("Memory replay differs in full public output")


def _worker(args: argparse.Namespace) -> None:
    """分离计时和独立 RSS 进程；逐文档保留完整公开输出及失败进度。"""
    sys.path.insert(0, str(args.source_root.resolve() / "src"))
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    os.environ["LOGURU_LEVEL"] = "WARNING"
    evidence = args.output.with_suffix("")
    evidence.mkdir(parents=True, exist_ok=False)
    record = {"status": "starting", "documents": []}
    try:
        helpers = _helpers()
        config = json.loads(args.config.read_text(encoding="utf-8"))
        expected = config["sources"][args.kind]
        manifest = config["inputs"]
        if _workload_manifest(args) != manifest:
            raise AssertionError("Input collection or bytes changed before worker")
        record.update(_runtime_identity(args.source_root, expected, helpers), inputs=manifest)
        _write_json(args.output, record)
        import onnxruntime
        from docvortex import parse
        from loguru import logger

        onnxruntime.disable_telemetry_events()
        logger.disable("docvortex")
        warmup_indices = (
            list(range(len(manifest)))
            if getattr(args, "manifest", None) is not None
            else sorted({0, len(manifest) // 4, len(manifest) // 2, 3 * len(manifest) // 4, len(manifest) - 1})
        )
        for index in warmup_indices:
            item = manifest[index]
            options = (
                helpers.public_parse_options(item["file_suffix"], item.get("source_context"))
                if "file_suffix" in item
                else {"file_suffix": "pdf", "keep_model_json": True}
            )
            parse(Path(item["path"]).read_bytes(), **options)
        record.update(status="timing", warmup_documents=len(warmup_indices))
        for index, source in enumerate(manifest):
            payload = Path(source["path"]).read_bytes()
            if hashlib.sha256(payload).hexdigest() != source["sha256"]:
                raise AssertionError(f"Document input changed: {source['path']}")
            gc.collect()
            durations, output = (
                helpers.public_once(payload, file_suffix=source["file_suffix"], source_context=source.get("source_context"))
                if "file_suffix" in source
                else helpers.public_once(payload)
            )
            folder = evidence / f"{index:04d}"
            folder.mkdir()
            _write_json(folder / "output.json", output)
            record["documents"].append(
                {
                    **source,
                    "seconds": durations["parse"],
                    "full_output_sha256": helpers.digest(output),
                    "artifact": str((folder / "output.json").relative_to(args.output.parent)),
                }
            )
            del output
            _write_json(args.output, record)
        record.update(status="memory", seconds=sum(item["seconds"] for item in record["documents"]))
        _write_json(args.output, record)
        for index, item in enumerate(record["documents"]):
            folder = evidence / f"{index:04d}"
            memory_config = {
                **record,
                "path": item["path"],
                "source_sha256": item["sha256"],
                "region_input_sha256": None,
                "suite": "public",
                "flash_baseline": None,
                "full_output_sha256": item["full_output_sha256"],
                "file_suffix": item.get("file_suffix", "pdf"),
                "source_context": item.get("source_context"),
            }
            memory_config.pop("documents")
            _write_json(folder / "memory-input.json", memory_config)
            _run_command(
                [
                    sys.executable,
                    str(BENCHMARKS / "pdf_memory.py"),
                    "--worker",
                    str(folder / "memory-input.json"),
                    "--output",
                    str(folder / "memory"),
                ],
                folder,
                env={**os.environ, "PYTHONPATH": str(args.source_root.resolve() / "src")},
            )
            memory = json.loads((folder / "memory" / "result.json").read_text(encoding="utf-8"))
            _check_memory(memory, memory_config)
            item["memory"] = memory["memory"]
            item["memory_artifact"] = str((folder / "memory" / "result.json").relative_to(args.output.parent))
            _write_json(args.output, record)
        if _workload_manifest(args) != manifest or _source_snapshot(args.source_root, helpers) != expected:
            raise AssertionError("Inputs, source or extension changed during worker")
        record.update(
            status="complete",
            peak_tree_rss_bytes=max(item["memory"]["sampled_tree_peak_rss_bytes"] for item in record["documents"]),
            memory_method="maximum_of_isolated_document_replays_before_serialization",
        )
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _write_json(args.output, record)


def _validate_runs(runs: dict, manifest: list[dict], sources: dict, folder: Path, helpers) -> None:
    """同时阻止输入漂移、单版本不稳定及稳定但跨版本不等价的结果。"""
    expected_output = None
    expected_python = None
    expected_environment = None
    for kind, records in runs.items():
        expected_runtime = None
        for record in records:
            if record["status"] != "complete" or record["inputs"] != manifest:
                raise AssertionError(f"Incomplete run or different input collection: {kind}")
            if {key: record[key] for key in sources[kind]} != sources[kind]:
                raise AssertionError(f"Different source or extension identity: {kind}")
            if any(record["compute"].get(key) != sources[kind][key] for key in ("extension", "extension_sha256")):
                raise AssertionError(f"Different loaded extension identity: {kind}")
            if record["compute"]["backend"] != "rust" or not isinstance(record["compute"].get("protocol"), int):
                raise AssertionError(f"Invalid native backend identity: {kind}")
            if expected_python is not None and record["python"] != expected_python:
                raise AssertionError("Different Python runtime between versions")
            expected_python = record["python"]
            if expected_environment is not None and record["environment"] != expected_environment:
                raise AssertionError("Different environment within or between versions")
            expected_environment = record["environment"]
            runtime = (record["python"], record["compute"]["backend"], record["compute"]["protocol"])
            if expected_runtime is not None and runtime != expected_runtime:
                raise AssertionError(f"Runtime identity changed between rounds: {kind}")
            expected_runtime = runtime
            if len(record["documents"]) != len(manifest):
                raise AssertionError(f"Different document collection: {kind}")
            outputs = []
            for source, document in zip(manifest, record["documents"], strict=True):
                if {key: document[key] for key in source} != source:
                    raise AssertionError(f"Different document input: {kind}, {source['name']}")
                output = json.loads((folder / document["artifact"]).read_text(encoding="utf-8"))
                if set(output) != {"model", "middle", "assets", "diagnostics"}:
                    raise AssertionError(f"Incomplete public output: {kind}, {source['name']}")
                actual = helpers.digest(output)
                if actual != document["full_output_sha256"]:
                    raise AssertionError(f"Output artifact differs from recorded hash: {kind}, {source['name']}")
                outputs.append(actual)
            if expected_output is not None and outputs != expected_output:
                changed = [
                    source["name"] for source, old, new in zip(manifest, expected_output, outputs, strict=True) if old != new
                ]
                raise AssertionError(f"Full public output differs within or across versions: {kind}, {changed}")
            expected_output = outputs


def main() -> None:
    """交替完成 AB/BA 测量，全部门禁通过后才写性能比例，失败保留原始证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input-dir", type=Path)
    inputs.add_argument("--manifest", type=Path, help="已冻结的非 PDF 字节、后缀和来源上下文清单")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--kind", choices=("before", "after"), help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if args.source_root is None or args.config is None or args.kind is None:
            parser.error("worker requires --source-root, --config and --kind")
        _worker(args)
        return
    if args.baseline_root is None or args.candidate_root is None:
        parser.error("--baseline-root and --candidate-root are required")
    if args.output.exists():
        parser.error("output must not already exist")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    report = {
        "status": "running",
        "rounds": args.rounds,
        "order": "AB/BA alternating",
        "timing_scope": "public_parse_excluding_serialization",
        "memory_method": "maximum_of_isolated_document_replays_before_serialization",
        "runs": {"before": [], "after": []},
    }
    _write_json(args.output / "report.json", report)
    try:
        if args.rounds < 1:
            raise ValueError("rounds must be positive")
        helpers = _helpers()
        manifest = _workload_manifest(args)
        roots = {"before": args.baseline_root.resolve(), "after": args.candidate_root.resolve()}
        # --python 可以选择另一 ABI；父进程不能用自身的扩展后缀替代目标解释器。
        extension_suffixes = json.loads(
            subprocess.check_output(
                [
                    args.python,
                    "-c",
                    "import importlib.machinery,json; print(json.dumps(importlib.machinery.EXTENSION_SUFFIXES))",
                ],
                text=True,
            )
        )
        sources = {kind: _source_snapshot(root, helpers, extension_suffixes) for kind, root in roots.items()}
        config = {"inputs": manifest, "sources": sources}
        _write_json(args.output / "config.json", config)
        runs = {"before": [], "after": []}
        for round_index in range(args.rounds):
            for kind in ["before", "after"] if round_index % 2 == 0 else ["after", "before"]:
                run_name = f"{round_index + 1}-{kind}"
                result_path = args.output / f"{run_name}.json"
                logs = args.output / f"{run_name}-logs"
                logs.mkdir()
                report["runs"][kind].append(result_path.name)
                _write_json(args.output / "report.json", report)
                _run_command(
                    [
                        args.python,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--source-root",
                        str(roots[kind]),
                        "--kind",
                        kind,
                        "--config",
                        str(args.output / "config.json"),
                        *(
                            ["--manifest", str(args.manifest.resolve())]
                            if args.manifest is not None
                            else ["--input-dir", str(args.input_dir.resolve())]
                        ),
                        "--output",
                        str(result_path),
                    ],
                    logs,
                    env={**os.environ, "PYTHONPATH": str(roots[kind] / "src")},
                )
                runs[kind].append(json.loads(result_path.read_text(encoding="utf-8")))
                _validate_runs(runs, manifest, sources, args.output, helpers)
                print(f"{run_name}: evidence recorded", flush=True)
        if _workload_manifest(args) != manifest:
            raise AssertionError("Input collection or bytes changed during benchmark")
        for kind, root in roots.items():
            if _source_snapshot(root, helpers, extension_suffixes) != sources[kind]:
                raise AssertionError(f"Source or native extension changed during benchmark: {kind}")
        summary = {}
        for kind, records in runs.items():
            summary[kind] = {
                "median_seconds": statistics.median(record["seconds"] for record in records),
                "median_peak_tree_rss_bytes": statistics.median(record["peak_tree_rss_bytes"] for record in records),
            }
        if any(value <= 0 for entry in summary.values() for value in entry.values()):
            raise AssertionError("Timing and RSS measurements must be positive")
        documents = []
        for index, item in enumerate(manifest):
            medians = {
                kind: {
                    "seconds": statistics.median(record["documents"][index]["seconds"] for record in records),
                    "peak_tree_rss_bytes": statistics.median(
                        record["documents"][index]["memory"]["sampled_tree_peak_rss_bytes"] for record in records
                    ),
                }
                for kind, records in runs.items()
            }
            documents.append(
                {
                    **item,
                    "medians": medians,
                    "time_ratio": medians["after"]["seconds"] / medians["before"]["seconds"],
                    "rss_ratio": medians["after"]["peak_tree_rss_bytes"] / medians["before"]["peak_tree_rss_bytes"],
                }
            )
        report.update(
            summary,
            documents=documents,
            status="complete",
            full_output_equal=True,
            time_ratio=summary["after"]["median_seconds"] / summary["before"]["median_seconds"],
            rss_ratio=summary["after"]["median_peak_tree_rss_bytes"] / summary["before"]["median_peak_tree_rss_bytes"],
        )
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _write_json(args.output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
