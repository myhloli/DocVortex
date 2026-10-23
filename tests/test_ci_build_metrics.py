"""验证 CI 耗时采集、实际构建守卫和缓存恢复边界。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

from tools import ci_build_metrics as metrics


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "    Finished `release` profile [optimized] target(s) in 1m 19s",
            {"event": "finished", "profile": "release", "seconds": 79},
        ),
        (
            "\x1b[1mFinished\x1b[0m `dev` profile [unoptimized] target(s) in 9.34s",
            {"event": "finished", "profile": "dev", "seconds": 9.34},
        ),
        (
            "[stderr] Finished `test` profile [unoptimized] target(s) in 250ms",
            {"event": "finished", "profile": "test", "seconds": 0.25},
        ),
        (
            "Fresh docvortex-python v0.1.0 (/checkout/rust/docvortex-python)",
            {"event": "fresh", "crate": "docvortex-python", "version": "0.1.0"},
        ),
        ("[stderr] Compiling pyo3 v0.28.2", {"event": "compiling", "crate": "pyo3", "version": "0.28.2"}),
        ("Finished uploading artifact content", None),
        ("Finished `release` profile target(s) in unknown", None),
        ("test result: ok. 13 passed; finished in 0.00s", None),
    ],
)
def test_cargo_events_preserve_profile_and_units(line, expected) -> None:
    """支持真实 Cargo 的颜色、前缀和单位，同时不把上传或测试时长误认作编译。"""
    assert metrics.cargo_event(line) == expected


def test_measured_command_keeps_output_arguments_exit_code_and_failure_report(tmp_path, monkeypatch, capsys) -> None:
    """失败命令仍实时输出、保存诊断并原样返回退出码，参数不能经过 shell 执行。"""
    monkeypatch.setattr(metrics, "build_context", lambda profile: {"profile": profile, "cargo_cache_exact_hit": "false"})
    command = [sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(7)", "literal $(not_a_shell) `also_literal`"]
    assert metrics.run_measured(command, tmp_path, "failed", "release", True) == 7
    assert command[-1] in capsys.readouterr().out
    record = json.loads((tmp_path / "failed.json").read_text())
    assert record["returncode"] == 7 and record["command"] == command and record["elapsed_seconds"] > 0
    assert record["context"]["cargo_cache_exact_hit"] == "false"


@pytest.mark.parametrize("output", ["cached wheel installed", "Finished `release` profile [optimized] target(s) in 1s"])
def test_success_without_current_native_cargo_evidence_is_rejected(output, tmp_path, monkeypatch) -> None:
    """零退出码或其他 crate 的 Cargo 日志都不能替代当前 DocVortex 的构建证据。"""
    monkeypatch.setattr(metrics, "build_context", lambda profile: {})
    with pytest.raises(RuntimeError, match="without observed native Cargo build"):
        metrics.run_measured([sys.executable, "-c", f"print({output!r})"], tmp_path, "build", "release", True)
    assert json.loads((tmp_path / "build.json").read_text())["returncode"] == 0


def test_fresh_native_crate_is_valid_only_with_a_real_completed_cargo_invocation(tmp_path, monkeypatch) -> None:
    """Cargo 校验源码后认定 Fresh 仍有效，不能要求每次都重新编译依赖。"""
    monkeypatch.setattr(metrics, "build_context", lambda profile: {})
    output = "Fresh docvortex-python v0.1.0\nFinished `release` profile [optimized] target(s) in 0.01s"
    assert metrics.run_measured([sys.executable, "-c", f"print({output!r})"], tmp_path, "build", "release", True) == 0


@pytest.mark.parametrize("override", [False, True])
def test_prepare_cache_keeps_explicit_pyo3_settings_and_records_python_abi(tmp_path, monkeypatch, override) -> None:
    """默认使用实际解释器及 ABI 签名，显式交叉编译设置仍保留。"""
    env_file = tmp_path / "github-env"
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.delenv("PYO3_PYTHON", raising=False)
    monkeypatch.delenv("PYO3_ENVIRONMENT_SIGNATURE", raising=False)
    if override:
        monkeypatch.setenv("PYO3_PYTHON", "explicit-python")
        monkeypatch.setenv("PYO3_ENVIRONMENT_SIGNATURE", "explicit-abi")
    metrics.prepare_cache()
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values["PYO3_PYTHON"] == ("explicit-python" if override else sys.executable)
    assert (
        values["PYO3_ENVIRONMENT_SIGNATURE"] == "explicit-abi"
        if override
        else "cpython" in values["PYO3_ENVIRONMENT_SIGNATURE"]
    )


def test_context_records_compiler_target_and_source_without_registry_tokens(tmp_path, monkeypatch) -> None:
    """构建身份记录真实目标及源摘要，不收集 Cargo registry 令牌。"""
    (tmp_path / "Cargo.lock").write_text("version = 4\n")
    monkeypatch.setattr(metrics, "ROOT", tmp_path)
    monkeypatch.setenv("CARGO_BUILD_TARGET", "aarch64-pc-windows-msvc")
    monkeypatch.setenv("CARGO_REGISTRIES_PRIVATE_TOKEN", "must-not-appear")

    def output(*command):
        """固定外部工具结果，核验身份收集对目标和环境变量的选择。"""
        return "rustc 1.98.1\nhost: x86_64-unknown-linux-gnu" if command[0] == "rustc" else "fixture"

    monkeypatch.setattr(metrics, "command_output", output)
    context = metrics.build_context("release")
    assert context["target"] == "aarch64-pc-windows-msvc"
    assert "Cargo.lock" in context["native_build_inputs"]
    assert "must-not-appear" not in json.dumps(context)


def test_cache_recovery_only_removes_restored_paths(tmp_path, monkeypatch) -> None:
    """恢复失败只清理 target、registry、git，保留工具链二进制、配置和仓库源码。"""
    root = tmp_path / "repo"
    cargo = tmp_path / "cargo"
    for path in (root / "target", root / "src", cargo / "registry", cargo / "git", cargo / "bin"):
        path.mkdir(parents=True)
        (path / "keep-or-clear").write_text("data")
    (cargo / "config.toml").write_text("[net]\n")
    monkeypatch.setattr(metrics, "ROOT", root)
    monkeypatch.setenv("CARGO_HOME", str(cargo))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(root / "target"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    metrics.clean_cargo_cache()
    assert not (root / "target").exists() and not (cargo / "registry").exists() and not (cargo / "git").exists()
    assert (root / "src/keep-or-clear").exists() and (cargo / "bin/keep-or-clear").exists()
    assert (cargo / "config.toml").exists()


def test_local_cache_and_unsafe_target_cannot_be_cleared(tmp_path, monkeypatch) -> None:
    """本地直接执行或目标误指仓库根目录时拒绝清理。"""
    monkeypatch.setattr(metrics, "ROOT", tmp_path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="only enabled inside GitHub Actions"):
        metrics.clean_cargo_cache()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="Unsafe Cargo target directory"):
        metrics.clean_cargo_cache()


@pytest.mark.parametrize("mismatch", [False, True])
def test_installed_extension_must_match_this_build(tmp_path, monkeypatch, mismatch) -> None:
    """实际安装的扩展摘要必须匹配本次 build 目录，而非仅匹配版本号和协议。"""
    root = tmp_path / "source"
    environment = tmp_path / "environment"
    package = environment / "site-packages/docvortex"
    package.mkdir(parents=True)
    binary = package / "_native.abi3.so"
    binary.write_bytes(b"current binary")
    build = root / "build/lib.example/docvortex"
    build.mkdir(parents=True)
    (build / binary.name).write_bytes(b"stale binary" if mismatch else binary.read_bytes())
    (root / "src/docvortex").mkdir(parents=True)
    (root / "src/docvortex/_compute_backend.py").write_text('"""验证原生协议。"""\n_PROTOCOL_VERSION = 36\n', encoding="utf-8")
    original_read_text = Path.read_text

    def read_with_windows_default(path, encoding=None, errors=None):
        """模拟 Windows 默认代码页，确保中文协议源码必须显式按 UTF-8 读取。"""
        return original_read_text(path, encoding=encoding or "cp1252", errors=errors)

    monkeypatch.setattr(Path, "read_text", read_with_windows_default)
    monkeypatch.setattr(metrics, "ROOT", root)
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setitem(sys.modules, "docvortex", SimpleNamespace(__file__=str(package / "__init__.py")))
    native = SimpleNamespace(__file__=str(binary), PROTOCOL_VERSION=36)
    monkeypatch.setitem(sys.modules, "docvortex._compute_backend", SimpleNamespace(get_native=lambda: native))
    monkeypatch.setattr(
        metrics.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="0.6.0", read_text=lambda name: "Tag: cp311-abi3"),
    )
    if mismatch:
        with pytest.raises(RuntimeError, match="does not match this checkout"):
            metrics.installed_identity()
    else:
        record = metrics.installed_identity()
        assert record["extension_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
        assert record["protocol"] == 36


def test_wheel_identity_checks_single_abi3_native_artifact(tmp_path) -> None:
    """记录被 ABI 检查使用的唯一 wheel 和真实扩展成员，拒绝多产物混淆。"""
    wheel = tmp_path / "docvortex-0.6.0-cp311-abi3-win_arm64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("docvortex/_native.pyd", b"arm64-binary")
    record = metrics.wheel_identities(tmp_path)[0]
    assert record["sha256"] == metrics.file_sha256(wheel)
    assert record["native_sha256"] == hashlib.sha256(b"arm64-binary").hexdigest()
    (tmp_path / "extra.whl").write_bytes(b"old")
    with pytest.raises(RuntimeError, match="exactly one"):
        metrics.wheel_identities(tmp_path)
