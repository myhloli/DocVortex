"""记录 CI 构建、安装和测试耗时及产物来源，不以缓存命中替代真实构建。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "build" / "ci-metrics"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CARGO_FINISHED = re.compile(r"Finished\s+`([^`]+)`\s+profile.*\bin\s+((?:\d+(?:\.\d+)?(?:ms|h|m|s)\s*)+)$")
CARGO_CRATE = re.compile(r"\b(Compiling|Checking|Fresh)\s+([\w-]+)\s+v([^\s]+)")
BUILD_ENV = (
    "CARGO_BUILD_TARGET",
    "CARGO_TARGET_DIR",
    "CARGO_INCREMENTAL",
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "PYO3_PYTHON",
    "PYO3_ENVIRONMENT_SIGNATURE",
    "PYO3_CROSS",
    "PYO3_CROSS_LIB_DIR",
    "PYO3_CROSS_PYTHON_VERSION",
    "PYO3_CONFIG_FILE",
    "PYO3_USE_ABI3_FORWARD_COMPATIBILITY",
    "SETUPTOOLS_RUST_CARGO_PROFILE",
    "MACOSX_DEPLOYMENT_TARGET",
    "CC",
    "CXX",
    "CFLAGS",
    "CXXFLAGS",
    "LDFLAGS",
)


def file_sha256(path: Path) -> str:
    """分块计算实际文件摘要，避免为了身份记录额外整文件分配。"""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command_output(*command: str) -> str:
    """读取已安装工具和当前检出的身份，失败时明确停止记录。"""
    return subprocess.check_output(command, cwd=ROOT, text=True, encoding="utf-8").strip()


def build_context(profile: str) -> dict:
    """记录源码、解释器、编译器及明确的构建变量，不导出令牌等环境机密。"""
    inputs = [ROOT / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "setup.py", "pyproject.toml")]
    inputs += sorted((ROOT / "rust").rglob("*.rs")) + sorted((ROOT / "rust").rglob("*.toml"))
    inputs += sorted((ROOT / ".cargo").glob("config*"))
    fingerprints = {path.relative_to(ROOT).as_posix(): file_sha256(path) for path in sorted(inputs) if path.is_file()}
    rustc = command_output("rustc", "-vV")
    host = next(line.removeprefix("host: ") for line in rustc.splitlines() if line.startswith("host: "))
    return {
        "git_commit": command_output("git", "rev-parse", "HEAD"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "job": os.environ.get("GITHUB_JOB"),
        "runner_os": os.environ.get("RUNNER_OS", platform.system()),
        "runner_arch": os.environ.get("RUNNER_ARCH", platform.machine()),
        "python": sys.version,
        "python_executable": sys.executable,
        "profile": profile,
        "target": os.environ.get("CARGO_BUILD_TARGET") or host,
        "rustc": rustc,
        "cargo": command_output("cargo", "--version"),
        "build_environment": {name: os.environ[name] for name in BUILD_ENV if name in os.environ},
        "native_build_inputs": fingerprints,
        "native_build_inputs_sha256": hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest(),
        # false 也可能是前缀回退命中，不能仅凭此字段把该次构建称为冷缓存。
        "cargo_cache_exact_hit": os.environ.get("CI_CARGO_CACHE_HIT", ""),
        "cargo_cache_outcome": os.environ.get("CI_CARGO_CACHE_OUTCOME", "not_used"),
        "cargo_cache_cleaned": os.environ.get("CI_CARGO_CACHE_CLEANED", ""),
    }


def cargo_event(line: str) -> dict | None:
    """解析 Cargo 原始完成耗时和编译状态，保留 profile，排除普通上传完成日志。"""
    plain = ANSI_ESCAPE.sub("", line).strip()
    if match := CARGO_FINISHED.search(plain):
        factors = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
        seconds = sum(float(value) * factors[unit] for value, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", match[2]))
        return {"event": "finished", "profile": match[1], "seconds": seconds}
    if match := CARGO_CRATE.search(plain):
        return {"event": match[1].lower(), "crate": match[2], "version": match[3]}
    return None


def write_record(output: Path, name: str, record: dict) -> None:
    """保存独立 JSON，并在 GitHub 摘要中给出可定位的阶段耗时。"""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("Metric name must contain only letters, digits, underscores or hyphens")
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(
                f"- {name}: {record.get('elapsed_seconds', 0):.3f} s; exit={record.get('returncode', 0)}; "
                f"Cargo exact hit={record.get('context', {}).get('cargo_cache_exact_hit', '') or 'unknown/skipped'}\n"
            )


def run_measured(command: list[str], output: Path, name: str, profile: str, require_cargo: bool = False) -> int:
    """实时透传命令输出并保持退出码，单独记录总耗时和 Cargo 自报阶段耗时。"""
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("Missing measured command")
    context = build_context(profile)
    output.mkdir(parents=True, exist_ok=True)
    events = []
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    with (output / f"{name}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            if event := cargo_event(line):
                events.append(event)
        returncode = process.wait()
    record = {
        "context": context,
        "command": command,
        "started_at": started_at,
        "elapsed_seconds": time.perf_counter() - started,
        "returncode": returncode,
        "cargo_events": events,
    }
    write_record(output, name, record)
    if returncode == 0 and require_cargo:
        finished = any(event["event"] == "finished" and event["profile"] == profile for event in events)
        native = any(event.get("crate") == "docvortex-python" for event in events)
        if not finished or not native:
            raise RuntimeError("Command succeeded without observed native Cargo build; refusing to accept a cached wheel")
    return returncode


def installed_identity() -> dict:
    """核验实际安装来源、原生协议和本次构建产物摘要，拒绝从 checkout 导入旧扩展。"""
    import docvortex
    from docvortex._compute_backend import get_native

    package = Path(docvortex.__file__).resolve()
    binary = Path(get_native().__file__).resolve()
    if package.is_relative_to(ROOT / "src") or not binary.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError(f"Expected independent installation, got {package} and {binary}")
    expected = re.search(r"^_PROTOCOL_VERSION = (\d+)$", (ROOT / "src/docvortex/_compute_backend.py").read_text(), re.MULTILINE)
    assert expected is not None and get_native().PROTOCOL_VERSION == int(expected[1])
    digest = file_sha256(binary)
    built = [path for path in (ROOT / "build").glob("lib*/docvortex/_native.*") if path.suffix in {".so", ".pyd"}]
    matching = [str(path.resolve()) for path in built if file_sha256(path) == digest]
    if not matching:
        raise RuntimeError("Installed native extension does not match this checkout's built artifact")
    distribution = metadata.distribution("docvortex")
    return {
        "package": str(package),
        "extension": str(binary),
        "extension_sha256": digest,
        "matching_build_artifacts": matching,
        "protocol": get_native().PROTOCOL_VERSION,
        "version": distribution.version,
        "wheel_metadata": distribution.read_text("WHEEL"),
        "installed_packages": {item.metadata["Name"]: item.version for item in metadata.distributions()},
    }


def wheel_identities(directory: Path) -> list[dict]:
    """记录实际 ABI 验证所用 wheel 与扩展摘要，不把缓存状态当作验证结果。"""
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1 or "-cp311-abi3-" not in wheels[0].name:
        raise RuntimeError(f"Expected exactly one cp311-abi3 wheel, got {wheels}")
    with zipfile.ZipFile(wheels[0]) as archive:
        binaries = [
            name for name in archive.namelist() if name.startswith("docvortex/_native.") and name.endswith((".so", ".pyd"))
        ]
        if len(binaries) != 1:
            raise RuntimeError(f"Expected one native extension in wheel, got {binaries}")
        return [
            {
                "path": str(wheels[0].resolve()),
                "sha256": file_sha256(wheels[0]),
                "native_member": binaries[0],
                "native_sha256": hashlib.sha256(archive.read(binaries[0])).hexdigest(),
            }
        ]


def clean_cargo_cache() -> None:
    """仅在 CI 恢复失败时清理本次缓存目录，让原始构建命令从干净状态运行。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Cargo cache recovery is only enabled inside GitHub Actions")
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
    if target.resolve() in {ROOT.resolve(), Path.home().resolve(), cargo_home.resolve()} or target.resolve() in ROOT.parents:
        raise RuntimeError(f"Unsafe Cargo target directory: {target}")
    for path in (target, cargo_home / "registry", cargo_home / "git"):
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
        print(f"Removed failed Cargo cache path: {path}")


