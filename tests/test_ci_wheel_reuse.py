"""验证同平台 wheel 的交接、失败边界、独立安装来源和依赖下界保留。"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
import zipfile

import pytest

from tools import ci_build_metrics as metrics
from tools import ci_wheel_reuse as reuse


@pytest.fixture
def wheel_fixture(tmp_path, monkeypatch):
    """生成独立产品 wheel、构建证据及安装文件，便于注入传输和缓存错误。"""
    root = tmp_path / "repo"
    directory = tmp_path / "wheelhouse"
    output = root / "build/ci-metrics"
    environment = tmp_path / "venv"
    site = environment / "site-packages"
    for path in (directory, output, site):
        path.mkdir(parents=True)
    wheel = directory / "docvortex-0.6.0-cp311-abi3-linux_x86_64.whl"
    members = {
        "docvortex/__init__.py": b"# test\n",
        "docvortex/_native.abi3.so": b"binary",
        "docvortex/resources/font": b"font",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
            installed = site / name
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(data)
    binary = root / "build/lib.test/docvortex/_native.abi3.so"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    identity = {
        "git_commit": "commit",
        "github_run_id": "run",
        "runner_os": "Linux",
        "runner_arch": "X64",
        "source_sha256": "source",
    }
    monkeypatch.setattr(metrics, "ROOT", root)
    monkeypatch.setattr(reuse, "source_identity", lambda: dict(identity))
    monkeypatch.setattr(
        metrics, "build_context", lambda profile: {"git_commit": "commit", "native_build_inputs_sha256": "native"}
    )
    build = {
        "context": metrics.build_context("release"),
        "returncode": 0,
        "cargo_events": [{"event": "compiling", "crate": "docvortex-python"}, {"event": "finished", "profile": "release"}],
    }
    (output / "build.json").write_text(json.dumps(build), encoding="utf-8")
    reuse.publish_manifest(directory, output)
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr(metrics, "installed_identity", lambda directory: {"extension_sha256": "installed"})
    direct = {"url": wheel.resolve().as_uri(), "archive_info": {"hashes": {"sha256": metrics.file_sha256(wheel)}}}
    distribution = SimpleNamespace(read_text=lambda name: json.dumps(direct), locate_file=lambda name: site / name)
    monkeypatch.setattr(metrics.metadata, "distribution", lambda name: distribution)
    return SimpleNamespace(
        directory=directory, output=output, wheel=wheel, binary=binary, identity=identity, site=site, direct=direct
    )


def test_build_and_installed_payload_are_bound_to_same_wheel(wheel_fixture) -> None:
    """生产清单和安装检查使用相同 wheel 摘要，并逐文件验证源码、资源与扩展。"""
    record = reuse.verify_installed_payload(wheel_fixture.directory)
    assert record["verified_payload_files"] == 3
    assert record["wheel_manifest"]["wheel"]["sha256"] == metrics.file_sha256(wheel_fixture.wheel)


@pytest.mark.parametrize("field", ["git_commit", "github_run_id", "runner_os", "runner_arch", "source_sha256"])
def test_wrong_source_run_or_platform_is_rejected(wheel_fixture, field) -> None:
    """相同版本号不能掩盖不同提交、运行、架构或工作树内容。"""
    wheel_fixture.identity[field] = "other"
    with pytest.raises(RuntimeError, match="source/run/platform"):
        reuse.verified_manifest(wheel_fixture.directory)


def test_changed_wheel_is_rejected_even_if_native_member_is_unchanged(wheel_fixture) -> None:
    """Python 源码或资源改变也会改变归档摘要，不能只检查原生扩展。"""
    with zipfile.ZipFile(wheel_fixture.wheel, "a") as archive:
        archive.writestr("docvortex/added.py", b"changed")
    with pytest.raises(RuntimeError, match="digest"):
        reuse.verified_manifest(wheel_fixture.directory)


@pytest.mark.parametrize("changed", ["docvortex/__init__.py", "docvortex/_native.abi3.so", "docvortex/resources/font"])
def test_installed_payload_must_match_archive(wheel_fixture, changed) -> None:
    """拒绝错误 wheel 缓存、安装后被替换的源码或字体资源。"""
    (wheel_fixture.site / changed).write_bytes(b"stale")
    with pytest.raises(RuntimeError, match="Installed payload differs"):
        reuse.verify_installed_payload(wheel_fixture.directory)


@pytest.mark.parametrize("kind", ["editable", "wrong-hash", "wrong-url"])
def test_install_origin_must_identify_downloaded_wheel(wheel_fixture, kind) -> None:
    """即使文件偶然相同，也不接受 editable 或不匹配的安装归档身份。"""
    if kind == "editable":
        wheel_fixture.direct["dir_info"] = {"editable": True}
    elif kind == "wrong-hash":
        wheel_fixture.direct["archive_info"]["hashes"]["sha256"] = "wrong"
    else:
        wheel_fixture.direct["url"] = "file:///other.whl"
    with pytest.raises(RuntimeError, match="does not identify"):
        reuse.verify_installed_payload(wheel_fixture.directory)


def test_local_wheel_optional_metadata_hash_is_not_required(wheel_fixture) -> None:
    """兼容 uv 合法省略可选摘要，实际归档和安装文件仍逐一检查。"""
    wheel_fixture.direct["archive_info"] = {}
    assert reuse.verify_installed_payload(wheel_fixture.directory)["verified_payload_files"] == 3


@pytest.mark.parametrize("fault", ["failure", "wrong-commit", "wrong-source", "fresh-only", "unfinished", "stale-binary"])
def test_manifest_requires_current_compilation(wheel_fixture, fault) -> None:
    """生产交接必须有本次编译、正确源码及相符二进制，零退出码并不足够。"""
    path = wheel_fixture.output / "build.json"
    build = reuse.read_json(path)
    if fault == "failure":
        build["returncode"] = 1
    elif fault == "wrong-commit":
        build["context"]["git_commit"] = "old"
    elif fault == "wrong-source":
        build["context"]["native_build_inputs_sha256"] = "old"
    elif fault == "fresh-only":
        build["cargo_events"][0]["event"] = "fresh"
    elif fault == "unfinished":
        build["cargo_events"].pop()
    else:
        wheel_fixture.binary.write_bytes(b"old")
    path.write_text(json.dumps(build), encoding="utf-8")
    with pytest.raises(RuntimeError, match="build"):
        reuse.publish_manifest(wheel_fixture.directory, wheel_fixture.output)


@pytest.mark.parametrize("returncode", [0, 7])
def test_install_keeps_test_extra_constraints_and_failure(wheel_fixture, monkeypatch, returncode) -> None:
    """安装仅指向 wheel，保留 test extra 和真实下界；依赖失败原样返回。"""
    calls = []

    def measured(command, output, name, profile):
        """截获无 shell 安装参数并模拟依赖解析退出码。"""
        calls.append((command, profile))
        return returncode

    monkeypatch.setattr(metrics, "run_measured", measured)
    constraints = ["pydantic==2.12.5", "pypdfium2==5.13.0", "onnxruntime==1.24.4"]
    assert reuse.install_wheel(wheel_fixture.directory, constraints, wheel_fixture.output) == returncode
    assert calls == [(["uv", "pip", "install", "--verbose", f"{wheel_fixture.wheel.resolve()}[test]", *constraints], "wheel")]
    assert (wheel_fixture.output / "installed.json").exists() is (returncode == 0)


def test_consumer_context_never_invokes_rust_tools(tmp_path, monkeypatch) -> None:
    """wheel 安装环境无需 Cargo 或 rustc，避免记录宿主预装工具链为生产工具链。"""
    commands = []

    def output(*command):
        """只允许读取 Git 提交，任何编译器调用都让此测试失败。"""
        commands.append(command)
        assert command[0] == "git"
        return "commit"

    monkeypatch.setattr(metrics, "ROOT", tmp_path)
    monkeypatch.setattr(metrics, "command_output", output)
    context = metrics.build_context("wheel")
    assert context["rustc"] is None and context["cargo"] is None
    assert commands == [("git", "rev-parse", "HEAD")]
