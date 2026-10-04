"""Naming of public materialized interfaces, host callbacks, historical references and conflict regression."""

from __future__ import annotations

import base64

import pytest

from docvortex.assets import AssetStore
from docvortex.export import materialize_middle, validate_materialized_assets
from docvortex.schema import ImagePayloadBlock, MiddleJson, PageInfo, TableBlock, TableBodyBlock


def _uri(payload: bytes = b"\xff\xd8\xffimage") -> str:
    """Generate JPEG payload that can pass signature verification to avoid introducing image decoding dependencies."""
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode()


def _document(content: str = "", image_base64: str | None = None) -> MiddleJson:
    """Construct the table of the original sixth page, the parent block and the text index adhere to the sharing agreement."""
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
    """Single and double quotes, no quotes, and external links are numbered by position, and non-src attributes and formats are retained as they are."""
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
    """Content with the same name and different content is assigned an independent suffix, the same payload is reused, and the input material is not overwritten."""
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
    """When all candidate names are occupied by different content, fail explicitly without overwriting any material."""
    assets = AssetStore({f"images/page_5_table_3{'_duplicate_' + str(n) if n else ''}.jpg": b"occupied" for n in range(10000)})
    with pytest.raises(ValueError, match="Too many conflicting image assets"):
        materialize_middle(_document(image_base64=_uri()), assets)
    assert len(assets) == 10000


def test_callbacks_receive_original_page_and_validated_relative_path() -> None:
    """The host only provides bytes, the image extension and the old HTML attribute are preserved by the shared export logic."""
    middle = _document('<img src="图%20片.JPEG">', _uri())
    calls: list[object] = []

    def image_resolver(block: ImagePayloadBlock, page_idx: int) -> tuple[bytes, str]:
        """The original page number is recorded and the simulation host obtains the payload from the source PDF or a local file."""
        calls.append((block.type, page_idx))
        return b"direct", ".PNG"

    def asset_resolver(path: str) -> bytes:
        """Receive the relative path after URL decoding and security check."""
        calls.append(path)
        return b"inline"

    saved, assets = materialize_middle(middle, image_resolver=image_resolver, asset_resolver=asset_resolver)
    assert calls == [("table_body", 5), "图 片.JPEG"]
    assert dict(assets) == {"images/page_5_table_3.png": b"direct", "images/page_5_table_image_3_1.jpeg": b"inline"}
    assert saved.pages[0].blocks[0].content[0].content == '<img src="images/page_5_table_image_3_1.jpeg">'
    assert middle.pages[0].blocks[0].content[0].image_base64 == _uri()
    validate_materialized_assets(saved, assets)


def test_callback_none_preserves_payload_without_default_fallback() -> None:
    """There will be no implicit fallback if the host acquisition fails, and it is compatible with Gradio's behavior of ignoring illegal direct payloads."""
    middle = _document(image_base64="invalid")

    def unavailable(block: ImagePayloadBlock, page_idx: int) -> None:
        """The simulation host cannot obtain the image bytes."""
        return None

    saved, assets = materialize_middle(middle, image_resolver=unavailable)
    assert saved == middle and not assets
    with pytest.raises(ValueError, match="Invalid image data URI"):
        materialize_middle(middle)


@pytest.mark.parametrize("reference", ["../outside.jpg", "%2e%2e/outside.jpg", "/outside.jpg"])
def test_unsafe_paths_rejected_before_asset_callback(reference: str) -> None:
    """The host read callback cannot receive directory traversals or absolute paths."""

    def reject_call(path: str) -> bytes:
        """Real file reading is not allowed to be triggered when the security check fails."""
        raise AssertionError(path)

    with pytest.raises(ValueError):
        materialize_middle(_document(f'<img src="{reference}">'), asset_resolver=reject_call)


def test_historical_paths_stay_unchanged_and_missing_assets_still_fail() -> None:
    """By default, historical references are not renamed, and missing images are not automatically read from the current directory."""
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