def prepare_cache() -> None:
    """固定 PyO3 使用本步骤的解释器，将完整 Python ABI 签名纳入缓存和构建脚本失效条件。"""
    signature = (
        f"{sys.implementation.name}-{platform.python_version()}-{sysconfig.get_config_var('SOABI')}-{platform.machine()}"
    )
    values = {
        "PYO3_PYTHON": os.environ.get("PYO3_PYTHON", sys.executable),
        "PYO3_ENVIRONMENT_SIGNATURE": os.environ.get("PYO3_ENVIRONMENT_SIGNATURE", signature),
    }
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")
    print(f"PyO3 interpreter: {values['PYO3_PYTHON']}; ABI signature: {values['PYO3_ENVIRONMENT_SIGNATURE']}")


def main() -> int:
    """分派计时、安装身份、wheel 身份与恢复命令，不修改产品构建配置。"""
    # Windows 的重定向流可能使用本地代码页，CI 日志统一输出 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run")
    run.add_argument("--name", required=True)
    run.add_argument("--profile", default="release")
    run.add_argument("--require-cargo", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("installed")
    wheels = commands.add_parser("wheels")
    wheels.add_argument("directory", type=Path)
    commands.add_parser("clean-cargo-cache")
    commands.add_parser("prepare-cache")
    args = parser.parse_args()
    if args.action == "run":
        return run_measured(args.command, args.output, args.name, args.profile, args.require_cargo)
    if args.action == "prepare-cache":
        prepare_cache()
    elif args.action == "clean-cargo-cache":
        clean_cargo_cache()
    else:
        details = installed_identity() if args.action == "installed" else wheel_identities(args.directory)
        write_record(args.output, args.action, {"context": build_context("release"), "artifacts": details})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
