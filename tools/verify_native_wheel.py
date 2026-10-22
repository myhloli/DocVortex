"""在独立安装环境验证原生发行物、真实文档语义及可选冻结输出基线。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


# 以下语义锚点来自切换前冻结的 native 输出，按输入摘要绑定，避免仅检查进程成功退出。
SMOKE_EXPECTATIONS = {
    "f3b3be345bf2df8979f2491ca9466e078e4fd1d6a216611faa8566e4c44d474b": {
        "page_count": 13,
        "min_tables": 5,
        "min_assets": 1,
        "required_text": [
            "The response of flow duration curves to afforestation",
            "Comparison with paired catchment studies",
            "Summary and conclusions",
            "Response of mean annual evapotranspiration to vegetation changes at catchment scale",
        ],
    },
    "9e94e95637356e1599510436278747d1150a3dfb822233bdc77a9dcb9a4fc6e4": {
        "page_count": 6,
        "min_tables": 2,
        "min_assets": 1,
        "required_text": [
            "Real-time Temporal Stereo Matching using Iterative Adaptive Support Weights",
            "Two-Pass Spatial Cost Aggregation",
            "V. CONCLUSION",
            "Realtime global stereo matching using hierarchical belief propagation",
        ],
    },
}
DENSE_EXPECTATIONS = {
    "page_count": 1,
    "min_tables": 1,
    "min_assets": 0,
    "required_text": ["Synthetic benchmark. All values are generated."],
    "dense_table": True,
}


def expected_version() -> str:
    """优先核验发布标签，否则读取当前构建源码版本，避免安装旧 wheel 仍自洽通过。"""
    if os.environ.get("RELEASE_TAG"):
        return os.environ["RELEASE_TAG"].removeprefix("v")
    source = Path(__file__).resolve().parents[1] / "src" / "docvortex" / "version.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    )


def capture(pdf: Path, output: Path, version: str) -> None:
    """要求加载安装 wheel 并真实调用原生桥接，捕获正文、模型、素材和诊断。"""
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    import onnxruntime

    # 安装验证不依赖遥测，关闭后台上传以免 SDK 的退出异常掩盖解析结果。
    onnxruntime.disable_telemetry_events()
    from dataclasses import asdict
    import docvortex
    from docvortex._compute_backend import backend_info
    from docvortex.api import render
    from verify_release_package import verify_release_package
    from verify_runtime_dependencies import verify_runtime_dependencies

    verify_runtime_dependencies()
    verify_release_package(pdf, version)
    package = Path(docvortex.__file__).resolve()
    assert (package.parent / "resources/unicode/EquivalentUnifiedIdeograph-17.0.0.txt").is_file()
    info = backend_info()
    assert info["backend"] == "rust", info
    assert Path(info["extension"]).resolve().is_relative_to(package.parent), info
    result = docvortex.parse(pdf, keep_model_json=True)
    actual = backend_info()
    assert actual["pdfium_object_bridge_calls"] > 0, actual
    assert actual["pdfium_object_bridge_unavailable_reason"] is None, actual
    assert actual["native_text_snapshot_calls"] > 0, actual
    assert actual["native_text_snapshot_unavailable_reason"] is None, actual
    if actual["pdfium_bridge_calls"] > 0:
        assert actual["pdfium_record_batch_size"] == 1024, actual
    assert result.middle_json.metadata.producer.version == version, result.middle_json.metadata.producer
    assert result.model_json.metadata.producer.version == version, result.model_json.metadata.producer
    data = {
        "input_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "model": result.model_json.model_dump(mode="json"),
        "middle": result.middle_json.model_dump(mode="json"),
        "markdown": render(result.middle_json, "markdown", assets=result.assets).content.decode("utf-8"),
        "assets": {key: hashlib.sha256(value).hexdigest() for key, value in result.assets.items()},
        "diagnostics": [asdict(value) for value in result.diagnostics],
    }
    cli = subprocess.check_output([sys.executable, "-I", "-m", "docvortex.cli", "--version"], text=True)
    assert cli.strip().endswith(f"version {version}"), cli
    output.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def verify_output(data: dict, expectations: dict | None, baseline: dict | None = None) -> None:
    """校验固定语义锚点或完整冻结输出，并拒绝空页、空正文及密集表格单元格丢失。"""
    model_pages = data["model"]["pages"]
    middle_pages = data["middle"]["pages"]
    assert model_pages and all(model_pages), "Model output contains no blocks or an empty page"
    assert middle_pages and all(page["blocks"] for page in middle_pages), "Middle output contains no blocks or an empty page"
    assert len(model_pages) == len(middle_pages), "Model and middle page counts differ"
    markdown = re.sub(r"\s+", " ", data["markdown"]).strip()
    assert markdown, "Markdown output is empty"
    assert expectations is not None or baseline is not None, "Unknown PDF: provide --expectations or --baseline"
    if expectations is not None:
        assert len(middle_pages) == expectations["page_count"], len(middle_pages)
        tables = sum(block["type"] == "table" for page in middle_pages for block in page["blocks"])
        assert tables >= expectations.get("min_tables", 0), tables
        assert len(data["assets"]) >= expectations.get("min_assets", 0), len(data["assets"])
        for fragment in expectations["required_text"]:
            assert fragment in markdown, f"Required document content is missing: {fragment}"
        if expectations.get("dense_table"):
            cells = re.findall(r"\b\d{3}-[0-6]-\d{4}\b", markdown)
            expected_cells = [f"{row:03d}-{column}-{row * 7 + column:04d}" for row in range(120) for column in range(7)]
            assert cells == expected_cells, "Dense table cells are missing, duplicated or reordered"
    if baseline is not None:
        assert data == baseline, "Installed native wheel differs from the frozen output baseline"


def main() -> None:
    """在干净子进程运行真实安装检查，再用固定预期或显式基线验证结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--expectation", choices=["dense-table"])
    parser.add_argument("--expectations", type=Path, help="JSON with page_count, required_text and optional minimum counts")
    parser.add_argument("--baseline", type=Path, help="Complete frozen JSON emitted by --output on a previously accepted wheel")
    parser.add_argument("--output", type=Path, help="Save the verified complete JSON snapshot")
    parser.add_argument("--capture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--expected-version", default=expected_version(), help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.capture is not None:
        capture(arguments.pdf, arguments.capture, arguments.expected_version)
        return
    pdf = arguments.pdf.resolve()
    expectations = SMOKE_EXPECTATIONS.get(hashlib.sha256(pdf.read_bytes()).hexdigest())
    if arguments.expectation == "dense-table":
        expectations = DENSE_EXPECTATIONS
    if arguments.expectations is not None:
        expectations = json.loads(arguments.expectations.read_text(encoding="utf-8"))
    baseline = json.loads(arguments.baseline.read_text(encoding="utf-8")) if arguments.baseline else None
    with tempfile.TemporaryDirectory(prefix="docvortex-wheel-") as directory:
        root = Path(directory)
        output = root / "native.json"
        env = dict(os.environ, LOGURU_LEVEL="WARNING")
        env.pop("PYTHONPATH", None)
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                str(pdf),
                "--capture",
                str(output),
                "--expected-version",
                arguments.expected_version,
            ],
            cwd=root,
            env=env,
            check=True,
        )
        data = json.loads(output.read_text(encoding="utf-8"))
        verify_output(data, expectations, baseline)
        if arguments.output is not None:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_bytes(output.read_bytes())
    print(f"Installed native wheel version, ABI provenance and document semantics verified: {pdf.name}", flush=True)


if __name__ == "__main__":
    main()
