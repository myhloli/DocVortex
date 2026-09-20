"""验证跨库素材导出接口的字节所有权及字面量边界。"""

from __future__ import annotations

import base64
from typing import get_type_hints

import pytest

from docvortex.assets import AssetStore
from docvortex.export import materialize_middle, validate_materialized_assets
from docvortex.public_api import PUBLIC_API
from docvortex.schema import MiddleJson


def test_public_export_preserves_payloads_geometry_and_code() -> None:
    """直接图与表内图外置后字节不变，代码中的同名字面量不被当作素材。"""
    image = b"\xff\xd8\xffexample\xff\xd9"
    uri = "data:image/jpeg;base64," + base64.b64encode(image).decode()
    markup = f'<img src="{uri}" width="10">'
    middle = MiddleJson.from_dict(
        {
            "schema": "docvortex.middle",
            "schema_version": "2.0",
            "is_full_document": False,
            "metadata": {"file_suffix": "pdf", "producer": {"name": "test", "version": "1"}},
            "extensions": {
                "docvortex_layout": {
                    "version": 1,
                    "pages": [{"page_idx": 4, "width_pt": 400, "height_pt": 600, "image_rotations": {"2": 270}}],
                }
            },
            "pages": [
                {
                    "page_idx": 4,
                    "blocks": [
                        {
                            "type": "table",
                            "index": 2,
                            "bbox": [0.1, 0.1, 0.4, 0.5],
                            "content": [{"type": "table_body", "index": 2, "content": markup, "image_base64": uri}],
                        },
                        {
                            "type": "code",
                            "sub_type": "code",
                            "guess_lang": "html",
                            "index": 3,
                            "bbox": [0.1, 0.6, 0.9, 0.8],
                            "content": [{"type": "code_body", "index": 3, "content": markup}],
                        },
                    ],
                }
            ],
        }
    )
    before = middle.to_json()
    exported, assets = materialize_middle(middle)
    validate_materialized_assets(exported, assets)
    assert set(assets) == {"images/page_4_table_2.jpg", "images/page_4_table_image_2_1.jpg"}
    assert all(payload == image for payload in assets.values())
    assert exported.extensions == middle.extensions
    assert exported.pages[0].blocks[1].content[0].content == markup
    assert middle.to_json() == before
    repeated, copied_assets = materialize_middle(exported, assets)
    assert repeated == exported and dict(copied_assets) == dict(assets)
    assert copied_assets is not assets
    with pytest.raises(ValueError, match="Missing materialized asset"):
        validate_materialized_assets(exported, AssetStore())
    assert set(PUBLIC_API["docvortex.export"]) == {"materialize_middle", "validate_materialized_assets"}
    assert get_type_hints(materialize_middle)["return"] == tuple[MiddleJson, AssetStore]
    assert get_type_hints(validate_materialized_assets)["return"] is type(None)
