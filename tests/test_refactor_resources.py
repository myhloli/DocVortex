"""守卫素材复制、按需 PDF 裁图和独立格式输入的行为边界。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from docvortex.assets import AssetStore


def test_asset_copy_keeps_independent_indexes_without_rehashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """已验证的不可变字节可共享，副本增删索引不得污染原集合。"""
    from docvortex.assets import store

    payload = b"existing immutable image"
    assets = AssetStore({"images/a.png": payload})

    def forbidden(_payload: bytes) -> object:
        """复制已经验证的素材时不应重新读取全部字节计算摘要。"""
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
    """独立解释器显式准备 CSV 时无需加载文件识别或数值计算依赖。"""
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
