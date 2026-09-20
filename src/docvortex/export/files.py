"""独立渲染文件和文档素材的统一写出。"""

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
    """在副本上按视觉父块外置图片；仅通过显式回调获取宿主文件或裁图。

    image_resolver 接收载荷块和原始页索引，返回字节与扩展名；返回 None
    表示保留当前载荷，不触发默认解析。asset_resolver 仅接收经过校验的
    HTML 图片相对路径。不提供回调时，只解析内嵌图片并保留已有素材引用。
    """
    return _materialize_images(middle_json, assets, image_resolver=image_resolver, asset_resolver=asset_resolver)


def validate_materialized_assets(middle_json: MiddleJson, assets: AssetStore) -> None:
    """拒绝缺失的已物化素材，保证结果包内部引用完整；源文档外链原样保留。"""
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
    """检查主文件与素材的路径关系后，以同一事务写出。"""
    path = path.absolute()
    relative_files = {path.name: artifact.content, **dict(artifact.assets)}
    if path.name in artifact.assets:
        raise ValueError("Main file conflicts with an asset")
    _validate_export_path_relationships(list(relative_files))
    targets = {_resolve_export_target(path.parent, name): payload for name, payload in relative_files.items()}
    _commit_export_files(targets, overwrite=overwrite)
    return ExportResult(path=path, asset_paths=tuple(path.parent / name for name in artifact.assets))


def _is_external_reference(reference: str) -> bool:
    """判断富文本图片引用是否为保留的受限 HTTP(S) 外链。"""
    try:
        return urlsplit(reference).scheme.casefold() in {"http", "https"}
    except ValueError:
        return False


__all__ = ["materialize_middle", "validate_materialized_assets", "write_artifact"]
