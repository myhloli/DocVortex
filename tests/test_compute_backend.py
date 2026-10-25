"""验证必需扩展的加载、协议及旧环境变量不影响原生行为。"""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docvortex import _compute_backend as backend

_LEGACY_VARIABLES = ("DOCVORTEX_COMPUTE_BACKEND", "DOCVORTEX_BUILD_NATIVE")
_VARIABLE_GROUPS = [(_LEGACY_VARIABLES[0],), (_LEGACY_VARIABLES[1],), _LEGACY_VARIABLES]


@pytest.fixture(autouse=True)
def reset_backend(monkeypatch):
    """隔离扩展缓存及环境配置，使每个用例独立验证加载行为。"""
    for name in _LEGACY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    backend.get_native.cache_clear()
    yield
    backend.get_native.cache_clear()


@pytest.mark.parametrize("names", _VARIABLE_GROUPS, ids=["compute", "build", "both"])
@pytest.mark.parametrize("value", ["", "auto", "python", "rust", "0", "1", "typo"])
def test_legacy_variables_do_not_affect_native_loading(monkeypatch, names, value, recwarn, caplog):
    """单独或同时设置任意旧值均加载原生扩展，不修改环境或增加警告。"""
    for name in names:
        monkeypatch.setenv(name, value)
    native = SimpleNamespace(PROTOCOL_VERSION=backend._PROTOCOL_VERSION)
    load = Mock(return_value=native)
    monkeypatch.setattr(backend.importlib, "import_module", load)
    assert backend.get_native() is native
    assert {name: os.environ[name] for name in names} == dict.fromkeys(names, value)
    load.assert_called_once_with("docvortex._native")
    assert not recwarn
    assert not caplog.records


@pytest.mark.parametrize("error", [ImportError("missing"), OSError("bad binary")])
@pytest.mark.parametrize("with_legacy_variables", [False, True])
def test_missing_or_broken_extension_fails_without_fallback(monkeypatch, error, with_legacy_variables):
    """旧变量不掩盖安装错误，失败保留 cause 且修复扩展后可以重新加载。"""
    if with_legacy_variables:
        monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "python")
        monkeypatch.setenv("DOCVORTEX_BUILD_NATIVE", "0")
    load = Mock(side_effect=error)
    monkeypatch.setattr(backend.importlib, "import_module", load)
    with pytest.raises(RuntimeError, match="compatible Rust native extension") as caught:
        backend.get_native()
    assert caught.value.__cause__ is error
    native = SimpleNamespace(PROTOCOL_VERSION=backend._PROTOCOL_VERSION)
    load.side_effect = None
    load.return_value = native
    assert backend.get_native() is native
    assert load.call_count == 2


@pytest.mark.parametrize("protocol", [None, -1, 28, 35, 36, 37, 38, 39])
def test_old_or_missing_protocol_is_rejected(monkeypatch, protocol):
    """旧 editable 编译产物不能通过新协议验证，也不会成为 Python 降级入口。"""
    monkeypatch.setattr(backend.importlib, "import_module", Mock(return_value=SimpleNamespace(PROTOCOL_VERSION=protocol)))
    with pytest.raises(RuntimeError, match="compatible Rust native extension") as caught:
        backend.get_native()
    assert isinstance(caught.value.__cause__, ImportError)
    assert "protocol mismatch" in str(caught.value.__cause__)


def test_unexpected_import_error_is_not_hidden(monkeypatch):
    """扩展代码自身异常直接传播，安装诊断不掩盖编程错误。"""
    monkeypatch.setattr(backend.importlib, "import_module", Mock(side_effect=ValueError("broken")))
    with pytest.raises(ValueError, match="broken"):
        backend.get_native()


def test_native_cache_ignores_environment_changes_and_can_be_cleared(monkeypatch, recwarn, caplog):
    """缓存建立后改变两个旧变量仍复用扩展，清理缓存后能够重新导入。"""
    native = SimpleNamespace(PROTOCOL_VERSION=backend._PROTOCOL_VERSION)
    load = Mock(return_value=native)
    monkeypatch.setattr(backend.importlib, "import_module", load)
    assert backend.get_native() is native
    assert backend.get_native() is native
    load.assert_called_once_with("docvortex._native")
    for compute, build in [("python", "0"), ("typo", ""), ("rust", "1")]:
        monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", compute)
        monkeypatch.setenv("DOCVORTEX_BUILD_NATIVE", build)
        assert backend.get_native() is native
        assert os.environ["DOCVORTEX_COMPUTE_BACKEND"] == compute
        assert os.environ["DOCVORTEX_BUILD_NATIVE"] == build
    load.assert_called_once()
    backend.get_native.cache_clear()
    assert backend.get_native() is native
    assert load.call_count == 2
    assert not recwarn
    assert not caplog.records
