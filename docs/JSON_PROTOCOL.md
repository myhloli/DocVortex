# Shared document JSON 2.0

DocVortex 0.2.0 and MinerU use the same Model/Middle wire protocol. This is a
direct format switch: old DocVortex 1.0, MinerU envelopes without a schema
identity, and historical `pdf_info` results must be regenerated from the source.
Loading never rewrites files, drops unknown application extensions, or invents a producer.

```json
{
  "schema": "docvortex.middle",
  "schema_version": "2.0",
  "metadata": {
    "file_suffix": "pdf",
    "producer": {"name": "mineru", "version": "4.0.0b1"}
  },
  "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"}},
  "is_full_document": true,
  "pages": []
}
```

Model JSON instead uses `schema: "docvortex.model"`, `page_index_map`, and its
existing array-of-page-block-arrays. Middle JSON keeps `PageInfo`, Block and Span
semantics unchanged. Both versions are `2.0`; the schema identity must also match.
The declared Pydantic fields generate `schemas/model-2.0.json` and
`schemas/middle-2.0.json`. Default serialization retains protocol fields,
complete metadata and even empty `extensions`.

## Python access and reading

```python
from docvortex.schema import DocumentMetadata, ModelJson, Producer

model = ModelJson(
    pages=[],
    page_index_map=[],
    metadata=DocumentMetadata(
        file_suffix="pdf",
        producer=Producer(name="my-application", version="1.0"),
    ),
)
restored = ModelJson.from_dict(model.to_dict())
assert restored == ModelJson.from_json(model.to_json())
print(restored.metadata.file_suffix, restored.metadata.producer.name)
```

`MiddleJson` has the same `from_dict`/`from_json` methods. The JSON codec loaders
delegate to them. Constructors require metadata; removed top-level `file_suffix`
and `producer` attributes have no aliases. Parsing entrypoint arguments such as
`file_suffix=` are unchanged. Native analyzers explicitly record DocVortex as
producer; applications provide their own genuine identity when creating documents.

## Product information and exports

DocVortex preserves `extensions` as JSON and does not depend on MinerU enums.
MinerU validates `extensions.mineru` only when present. It records actual
`tier` (`flash/basic/standard/advanced`) and resolved `parse_mode` (`txt/ocr`).
Internal efforts `flash/medium/high/xhigh` map to those tiers respectively.
Requested settings, `auto`, `effort`, and a duplicate `mineru_version` are not stored.

Loading native results in MinerU does not require a MinerU extension. Reading,
rendering and serialization retain the producer; deterministic postprocessing
deep-copies metadata and extensions from Model to Middle.

`DocumentResult` and MinerU `ParseResult` remain separate convenience wrappers.
MinerU's PDF `ParseResult.to_dict()` still omits nested `image_base64`; non-PDF
output retains its existing image behavior. This deliberate image policy means
wrapper JSON need not equal the underlying Middle JSON byte for byte.
DocVortex retains its AssetStore and explicit sidecar export behavior.

Structured Content keeps its own content projection, shares metadata/extensions,
and carries no Model/Middle schema identity. Content List V1/V2 remain MinerU outputs.
Bundle manifests now use `docvortex.bundle` version `2.0`, with current document
JSON and the existing asset paths, sizes and hashes. Old bundles are rejected.

MinerU HTTP output filenames and request parameters are unchanged. Only Doclib's
persisted Middle JSON reader additionally accepts recognizable MinerU 3.4.5/1.0
pages and the former MinerU schema 2.0 envelope, converting them in memory to the
current document. Generic ParseResult, HTTP/ZIP, Gradio and DocVortex codecs remain
strict. Successfully converted batches can be cached and compacted when their
metadata/extensions/full-document flags agree; ordinary reads never rewrite files.
Unknown or damaged formats still require source reparse.

## Optional source properties (DocVortex 0.2.5)

`metadata.document` optionally carries a typed `DocumentProperties` object. Its descriptive,
publication, date, authoring-software, and qualified count fields are shared by the lightweight
`extract_metadata()` API and native analysis. It is distinct from the parser identity in
`metadata.producer` and application data in `extensions`.

The schema version remains 2.0. New readers accept absent source properties in existing documents;
old strict readers require an upgrade to accept this addition. No legacy data is rewritten or
silently enriched. See [source metadata](METADATA.md) for field and format semantics.
