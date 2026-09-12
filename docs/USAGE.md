# DocVortex usage guide

[English README](../README.md) · [中文 README](../README_zh-CN.md)

Use the complete pipeline for everyday conversion, or compose its stages when
you need access to intermediate results. All examples use the public Python SDK.
Install with `pip install docvortex` and replace `report.pdf` with a local text PDF.
Each example can be run independently in a fresh output directory.

## Stage APIs

The pipeline has three stages:

```text
Source → analyze → AnalysisResult → postprocess → DocumentResult → render → RenderArtifact
```

```python
from docvortex.api import analyze, postprocess, render

analysis = analyze("report.pdf", page_range="1-5")
result = postprocess(analysis)
artifact = render(result.middle_json, "docx", assets=result.assets)
artifact.write("output/report.docx")
```

| Stage | Result | Purpose |
| --- | --- | --- |
| `analyze(source)` | `AnalysisResult` | Read native content into `model_json` |
| `postprocess(analysis)` | `DocumentResult` | Build `middle_json` and materialize assets |
| `render(middle_json, format, assets=...)` | `RenderArtifact` | Encode an output without writing files |
| `artifact.write(path)` | `ExportResult` | Write the main file and required sidecar assets |

`docvortex.parse()` combines analysis and postprocessing.
`docvortex.convert()` also renders and writes one output.
`DocumentResult.export()` renders and writes from an existing result.

The stage functions live in `docvortex.api`. Root-level aliases include
`docvortex.analyze`, `docvortex.postprocess_document` and `docvortex.render_artifact`.
The `docvortex.render` package provides lower-level renderers whose return values
can be strings, bytes, dictionaries or lists, rather than `RenderArtifact` objects.

## PDF page selection and ownership

`parse()` and `analyze()` accept a filesystem path, document bytes or a
caller-owned `PDFDocument`. For byte input, use `file_suffix` when you need to
explicitly identify the format, such as `file_suffix="docx"`.

PDF selection uses **one-based** page numbers:

| Selection | Meaning |
| --- | --- |
| `1-5` | Pages 1 through 5, inclusive |
| `1,3,5` | Selected individual pages |
| `r1` | The last page |
| `r3-r1` | The last three pages |
| `all` or omitted | Every page |

Selections are sorted and deduplicated; out-of-bounds pages are omitted.
Reversed ranges and negative page numbers are invalid.
Page selection applies only to PDF input; other native formats are parsed whole.

```bash
docvortex convert report.pdf --pages 1-5 --format markdown --output output/pages.md
```

A caller-owned document stays open after parsing:

```python
import docvortex
from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    result = docvortex.parse(document, page_range="r1")
    print(document.page_count)

result.export("output/last-page.md", output_format="markdown")
```

The result owns the data needed for later exports, so it can outlive the open
document and the original source file.

## Explicit PDF classification

DocVortex parses native document content without OCR or VLM inference.
For scanned or otherwise unsuitable PDFs, applications can classify the document
and choose their own OCR or inference service.

```bash
docvortex classify report.pdf
```

The command returns `txt` or `ocr`. The equivalent Python operation is explicit:

```python
import docvortex
from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    mode = document.classify()
    if mode == "txt":
        result = docvortex.parse(document)
        result.export("output/native.md", output_format="markdown")
    else:
        print("This PDF needs an external OCR or inference workflow.")
```

Classification does not start inference. Native analysis does not classify
automatically or switch to another backend.

## Page images and embedded assets

Render a whole PDF page or crop a region to an encoded image:

```python
from pathlib import Path

from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    image = document.render_image(0, bbox=(0.1, 0.2, 0.8, 0.7), image_format="png")

Path("output").mkdir(parents=True, exist_ok=True)
Path("output/crop.png").write_bytes(image.data)
print(image.width, image.height, image.mime_type, image.extension)
```

- Page indices are **zero-based**, unlike `page_range` selections.
- `bbox` uses normalized coordinates `(left, top, right, bottom)`; omit it for the whole page.
- Supported encodings are `jpeg` (default), `png` and `webp`. Crops are encoded directly to the requested format.
- The returned `ImageArtifact` is immutable and remains usable after the document closes. `crop_image()` returns JPEG bytes.

