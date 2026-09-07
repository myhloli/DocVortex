"""统一文档外层协议的读写、严格校验和来源保护回归。"""

from copy import deepcopy
import json

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from docvortex.schema import DocumentMetadata, ModelJson, Producer
from docvortex.postprocess.document import model_json_to_middle_json


def model() -> ModelJson:
    """构造包含可辨正文和第三方嵌套扩展的原生分析结果。"""
    return ModelJson(
        pages=[[{"type": "text", "content": [{"type": "text", "content": "保留正文"}]}]],
        page_index_map=[3],
        metadata=DocumentMetadata(file_suffix="html", producer=Producer(name="external", version="original")),
        extensions={"application": {"values": [None, True, 2, {"text": "中文"}]}},
    )


@pytest.mark.parametrize("kind", ["model", "middle"])
@pytest.mark.parametrize("skip_defaults", [True, False])
@pytest.mark.parametrize("schema_mode", ["validation", "serialization"])
def test_shared_schema_and_lossless_roundtrip(kind: str, skip_defaults: bool, schema_mode: str) -> None:
    """验证实际 JSON 与公开 Schema 一致，往返保留页号、正文和生产者。"""
    source = model()
    document = source if kind == "model" else model_json_to_middle_json(source)
    payload = document.to_dict(skip_defaults=skip_defaults)
    assert payload["schema"] == f"docvortex.{kind}"
    assert payload["schema_version"] == "2.0"
    assert payload["metadata"]["producer"] == {"name": "external", "version": "original"}
    assert not {"file_suffix", "producer", "effort", "mineru_version"} & payload.keys()
    schema = type(document).model_json_schema(mode=schema_mode)
    assert {"schema", "schema_version", "metadata", "extensions", "pages"} <= schema["properties"].keys()
    Draft202012Validator(schema).validate(payload)
    assert type(document).from_dict(payload) == document
    assert type(document).from_json(document.to_json(skip_defaults=skip_defaults)) == document
    assert document.model_dump(mode="json") == json.loads(document.model_dump_json())
    assert not hasattr(document, "file_suffix") and not hasattr(document, "producer")


@pytest.mark.parametrize("field", ["schema", "schema_version", "metadata", "pages", "page_index_map"])
def test_missing_required_wire_fields_fail(field: str) -> None:
    """读取缺少必需字段的数据必须失败，不补造协议或来源。"""
    payload = model().to_dict()
    del payload[field]
    with pytest.raises(ValueError):
        ModelJson.from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [("schema", "docvortex.middle"), ("schema", "mineru.model"), ("schema_version", "1.0"), ("schema_version", "3.0")],
)
def test_wrong_protocol_is_rejected(field: str, value: str) -> None:
    """拒绝混淆 Model/Middle 或通过版本号猜测历史格式。"""
    payload = model().to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        ModelJson.from_dict(payload)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"file_suffix": "html"},
        {"file_suffix": "html", "producer": {}},
        {"file_suffix": "html", "producer": {"name": "", "version": "x"}},
    ],
)
def test_source_metadata_is_required(metadata: dict) -> None:
    """拒绝缺失格式、生产者或空白来源标识。"""
    payload = model().to_dict()
    payload["metadata"] = metadata
    with pytest.raises(ValidationError):
        ModelJson.from_dict(payload)


def test_postprocess_deep_copies_metadata_and_extensions() -> None:
    """后处理和下游修改不能污染分析结果的来源与嵌套扩展。"""
    source = model()
    before = deepcopy(source.to_dict())
    middle = model_json_to_middle_json(source)
    middle.metadata.producer.name = "changed"
    middle.extensions["application"]["values"].append("changed")
    assert source.to_dict() == before
    assert middle.pages[0].page_idx == 3
    assert middle.pages[0].blocks[0].content[0].content == "保留正文"


def test_empty_extensions_and_explicit_field_exclusion() -> None:
    """默认输出保留空扩展，专用渲染器仍可显式排除文档协议标识。"""
    source = model()
    source.extensions = {}
    assert source.to_dict()["extensions"] == {}
    payload = source.to_dict()
    payload["extensions"]["application"] = {"added": True}
    assert source.extensions == {}
    projected = source.model_dump(mode="json", exclude={"schema_id", "schema_version", "pages"}, exclude_defaults=True)
    assert not {"schema", "schema_id", "schema_version", "pages"} & projected.keys()
    assert projected["metadata"] == source.to_dict()["metadata"]


def test_image_omission_does_not_touch_application_extensions() -> None:
    """扩展中的同名字段不是页面块，图片省略不得修改第三方信息。"""
    source = model()
    source.extensions = {"application": {"type": "image", "image_base64": "keep-original"}}
    assert source.to_dict(exclude_block_fields={"image_base64"})["extensions"] == source.extensions


@pytest.mark.parametrize("field", ["schema", "schema_version"])
def test_json_schema_requires_explicit_identity(field: str) -> None:
    """公开 JSON Schema 与读取器一样拒绝缺少身份或版本的文档。"""
    source = model()
    payload = source.to_dict()
    del payload[field]
    assert list(Draft202012Validator(ModelJson.model_json_schema()).iter_errors(payload))
