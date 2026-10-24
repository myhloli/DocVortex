"""验证内部原地物化不泄漏可变别名，公开入口继续保护调用方数据。"""

import base64
from copy import deepcopy
from io import BytesIO

from PIL import Image
from pydantic import BaseModel
import pytest

from docvortex import api
from docvortex.assets import AssetStore
from docvortex.export import materialize_middle, validate_materialized_assets
from docvortex.export import _images
from docvortex.postprocess import document as conversion
from docvortex.result import AnalysisResult, Diagnostic, DocumentResult
from docvortex.schema import ModelJson


def _png() -> bytes:
    """生成可渲染的小图片，用同一载荷覆盖多个父块和 HTML 内嵌引用。"""
    output = BytesIO()
    Image.new("RGB", (3, 2), "green").save(output, format="PNG")
    return output.getvalue()


def _model() -> ModelJson:
    """构造嵌套列表、行内样式、表图、原始页号和第三方可变扩展。"""
    uri = "data:image/png;base64," + base64.b64encode(_png()).decode()
    return ModelJson(
        pages=[
            [
                {
                    "type": "text",
                    "content": [
                        {"type": "text", "content": "原始正文", "styles": ["bold"]},
                        {
                            "type": "hyperlink",
                            "url": "https://example.test",
                            "content": [{"type": "text", "content": "原始链接"}],
                        },
                    ],
                },
                {
                    "type": "list",
                    "content": [
                        {"type": "text", "content": [{"type": "text", "content": "第一层"}]},
                        {
                            "type": "list",
                            "content": [{"type": "text", "content": [{"type": "text", "content": "第二层"}]}],
                        },
                    ],
                },
                {"type": "image", "content": None, "image_base64": uri},
                {"type": "table", "content": f'<table><tr><td><img src="{uri}"></td></tr></table>', "image_base64": uri},
            ],
            [{"type": "image", "content": None, "image_base64": uri}],
        ],
        page_index_map=[3, 5],
        metadata={"file_suffix": "html", "producer": {"name": "external", "version": "original"}},
        extensions={"application": {"items": [{"values": [None, True, "原始扩展"]}]}},
    )


def _mutable_ids(value: object) -> set[int]:
    """递归收集文档字段中的可变容器和模型身份，允许共享字符串及不可变字节。"""
    found = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, (BaseModel, dict, list, set)):
            if id(current) in found:
                continue
            found.add(id(current))
        if isinstance(current, BaseModel):
            pending.extend([vars(current), current.model_fields_set])
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set)):
            pending.extend(current)
    return found


@pytest.mark.parametrize("keep_model", [False, True])
@pytest.mark.parametrize("analysis_result", [False, True])
def test_postprocess_owns_complete_tree_and_isolates_asset_indexes(keep_model, analysis_result):
    """物化、后续修改和重复调用均不污染源树、保留模型或已有素材索引。"""
    model = _model()
    payload = _png()
    supplied = AssetStore({"images/existing.png": payload, "images/page_3_image_2.png": b"occupied"})
    source = AnalysisResult(model, supplied, (Diagnostic("broken_page", "broken", 4),)) if analysis_result else model
    before = deepcopy(model.model_dump())
    result = api.postprocess(source, assets=None if analysis_result else supplied, keep_model_json=keep_model)
    repeated = api.postprocess(source, assets=None if analysis_result else supplied, keep_model_json=keep_model)
    assert model.model_dump() == before
    assert result.model_json is (model if keep_model else None)
    assert result.middle_json == repeated.middle_json
    assert dict(result.assets) == dict(repeated.assets)
    assert _mutable_ids(result.middle_json).isdisjoint(_mutable_ids(model))
    assert _mutable_ids(result.middle_json).isdisjoint(_mutable_ids(repeated.middle_json))
    assert [page.page_idx for page in result.middle_json.pages] == ([3, 4, 5] if analysis_result else [3, 5])
    assert result.assets["images/existing.png"] is supplied["images/existing.png"]
    assert result.assets["images/page_3_image_2_duplicate_1.png"] is payload
    validate_materialized_assets(result.middle_json, result.assets)
    result.middle_json.metadata.producer.name = "changed"
    result.middle_json.extensions["application"]["items"][0]["values"].append("changed")
    result.middle_json.pages[0].blocks[0].content[0].styles.append("italic")
    result.middle_json.pages[0].blocks[1].content[1].content[0].content[0].content = "changed"
    result.assets.add("images/new.png", b"new")
    assert model.model_dump() == before
    assert repeated.middle_json.metadata.producer.name == "external"
    assert "images/new.png" not in supplied and "images/new.png" not in repeated.assets
    after = repeated.middle_json.model_dump()
    model.pages[0][0]["content"][0]["content"] = "changed source"
    model.extensions["application"]["items"].clear()
    assert repeated.middle_json.model_dump() == after


