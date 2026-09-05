"""按显式模块映射抽取源代码，并保留源码排版与相对导入。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import shutil
from importlib.util import resolve_name

SOURCE = Path(__file__).resolve().parents[2] / "Magic-PDF"
TARGET = Path(__file__).resolve().parents[1]
PREFIXES = {
    "mineru.model.flash": "docgale.analyzers.native",
    "mineru.model.flash._shared.spans": "docgale.content.spans",
    "mineru.model.flash.html.wire": "docgale.codecs.html",
    "mineru.backend.postprocess": "docgale.postprocess",
    "mineru.backend.postprocess.inline": "docgale.content.inline",
    "mineru.backend.postprocess.table_merge": "docgale.content.table",
    "mineru.backend.postprocess.legacy_schema_adapter": "docgale.compat.legacy_schema_adapter",
    "mineru.render": "docgale.render",
    "mineru.types": "docgale.schema",
    "mineru.utils": "docgale.foundation",
    "mineru.filetypes": "docgale.document.filetypes",
    "mineru.parser.file_type": "docgale.document.detection",
    "mineru.parser.page_range": "docgale.document.page_range",
    "mineru.config": "docgale.options",
    "mineru.errors": "docgale.errors",
}
for name in ("document", "pdfium", "raster", "classify", "diagnostics"):
    PREFIXES[f"mineru.model.flash.pdf.{name}"] = f"docgale.document.pdf.{name}"
for name in ("images", "visuals", "geometry", "constants"):
    PREFIXES[f"mineru.backend.analysis.pdf.{name}"] = f"docgale.document.pdf.{name if name != 'geometry' else 'visual_geometry'}"


def renamed(module: str) -> str:
    """按最长前缀将源模块归入唯一的目标职责模块。"""
    for prefix in sorted(PREFIXES, key=len, reverse=True):
        if module == prefix or module.startswith(prefix + "."):
            return PREFIXES[prefix] + module[len(prefix):]
    return module


def relative_module(module: str, package: str) -> str:
    """计算包内静态相对导入，保留第三方绝对导入。"""
    if not module.startswith("docgale"):
        return module
    destination = module.split(".")
    source = package.split(".")
    common = 0
    for left, right in zip(source, destination):
        if left != right:
            break
        common += 1
    return "." * (len(source) - common + 1) + ".".join(destination[common:])


def rewrite_imports(text: str, old_module: str, new_module: str, is_package: bool) -> str:
    """只替换 import 节点，避免重新格式化已验证的算法代码。"""
    old_package = old_module if is_package else old_module.rpartition(".")[0]
    new_package = new_module if is_package else new_module.rpartition(".")[0]
    lines = text.splitlines(keepends=True)
    edits = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.ImportFrom):
            continue
        old_base = resolve_name("." * node.level + (node.module or ""), old_package) if node.level else node.module or ""
        if not old_base.startswith("mineru"):
            continue
        new_base = renamed(old_base)
        if new_base.startswith("mineru"):
            raise ValueError(f"Unmapped import: {old_module}: {old_base}")
        target = relative_module(new_base, new_package)
        aliases = ", ".join(item.name + (f" as {item.asname}" if item.asname else "") for item in node.names)
        indent = " " * node.col_offset
        edits.append((node.lineno - 1, node.end_lineno, f"{indent}from {target} import {aliases}\n"))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    return "".join(lines)


def main() -> None:
    """抽取完整能力闭包，同时记录供 MinerU 调用迁移使用的映射。"""
    files = set()
    for folder in ("mineru/model/flash", "mineru/backend/postprocess", "mineru/render"):
        files.update((SOURCE / folder).rglob("*.py"))
    for name in ("geometry", "hyperlink", "image", "image_payload", "language", "platform", "text"):
        files.add(SOURCE / f"mineru/utils/{name}.py")
    for path in ("mineru/types.py", "mineru/filetypes.py", "mineru/parser/file_type.py", "mineru/parser/page_range.py"):
        files.add(SOURCE / path)
    for name in ("images", "visuals", "geometry", "constants"):
        files.add(SOURCE / f"mineru/backend/analysis/pdf/{name}.py")
    excluded = {"llm_aided.py", "llm_client.py", "title_leveling.py", "llm_cell_merge.py", "pdftext_adapter.py"}
    mapping = {}
    for path in sorted(files):
        if path.name in excluded:
            continue
        old_module = ".".join(path.relative_to(SOURCE).with_suffix("").parts)
        is_package = old_module.endswith(".__init__")
        if is_package:
            old_module = old_module.removesuffix(".__init__")
        new_module = renamed(old_module)
        text = path.read_text()
        if old_module == "mineru.types":
            text = text[:text.index("    def export(\n")]
            text = text[:text.index("Tier = Literal[")] + text[text.index("class BlockType"):]
        if old_module == "mineru.filetypes":
            tree = ast.parse(text)
            lines = text.splitlines(keepends=True)
            for node in reversed(tree.body):
                if isinstance(node, ast.ImportFrom) and node.module == "types" or isinstance(node, ast.FunctionDef) and node.name in {"ensure_tier_supported_for_parse_extension", "batch_effective_parse_tier"}:
                    del lines[node.lineno - 1:node.end_lineno]
            text = "".join(lines)
        if old_module == "mineru.backend.postprocess.document":
            text = '''"""将规范化分析结果转换为独立的语义文档。"""
from __future__ import annotations
from ..schema import MiddleJson, ModelJson
from .pages import model_json_to_pages

def model_json_to_middle_json(model_json: ModelJson) -> MiddleJson:
    """只执行确定性后处理，智能增强由调用方另行执行。"""
    return MiddleJson(pages=model_json_to_pages(model_json), is_full_document=model_json.is_full_document,
                      file_suffix=model_json.file_suffix, producer=model_json.producer.model_copy(deep=True),
                      extensions=model_json.extensions.copy())

__all__ = ["model_json_to_middle_json"]
'''
        else:
            text = rewrite_imports(text, old_module, new_module, is_package)
        destination = TARGET / "src" / Path(*new_module.split("."))
        destination = destination / "__init__.py" if is_package else destination.with_suffix(".py")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
        mapping[old_module] = new_module
    for folder in ("html", "epub", "fasttext-langdetect"):
        shutil.copytree(SOURCE / "mineru/resources" / folder, TARGET / "src/docgale/resources" / folder, dirs_exist_ok=True)
    shutil.copyfile(SOURCE / "LICENSE.md", TARGET / "LICENSE.md")
    (TARGET / ".baseline/module_mapping.json").write_text(json.dumps(mapping, indent=2))
    print(f"Extracted {len(mapping)} modules")


if __name__ == "__main__":
    main()
