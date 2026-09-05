# Copyright (c) Opendatalab. All rights reserved.

"""原生 PDF 的公共复用能力；模型区域融合可独立使用这些纯数据操作。"""

from .native_text import _build_native_line_items as build_native_line_items
from .line_merging import (
    _merge_overlapping_inline_text_clusters as merge_overlapping_inline_text_clusters,
    _merge_same_baseline_text_lines as merge_same_baseline_text_lines,
)
from .script_geometry import ScriptRole, classify_char_script_roles
from .spatial_text import project_ocr_table_text as project_table_text
from .table_text_styles import render_native_table_html_with_scripts
from .table_recovery import NativeTableInput, coerce_native_table_rectangles, coerce_native_table_rules, recover_native_pdf_table

__all__ = ["build_native_line_items", "merge_overlapping_inline_text_clusters", "merge_same_baseline_text_lines",
           "ScriptRole", "classify_char_script_roles", "project_table_text", "render_native_table_html_with_scripts",
           "NativeTableInput", "coerce_native_table_rectangles", "coerce_native_table_rules", "recover_native_pdf_table"]
