"""Selecting an in-process private compute backend does not change the public Python type or error semantics."""

from __future__ import annotations

import importlib
import os
import hashlib
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType

_PROTOCOL_VERSION = 30
_SELECTED_MODE = None
_LOAD_FAILURE = None


@lru_cache(maxsize=1)
def get_native() -> ModuleType | None:
    """The default auto takes precedence over Rust, and the backend is fixed when used for the first time; only auto allows rollback on loading failure, and calculation exceptions are propagated directly."""
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
    """To report the actual backend for baseline and installation checks, environment variables cannot be used to impersonate executed Rust."""
    native = get_native()
    path = getattr(native, "__file__", None)
    bridge = sys.modules.get("docvortex.document.pdf.text._pdfium_bridge")
    objects = sys.modules.get("docvortex.document.pdf._object_bridge")
    fonts = sys.modules.get("docvortex.document.pdf.font_runtime")
    snapshots = sys.modules.get("docvortex.document.pdf.snapshot_bridge")
    classification = sys.modules.get("docvortex.document.pdf.classification_bridge")
    snapshot_stats = getattr(native, "text_snapshot_stats", None)
    span_calls, span_unsupported, span_contents = snapshot_stats() if snapshot_stats is not None else (0, 0, 0)
    return {
        "native_inline_style_batches": native.inline_style_stats() if native is not None else 0,
        "native_span_content_calls": span_contents,
        **(
            classification.classification_bridge_info()
            if classification is not None
            else {"native_classification_unavailable_reason": "not probed"}
        ),
        "native_classification_snapshot_calls": native.classification_snapshot_stats() if native is not None else 0,
        "native_script_snapshot_batches": native.script_snapshot_stats() if native is not None else 0,
        "native_span_assignment_calls": span_calls,
        "native_table_script_cell_batches": table_scripts.table_script_stats()
        if (table_scripts := sys.modules.get("docvortex.analyzers.native.pdf.table_text_styles")) is not None
        else (0, 0, 0),
        "native_geometry_evidence_stats": native.geometry_evidence_stats()
        if native is not None and hasattr(native, "geometry_evidence_stats")
        else (0, 0, 0),
        "native_span_assignment_unsupported": span_unsupported,
        "backend": "rust" if native is not None else "python",
        "protocol": getattr(native, "PROTOCOL_VERSION", None),
        "extension": path,
        "extension_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None,
        "requested_backend": _SELECTED_MODE,
        "unavailable_reason": _LOAD_FAILURE,
        **(
            snapshots.snapshot_bridge_info()
            if snapshots is not None
            else {
                "native_text_snapshot_calls": 0,
                "native_text_snapshot_empty_pages": 0,
                "native_text_snapshot_unavailable_reason": "not probed",
            }
        ),
        **(
            fonts.native_font_runtime_info()
            if fonts is not None
            else {"native_font_provider_installed": False, "native_font_provider_unavailable_reason": "not probed"}
        ),
        **(
            objects.bridge_info()
            if objects is not None
            else {
                "pdfium_object_bridge_calls": 0,
                "pdfium_object_bridge_unavailable_reason": "not probed",
                "pdfium_text_visibility_bridge_calls": 0,
                "pdfium_text_visibility_bridge_unavailable_reason": "not probed",
                "pdfium_path_evidence_bridge_calls": 0,
                "pdfium_path_evidence_bridge_unavailable_reason": "not probed",
            }
        ),
        **(
            bridge.bridge_info()
            if bridge is not None
            else {"pdfium_bridge_calls": 0, "pdfium_bridge_unavailable_reason": "not probed"}
        ),
    }
