"""逐文档交替运行 Python/Rust 基准，核对完整输出并复测超过 5% 的退化。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def code_digest() -> str:
    """冻结实际 Python/Rust 源码与构建输入，拒绝在测量中途混用版本。"""
    digest = hashlib.sha256()
    paths = [*ROOT.glob("src/**/*.py"), *ROOT.glob("rust/**/*.rs"), *ROOT.glob("rust/**/*.toml")]
    paths += [ROOT / name for name in ("Cargo.toml", "Cargo.lock", "pyproject.toml", "setup.py", "rust-toolchain.toml")]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def measure(path: Path, folder: Path, backend: str, runs: int) -> dict:
    """复用既有 worker 的计时、输出和 RSS 定义；每种后端独立解释器。"""
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/benchmarks/flash_pdf.py"),
            "--worker",
            str(path.resolve()),
            "--output",
            str(folder.resolve()),
            "--backend",
            backend,
            "--runs",
            str(runs),
        ],
        cwd=ROOT,
        check=True,
    )
    return json.loads((folder / "result.json").read_text())


def comparison(pair: dict[str, dict]) -> dict:
    """比较源文件和完整语义摘要，性能不用于掩盖输出变化。"""
    python, rust = pair["python"], pair["rust"]
    assert python["source_sha256"] == rust["source_sha256"]
    assert python["compute"]["backend"] == "python" and rust["compute"]["backend"] == "rust"
    return {
        "equal": python["full_output_sha256"] == rust["full_output_sha256"],
        "time_ratio": rust["median_seconds"] / python["median_seconds"],
        "rss_ratio": rust["peak_rss_bytes"] / python["peak_rss_bytes"],
    }


def main() -> None:
    """轮换先后次序，保存全部第一次与复测记录，不刷新任何历史金标。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--path", type=Path, action="append")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.output.exists() or args.runs < 1:
        parser.error("output must be new and runs must be positive")
    manifest = json.loads((ROOT / "tests/fixtures/flash_layout_geometry_manifest.json").read_text())
    paths = args.path or [
        *(ROOT / item["path"] for item in manifest["documents"]),
        *sorted((ROOT / "tests/unittest/pdfs/native_pdf_tables").glob("*.pdf")),
    ]
    paths = list(dict.fromkeys(paths))
    frozen_code = code_digest()
    args.output.mkdir(parents=True)
    report = {
        "code_sha256": frozen_code,
        "python": sys.version,
        "platform": platform.platform(),
        "runs": args.runs,
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "documents": [],
    }
    for index, path in enumerate(paths):
        order = ("python", "rust") if index % 2 == 0 else ("rust", "python")
        label = f"{index:02d}-{path.stem}"
        pair = {backend: measure(path, args.output / label / backend, backend, args.runs) for backend in order}
        record = {"path": str(path.resolve()), "measurements": pair, **comparison(pair)}
        if record["time_ratio"] > 1.05 or record["rss_ratio"] > 1.05:
            repeated = {
                backend: measure(path, args.output / label / f"repeat-{backend}", backend, args.runs)
                for backend in reversed(order)
            }
            record["repeat_measurements"] = repeated
            record["repeat_comparison"] = comparison(repeated)
            record["persistent_regression"] = any(
                record[key] > 1.05 and record["repeat_comparison"][key] > 1.05 for key in ("time_ratio", "rss_ratio")
            )
        assert code_digest() == frozen_code, "Source changed during benchmark"
        report["documents"].append(record)
        (args.output / "progress.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            f"{index + 1}/{len(paths)} {path.name}: equal={record['equal']} time={record['time_ratio']:.3f} RSS={record['rss_ratio']:.3f}",
            flush=True,
        )
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if any(not item["equal"] or not item.get("repeat_comparison", {"equal": True})["equal"] for item in report["documents"]):
        raise SystemExit("Full output parity failed")
    if any(item.get("persistent_regression") for item in report["documents"]):
        raise SystemExit("Persistent time/RSS regression exceeds 5%; inspect report.json")


if __name__ == "__main__":
    main()
