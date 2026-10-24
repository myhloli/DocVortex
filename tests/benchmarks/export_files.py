"""独立测量导出预检和三种文件事务；正式时间、阶段诊断、RSS 与输出校验分离。"""

from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from dataclasses import asdict
from functools import wraps
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import statistics
import struct
import subprocess
import sys
import time
from unittest.mock import patch
import zlib

if __package__:
    from .pipeline import MemorySampler, memory_bytes, write_json
else:
    from pipeline import MemorySampler, memory_bytes, write_json

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = ("write_artifact", "save_bundle", "export_middle_json")
MODES = ("new", "same", "overwrite")
SCHEMA = "docvortex.export-benchmark.v1"


def select_source(source_root: Path) -> Path:
    """显式选择被测源码并核验实际包路径，拒绝已导入另一工作树时悄悄混用。"""
    source_root = source_root.resolve()
    expected = source_root / "src/docvortex/__init__.py"
    if not expected.is_file():
        raise ValueError(f"Missing selected DocVortex source: {expected}")
    sys.path.insert(0, str(source_root / "src"))
    import docvortex

    if Path(docvortex.__file__).resolve() != expected.resolve():
        raise RuntimeError("Imported DocVortex does not belong to the selected source root")
    return source_root


def runtime_identity(source_root: Path) -> dict:
    """复用统一源码/资源和环境指纹，并记录实际加载扩展的稳定身份。"""
    import docvortex
    from docvortex._compute_backend import backend_info

    expected = (source_root / "src/docvortex/__init__.py").resolve()
    if Path(docvortex.__file__).resolve() != expected:
        raise RuntimeError("Imported DocVortex does not belong to the selected source root")
    # 既有独立脚本使用同目录绝对导入；临时加路径只为复用这些辅助函数。
    original_path = sys.path[:]
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from rust_pdf import environment_identity, source_identity
    finally:
        sys.path[:] = original_path
    compute = backend_info()
    return {
        "source_root": str(source_root.resolve()),
        "source_package": str(expected),
        **source_identity(expected),
        "environment": environment_identity(),
        "compute": {name: compute[name] for name in ("backend", "protocol", "extension", "extension_sha256")},
    }


def assert_runtime_unchanged(expected: dict, source_root: Path) -> None:
    """拒绝计时期间发生的源码、包内资源、扩展或依赖环境变化。"""
    if runtime_identity(source_root) != expected:
        raise RuntimeError("Source, native extension or environment changed during export benchmark")


