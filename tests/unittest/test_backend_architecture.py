from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


_DOCVORTEX_ROOT = Path(importlib.util.find_spec("docvortex").origin).parent


_DOCSTRING_PATHS = (
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
    """Convert real files in the engine installation directory to full module names."""
    parts = list(path.relative_to(_DOCVORTEX_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolved_imports(path: Path) -> set[str]:
    """Resolve both absolute and relative import to full module names."""
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
    """Reads the relative import name of the module from the Python file for the same package."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module}


def _iter_python_paths(path: Path) -> list[Path]:
    """Return the file itself or all Python files in the directory."""
    return sorted(path.rglob("*.py")) if path.is_dir() else [path]


def test_new_first_party_definitions_have_docstrings() -> None:
    """Require responsibility descriptions for first-party functions, methods, and classes, regardless of language."""
    offenders: list[str] = []
    for configured_path in _DOCSTRING_PATHS:
        for path in _iter_python_paths(configured_path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                docstring = ast.get_docstring(node)
                if not docstring:
                    offenders.append(f"{path}:{node.lineno}:{node.name}")
    assert not offenders


def test_latex_and_render_common_keep_private_dependencies_one_way() -> None:
    """Guard LaTeX does not cross over to other format private implementations, and common does not rely on format packages in reverse."""
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
    """The guard Office format only reuses neutral capabilities and does not rely inversely on the Flash PDF implementation."""
    offenders = {
        str(path): sorted(module for module in _resolved_imports(path) if module.startswith("docvortex.analyzers.native.pdf"))
        for path in _python_paths("analyzers/native/office")
    }
    assert not {path: imports for path, imports in offenders.items() if imports}


def test_flash_spreadsheet_dependencies_are_one_way() -> None:
    """Guard XLS/XLSX relies only on the neutral spreadsheet layer and the shared layer does not backreference the format implementation."""
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
    """The guard formula layer does not back-reference the format implementation, and the legacy layer does not re-host formula parsing."""
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
    """Active production code that guards strict migration no longer references the old private path."""
    offenders: list[str] = []
    for path in _python_paths(""):
        text = path.read_text(encoding="utf-8")
        for module_name in _REMOVED_INTERNAL_MODULES:
            if module_name in text:
                offenders.append(f"{path.relative_to(_DOCVORTEX_ROOT)}:{module_name}")
    assert not offenders


def test_removed_private_module_paths_are_not_importable() -> None:
    """Verify that there are no old private module shells that could be misused after the strict switch."""
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
    """Guard table_merge Low-level modules do not reverse-import content merging or document layout modules."""
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
    """Requires the checked engine directory to exist and be non-empty before returning its Python file."""
    root = _DOCVORTEX_ROOT / relative
    assert root.is_dir(), root
    paths = sorted(root.rglob("*.py"))
    assert paths, root
    return paths
