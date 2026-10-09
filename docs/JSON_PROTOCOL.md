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

## Optional visual annotation anchors

Image, table, chart and code captions and footnotes accept an optional nullable
`anchor` string, using the same source bookmark identity as body text and titles.
The schema version remains 2.0. Existing documents without this field remain
readable; older strict readers need an upgrade to accept it.

Markdown, HTML, DOCX, PDF, EPUB and LaTeX render visible annotations as internal
link targets. Empty annotations do not claim a target, and links to duplicate
anchors resolve to the first visible target in document order. An anchored table
annotation prevents the default continuation merge from dropping its caption or
replacing its footnote. Tables without a nonempty annotation anchor retain the
existing merge behavior.

Structured Content preserves the optional `anchor` in its `captions` and
`footnotes` entries; their `content` remains plain rendered annotation content
without an injected target tag.

## Optional PDF layout geometry

Native PDF analysis adds `extensions.docvortex_layout` and deterministic
postprocessing preserves it in Middle JSON and result bundles:

```json
{"docvortex_layout":{"version":1,"pages":[{"page_idx":0,"width_pt":595.28,"height_pt":841.89}]}}
```

`page_idx` is the original zero-based source index, including for page selections.
Dimensions are finite positive PDF points (72 points per inch), in the same
visible-page orientation as normalized block bboxes. Page rotation is already
accounted for; consumers must not rotate these coordinates again. Blank pages
also have geometry. Producers should provide an entry for each result page.
Duplicate indices, unsupported extension versions, and missing/invalid dimensions
cannot be used for original PDF layout. Extra source-page entries may be retained
when selecting a subset of an existing result.

Pages may also contain `image_rotations`, such as `{"3":270}`: keys are source
block indices encoded as strings, and values are the 0/90/180/270-degree angles
used to turn region crops upright. The original-layout renderer reverses this
crop transform when placing the image. Structured HTML tables use the same angle
to restore the source orientation after layout in upright coordinates. No extra
image data or schema fields are required.

This uses the existing JSON extension mechanism; Model/Middle/bundle schema
versions remain 2.0. Older documents without this extension remain readable.
OFD geometry is not collected in this version. Other producers can supply the
same extension for PDF results without depending on a live PDFDocument.

Host analyzers can reuse `docvortex.document.pdf.layout.extract_layout_geometry`
before closing their existing PDFDocument, then `attach_layout_image_rotations`
after block filtering. Preserve crop angles before removing inference-only
metadata. `remap_layout_geometry` maps local indices to source indices;
`merge_layout_extensions` merges cached page batches while requiring other
extensions to match and retaining the winning page's crop orientation.
MinerU uses these helpers for all PDF tiers alongside `extensions.mineru`.

Since 0.4.2, native PDF display equations have `content: ""` at the final output
boundary and keep their image payload, bbox and detected equation-number region.
Detection still uses internal text evidence. Inline formulas and model-assisted
non-Flash formulas are unchanged; existing results are not rewritten.