def fingerprint(value: object) -> str:
    """对完整 JSON 值计算稳定摘要，保留全部输入和文件引用差异。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _relative_parts(value: str) -> list[str]:
    """仅接收规范相对 POSIX 路径，拒绝父级、绝对路径及跨平台盘符逃逸。"""
    if not isinstance(value, str) or not value or any(character in value for character in ("\\", ":", "\x00")):
        raise ValueError("Artifact path must be a safe relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Artifact path must be a safe relative POSIX path")
    return parts


def _artifact_directory(report_root: Path, artifact: str) -> Path:
    """在报告根目录内逐段解析产物目录，禁止任何一级通过符号链接逃逸。"""
    if report_root.is_symlink() or not report_root.is_dir():
        raise ValueError("Artifact report root must be a real directory")
    root = report_root.resolve(strict=True)
    folder = root
    for part in _relative_parts(artifact):
        folder /= part
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("Artifact path contains a symlink or missing directory")
    if not folder.resolve(strict=True).is_relative_to(root):
        raise ValueError("Artifact path escapes its report directory")
    return folder


def validate_artifact_files(report_root: Path, artifact: str, output: dict, output_sha256: str) -> None:
    """控制进程流式核验完整文件集合及摘要，不把结果文件分配计入 RSS 进程。"""
    if fingerprint(output) != output_sha256:
        raise AssertionError("Output manifest digest differs")
    folder = _artifact_directory(report_root, artifact)
    expected = output.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("Output manifest must contain all exported files")
    for name, identity in expected.items():
        _relative_parts(name)
        if (
            not isinstance(identity, dict)
            or type(identity.get("bytes")) is not int
            or identity["bytes"] < 0
            or not isinstance(identity.get("sha256"), str)
            or len(identity["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in identity["sha256"])
        ):
            raise ValueError("Invalid file size or SHA256 in output manifest")
    references = output.get("references")
    if not isinstance(references, list):
        raise ValueError("Output manifest must contain asset references")
    for reference in references:
        _relative_parts(reference)
        if reference not in expected:
            raise ValueError("Output reference is missing from its file manifest")
    actual = {}

    def failed_walk(error: OSError) -> None:
        """目录读取失败时拒绝验证，不能把未读取子树当作不存在。"""
        raise error

    for directory, folders, files in os.walk(folder, followlinks=False, onerror=failed_walk):
        parent = Path(directory)
        for name in folders:
            if (parent / name).is_symlink():
                raise ValueError("Artifact contains a directory symlink")
        for name in files:
            path = parent / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("Artifact contains a symlink or non-regular file")
            actual[path.relative_to(folder).as_posix()] = path
    if set(actual) != set(expected):
        raise AssertionError("Artifact file set differs from output manifest")
    for name, path in actual.items():
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while block := stream.read(64 * 1024):
                digest.update(block)
                size += len(block)
        if {"bytes": size, "sha256": digest.hexdigest()} != expected[name]:
            raise AssertionError(f"Artifact file differs from output manifest: {name}")


def validate_report_artifacts(report: dict, report_root: Path) -> None:
    """接受基线前验证每个记录的真实产物，不能只比较两份报告声明的摘要。"""
    for record in report["records"]:
        validate_artifact_files(report_root, record["artifact"], record["output"], record["output_sha256"])


def validate_timing_seed(timing_result: Path, *, report_root: Path) -> dict:
    """启动 RSS 子进程前核验已冻结计时文件，避免 overwrite 掩盖损坏的播种素材。"""
    relative_parent = timing_result.absolute().parent.relative_to(report_root.absolute()).as_posix()
    parent = _artifact_directory(report_root, relative_parent)
    result = parent / timing_result.name
    if result.is_symlink() or not result.is_file():
        raise ValueError("Timing result must be a regular report file")
    record = json.loads(result.read_text(encoding="utf-8"))
    validate_artifact_files(report_root, f"{relative_parent}/data", record["output"], record["output_sha256"])
    return record


def png_payload(index: int, size: int) -> bytes:
    """生成精确字节数、带独立确定性文本块的有效单像素 PNG。"""

    def chunk(kind: bytes, content: bytes) -> bytes:
        """按 PNG 格式写入长度和 CRC，避免以伪图片测量外置流程。"""
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))

    prefix = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    suffix = chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30\xff")) + chunk(b"IEND", b"")
    count = size - len(prefix) - len(suffix) - 12 - len(b"benchmark\x00")
    if count < 16:
        raise ValueError("payload bytes must be at least 128")
    token = f"{index:016x}".encode()
    text = (token * ((count + len(token) - 1) // len(token)))[:count]
    return prefix + chunk(b"tEXt", b"benchmark\x00" + text) + suffix


def build_workload(entry: str, count: int, payload_bytes: int) -> dict:
    """在计时前建立独立素材、内嵌文档和已物化文档，供公开入口实际写盘。"""
    from docvortex.assets import AssetStore
    from docvortex.render.contracts import RenderFormat
    from docvortex.result import Diagnostic, DocumentResult, RenderArtifact
    from docvortex.schema import ImageBlock, ImageBodyBlock, MiddleJson, PageInfo

    payloads = {f"images/page_0_image_{index}.png": png_payload(index, payload_bytes) for index in range(count)}
    assets = AssetStore(payloads)
    if entry == "write_artifact":
        content = "".join(f"![asset-{index}]({name})\n" for index, name in enumerate(payloads)).encode()
        artifact = RenderArtifact(content, RenderFormat.MARKDOWN, "text/markdown", assets)
        return {"entry": entry, "payloads": payloads, "artifact": artifact}
    blocks = [
        ImageBlock(
            type="image", index=index, content=[ImageBodyBlock(type="image_body", index=index, content="", image_path=name)]
        )
        for index, name in enumerate(payloads)
    ]
    middle = MiddleJson(
        pages=[PageInfo(page_idx=0, blocks=blocks)],
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": {"name": "export-benchmark", "version": "1"}},
    )
    if entry == "save_bundle":
        document = DocumentResult(middle, assets, diagnostics=(Diagnostic("benchmark_fixture", "确定性导出素材"),))
        return {"entry": entry, "payloads": payloads, "middle": middle, "document": document}
    inline = middle.model_copy(deep=True)
    for block, payload in zip(inline.pages[0].blocks, payloads.values(), strict=True):
        block.content[0].image_path = None
        block.content[0].image_base64 = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
    return {"entry": entry, "payloads": payloads, "middle": middle, "inline": inline}


def input_snapshot(workload: dict) -> dict:
    """在计时和采样外冻结实际入口参数，检测文档、素材或产物输入被原地污染。"""
    entry = workload["entry"]
    if entry == "export_middle_json":
        return {"entry": entry, "middle": workload["inline"].model_dump(mode="json")}
    value = workload["artifact"] if entry == "write_artifact" else workload["document"]
    assets = {
        name: {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in value.assets.items()
    }
    if entry == "write_artifact":
        return {
            "entry": entry,
            "assets": assets,
            "content_sha256": hashlib.sha256(value.content).hexdigest(),
            "format": str(value.output_format),
            "mime_type": value.mime_type,
            "diagnostics": [asdict(item) for item in value.diagnostics],
        }
    return {
        "entry": entry,
        "assets": assets,
        "middle": value.middle_json.model_dump(mode="json"),
        "model": None if value.model_json is None else value.model_json.model_dump(mode="json"),
        "diagnostics": [asdict(item) for item in value.diagnostics],
    }


def invoke(workload: dict, folder: Path, mode: str):
    """仅执行被测入口，输入生成、已有文件准备和校验均由调用方置于边界之外。"""
    from docvortex.export.bundle import save_bundle
    from docvortex.export.files import write_artifact
    from docvortex.export.middle import export_middle_json

    overwrite = mode == "overwrite"
    if workload["entry"] == "write_artifact":
        return write_artifact(workload["artifact"], folder / "document.md", overwrite=overwrite)
    if workload["entry"] == "save_bundle":
        return save_bundle(workload["document"], folder, overwrite=overwrite)
    return export_middle_json(workload["inline"], folder, overwrite=overwrite)


def validate_output(workload: dict, folder: Path, result) -> dict:
    """逐文件验证字节、完整语义和全部引用，不能以成功退出或文件数量代替正确性。"""
    from docvortex.codecs.json import load_middle
    from docvortex.export.bundle import load_bundle

    entry = workload["entry"]
    expected_assets = workload["payloads"]
    expected_names = set(expected_assets)
    main_name = {"write_artifact": "document.md", "save_bundle": "manifest.json", "export_middle_json": "middle_json.json"}[
        entry
    ]
    expected_names.add(main_name)
    if entry == "save_bundle":
        expected_names.add("middle.json")
    actual_names = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}
    if actual_names != expected_names:
        raise AssertionError("Export file set differs from the complete expected set")
    files = {}
    for name in sorted(actual_names):
        path = folder / name
        if path.is_symlink():
            raise AssertionError("Export unexpectedly contains a symlink")
        payload = path.read_bytes()
        if name in expected_assets and payload != expected_assets[name]:
            raise AssertionError(f"Export asset differs: {name}")
        files[name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    returned_main = result.json_path if entry == "export_middle_json" else result.path
    returned_assets = result.image_paths if entry == "export_middle_json" else result.asset_paths
    if returned_main.resolve() != (folder / main_name).resolve() or {path.resolve() for path in returned_assets} != {
        (folder / name).resolve() for name in expected_assets
    }:
        raise AssertionError("Returned export paths differ from written files")
    if entry == "write_artifact":
        if (folder / main_name).read_bytes() != workload["artifact"].content:
            raise AssertionError("Markdown content or asset references differ")
        references = list(expected_assets)
    else:
        json_name = "middle.json" if entry == "save_bundle" else main_name
        exported = load_middle(json.loads((folder / json_name).read_text(encoding="utf-8")))
        if exported.model_dump(mode="json") != workload["middle"].model_dump(mode="json"):
            raise AssertionError("Exported semantic document or image references differ")
        references = [block.content[0].image_path for block in exported.pages[0].blocks]
        if set(references) != set(expected_assets):
            raise AssertionError("Not all expected assets are referenced")
        if entry == "export_middle_json" and result.middle_json != exported:
            raise AssertionError("Returned MiddleJson differs from its saved document")
        if entry == "save_bundle":
            restored = load_bundle(folder)
            if (
                restored.middle_json != exported
                or dict(restored.assets) != expected_assets
                or restored.diagnostics != workload["document"].diagnostics
            ):
                raise AssertionError("Bundle replay differs from the source document")
    return {"files": files, "references": references}


def prepare_target(reference: Path, target: Path, mode: str) -> dict:
    """在计时前准备空目录、相同文件或逐文件不同内容；同内容模式保留 inode/mtime 供复用校验。"""
    if target.exists():
        shutil.rmtree(target)
    if mode == "new":
        return {}
    shutil.copytree(reference, target)
    before = {}
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        if mode == "overwrite":
            payload = path.read_bytes()
            path.write_bytes(bytes([payload[0] ^ 255]) + payload[1:])
        stat = path.stat()
        before[path.relative_to(target).as_posix()] = (stat.st_ino, stat.st_mtime_ns)
    return before


def validate_reuse(folder: Path, before: dict, mode: str) -> None:
    """确认 same 模式完整复用已有文件，避免误把覆盖写入计为同内容比较。"""
    if mode == "same":
        for name, expected in before.items():
            stat = (folder / name).stat()
            if (stat.st_ino, stat.st_mtime_ns) != expected:
                raise AssertionError(f"Same-content export rewrote a file: {name}")


def instrument(stack: ExitStack, stages: dict) -> None:
    """仅在额外诊断运行中包装阶段，正式总时间和独立 RSS 不携带包装开销。"""
    from docvortex.export import bundle, files, middle

    def wrap(owner, name: str, label: str) -> None:
        """按模块绑定替换函数，并由 ExitStack 恢复所有入口别名。"""
        original = getattr(owner, name)

        @wraps(original)
        def measured(*args, **kwargs):
            """异常路径也累计阶段时间，保持原函数结果和异常语义。"""
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                row = stages.setdefault(label, {"calls": 0, "seconds": 0.0})
                row["calls"] += 1
                row["seconds"] += time.perf_counter() - started

        stack.enter_context(patch.object(owner, name, measured))

    for module in (bundle, files, middle):
        for name, label in (
            ("_validate_export_path_relationships", "path_relationships"),
            ("_resolve_export_target", "path_resolution"),
            ("_commit_export_files", "commit_compare_stage_replace"),
        ):
            wrap(module, name, label)
    wrap(bundle, "materialize_middle", "materialize")
    wrap(middle, "_prepare_export_copy", "materialize")


class ExportMemorySampler(MemorySampler):
    """复用既有 RSS 采样器，并保证短写盘任务也有起止观测。"""

    def __init__(self) -> None:
        """记录后台采样异常，不能因端点观测成功而接受中途丢失的峰值。"""
        self.error: Exception | None = None
        super().__init__()

    def snapshot(self) -> None:
        """导出没有计算子进程，只采样当前导出进程并拒绝空观测。"""
        values = memory_bytes([os.getpid()])
        resident = values.get(os.getpid())
        if isinstance(resident, bool) or not isinstance(resident, int) or resident <= 0:
            raise RuntimeError("Export RSS sampling produced no parent observation")
        for pid, value in values.items():
            self.peaks[pid] = max(value, self.peaks.get(pid, 0))
        self.total_peak = max(self.total_peak, sum(values.values()))
        self.samples += 1

    def collect(self) -> None:
        """后台只采样导出进程，任何查询错误均传递给主线程的验收出口。"""
        try:
            while not self.stop.is_set():
                self.snapshot()
                self.stop.wait(0.05)
        except Exception as error:
            self.error = error
            self.stop.set()

    def finish(self) -> dict:
        """拒绝后台异常或空采样，防止低估内存后仍生成成功报告。"""
        result = super().finish()
        if self.error is not None:
            raise RuntimeError("Export RSS sampling failed") from self.error
        if not self.samples:
            raise RuntimeError("Export RSS sampling produced no observations")
        return result


def run_worker(config: dict, folder: Path, memory_only: bool, timing_result: Path | None = None) -> dict:
    """每个工作负载在独立进程预热，正式计时及 RSS 运行互不共享进程。"""
    source_root = Path(config.get("source_root", ROOT)).resolve()
    identity = runtime_identity(source_root)
    if config.get("identity", identity) != identity:
        raise RuntimeError("Worker source, native extension or environment differs from controller")
    workload = build_workload(config["entry"], config["files"], config["payload_bytes"])
    original = input_snapshot(workload)
    reference, target = folder / "reference", folder / "data"
    if memory_only:
        if timing_result is None:
            raise ValueError("Memory worker requires a validated timing result")
        timed = json.loads(timing_result.read_text(encoding="utf-8"))
        if timed["identity"] != identity or timed["config"] != config or timed["input_sha256"] != fingerprint(original):
            raise AssertionError("Memory worker input or identity differs from timing")
        expected = timed["output"]
        if timed["output_sha256"] != fingerprint(expected):
            raise AssertionError("Timing output manifest digest differs")
        # 直接复用已校验的计时产物播种；RSS 启动前不重复 load_bundle 或输出摘要。
        reference = timing_result.parent / "data"
    else:
        expected = validate_output(workload, reference, invoke(workload, reference, "new"))
    before = prepare_target(reference, target, config["mode"])
    warm = invoke(workload, target, config["mode"])
    if not memory_only and validate_output(workload, target, warm) != expected:
        raise AssertionError("Warmup changed output")
    if input_snapshot(workload) != original:
        raise AssertionError("Warmup changed output or input")
    validate_reuse(target, before, config["mode"])
    record = {
        "config": config,
        "identity": identity,
        "pid": os.getpid(),
        "input_sha256": fingerprint(original),
        "output": expected,
        "output_sha256": fingerprint(expected),
    }
    if memory_only:
        before = prepare_target(reference, target, config["mode"])
        del warm
        gc.collect()
        sampler = ExportMemorySampler()
        sampler.snapshot()
        sampler.thread.start()
        try:
            result = invoke(workload, target, config["mode"])
            sampler.snapshot()
        finally:
            record["memory"] = sampler.finish()
        if validate_output(workload, target, result) != expected:
            raise AssertionError("Independent memory replay changed output")
        validate_reuse(target, before, config["mode"])
    else:
        times = []
        for _ in range(config["runs"]):
            before = prepare_target(reference, target, config["mode"])
            started = time.perf_counter()
            result = invoke(workload, target, config["mode"])
            times.append(time.perf_counter() - started)
            if validate_output(workload, target, result) != expected or input_snapshot(workload) != original:
                raise AssertionError("Timed export changed output or input")
            validate_reuse(target, before, config["mode"])
        record["seconds"] = times
        record["median_seconds"] = statistics.median(times)
        before = prepare_target(reference, target, config["mode"])
        stages = {}
        with ExitStack() as stack:
            instrument(stack, stages)
            started = time.perf_counter()
            result = invoke(workload, target, config["mode"])
            duration = time.perf_counter() - started
        if validate_output(workload, target, result) != expected:
            raise AssertionError("Stage instrumentation changed output")
        validate_reuse(target, before, config["mode"])
        record["stage_diagnostic"] = {
            "total_seconds": duration,
            "stages": stages,
            "other_seconds": duration - sum(item["seconds"] for item in stages.values()),
        }
    if input_snapshot(workload) != original:
        raise AssertionError("Export mutated caller-owned input")
    record["input_unchanged"] = True
    assert_runtime_unchanged(identity, source_root)
    if not memory_only:
        shutil.rmtree(reference)
    if memory_only:
        shutil.rmtree(target)
    return record


def precheck(count: int, runs: int) -> dict:
    """单独测量纯路径关系预检的规模梯度，不混入文件准备或 I/O。"""
    from docvortex.export.middle import _validate_export_path_relationships

    paths = ["document.md", *(f"images/page_0_image_{index}.png" for index in range(count))]
    original = list(paths)
    _validate_export_path_relationships(paths)
    values = []
    for _ in range(runs):
        started = time.perf_counter()
        _validate_export_path_relationships(paths)
        values.append(time.perf_counter() - started)
    if paths != original:
        raise AssertionError("Path precheck mutated its input")
    return {
        "asset_files": count,
        "total_paths": len(paths),
        "input_sha256": fingerprint(paths),
        "seconds": values,
        "median_seconds": statistics.median(values),
    }


def _positive_measurement(value: float, label: str) -> float:
    """拒绝损坏报告中的零、负数、布尔值及非有限指标，保证比较比率有意义。"""
    try:
        valid = type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"Benchmark {label} must be a positive finite number")
    return value


def compare_baseline(report: dict, previous: dict, *, report_root: Path, baseline_root: Path) -> list[dict]:
    """严格比对全部工作负载和完整文件摘要，拒绝配置或输入不等价的基线。"""
    if previous.get("schema") != SCHEMA or previous.get("workload") != report["workload"]:
        raise ValueError("Baseline workload is not equivalent")
    if previous.get("measurement") != report["measurement"] or previous.get("environment") != report["environment"]:
        raise ValueError("Baseline measurement protocol or environment differs")
    precheck_fields = ("asset_files", "total_paths", "input_sha256")
    if [[row[name] for name in precheck_fields] for row in previous["precheck"]] != [
        [row[name] for name in precheck_fields] for row in report["precheck"]
    ]:
        raise ValueError("Baseline path-precheck input differs")
    old = {row["case"]: row for row in previous["records"]}
    if len(old) != len(previous["records"]) or set(old) != {row["case"] for row in report["records"]}:
        raise ValueError("Baseline case set differs")
    validate_report_artifacts(previous, baseline_root)
    validate_report_artifacts(report, report_root)
    comparisons = []
    for row in report["records"]:
        before = old[row["case"]]
        old_seconds = _positive_measurement(before["median_seconds"], "median_seconds")
        new_seconds = _positive_measurement(row["median_seconds"], "median_seconds")
        old_rss = _positive_measurement(before["memory"]["parent_peak_rss_bytes"], "RSS")
        new_rss = _positive_measurement(row["memory"]["parent_peak_rss_bytes"], "RSS")
        equal = (
            before["input_sha256"] == row["input_sha256"]
            and before["output"] == row["output"]
            and before["output_sha256"] == row["output_sha256"]
        )
        comparisons.append(
            {
                "case": row["case"],
                "equal": equal,
                "time_ratio": new_seconds / old_seconds,
                "rss_ratio": new_rss / old_rss,
            }
        )
    if not all(row["equal"] for row in comparisons):
        raise AssertionError("Baseline input or complete output differs")
    return comparisons


def main() -> None:
    """按规模顺序运行三入口三模式，并保存可严格比较的报告及实际落盘结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT, help="包含 src/docvortex 的实际被测工作树或冻结源码目录")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--files", type=int, nargs="+", default=[8, 64, 256], help="每个规模的素材文件数，不包含主文件")
    parser.add_argument("--payload-bytes", type=int, default=4096)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--memory-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timing-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        config = json.loads(args.worker.read_text(encoding="utf-8"))
        selected = select_source(args.source_root)
        if selected != Path(config["source_root"]):
            raise ValueError("Worker source root differs from controller configuration")
        write_json(args.output / "result.json", run_worker(config, args.output, args.memory_only, args.timing_result))
        return
    if (
        args.runs < 1
        or args.payload_bytes < 128
        or any(count < 1 for count in args.files)
        or len(set(args.files)) != len(args.files)
    ):
        parser.error("require positive runs, unique positive file counts and payload-bytes >= 128")
    if args.output.exists():
        parser.error("output directory must not already exist")
    selected = select_source(args.source_root)
    identity = runtime_identity(selected)
    args.output.mkdir(parents=True)
    report = {
        "schema": SCHEMA,
        **identity,
        "workload": {
            "files": sorted(args.files),
            "payload_bytes": args.payload_bytes,
            "entries": list(ENTRIES),
            "modes": list(MODES),
        },
        "measurement": {
            "runs": args.runs,
            "timing": "warm API-only wall time; no profiler, sampler, seed or output validation",
            "stages": "separate diagnostic pass; materialize, path relationships, path resolution, commit/compare/stage/replace; other includes serialization and validation",
            "rss": "separate process per case; timing output manifest and files reused without pre-sampling output hashing or load_bundle; one mode-specific warmup then seeding and gc; 50ms sampled parent RSS plus endpoint observations during API call; source/env checks, fixture construction, input hashing, warmup and copying may leave resident allocator pages; input and expected manifest remain resident; final hashing and replay occur after sampling",
            "disk": "OS page cache is warm; atomic file writes without fsync; no crash-durability claim",
            "files": "asset counts; main files and bundle manifest are additional",
        },
        "precheck": [],
        "records": [],
    }
    for count in sorted(args.files):
        report["precheck"].append(precheck(count, args.runs))
        for entry in ENTRIES:
            for mode in MODES:
                name = f"{count:06d}-{entry}-{mode}"
                config = {
                    "entry": entry,
                    "mode": mode,
                    "files": count,
                    "payload_bytes": args.payload_bytes,
                    "runs": args.runs,
                    "source_root": str(selected),
                    "identity": identity,
                }
                folder = args.output / name
                write_json(folder / "config.json", config)
                records = []
                for memory_only in (False, True):
                    output = folder / ("memory" if memory_only else "timing")
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(folder / "config.json"),
                        "--output",
                        str(output),
                        "--source-root",
                        str(selected),
                    ]
                    if memory_only:
                        if validate_timing_seed(folder / "timing/result.json", report_root=args.output) != records[0]:
                            raise AssertionError("Timing result changed before RSS replay")
                        command.extend(["--memory-only", "--timing-result", str(folder / "timing/result.json")])
                    subprocess.run(command, check=True)
                    records.append(json.loads((output / "result.json").read_text(encoding="utf-8")))
                timed, memory = records
                if (
                    timed["pid"] == memory["pid"]
                    or timed["identity"] != identity
                    or memory["identity"] != identity
                    or timed["input_sha256"] != memory["input_sha256"]
                    or timed["output"] != memory["output"]
                ):
                    raise AssertionError("Independent RSS replay identity differs")
                report["records"].append(
                    {
                        **timed,
                        "case": name,
                        "memory_pid": memory["pid"],
                        "memory": memory["memory"],
                        "artifact": f"{name}/timing/data",
                    }
                )
                write_json(args.output / "progress.json", report)
                print(name, f"{timed['median_seconds']:.6f}s", flush=True)
    assert_runtime_unchanged(identity, selected)
    if args.baseline:
        baseline = args.baseline / "report.json" if args.baseline.is_dir() else args.baseline
        report["comparison"] = compare_baseline(
            report,
            json.loads(baseline.read_text(encoding="utf-8")),
            report_root=args.output,
            baseline_root=baseline.parent,
        )
    else:
        validate_report_artifacts(report, args.output)
    write_json(args.output / "report.json", report)


if __name__ == "__main__":
    main()
