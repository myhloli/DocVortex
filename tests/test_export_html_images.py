"""HTML 单文件导出只使用显式素材，保留正文而不读取外部资源。"""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from docvortex.export import embed_html_images


def _png() -> bytes:
    """提供可解码的最小 PNG，避免只验证 data URI 外壳。"""
    stream = BytesIO()
    Image.new("RGB", (2, 3), (10, 80, 160)).save(stream, format="PNG")
    return stream.getvalue()


def test_embeds_body_and_table_images_without_changing_literals() -> None:
    """正文、表格与 SVG 图片引用都使用素材映射，文本字面量保持不变。"""
    png = _png()
    assets = {"images/a b.png": png}
    before = dict(assets)
    html = '<!doctype html><html><body><p>images/a b.png</p><img src="images/a%20b.png" srcset="other.png 2x"><table><tr><td><img src="images/a b.png"></td></tr></table><svg><image href="images/a%20b.png"/></svg></body></html>'
    result = embed_html_images(html, assets)
    document = BeautifulSoup(result, "html.parser")
    assert document.p.get_text() == "images/a b.png"
    assert document.img.get("srcset") is None
    for tag in document.find_all(["img", "image"]):
        uri = tag.get("src") or tag["href"]
        assert base64.b64decode(uri.split(",", 1)[1]) == png
    assert assets == before
    assert "<!DOCTYPE html>" in result


def test_preserves_existing_inline_images() -> None:
    """有效内嵌图片不需要素材映射，也不重复转码。"""
    uri = "data:image/png;base64," + base64.b64encode(_png()).decode()
    result = embed_html_images(f'<img src="{uri}">', {})
    assert BeautifulSoup(result, "html.parser").img["src"] == uri


@pytest.mark.parametrize(
    "source",
    [
        "images/missing.png",
        "../outside.png",
        "/outside.png",
        "https://host/image.png",
        "//host/image.png",
        "data:text/html;base64,SGk=",
    ],
)
def test_rejects_missing_or_unmaterialized_images(source: str) -> None:
    """缺失、外链、越界和非图片 data URI 不能被包装成成功单文件。"""
    with pytest.raises((ValueError, KeyError)):
        embed_html_images(f'<img src="{source}">', {})
