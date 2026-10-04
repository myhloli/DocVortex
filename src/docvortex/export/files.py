"""Unified writing of independent rendering files and document materials."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from ..assets import AssetStore
from ..content.tree import iter_child_blocks
from ..result import ExportResult, RenderArtifact
from ..schema import ImagePayloadBlock, MiddleJson
from ._images import _materialize_images
from .middle import _commit_export_files, _resolve_export_target, _validate_export_path_relationships


def materialize_middle(
    middle_json: MiddleJson,
    assets: AssetStore | None = None,
    *,
    image_resolver: Callable[[ImagePayloadBlock, int], tuple[bytes, str] | None] | None = None,
    asset_resolver: Callable[[str], bytes] | None = None,
) -> tuple[MiddleJson, AssetStore]:
    """External image by visual parent block on copy; only get host file or crop through explicit callback.

    image_resolver receives the payload block and original page index, returns bytes and extension; returns None
    Indicates that the current payload is retained and default parsing is not triggered. asset_resolver only accepts verified
    HTML Image relative path. When no callback is provided, only embedded images are parsed and existing material references are retained.
    """
    return _materialize_images(middle_json, assets, image_resolver=image_resolver, asset_resolver=asset_resolver)


def validate_materialized_assets(middle_json: MiddleJson, assets: AssetStore) -> None:
    """Missing materialized materials are rejected to ensure complete internal references in the result package; external links to the source documents are retained as they are."""
    from bs4 import BeautifulSoup

    pending = [block for page in middle_json.pages for block in page.blocks]
    while pending:
        block = pending.pop()
        pending.extend(iter_child_blocks(block))
        image_path = getattr(block, "image_path", None)
        if image_path and image_path not in assets:
            raise ValueError(f"Missing materialized asset: {image_path}")
        content = getattr(block, "content", None)
        if str(block.type) not in {"table_body", "chart_body", "image_body"} or not isinstance(content, str):
            continue
        if "<img" not in content.lower():
            continue
        for image in BeautifulSoup(content, "html.parser").find_all("img"):
            reference = image.get("src")
            if reference and _is_external_reference(reference):
                continue
            if reference and reference not in assets:
                raise ValueError(f"Missing materialized HTML image: {reference}")


def write_artifact(artifact: RenderArtifact, path: Path, *, overwrite: bool) -> ExportResult:
    """After checking the path relationship between the main file and the material, write it out in the same transaction."""
    path = path.absolute()
    relative_files = {path.name: artifact.content, **dict(artifact.assets)}
    if path.name in artifact.assets:
        raise ValueError("Main file conflicts with an asset")
    _validate_export_path_relationships(list(relative_files))
    targets = {_resolve_export_target(path.parent, name): payload for name, payload in relative_files.items()}
    _commit_export_files(targets, overwrite=overwrite)
    return ExportResult(path=path, asset_paths=tuple(path.parent / name for name in artifact.assets))


def _is_external_reference(reference: str) -> bool:
    """Determine whether the rich text image reference is a reserved restricted HTTP(S) external link."""
    try:
        return urlsplit(reference).scheme.casefold() in {"http", "https"}
    except ValueError:
        return False


__all__ = ["materialize_middle", "validate_materialized_assets", "write_artifact"]
