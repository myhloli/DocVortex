"""验证源码构建必须包含原生扩展，并明确拒绝旧后端环境变量。"""

from pathlib import Path
import platform
import runpy
import sys
import sysconfig
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def build_setup(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """隔离 setuptools 调用，使构建配置测试不编译扩展或写入项目元数据。"""
    setup = Mock()
    monkeypatch.setitem(sys.modules, "setuptools", SimpleNamespace(setup=setup))
    monkeypatch.setitem(
        sys.modules, "setuptools_rust", SimpleNamespace(Binding=SimpleNamespace(PyO3="pyo3"), RustExtension=Mock())
    )
    monkeypatch.setattr(platform, "python_implementation", lambda: "CPython")
    monkeypatch.setattr(sysconfig, "get_config_var", lambda name: 0)
    monkeypatch.delenv("DOCVORTEX_BUILD_NATIVE", raising=False)
    monkeypatch.delenv("DOCVORTEX_COMPUTE_BACKEND", raising=False)
    return setup


def run_setup() -> None:
    """执行仓库构建入口，让测试核验真实配置而非另写一份选择逻辑。"""
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "setup.py"))


def test_source_build_always_declares_abi3_extension(build_setup: Mock) -> None:
    """源码安装始终声明非可选扩展和 cp311 ABI3，不能产出兼容纯包。"""
    run_setup()
    arguments = build_setup.call_args.kwargs
    assert len(arguments["rust_extensions"]) == 1
    assert arguments["options"]["bdist_wheel"]["py_limited_api"] == "cp311"
    extension = sys.modules["setuptools_rust"].RustExtension.call_args.kwargs
    assert extension["py_limited_api"] is True
    assert extension["optional"] is False


@pytest.mark.parametrize("variable", ["DOCVORTEX_BUILD_NATIVE", "DOCVORTEX_COMPUTE_BACKEND"])
@pytest.mark.parametrize("value", ["", "0", "1", "auto", "python", "rust"])
def test_removed_build_switch_is_rejected(
    build_setup: Mock, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """任何旧环境变量值（包括空值及过去的 native 值）都要求用户显式移除。"""
    monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError, match=f"{variable} was removed"):
        run_setup()
    build_setup.assert_not_called()


@pytest.mark.parametrize("unsupported", ["pypy", "free-threaded"])
def test_unsupported_interpreter_cannot_build_compatibility_package(
    build_setup: Mock, monkeypatch: pytest.MonkeyPatch, unsupported: str
) -> None:
    """不支持的解释器必须失败，不能转而生成纯 Python 包。"""
    if unsupported == "pypy":
        monkeypatch.setattr(platform, "python_implementation", lambda: "PyPy")
    else:
        monkeypatch.setattr(sysconfig, "get_config_var", lambda name: 1)
    with pytest.raises(RuntimeError, match="GIL-enabled CPython"):
        run_setup()
    build_setup.assert_not_called()
