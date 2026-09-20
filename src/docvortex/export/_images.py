"""各导出入口共享的图片命名、冲突分配与引用改写。"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import unquote

from ..assets import AssetStore, parse_image_data_uri_strict, validate_image_sidecar_path
from ..content.tree import iter_child_blocks
from ..schema import (
    AlgorithmBodyBlock,
    BlockBase,
    ChartBlock,
    ChartBodyBlock,
    CodeBlock,
    CodeBodyBlock,
    ImageBlock,
    ImageBodyBlock,
    ImagePayloadBlock,
    MiddleJson,
    TableBlock,
    TableBodyBlock,
)

_HTML_IMAGE_SOURCE_RE = re.compile(
    r"(?P<prefix><img\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*?\s+src\s*=\s*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)|(?P<unquoted>[^\s>]+))",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg"})


def _register_image(
    assets: AssetStore,
    payload: bytes,
    extension: str,
    *,
    page_idx: int,
    owner: BlockBase,
    ordinal: int | None = None,
) -> str:
    """按原始页和视觉父块命名，冲突后缀独立于正文图片序号。"""
    extension = extension.lower().lstrip(".") or "jpg"
    if extension not in _IMAGE_EXTENSIONS:
        raise ValueError(f"Unsupported image extension: {extension}")
    if owner.index is None:
        raise ValueError("Image owner must have a block index")
    kind = str(owner.type)
    if ordinal is not None and kind != "image":
        kind += "_image"
    suffix = f"_{ordinal}" if ordinal is not None else ""
    stem = f"page_{page_idx}_{kind}_{owner.index}{suffix}"
    for attempt in range(10000):
        duplicate = f"_duplicate_{attempt}" if attempt else ""
        path = validate_image_sidecar_path(f"images/{stem}{duplicate}.{extension}")
        if path not in assets or assets[path] == payload:
            assets.add(path, payload)
            return path
    raise ValueError("Too many conflicting image assets")


def _materialize_markup(
    content: str,
    assets: AssetStore,
    *,
    page_idx: int,
    owner: BlockBase,
    asset_resolver: Callable[[str], bytes] | None,
) -> str:
    """按所有 img src 的出现顺序编号，仅替换图片引用的值。"""
    ordinal = 0

    def replace_source(match: re.Match[str]) -> str:
        """保留外链及原 HTML 属性，外链同样占用正文图片序号。"""
        nonlocal ordinal
        ordinal += 1
        group = "quoted" if match.group("quote") else "unquoted"
        source = html.unescape(match.group(group)).strip()
        if source.startswith("data:"):
            payload, extension = parse_image_data_uri_strict(source)
        elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", source) or asset_resolver is None:
            return match.group(0)
        else:
            safe_path = validate_image_sidecar_path(unquote(source))
            payload = asset_resolver(safe_path)
            extension = PurePosixPath(safe_path).suffix.lstrip(".")
        path = _register_image(assets, payload, extension, page_idx=page_idx, owner=owner, ordinal=ordinal)
        start, end = match.span(group)
        original = match.group(0)
        return original[: start - match.start()] + path + original[end - match.start() :]

    return _HTML_IMAGE_SOURCE_RE.sub(replace_source, content)


def _materialize_images(
    middle_json: MiddleJson,
    assets: AssetStore | None = None,
    *,
    image_resolver: Callable[[ImagePayloadBlock, int], tuple[bytes, str] | None] | None = None,
    asset_resolver: Callable[[str], bytes] | None = None,
) -> tuple[MiddleJson, AssetStore]:
    """复制文档及素材索引，在同一次树遍历中完成命名和引用回填。"""
    document = middle_json.model_copy(deep=True)
    result = assets.copy() if assets is not None else AssetStore()
    for page in document.pages:
        pending = [(block, block) for block in reversed(page.blocks)]
        while pending:
            block, owner = pending.pop()
            if isinstance(block, (CodeBlock, CodeBodyBlock, AlgorithmBodyBlock)):
                continue
            if isinstance(block, (ImageBlock, TableBlock, ChartBlock)):
                owner = block
            if isinstance(block, ImagePayloadBlock):
                resolved = None
                if image_resolver is not None:
                    resolved = image_resolver(block, page.page_idx)
                elif block.image_base64 is not None:
                    resolved = parse_image_data_uri_strict(block.image_base64)
                if resolved is not None:
                    payload, extension = resolved
                    block.image_path = _register_image(result, payload, extension, page_idx=page.page_idx, owner=owner)
                    block.image_base64 = None
                    block.image_url = None
                elif image_resolver is None and block.image_path and block.image_path in result:
                    block.image_url = None
            if isinstance(block, (ImageBodyBlock, TableBodyBlock, ChartBodyBlock)):
                block.content = _materialize_markup(
                    block.content, result, page_idx=page.page_idx, owner=owner, asset_resolver=asset_resolver
                )
            pending.extend((child, owner) for child in reversed(list(iter_child_blocks(block))))
    return document, result
