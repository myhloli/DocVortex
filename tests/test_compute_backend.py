"""Verify bounds for missing optional extensions, protocol mismatches, and explicit backends."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from docvortex import _compute_backend as backend


@pytest.fixture(autouse=True)
def reset_backend():
    """Isolate the process-level selection cache and prevent test environment variables from affecting other regressions."""
    backend.get_native.cache_clear()
    yield
    backend.get_native.cache_clear()


def test_python_never_imports_extension(monkeypatch):
    """When forcing Python the extension cannot be imported even if it is available."""
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "python")
    load = Mock(side_effect=AssertionError("must not import"))
    monkeypatch.setattr(backend.importlib, "import_module", load)
    assert backend.get_native() is None
    load.assert_not_called()


@pytest.mark.parametrize("error", [ImportError("missing"), OSError("bad binary")])
def test_missing_extension_modes(monkeypatch, error):
    """auto can be downgraded, rust must explicitly expose the installation problem and cause be retained."""
    monkeypatch.setattr(backend.importlib, "import_module", Mock(side_effect=error))
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "auto")
    assert backend.get_native() is None
    backend.get_native.cache_clear()
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "rust")
    with pytest.raises(RuntimeError) as caught:
        backend.get_native()
    assert caught.value.__cause__ is error


def test_protocol_and_invalid_mode(monkeypatch):
    """Old editable compiled products cannot be loaded as compatible extensions."""
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
    """Old protocol 28 falls back under auto and reports extension incompatibility under explicit rust."""
    monkeypatch.setattr(backend.importlib, "import_module", Mock(return_value=SimpleNamespace(PROTOCOL_VERSION=28)))
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", mode)
    if mode == "auto":
        assert backend.get_native() is None
        assert "protocol mismatch" in backend.backend_info()["unavailable_reason"]
    else:
        with pytest.raises(RuntimeError, match="compatible"):
            backend.get_native()


def test_unexpected_import_error_is_not_hidden(monkeypatch):
    """Errors in the extension's own code cannot be covered up by automatic fallback."""
    monkeypatch.setenv("DOCVORTEX_COMPUTE_BACKEND", "auto")
    monkeypatch.setattr(backend.importlib, "import_module", Mock(side_effect=ValueError("broken")))
    with pytest.raises(ValueError, match="broken"):
        backend.get_native()


def test_default_auto_prefers_rust_and_allows_missing_extension(monkeypatch):
    """By default, auto is preferred to be compatible with Rust, and Python is allowed to fall back when the extension is missing."""
    monkeypatch.delenv("DOCVORTEX_COMPUTE_BACKEND", raising=False)
    native = SimpleNamespace(PROTOCOL_VERSION=backend._PROTOCOL_VERSION)
    load = Mock(return_value=native)
    monkeypatch.setattr(backend.importlib, "import_module", load)
    assert backend.get_native() is native
    assert backend._SELECTED_MODE == "auto"
    backend.get_native.cache_clear()
    load.side_effect = ImportError("missing")
    assert backend.get_native() is None
