"""选择进程内的私有计算后端，不改变公共 Python 类型或错误语义。"""

from __future__ import annotations

import importlib
import os
import hashlib
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

_PROTOCOL_VERSION = 8
_SELECTED_MODE = None
_LOAD_FAILURE = None


@lru_cache(maxsize=1)
def get_native() -> ModuleType | None:
    """首次使用时固定后端；仅加载失败允许 auto 回退，计算异常直接传播。"""
    global _SELECTED_MODE, _LOAD_FAILURE
    mode = os.environ.get("DOCVORTEX_COMPUTE_BACKEND", "auto")
    _SELECTED_MODE, _LOAD_FAILURE = mode, None
    if mode not in {"auto", "python", "rust"}:
        raise ValueError("DOCVORTEX_COMPUTE_BACKEND must be auto, python or rust")
    if mode == "python":
        return None
    try:
        native = importlib.import_module("docvortex._native")
        if getattr(native, "PROTOCOL_VERSION", None) != _PROTOCOL_VERSION:
            raise ImportError("DocVortex native protocol mismatch; rebuild the extension")
    except (ImportError, OSError) as exc:
        _LOAD_FAILURE = f"{type(exc).__name__}: {exc}"
        if mode == "rust":
            raise RuntimeError("DOCVORTEX_COMPUTE_BACKEND=rust requires a compatible native extension") from exc
        return None
    return native


def backend_info() -> dict[str, str | int | None]:
    """为基准和安装检查报告实际后端，不能用环境变量冒充已执行 Rust。"""
    native = get_native()
    path = getattr(native, "__file__", None)
    bridge = sys.modules.get("docvortex.document.pdf.text._pdfium_bridge")
    return {
        "backend": "rust" if native is not None else "python",
        "protocol": getattr(native, "PROTOCOL_VERSION", None),
        "extension": path,
        "extension_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None,
        "requested_backend": _SELECTED_MODE,
        "unavailable_reason": _LOAD_FAILURE,
        **(
            bridge.bridge_info()
            if bridge is not None
            else {"pdfium_bridge_calls": 0, "pdfium_bridge_unavailable_reason": "not probed"}
        ),
    }
