"""守卫独立引擎边界、导入副作用和唯一协议身份。"""

from __future__ import annotations

import ast
import subprocess
import sys
from importlib.util import find_spec
from importlib.metadata import requires
from pathlib import Path

import docvortex


def test_distribution_does_not_require_opencv() -> None:
    """安装元数据的必需和可选依赖都不能重新引入图像重依赖。"""
    assert not any("opencv" in requirement.lower() for requirement in requires("docvortex") or [])


def test_engine_does_not_ship_host_protocol_adapters() -> None:
    """宿主产品封装和旧结果迁移不再作为引擎模块发布。"""
    assert find_spec("docvortex.compat") is None
    assert not (Path(docvortex.__file__).parent / "compat").exists()


def test_source_has_no_host_pdftext_or_opencv_imports() -> None:
    """普通、惰性、类型检查及动态导入均不得依赖已移除的库。"""
    root = Path(docvortex.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        nodes = list(ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
        # 收集标准动态导入函数的别名，避免只识别 importlib.import_module 属性调用。
        dynamic_import_names = {"__import__"}
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in {"importlib", "builtins"}:
                dynamic_import_names.update(
                    item.asname or item.name for item in node.names if item.name in {"import_module", "__import__"}
                )
        for node in nodes:
            names = (
                [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in dynamic_import_names
                    or (isinstance(node.func, ast.Attribute) and node.func.attr in {"import_module", "__import__"})
                ):
                    if isinstance(node.args[0].value, str):
                        names.append(node.args[0].value)
            if any(name.split(".")[0] in {"mineru", "pdftext", "cv2"} for name in names):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders


def test_engine_tests_do_not_import_host_packages() -> None:
    """测试与辅助模块也必须独立运行，避免依赖宿主安装或测试服务器。"""
    root = Path(__file__).parent
    offenders = []
    forbidden = {"mineru", "pdftext", "fastapi", "httpx", "cv2"}
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
