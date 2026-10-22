"""把已经渲染的 HTML 与显式素材集合合并成图片内嵌的单文件。"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from ..assets import parse_image_data_uri_strict, validate_image_sidecar_path


def embed_html_images(html: str, assets: Mapping[str, bytes]) -> str:
    """内嵌正文及富文本图片，缺失素材时拒绝输出，不读取宿主文件或网络。"""
    from bs4 import BeautifulSoup

    document = BeautifulSoup(html, "html.parser")
    for element in document.find_all(["img", "image", "source"]):
        if element.name == "source" and element.find_parent("picture") is None:
            continue
        # srcset 的候选可能重新引入外部图片；有明确 src 时只保留该完整图片。
        attribute = "src" if element.name in {"img", "source"} else "href"
        if element.name == "image" and not element.get(attribute):
            attribute = "xlink:href"
        source = element.get(attribute)
        if not isinstance(source, str) or not source:
            if element.get("srcset"):
                raise ValueError("Image srcset has no materialized src")
            continue
        if source.startswith("data:"):
            parse_image_data_uri_strict(source)
        else:
            if urlsplit(source).scheme or source.startswith("//"):
                raise ValueError(f"Image is not materialized: {source}")
            path = validate_image_sidecar_path(unquote(source))
            if path not in assets:
                raise ValueError(f"Missing materialized HTML image: {path}")
            mime = mimetypes.guess_type(path)[0]
            if mime is None or not mime.startswith("image/"):
                raise ValueError(f"Unsupported image type: {path}")
            element[attribute] = f"data:{mime};base64,{base64.b64encode(assets[path]).decode('ascii')}"
        element.attrs.pop("srcset", None)
    return str(document)


__all__ = ["embed_html_images"]
