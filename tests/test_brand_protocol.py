"""验证 DocVortex 独立命名和已明确移除的旧品牌协议。"""

from __future__ import annotations

from importlib import util
import json
from pathlib import Path

import pytest

from docvortex.codecs.json import load_middle, load_model
from docvortex.export.bundle import load_bundle, save_bundle
from docvortex.result import DocumentResult
from docvortex.schema import MiddleJson, ModelJson


def test_only_new_package_and_protocol_names_are_available() -> None:
    """包和原生协议仅使用新品牌，不提供旧导入名。"""
    assert util.find_spec("docgale") is None
    model = ModelJson(pages=[], page_index_map=[], file_suffix="pdf")
    middle = MiddleJson(pages=[], is_full_document=True, file_suffix="pdf")
    for document, schema, loader in [(model, "docvortex.model", load_model), (middle, "docvortex.middle", load_middle)]:
        payload = document.to_dict(skip_defaults=False)
        assert payload["schema"] == schema
        assert payload["schema_version"] == "1.0"
        assert payload["producer"] == {"name": "docvortex", "version": "0.1.0"}
        assert loader(payload).to_dict(skip_defaults=False) == payload
        payload["schema"] = schema.replace("docvortex", "docgale")
        with pytest.raises(ValueError, match="Expected docvortex"):
            loader(payload)


def test_old_bundle_schema_is_rejected_without_rewriting(tmp_path: Path) -> None:
    """旧结果包明确拒绝，读取失败不得重写清单或尝试隐式迁移。"""
    result = DocumentResult(MiddleJson(pages=[], is_full_document=True, file_suffix="pdf"))
    save_bundle(result, tmp_path)
    assert load_bundle(tmp_path).middle_json == result.middle_json
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    assert manifest["schema"] == "docvortex.bundle"
    manifest["schema"] = "docgale.bundle"
    original = json.dumps(manifest).encode()
    path.write_bytes(original)
    with pytest.raises(ValueError, match="Unsupported DocVortex bundle"):
        load_bundle(tmp_path)
    assert path.read_bytes() == original


def test_old_producer_and_user_text_are_not_rebranded_on_load() -> None:
    """新协议中的真实生产者与用户文字不因品牌名相同而被字符串替换。"""
    model = ModelJson(
        pages=[
            [{"type": "text", "content": [{"type": "text", "content": "DocGale docgale-file https://example.com/docgale/"}]}]
        ],
        page_index_map=[],
        file_suffix="html",
        producer={"name": "docgale", "version": "custom"},
    )
    payload = model.to_dict(skip_defaults=False)
    assert load_model(payload).to_dict(skip_defaults=False) == payload
