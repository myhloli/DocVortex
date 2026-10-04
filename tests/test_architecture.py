"""Guard independent engine boundaries, import side effects, and unique protocol identities."""

from __future__ import annotations

import ast
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

import docvortex


def test_engine_does_not_ship_host_protocol_adapters() -> None:
    """Host product packaging and legacy result migrations are no longer released as engine modules."""
    assert find_spec("docvortex.compat") is None
    assert not (Path(docvortex.__file__).parent / "compat").exists()


def test_source_has_no_host_or_pdftext_imports() -> None:
    """Normal, lazy, and type-checked imports must not rely on the host or removed extraction libraries."""
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


def test_engine_tests_do_not_import_host_packages() -> None:
    """Test and auxiliary modules must also run independently to avoid dependence on the host installation or test server."""
    root = Path(__file__).parent
    offenders = []
    forbidden = {"mineru", "pdftext", "fastapi", "httpx"}
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            if any(name.split(".")[0] in forbidden for name in names):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders


def test_public_import_is_lightweight_and_does_not_mutate_environment() -> None:
    """When the standalone interpreter imports public API, it does not load heavy dependencies or modify the host environment."""
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
    """Each package explicitly lists public symbols to avoid automatic discovery of interfaces at runtime."""
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
