"""Unified document outer protocol reading and writing, strict verification and source protection return."""

from copy import deepcopy
import json

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from docvortex.schema import DocumentMetadata, ModelJson, Producer
from docvortex.postprocess.document import model_json_to_middle_json


def model() -> ModelJson:
    """Construct native parsing results that include discernible text and third-party nested extensions."""
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
    """Verify that the actual JSON is consistent with the public Schema, round-trip preserving page number, text, and producer."""
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
    """Reading data that is missing a required field must fail without tinkering with the protocol or source."""
    payload = model().to_dict()
    del payload[field]
    with pytest.raises(ValueError):
        ModelJson.from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [("schema", "docvortex.middle"), ("schema", "mineru.model"), ("schema_version", "1.0"), ("schema_version", "3.0")],
)
def test_wrong_protocol_is_rejected(field: str, value: str) -> None:
    """Refuse to obfuscate Model/Middle or guess historical formats by version number."""
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
    """Reject missing format, producer, or blank source identifiers."""
    payload = model().to_dict()
    payload["metadata"] = metadata
    with pytest.raises(ValidationError):
        ModelJson.from_dict(payload)


def test_postprocess_deep_copies_metadata_and_extensions() -> None:
    """Post-processing and downstream modifications must not contaminate the source of the analysis results with nested extensions."""
    source = model()
    before = deepcopy(source.to_dict())
    middle = model_json_to_middle_json(source)
    middle.metadata.producer.name = "changed"
    middle.extensions["application"]["values"].append("changed")
    assert source.to_dict() == before
    assert middle.pages[0].page_idx == 3
    assert middle.pages[0].blocks[0].content[0].content == "保留正文"


def test_empty_extensions_and_explicit_field_exclusion() -> None:
    """The default output leaves empty extensions, and specialized renderers can still explicitly exclude document protocol flags."""
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
    """The field with the same name in the extension is not a page block, and the omission of the image must not modify third-party information."""
    source = model()
    source.extensions = {"application": {"type": "image", "image_base64": "keep-original"}}
    assert source.to_dict(exclude_block_fields={"image_base64"})["extensions"] == source.extensions


@pytest.mark.parametrize("field", ["schema", "schema_version"])
def test_json_schema_requires_explicit_identity(field: str) -> None:
    """Public JSON Schema Like the reader, reject documents that are missing an identity or version."""
    source = model()
    payload = source.to_dict()
    del payload[field]
    assert list(Draft202012Validator(ModelJson.model_json_schema()).iter_errors(payload))
