"""守卫独立引擎边界、导入副作用和唯一协议身份。"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys

import docvortex


def test_source_has_no_host_or_pdftext_imports() -> None:
    """普通、惰性和类型检查导入均不得依赖宿主或已移除的抽取库。"""
    root = Path(docvortex.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            if any(name.split(".")[0] in {"mineru", "pdftext"} for name in names):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders


def test_public_import_is_lightweight_and_does_not_mutate_environment() -> None:
    """独立解释器导入公共 API 时，不加载重依赖或修改宿主环境。"""
    code = """
import os, sys
before = dict(os.environ)
import docvortex
from docvortex.api import analyze, postprocess, parse, render, convert
from docvortex.render.fragments import parse_list_item_marker, format_embedded_html
assert callable(parse) and callable(render)
assert before == dict(os.environ)
for name in ('torch', 'cv2', 'pypdfium2', 'pdftext', 'docgale', 'mineru', 'lxml', 'bs4', 'PIL', 'docx', 'reportlab', 'openai'):
    assert name not in sys.modules, name
"""
    completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_all_package_boundaries_are_explicit() -> None:
    """每个包都显式列出公开符号，避免运行时自动发现接口。"""
    root = Path(docvortex.__file__).parent
    for path in root.rglob("__init__.py"):
        nodes = ast.parse(path.read_text(encoding="utf-8")).body
        assert any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
            or isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
            for node in nodes
        ), path
