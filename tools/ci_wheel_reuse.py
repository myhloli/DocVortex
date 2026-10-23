"""在当前 CI 运行内复用同平台 ABI3 wheel，并核验生产、传输及实际安装身份。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import ci_build_metrics as metrics


def source_identity() -> dict:
    """冻结产品源码及资源摘要，区分提交、运行、平台和架构，不收集凭据。"""
    paths = metrics.command_output(
        "git",
        "ls-files",
        "-c",
        "-o",
        "--exclude-standard",
        "-z",
        "--",
        "src",
        "rust",
        ".cargo",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "setup.py",
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "LICENSE.md",
    ).split("\0")
    files = {name: metrics.file_sha256(metrics.ROOT / name) for name in sorted(set(paths)) if name}
    context = metrics.build_context("wheel")
    return {
        **{key: context[key] for key in ("git_commit", "github_run_id", "runner_os", "runner_arch")},
        "source_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
    }


def read_json(path: Path) -> dict:
    """显式按 UTF-8 读取跨平台身份文件，保留 Windows 中文源码兼容性。"""
    return json.loads(path.read_text(encoding="utf-8"))


def publish_manifest(directory: Path, output: Path) -> None:
    """只为本次完成原生编译且与 build 目录一致的唯一 wheel 生成交接清单。"""
    identity = source_identity()
    build = read_json(output / "build.json")
    events = build["cargo_events"]
    if (
        build["returncode"] != 0
        or build["context"]["git_commit"] != identity["git_commit"]
        or build["context"]["native_build_inputs_sha256"] != metrics.build_context("wheel")["native_build_inputs_sha256"]
        or not any(event["event"] == "finished" and event["profile"] == "release" for event in events)
        or not any(event["event"] == "compiling" and event.get("crate") == "docvortex-python" for event in events)
    ):
        raise RuntimeError("Wheel manifest requires a successful native build from the current source")
    wheel = metrics.wheel_identities(directory)[0]
    built = (metrics.ROOT / "build").glob("lib*/docvortex/_native.*")
    if not any(path.suffix in {".so", ".pyd"} and metrics.file_sha256(path) == wheel["native_sha256"] for path in built):
        raise RuntimeError("Wheel native extension does not match this build")
    manifest = {
        "schema": "docvortex-ci-wheel-v1",
        "source": identity,
        "producer": build["context"],
        "wheel": {**{key: value for key, value in wheel.items() if key != "path"}, "filename": Path(wheel["path"]).name},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    metrics.write_record(output, "wheel-manifest", manifest)


def verified_manifest(directory: Path) -> dict:
    """拒绝跨提交、跨运行、跨平台、源码改变或传输损坏的 wheel，不回落到源码构建。"""
    manifest = read_json(directory / "manifest.json")
    if manifest.get("schema") != "docvortex-ci-wheel-v1" or manifest.get("source") != source_identity():
        raise RuntimeError("Wheel source/run/platform identity does not match this checkout")
    actual = metrics.wheel_identities(directory)[0]
    expected = manifest["wheel"]
    if expected != {**{key: value for key, value in actual.items() if key != "path"}, "filename": Path(actual["path"]).name}:
        raise RuntimeError("Downloaded wheel or native extension digest does not match producer manifest")
    return manifest


def verify_installed_payload(directory: Path) -> dict:
    """逐文件核对安装后的产品源码、资源和扩展，拒绝 editable、错误缓存或其他 wheel。"""
    manifest = verified_manifest(directory)
    installed = metrics.installed_identity(directory)
    distribution = metrics.metadata.distribution("docvortex")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    # uv 对本地 wheel 可省略可选的 archive_info.hashes；归档及安装文件仍由本工具完整校验。
    recorded_hash = direct.get("archive_info", {}).get("hashes", {}).get("sha256")
    if (
        "dir_info" in direct
        or "archive_info" not in direct
        or direct.get("url") != (directory / manifest["wheel"]["filename"]).resolve().as_uri()
        or recorded_hash is not None
        and recorded_hash != manifest["wheel"]["sha256"]
    ):
        raise RuntimeError("Installed distribution does not identify the downloaded wheel archive")
    with zipfile.ZipFile(directory / manifest["wheel"]["filename"]) as archive:
        members = [name for name in archive.namelist() if name.startswith("docvortex/") and not name.endswith("/")]
        for name in members:
            target = Path(distribution.locate_file(name)).resolve()
            if (
                not target.is_relative_to(Path(sys.prefix).resolve())
                or metrics.file_sha256(target) != hashlib.sha256(archive.read(name)).hexdigest()
            ):
                raise RuntimeError(f"Installed payload differs from downloaded wheel: {name}")
    return {**installed, "verified_payload_files": len(members), "wheel_manifest": manifest}


def install_wheel(directory: Path, constraints: list[str], output: Path) -> int:
    """从明确 wheel 路径安装 test extra 和各任务原有下界约束，再验证全部产品文件。"""
    manifest = verified_manifest(directory)
    wheel = (directory / manifest["wheel"]["filename"]).resolve()
    result = metrics.run_measured(
        ["uv", "pip", "install", "--verbose", f"{wheel}[test]", *constraints], output, "install", "wheel"
    )
    if result == 0:
        metrics.write_record(
            output, "installed", {"context": metrics.build_context("wheel"), "artifacts": verify_installed_payload(directory)}
        )
    return result


def main() -> int:
    """提供构建交接和独立安装入口；同运行的失败任务复跑可复用已验证的同提交产物。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=metrics.DEFAULT_OUTPUT)
    commands = parser.add_subparsers(dest="action", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("directory", type=Path)
    install = commands.add_parser("install")
    install.add_argument("directory", type=Path)
    install.add_argument("constraints", nargs="*")
    args = parser.parse_args()
    if args.action == "publish":
        publish_manifest(args.directory, args.output)
        return 0
    return install_wheel(args.directory, args.constraints, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
