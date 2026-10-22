"""验证必需扩展的加载、协议和已退役环境变量边界。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docvortex import _compute_backend as backend


@pytest.fixture(autouse=True)
def reset_backend(monkeypatch):
    """隔离扩展缓存及环境配置，使每个用例独立验证加载行为。"""
    for name in backend._RETIRED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    backend.get_native.cache_clear()
    yield
    backend.get_native.cache_clear()


@pytest.mark.parametrize("name", backend._RETIRED_VARIABLES)
@pytest.mark.parametrize("value", ["", "auto", "python", "rust", "0", "1", "typo"])
def test_retired_variables_are_rejected_before_import(monkeypatch, name, value):
    """旧配置的所有值包括空值均提示删除，不再解释为后端或构建开关。"""
    monkeypatch.setenv(name, value)
    load = Mock(side_effect=AssertionError("must not import"))
    monkeypatch.setattr(backend.importlib, "import_module", load)
    with pytest.raises(RuntimeError, match=f"Remove retired environment variable.*{name}"):
        backend.get_native()
    load.assert_not_called()


@pytest.mark.parametrize("error", [ImportError("missing"), OSError("bad binary")])
def test_missing_or_broken_extension_fails_without_fallback(monkeypatch, error):
    """安装错误保持 cause 且不缓存失败，修复扩展后可以重新加载。"""
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


@pytest.mark.parametrize("protocol", [None, -1, 28, 35])
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


def test_native_is_cached_but_retired_configuration_is_always_checked(monkeypatch):
    """成功扩展仅导入一次，随后出现的旧配置仍明确报错。"""
    native = SimpleNamespace(PROTOCOL_VERSION=backend._PROTOCOL_VERSION)
    load = Mock(return_value=native)
    monkeypatch.setattr(backend.importlib, "import_module", load)
    assert backend.get_native() is native
    assert backend.get_native() is native
    load.assert_called_once_with("docvortex._native")
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "rust")
    with pytest.raises(RuntimeError, match="Remove retired"):
        backend.get_native()
    load.assert_called_once()
