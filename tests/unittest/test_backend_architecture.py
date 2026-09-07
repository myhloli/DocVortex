from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


_DOCVORTEX_ROOT = Path(importlib.util.find_spec("docvortex").origin).parent


_CHINESE_DOCSTRING_PATHS = (
    _DOCVORTEX_ROOT / "render/_internal/common/html_table.py",
    _DOCVORTEX_ROOT / "render/_internal/latex",
)


_REMOVED_INTERNAL_MODULES = (
    "docvortex.compat",
    "docvortex.analyzers.native.model",
    "docvortex.analyzers.native.native_pdf",
    "docvortex.analyzers.native.office.chart",
    "docvortex.analyzers.native.office.docx.tools",
    "docvortex.analyzers.native.office.docx.tools.math",
    "docvortex.analyzers.native.office.image_equation",
    "docvortex.analyzers.native.office.legacy.errors",
    "docvortex.analyzers.native.office.legacy.limits",
    "docvortex.analyzers.native.office.legacy.mtef",
    "docvortex.analyzers.native.office.legacy.mtef_v5",
    "docvortex.analyzers.native.office.legacy.stream",
    "docvortex.analyzers.native.office.math",
    "docvortex.analyzers.native.office.ooxml_equation",
    "docvortex.render._internal.common.inline",
)


