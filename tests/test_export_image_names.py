"""公共物化接口的命名、宿主回调、历史引用及冲突回归。"""

from __future__ import annotations

import base64

import pytest

from docvortex.assets import AssetStore
from docvortex.export import materialize_middle, validate_materialized_assets
from docvortex.schema import ImagePayloadBlock, MiddleJson, PageInfo, TableBlock, TableBodyBlock


def _uri(payload: bytes = b"\xff\xd8\xffimage") -> str:
    """生成可通过签名校验的 JPEG 载荷，避免引入图像解码依赖。"""
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode()


def _document(content: str = "", image_base64: str | None = None) -> MiddleJson:
    """构造原始第六页的表格，父块和正文索引遵守共享协议。"""
    return MiddleJson(
        pages=[
            PageInfo(
                page_idx=5,
                blocks=[
                    TableBlock(
                        type="table",
                        index=3,
                        content=[TableBodyBlock(type="table_body", index=3, content=content, image_base64=image_base64)],
                    )
                ],
            )
        ],
        metadata={"file_suffix": "docx", "producer": {"name": "test", "version": "1"}},
        is_full_document=False,
    )


def test_markup_ordinals_preserve_attributes_and_count_external_sources() -> None:
    """单双引号、无引号和外链按位置编号，非 src 属性及格式原样保留。"""
    uri = _uri()
    markup = (
        '<img src="https://example.test/a.png">'
        f'<IMG alt="a > b" data-src="untouched" SRC = \'{uri}\' width="20">'
        f"<img src={uri} height=3>"
        f'<img src="{uri}">'
    )
    middle = _document(markup)
    original = middle.to_json()
    saved, assets = materialize_middle(middle)
    expected = markup
    for ordinal in (2, 3, 4):
        expected = expected.replace(uri, f"images/page_5_table_image_3_{ordinal}.jpg", 1)
    assert saved.pages[0].blocks[0].content[0].content == expected
    assert set(assets) == {f"images/page_5_table_image_3_{ordinal}.jpg" for ordinal in (2, 3, 4)}
    validate_materialized_assets(saved, assets)
    assert middle.to_json() == original
    repeated, repeated_assets = materialize_middle(saved, assets)
    assert repeated == saved and dict(repeated_assets) == dict(assets)


def test_collision_allocation_preserves_existing_assets_and_inline_ordinals() -> None:
    """同名异内容分配独立后缀，相同载荷复用，输入素材不被覆盖。"""
    red, blue = b"\xff\xd8\xffred", b"\xff\xd8\xffblue"
    direct = "images/page_5_table_3.jpg"
    inline = "images/page_5_table_image_3_1.jpg"
    supplied = AssetStore({direct: red, inline: red, "images/unrelated.png": b"keep"})
    middle = _document(f'<img src="{_uri(blue)}">', _uri(blue))
    saved, assets = materialize_middle(middle, supplied)
    body = saved.pages[0].blocks[0].content[0]
    assert body.image_path == "images/page_5_table_3_duplicate_1.jpg"
    assert body.content == '<img src="images/page_5_table_image_3_1_duplicate_1.jpg">'
    assert assets[direct] == supplied[direct] == red
    assert assets[inline] == supplied[inline] == red
    assert assets[body.image_path] == blue
    assert set(supplied) == {direct, inline, "images/unrelated.png"}
    repeated, repeated_assets = materialize_middle(middle, assets)
    assert repeated == saved and dict(repeated_assets) == dict(assets)
    with pytest.raises(ValueError, match="Conflicting asset payload"):
        supplied.add(direct, blue)


def test_collision_candidates_are_bounded() -> None:
    """全部候选名称被不同内容占用时，明确失败而不覆盖任意素材。"""
    assets = AssetStore({f"images/page_5_table_3{'_duplicate_' + str(n) if n else ''}.jpg": b"occupied" for n in range(10000)})
    with pytest.raises(ValueError, match="Too many conflicting image assets"):
        materialize_middle(_document(image_base64=_uri()), assets)
    assert len(assets) == 10000


def test_callbacks_receive_original_page_and_validated_relative_path() -> None:
    """宿主只提供字节，图片扩展名和旧 HTML 属性由共享导出逻辑保留。"""
    middle = _document('<img src="图%20片.JPEG">', _uri())
    calls: list[object] = []

    def image_resolver(block: ImagePayloadBlock, page_idx: int) -> tuple[bytes, str]:
        """记录原始页号，模拟宿主从源 PDF 或本地文件取得载荷。"""
        calls.append((block.type, page_idx))
        return b"direct", ".PNG"

    def asset_resolver(path: str) -> bytes:
        """接收完成 URL 解码及安全检查后的相对路径。"""
        calls.append(path)
        return b"inline"

    saved, assets = materialize_middle(middle, image_resolver=image_resolver, asset_resolver=asset_resolver)
    assert calls == [("table_body", 5), "图 片.JPEG"]
    assert dict(assets) == {"images/page_5_table_3.png": b"direct", "images/page_5_table_image_3_1.jpeg": b"inline"}
    assert saved.pages[0].blocks[0].content[0].content == '<img src="images/page_5_table_image_3_1.jpeg">'
    assert middle.pages[0].blocks[0].content[0].image_base64 == _uri()
    validate_materialized_assets(saved, assets)


def test_callback_none_preserves_payload_without_default_fallback() -> None:
    """宿主获取失败不会隐式回退，兼容 Gradio 忽略非法直接载荷的行为。"""
    middle = _document(image_base64="invalid")

    def unavailable(block: ImagePayloadBlock, page_idx: int) -> None:
        """模拟宿主无法取得图片字节。"""
        return None

    saved, assets = materialize_middle(middle, image_resolver=unavailable)
    assert saved == middle and not assets
    with pytest.raises(ValueError, match="Invalid image data URI"):
        materialize_middle(middle)


@pytest.mark.parametrize("reference", ["../outside.jpg", "%2e%2e/outside.jpg", "/outside.jpg"])
def test_unsafe_paths_rejected_before_asset_callback(reference: str) -> None:
    """宿主读取回调不能接收到目录穿越或绝对路径。"""

    def reject_call(path: str) -> bytes:
        """安全校验失败时不允许触发真实文件读取。"""
        raise AssertionError(path)

    with pytest.raises(ValueError):
        materialize_middle(_document(f'<img src="{reference}">'), asset_resolver=reject_call)


def test_historical_paths_stay_unchanged_and_missing_assets_still_fail() -> None:
    """默认不重命名历史引用，也不自动从当前目录读取缺失图片。"""
    direct = "images/page_5_table_body_3.jpg"
    inline = "images/page_5_table_body_3_1.jpg"
    middle = _document(f'<img src="{inline}">')
    middle.pages[0].blocks[0].content[0].image_path = direct
    supplied = AssetStore({direct: b"old-direct", inline: b"old-inline"})
    saved, assets = materialize_middle(middle, supplied)
    assert saved == middle and dict(assets) == dict(supplied)
    validate_materialized_assets(saved, assets)
    missing, missing_assets = materialize_middle(middle)
    with pytest.raises(ValueError, match="Missing materialized asset"):
        validate_materialized_assets(missing, missing_assets)
