"""独立渲染文件和文档素材的统一写出。"""

from __future__ import annotations

from pathlib import Path

from ..assets import AssetStore
from ..schema import MiddleJson
from ..result import ExportResult, RenderArtifact
from .middle import _commit_export_files, _prepare_export_copy, _resolve_export_target, _validate_export_path_relationships


def materialize_middle(middle_json: MiddleJson, assets: AssetStore | None = None) -> tuple[MiddleJson, AssetStore]:
    """在文档副本上外置所有内嵌图片，保留调用者提供的外部素材。"""
    document, embedded = _prepare_export_copy(middle_json)
    result = assets.copy() if assets is not None else AssetStore()
    for path, payload in embedded.items():
        result.add(path, payload)
    return document, result


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


__all__ = ["materialize_middle", "write_artifact"]