def _module_name(path: Path) -> str:
    """把引擎安装目录内的真实文件转换为完整模块名。"""
    parts = list(path.relative_to(_DOCVORTEX_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolved_imports(path: Path) -> set[str]:
    """把绝对和相对 import 都解析为完整模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_name = _module_name(path)
    package_parts = module_name.split(".") if path.name == "__init__.py" else module_name.split(".")[:-1]
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            if node.module:
                imports.add(node.module)
            continue
        resolved_parts = package_parts[:]
        if node.level > 1:
            resolved_parts = resolved_parts[: -(node.level - 1)]
        if node.module:
            resolved_parts.extend(node.module.split("."))
        imports.add(".".join(resolved_parts))
    return imports


def _relative_imports(path: Path) -> set[str]:
    """读取 Python 文件对同包模块的相对 import 名称。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module}


def _contains_chinese(text: str) -> bool:
    """判断职责说明中是否至少包含一个中文字符。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _iter_python_paths(path: Path) -> list[Path]:
    """返回文件自身或目录下全部 Python 文件。"""
    return sorted(path.rglob("*.py")) if path.is_dir() else [path]


def test_new_first_party_definitions_have_chinese_docstrings() -> None:
    """守卫本次新增的一方函数、方法和类都有中文职责说明。"""
    offenders: list[str] = []
    for configured_path in _CHINESE_DOCSTRING_PATHS:
        for path in _iter_python_paths(configured_path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                docstring = ast.get_docstring(node)
                if not docstring or not _contains_chinese(docstring):
                    offenders.append(f"{path}:{node.lineno}:{node.name}")
    assert not offenders


def test_latex_and_render_common_keep_private_dependencies_one_way() -> None:
    """守卫 LaTeX 不跨入其它格式私有实现，common 也不反向依赖格式包。"""
    format_names = ("content_list", "docx", "epub", "html", "markdown", "pdf", "structured_content")
    latex_offenders = {
        str(path): sorted(
            module
            for module in _resolved_imports(path)
            if module.startswith(tuple(f"docvortex.render._internal.{name}" for name in format_names))
        )
        for path in _python_paths("render/_internal/latex")
    }
    common_offenders = {
        str(path): sorted(
            module
            for module in _resolved_imports(path)
            if module.startswith(tuple(f"docvortex.render._internal.{name}" for name in (*format_names, "latex")))
        )
        for path in _python_paths("render/_internal/common")
    }
    assert not {path: imports for path, imports in latex_offenders.items() if imports}
    assert not {path: imports for path, imports in common_offenders.items() if imports}


def test_flash_office_does_not_depend_on_pdf_implementation() -> None:
    """守卫 Office 格式只复用中立能力，不反向依赖 Flash PDF 实现。"""
    offenders = {
        str(path): sorted(module for module in _resolved_imports(path) if module.startswith("docvortex.analyzers.native.pdf"))
        for path in _python_paths("analyzers/native/office")
    }
    assert not {path: imports for path, imports in offenders.items() if imports}


def test_flash_spreadsheet_dependencies_are_one_way() -> None:
    """守卫 XLS/XLSX 只依赖中立 spreadsheet 层且共享层不反向引用格式实现。"""
    xls_offenders = {
        str(path): sorted(
            module for module in _resolved_imports(path) if module.startswith("docvortex.analyzers.native.office.xlsx")
        )
        for path in _python_paths("analyzers/native/office/xls")
    }
    spreadsheet_offenders = {
        str(path): sorted(
            module
            for module in _resolved_imports(path)
            if module.startswith(("docvortex.analyzers.native.office.xls", "docvortex.analyzers.native.office.xlsx"))
        )
        for path in _python_paths("analyzers/native/office/spreadsheet")
    }
    assert not {path: imports for path, imports in xls_offenders.items() if imports}
    assert not {path: imports for path, imports in spreadsheet_offenders.items() if imports}


def test_flash_equation_and_legacy_dependencies_are_one_way() -> None:
    """守卫公式层不反向引用格式实现，legacy 层也不重新承载公式解析。"""
    format_prefixes = tuple(
        f"docvortex.analyzers.native.office.{name}"
        for name in ("doc", "docx", "odf", "ppt", "pptx", "rtf", "spreadsheet", "xls", "xlsx")
    )
    equation_offenders = {
        str(path): sorted(module for module in _resolved_imports(path) if module.startswith(format_prefixes))
        for path in _python_paths("analyzers/native/office/equation")
    }
    legacy_offenders = {
        str(path): sorted(
            module for module in _resolved_imports(path) if module.startswith("docvortex.analyzers.native.office.equation")
        )
        for path in _python_paths("analyzers/native/office/legacy")
    }
    assert not {path: imports for path, imports in equation_offenders.items() if imports}
    assert not {path: imports for path, imports in legacy_offenders.items() if imports}


def test_removed_private_module_paths_have_no_active_references() -> None:
    """守卫严格迁移后的活动生产代码不再引用旧私有路径。"""
    offenders: list[str] = []
    for path in _python_paths(""):
        text = path.read_text(encoding="utf-8")
        for module_name in _REMOVED_INTERNAL_MODULES:
            if module_name in text:
                offenders.append(f"{path.relative_to(_DOCVORTEX_ROOT)}:{module_name}")
    assert not offenders


def test_removed_private_module_paths_are_not_importable() -> None:
    """验证严格切换后不存在可被误用的旧私有模块壳。"""
    offenders: list[str] = []
    for module_name in _REMOVED_INTERNAL_MODULES:
        try:
            spec = importlib.util.find_spec(module_name)
        except ModuleNotFoundError:
            spec = None
        if spec is not None:
            offenders.append(module_name)
    assert not offenders


def test_table_merge_package_keeps_one_way_internal_dependencies() -> None:
    """守卫 table_merge 低层模块不反向导入内容合并或文档编排模块。"""
    package_path = _DOCVORTEX_ROOT / "content/table"
    allowed_imports = {
        "models.py": set(),
        "html.py": {"models"},
        "blocks.py": {"html", "models", "rules"},
        "structure.py": {"blocks", "html", "models"},
        "content.py": {"blocks", "html", "models", "structure"},
        "document.py": {"blocks", "models", "structure"},
        "rules.py": set(),
    }
    actual_imports = {filename: _relative_imports(package_path / filename) for filename in allowed_imports}
    assert actual_imports == allowed_imports


def _python_paths(relative: str) -> list[Path]:
    """要求被检查的引擎目录存在且非空，再返回其 Python 文件。"""
    root = _DOCVORTEX_ROOT / relative
    assert root.is_dir(), root
    paths = sorted(root.rglob("*.py"))
    assert paths, root
    return paths
