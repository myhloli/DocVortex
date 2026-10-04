"""Restore common internal entry for Native PDF table structure that already has table bbox."""

from .contracts import NativeTableCell, NativeTableInput, NativeTableRectangle, NativeTableResult, NativeTableRule
from .engine import coerce_native_table_rectangles, coerce_native_table_rules, recover_native_pdf_table

__all__ = [
    "NativeTableCell",
    "NativeTableInput",
    "NativeTableRectangle",
    "NativeTableResult",
    "NativeTableRule",
    "coerce_native_table_rectangles",
    "coerce_native_table_rules",
    "recover_native_pdf_table",
]