`PDFDocument.from_image()` can wrap an image as a PDF document for low-level
document operations; it does not add OCR to the native parser.

Use `docvortex.assets` to validate embedded images or transcode image bytes:

```python
import base64

from docvortex.assets import parse_image_data_uri_strict, transcode_image
from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    image = document.render_image(0, image_format="png")

uri = "data:image/png;base64," + base64.b64encode(image.data).decode("ascii")
data, extension = parse_image_data_uri_strict(uri)
webp = transcode_image(data, image_format="webp")
print(extension, webp.mime_type, webp.width, webp.height)
```

`parse_image_data_uri_strict()` returns validated `(data, extension)`;
`transcode_image()` returns an `ImageArtifact`. Neither function fetches URLs or
resolves filesystem paths. Transcoding does not provide SVG rasterization.

For tree traversal, `docvortex.content.tree.iter_image_payloads(block)` yields
the current node if it carries an image, followed by its descendants in depth-first
order. It does not modify the document tree.

## Portable Bundles

A Bundle is a directory containing document JSON and the image assets needed for
later exports. The `.bundle` suffix in examples is a naming convention.

```python
import docvortex

result = docvortex.parse("report.pdf", keep_model_json=True)
result.save_bundle("output/report.bundle")

restored = docvortex.load_bundle("output/report.bundle")
restored.export("output/report.pdf", output_format="pdf")
```

| Bundle entry | Contents |
| --- | --- |
| `manifest.json` | Bundle metadata and asset hashes |
| `middle.json` | The processed document representation |
| `model.json` | Analysis data, included when retained with `keep_model_json=True` |
| Image assets | Materialized image bytes referenced by the document |

Loading verifies asset hashes. The restored result does not need the source
document or the process that parsed it. Missing external assets must be supplied
before saving a portable Bundle.

### Assets and file replacement

Pass `assets=result.assets` when rendering `result.middle_json` directly.
`DocumentResult.export()` supplies its assets automatically. File-based outputs
write any required sidecar assets; DOCX, EPUB and PDF package their assets inside
the main output.

Existing files with identical bytes are reused. Replacing a file with different
content requires an explicit opt-in:

- Python: set `overwrite=True` on `convert()`, `export()`, `save_bundle()` or `RenderArtifact.write()`.
- CLI: add `--overwrite` to `docvortex convert`.

These protections apply to DocVortex export operations. Direct filesystem writes,
such as `Path.write_bytes()` in the crop example, follow normal Python behavior
and replace an existing file.

## Read metadata without parsing the body

```python
from docvortex import extract_metadata

inspection = extract_metadata("report.pdf")
print(inspection.metadata.document.title)
print(inspection.metadata.document.authors)
```

All 15 native document formats support metadata inspection. Available fields
depend on the source. Normal parsing also retains source properties in
`metadata.document`, including original PDF properties across page selections.
See the [metadata guide](METADATA.md) for fields and per-format coverage.

## Output semantics and integration

PDF export reflows document content rather than reproducing the original page
drawing instructions. Short paragraphs and short tables with annotations stay
together when possible. Longer content can paginate, tables repeat existing
headers, and images scale to fit while keeping short captions on the same page.

The output targets are Markdown, HTML, LaTeX, DOCX, EPUB, PDF and
`structured_content`. Input support for PPTX or XLSX does not imply support for
exporting those formats. MinerU-specific Content List outputs belong to MinerU;
see [rendering ownership](RENDER_OWNERSHIP.md).

DocVortex document JSON uses `docvortex.model` or `docvortex.middle` schema identity
and schema version `2.0`, with required `metadata.file_suffix` and
`metadata.producer`. Application-specific metadata belongs in `extensions`.
See the [JSON protocol](JSON_PROTOCOL.md), [schema definitions](../schemas/)
and [HTML protocol](HTML_PROTOCOL.md).

For cross-package integrations, `docvortex.public_api.PUBLIC_API` lists supported
modules and symbols. Read the [public SDK and 0.4 migration guide](sdk-0.4.md)
before upgrading a host application that uses DocVortex internals or older imports.