def test_internal_postprocess_reuses_new_tree_but_public_materialization_copies(monkeypatch):
    """确认只省去刚创建 MiddleJson 的重复深复制，外部物化仍返回完整独立副本。"""
    created = []
    original = conversion.model_json_to_middle_json

    def capture(model):
        """记录转换层创建的真实独占文档，检查内部路径没有再次复制该树。"""
        middle = original(model)
        created.append(middle)
        return middle

    monkeypatch.setattr(conversion, "model_json_to_middle_json", capture)
    result = api.postprocess(_model())
    assert result.middle_json is created[0]
    copied, assets = materialize_middle(result.middle_json, result.assets)
    assert copied == result.middle_json and dict(assets) == dict(result.assets)
    assert _mutable_ids(copied).isdisjoint(_mutable_ids(result.middle_json))


@pytest.mark.parametrize("keep_model", [False, True])
def test_partial_materialization_failure_leaves_input_and_assets_untouched(monkeypatch, keep_model):
    """后面的坏图片导致失败时，前面已回填的局部结果不会泄漏到模型或素材。"""
    model = _model()
    valid = model.pages[1][0]["image_base64"]
    model.pages[1][0]["image_base64"] = "invalid"
    supplied = AssetStore({"images/existing.png": _png()})
    before = deepcopy(model.model_dump())
    calls = []
    register = _images._register_image

    def counted(*args, **kwargs):
        """确认异常发生前确实已经物化图片，避免仅验证未发生写入的早退路径。"""
        path = register(*args, **kwargs)
        calls.append(path)
        return path

    monkeypatch.setattr(_images, "_register_image", counted)
    with pytest.raises(ValueError, match="Invalid image data URI"):
        api.postprocess(AnalysisResult(model, supplied), keep_model_json=keep_model)
    assert calls
    assert model.model_dump() == before
    assert list(supplied) == ["images/existing.png"]
    model.pages[1][0]["image_base64"] = valid
    recovered = api.postprocess(AnalysisResult(model, supplied), keep_model_json=keep_model)
    validate_materialized_assets(recovered.middle_json, recovered.assets)
    assert list(supplied) == ["images/existing.png"]
    assert model.pages[1][0]["image_base64"] == valid


@pytest.mark.parametrize("operation", ["materialize", "render", "bundle"])
def test_public_operations_preserve_external_middle_on_partial_failure(operation, tmp_path):
    """公开物化、渲染与打包仍在副本上处理，晚发图片错误不修改外部树。"""
    model = _model()
    model.pages[1][0]["image_base64"] = "invalid"
    middle = conversion.model_json_to_middle_json(model)
    supplied = AssetStore({"images/existing.png": _png()})
    before = deepcopy(middle.model_dump())
    with pytest.raises(ValueError, match="Invalid image data URI"):
        if operation == "materialize":
            materialize_middle(middle, supplied)
        elif operation == "render":
            api.render(middle, "html", assets=supplied)
        else:
            DocumentResult(middle, supplied).save_bundle(tmp_path / "bundle")
    assert middle.model_dump() == before
    assert list(supplied) == ["images/existing.png"]
    assert not (tmp_path / "bundle").exists()
