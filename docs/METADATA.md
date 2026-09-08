# Source document metadata

`docvortex.extract_metadata()` reads the same source properties that native analysis places in
`ModelJson.metadata.document` and `MiddleJson.metadata.document`. It does not run body prediction,
OCR, VLM, PDF classification, scripts, or resource downloads.

```python
from docvortex import extract_metadata, parse

inspection = extract_metadata("report.pdf")
print(inspection.metadata.document)
print(inspection.diagnostics)
result = parse("report.pdf", page_range="2-3", keep_model_json=True)
assert result.middle_json.metadata.document == inspection.metadata.document
```

The source can be a path, `bytes`, or a caller-owned `PDFDocument`. Bytes without a recognizable
signature can use `file_suffix=`. HTML can additionally receive `source_context=` for its declared
transport encoding. A caller-owned PDF remains open; internally opened documents are closed.
PDF source properties are captured before page selection, so the count refers to the whole input.

## Properties

`DocumentProperties` is defined in `docvortex.schema`. Optional strings are `title`, `subject`,
`description`, `publisher`, `created_at`, `modified_at`, `creator_application`, and
`producer_application`. `authors`, `keywords`, `languages`, and `identifiers` are ordered lists:
empty values are removed and duplicates are removed without splitting individual source strings.
Dates are ISO 8601 strings retaining source precision and explicit timezone information. OLE
FILETIME values are UTC. Missing or invalid dates are not replaced with filesystem or current time.

`page_count` is a nonnegative source count or null; `page_count_kind` describes its meaning:

| Native format | Property sources | Count kind |
| --- | --- | --- |
| PDF | Info first, XMP fills missing properties, catalog language | `physical` |
| DOCX | OOXML core/extended properties | `declared`, if stored |
| PPTX | OOXML properties and slide directory, including hidden slides | `slide` |
| XLSX | OOXML properties and sheet directory, including hidden sheets | `sheet` |
| DOC / PPT / XLS | OLE SummaryInformation / DocumentSummaryInformation | `declared`, if stored |
| ODT | `meta.xml` | `declared`, if stored |
| ODS / ODP | `meta.xml` and visible sheet/slide structure | `sheet` / `slide` |
| RTF | `info`, generator, declared language | `declared`, if stored |
| EPUB | OPF Dublin Core and recognized meta properties | `spine` |
| OFD | DocInfo; first nonempty scalar across DocBody entries | `physical`, summed across documents |
| HTML | title, standard meta, Dublin Core, Open Graph, document language | `logical`, 1 |
| CSV | no embedded descriptive property convention | `logical`, 1 |

A declared count may be stale and is not a measured layout. Counts are never inferred from the
number of pages selected for parsing. Source author strings are not authors inferred from body text.
Publication dates are not relabeled as creation dates. Image EXIF and arbitrary custom properties
are outside this interface.

## Errors and protocol

`MetadataResult` contains `metadata: DocumentMetadata` and `diagnostics: tuple[Diagnostic, ...]`.
An unreadable source raises `DocumentError` with `open_failed`; unsupported inputs use
`file_type_unsupported`. Optional property-part failures produce `read_metadata_failed` diagnostics
and retain other successfully extracted fields. Ordinary native analysis can continue if optional
metadata extraction fails and its body reader can still parse the document.

`metadata.producer` identifies the parser producing the JSON. Original authoring/conversion software
belongs inside `metadata.document`, and never replaces that parser identity. Postprocessing deep
copies the properties. JSON, Structured Content, and bundles retain them without needing the source.
The new fields do not change rendering layout or the existing exported file-property defaults.

This is an optional addition to shared JSON **2.0**, introduced in DocVortex **0.2.5**. Existing 2.0
JSON without `metadata.document` remains readable and no source facts are invented. Older strict
readers must be upgraded before consuming new fields. Schema and bundle version numbers remain 2.0.

## MinerU integration

Doclib calls the public lightweight API during ingestion. It maps existing database columns and
applies its own truncation limits. It does not store all expanded properties in new database columns;
complete properties are retained in subsequent parse JSON. Authors join with `; `, keywords with
`, `, and the language column takes the first declared language. Reflow scheduling remains a product
policy and does not change the source count recorded in JSON.

After parsing, nonempty source properties update existing columns and title/author search fields
without downgrading the indexed body tier. Old caches are not rewritten or forcibly reparsed;
compaction still declines batches with different metadata envelopes.
