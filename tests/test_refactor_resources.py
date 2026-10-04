"""Guard behavioral boundaries for material duplication, on-demand PDF cropping, and independent format import."""

from __future__ import annotations

import subprocess
import sys

import pytest

from docvortex.assets import AssetStore


def test_asset_copy_keeps_independent_indexes_without_rehashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verified immutable bytes can be shared, and replica addition and deletion indexes must not pollute the original collection."""
    from docvortex.assets import store

    payload = b"existing immutable image"
    assets = AssetStore({"images/a.png": payload})

    def forbidden(_payload: bytes) -> object:
        """The full byte count digest should not be re-read when copying already verified material."""
        raise AssertionError("Existing assets must not be hashed again")

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "sha256", forbidden)
        copied = assets.copy()
    assert copied["images/a.png"] is assets["images/a.png"]
    copied.add("images/b.png", payload)
    assets.add("images/c.png", b"other image")
    assert set(copied) == {"images/a.png", "images/b.png"}
    assert set(assets) == {"images/a.png", "images/c.png"}
    with pytest.raises(ValueError, match="Conflicting"):
        copied.add("images/a.png", b"changed")


def test_explicit_source_does_not_load_detection_models() -> None:
    """The standalone interpreter explicitly prepares CSV without loading file recognition or numerical calculation dependencies."""
    code = """
import sys
from docvortex.document.source import prepare_source
result = prepare_source(b'a,b\\n1,2\\n', file_suffix='csv')
assert result.file_suffix == 'csv'
for name in ('docvortex.document.detection', 'magika', 'onnxruntime', 'numpy'):
    assert name not in sys.modules, name
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
