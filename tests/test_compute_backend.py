"""验证可选扩展缺失、协议不匹配和显式后端的边界。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docvortex import _compute_backend as backend


@pytest.fixture(autouse=True)
def reset_backend():
    """隔离进程级选择缓存，不让测试环境变量影响其它回归。"""
    backend.get_native.cache_clear()
    yield
    backend.get_native.cache_clear()


def test_python_never_imports_extension(monkeypatch):
    """强制 Python 时即使扩展可用也不能导入。"""
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "python")
    load = Mock(side_effect=AssertionError("must not import"))
    monkeypatch.setattr(backend.importlib, "import_module", load)
    assert backend.get_native() is None
    load.assert_not_called()


@pytest.mark.parametrize("error", [ImportError("missing"), OSError("bad binary")])
def test_missing_extension_modes(monkeypatch, error):
    """auto 可以降级，rust 必须显式暴露安装问题并保留 cause。"""
    monkeypatch.setattr(backend.importlib, "import_module", Mock(side_effect=error))
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "auto")
    assert backend.get_native() is None
    backend.get_native.cache_clear()
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "rust")
    with pytest.raises(RuntimeError) as caught:
        backend.get_native()
    assert caught.value.__cause__ is error


def test_protocol_and_invalid_mode(monkeypatch):
    """旧 editable 编译产物不能当作兼容扩展加载。"""
    monkeypatch.setattr(backend.importlib, "import_module", Mock(return_value=SimpleNamespace(PROTOCOL_VERSION=-1)))
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "rust")
    with pytest.raises(RuntimeError, match="compatible"):
        backend.get_native()
    backend.get_native.cache_clear()
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "typo")
    with pytest.raises(ValueError, match="must be"):
        backend.get_native()


@pytest.mark.parametrize("mode", ["auto", "rust"])
def test_protocol_28_obeys_backend_selection(monkeypatch, mode):
    """旧协议 28 在 auto 下回退，在显式 rust 下报告扩展不兼容。"""
    monkeypatch.setattr(backend.importlib, "import_module", Mock(return_value=SimpleNamespace(PROTOCOL_VERSION=28)))
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", mode)
    if mode == "auto":
        assert backend.get_native() is None
        assert "protocol mismatch" in backend.backend_info()["unavailable_reason"]
    else:
        with pytest.raises(RuntimeError, match="compatible"):
            backend.get_native()


def test_unexpected_import_error_is_not_hidden(monkeypatch):
    """扩展自身代码错误不能被自动回退掩盖。"""
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "auto")
    monkeypatch.setattr(backend.importlib, "import_module", Mock(side_effect=ValueError("broken")))
    with pytest.raises(ValueError, match="broken"):
        backend.get_native()


def test_default_auto_prefers_rust_and_allows_missing_extension(monkeypatch):
    """默认 auto 优先选择兼容 Rust，扩展缺失时允许 Python 回退。"""
    monkeypatch.delenv("DOCVORTEX_COMPUTE_BACKEND", raising=False)
    native = SimpleNamespace(PROTOCOL_VERSION=backend._PROTOCOL_VERSION)
    load = Mock(return_value=native)
    monkeypatch.setattr(backend.importlib, "import_module", load)
    assert backend.get_native() is native
    assert backend._SELECTED_MODE == "auto"
    backend.get_native.cache_clear()
    load.side_effect = ImportError("missing")
    assert backend.get_native() is None
