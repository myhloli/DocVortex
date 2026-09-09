"""守卫共享能力的归属、类型身份与轻量输入契约。"""

from __future__ import annotations

import ast
import pickle
from importlib.util import resolve_name
from pathlib import Path

import docvortex


def test_shared_layers_do_not_import_native_implementations() -> None:
    """基础、内容、codec 和文档操作不反向依赖原生实现；识别分派单独保留。"""
    root = Path(docvortex.__file__).parent
    offenders = []
    for group in ("foundation", "content", "codecs", "document"):
        for path in (root / group).rglob("*.py"):
            # 文件识别显式调用格式专用容器校验器，属于保留的格式路由边界。
            if path == root / "document/detection.py":
                continue
            name = "docvortex." + ".".join(path.relative_to(root).with_suffix("").parts)
            package = name.removesuffix(".__init__") if path.name == "__init__.py" else name.rpartition(".")[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    target = resolve_name("." * node.level + (node.module or ""), package)
                    if target.startswith("docvortex.analyzers"):
                        offenders.append(f"{path}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path}:{node.lineno}" for alias in node.names if alias.name.startswith("docvortex.analyzers")
                    )
    assert not offenders


def test_existing_shared_imports_keep_type_and_function_identity() -> None:
    """所有历史入口仍重导出同一实现，类型的 pickle 路径能够往返。"""
    from docvortex.analyzers.native._shared.image import image_to_bytes as original_image
    from docvortex.analyzers.native._shared.markup import TextStyle as original_style
    from docvortex.content.markup import TextStyle
    from docvortex.document.contracts import HtmlSourceContext
    from docvortex.document.contracts import HtmlSourceContext as original_context
    from docvortex.foundation.image_encoding import image_to_bytes

    assert original_context is HtmlSourceContext
    assert original_image is image_to_bytes
    assert original_style is TextStyle
    for value in (HtmlSourceContext(source_uri="https://example.com/document"), TextStyle()):
        restored = pickle.loads(pickle.dumps(value))
        assert type(restored) is type(value)
        assert restored == value


def test_canonical_type_annotations_work_without_importing_old_facades() -> None:
    """新入口单独导入时也能解析注解和构造 Pydantic 适配器，不要求预加载旧模块。"""
    import subprocess
    import sys

    code = """
import sys
from typing import get_type_hints
from pydantic import TypeAdapter
from docvortex.document.contracts import HtmlSourceContext
from docvortex.content.markup.formula import FormulaExtraction
from docvortex.content.markup.styles import ElementStyle, TextStyle
from docvortex.content.markup.anchors import MarkupAnchorDocument
assert 'docvortex.analyzers.native.html.contracts' not in sys.modules
for cls in (HtmlSourceContext, FormulaExtraction, ElementStyle, MarkupAnchorDocument):
    assert get_type_hints(cls)
assert get_type_hints(ElementStyle)['text'] is TextStyle
context = TypeAdapter(HtmlSourceContext).validate_python({'source_uri': 'https://example.com'})
assert context.source_uri == 'https://example.com'
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
