"""独立渲染文件和文档素材的统一写出。"""

from __future__ import annotations

from pathlib import Path

from ..assets import AssetStore
from ..content.tree import iter_child_blocks
from ..schema import MiddleJson
from ..result import ExportResult, RenderArtifact
from .middle import _commit_export_files, _prepare_export_copy, _resolve_export_target, _validate_export_path_relationships


def materialize_middle(middle_json: MiddleJson, assets: AssetStore | None = None) -> tuple[MiddleJson, AssetStore]:
    """在文档副本上外置所有内嵌图片，保留调用者提供的外部素材。"""
    document, embedded = _prepare_export_copy(middle_json)
    result = assets.copy() if assets is not None else AssetStore()
    for path, payload in embedded.items():
        result.add(path, payload)
    pending = [block for page in document.pages for block in page.blocks]
    while pending:
        block = pending.pop()
        pending.extend(iter_child_blocks(block))
        image_path = getattr(block, "image_path", None)
        if image_path and image_path in result and getattr(block, "image_url", None):
            block.image_url = None
    return document, result


def validate_materialized_assets(middle_json: MiddleJson, assets: AssetStore) -> None:
    """拒绝依赖外部文件或网络的未物化图片，保证结果包能离线重渲染。"""
    from bs4 import BeautifulSoup

    pending = [block for page in middle_json.pages for block in page.blocks]
    while pending:
        block = pending.pop()
        pending.extend(iter_child_blocks(block))
        image_path = getattr(block, "image_path", None)
        if image_path and image_path not in assets:
            raise ValueError(f"Missing materialized asset: {image_path}")
        if getattr(block, "image_url", None) and not image_path:
            raise ValueError("An external image must be materialized before saving a bundle")
        content = getattr(block, "content", None)
        if str(block.type) not in {"table_body", "chart_body", "image_body"} or not isinstance(content, str):
            continue
        if "<img" not in content.lower():
            continue
        for image in BeautifulSoup(content, "html.parser").find_all("img"):
            reference = image.get("src")
            if reference and reference not in assets:
                raise ValueError(f"Missing materialized HTML image: {reference}")


def write_artifact(artifact: RenderArtifact, path: Path, *, overwrite: bool) -> ExportResult:
    """检查主文件与素材的路径关系后，以同一事务写出。"""
    path = path.absolute()
    relative_files = {path.name: artifact.content, **dict(artifact.assets)}
    if path.name in artifact.assets:
        raise ValueError("Main file conflicts with an asset")
    _validate_export_path_relationships(list(relative_files))
    targets = {_resolve_export_target(path.parent, name): payload for name, payload in relative_files.items()}
    _commit_export_files(targets, overwrite=overwrite)
    return ExportResult(path=path, asset_paths=tuple(path.parent / name for name in artifact.assets))


__all__ = ["materialize_middle", "validate_materialized_assets", "write_artifact"]
