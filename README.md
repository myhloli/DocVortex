
![DocVortex overview: native document inputs flow through a unified document model to Markdown, HTML, LaTeX, DOCX, EPUB, PDF and structured content.](https://gcore.jsdelivr.net/gh/myhloli/DocVortex@main/docs/images/docvortex-overview.jpg)

# DocVortex

A fast, multi-format document parsing and conversion engine.

DocVortex provides a complete, standalone document pipeline:

```text
Document -> Unified Intermediate Representation -> Render / Export
```

Native inputs include text PDFs, DOC/DOCX, PPT/PPTX, XLS/XLSX, RTF, ODT/ODS/ODP,
EPUB, HTML, OFD and CSV. Output formats include Markdown, HTML, LaTeX, DOCX,
EPUB, PDF and structured content. 

Native parsing runs without OCR or VLM inference services.
PDF classification is an explicit document operation; native analysis does not
silently classify the document or select another inference backend.

## Install

```bash
pip install docvortex
docvortex convert report.pdf --format markdown --output output/report.md
docvortex classify report.pdf
```

Python 3.10–3.14 is supported. Native parsing does not require OCR/VLM inference
services. 

## Parse once, export many times

```python
import docvortex

result = docvortex.parse("report.pdf", keep_model_json=True)
result.export("output/report.md", output_format="markdown")
result.export("output/report.docx", output_format="docx")
result.export("output/report.epub", output_format="epub")
result.save_bundle("output/report.bundle")

# This works after the source document and its parsing process are gone.
restored = docvortex.load_bundle("output/report.bundle")
restored.export("output/report.pdf", output_format="pdf")
```

Bundles contain `manifest.json`, `middle.json`, optional `model.json`, and image
assets. The loader verifies asset hashes. Missing external assets must be supplied
before saving a portable bundle. Existing files are protected unless the caller
explicitly sets `overwrite=True`.

## Stage APIs

```python
from docvortex.api import analyze, postprocess, render

analysis = analyze("report.pdf", page_range="1-5")
result = postprocess(analysis)
artifact = render(result.middle_json, "docx", assets=result.assets)
artifact.write("output/report.docx")
```

The stage API lives in `docvortex.api`. Root-level conveniences include `parse`,
`analyze`, `convert`, `postprocess_document`, and `render_artifact`. The
`docvortex.render` package also exposes the low-level renderers and their original
string, bytes, dictionary, or list return values.

PDF page selections use `1-5`, `r1` and `all`; other native formats are parsed as
whole documents. A caller-owned `PDFDocument` can be passed to `analyze` or `parse`
and remains open afterward.

## Page images and embedded assets

```python
from docvortex.assets import parse_image_data_uri_strict, transcode_image
from docvortex.content.tree import iter_image_payloads
from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    image = document.render_image(0, bbox=(0.1, 0.2, 0.8, 0.7), image_format="png")
# image.data, image.width, image.height, image.mime_type and image.extension
# remain available after the document closes.
```

`render_image` accepts zero-based page indices, optional normalized bounding boxes
and `jpeg` (default), `png` or `webp` output. Omitting `bbox` renders the whole page.
Crops are encoded directly to the requested format. `crop_image` continues to
return JPEG bytes. Image sources opened with `PDFDocument.from_image` retain the
existing image-to-PDF conversion behavior.

`docvortex.assets` exposes immutable `ImageArtifact` and `ImageFormat` contracts.
`parse_image_data_uri_strict(uri)` validates embedded image bytes and returns
`(data, extension)`; `transcode_image(data, image_format="png")` returns an
`ImageArtifact`. These operations do not fetch URLs or resolve filesystem paths,
and transcoding does not add SVG rasterization support.
`iter_image_payloads(block)` yields the current node, if it carries an image,
then traverses its children depth first without modifying the document tree.

## Explicit PDF classification

```python
from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    mode = document.classify()  # "txt" or "ocr"; no inference is started
    if mode == "txt":
        result = docvortex.parse(document)
```

Native analysis trusts the caller's choice and does not classify automatically.
Applications can use the classification result to select their own OCR or
inference service when a document requires it.

DocVortex JSON uses schema identity `docvortex.model` or `docvortex.middle`, schema
version `2.0`, and required `metadata.file_suffix` / `metadata.producer`. Definitions are in `schemas/`.
Application-specific metadata belongs in `extensions`. See the
[shared JSON protocol and migration guide](docs/JSON_PROTOCOL.md) and the
[compatibility guide](docs/COMPATIBILITY.md) for existing application integrations
and historical data formats. The [HTML protocol](docs/HTML_PROTOCOL.md) describes
DocVortex markers and semantic round trips.

## Scope and development

PDF output is a semantic reflow of the document, not a lossless reproduction of
the original page drawing instructions. Input support for PPTX/XLSX does not imply
PPTX/XLSX output support. Rust implementation work is a future stage behind these
public data and processing boundaries.

```bash
uv venv
uv pip install -e ".[test,dev]"
uv run --no-project python -m pytest -q
uv run --no-project ruff check src
uv run --no-project ruff format --check src
uv build
```

DocVortex project code is licensed under the [MIT License](LICENSE.md).

See [rendering ownership](docs/RENDER_OWNERSHIP.md) for the seven engine targets,
MinerU Content List integration and public fragment helpers.

## Read source metadata without parsing the body

```python
from docvortex import extract_metadata

inspection = extract_metadata("report.pdf")
print(inspection.metadata.document.title)
print(inspection.metadata.document.authors)
```

All 15 native document formats support this API. Normal parsing also carries these properties in
`metadata.document`, preserving the original PDF properties across page selections. See the
[field definitions, format matrix, and compatibility notes](docs/METADATA.md).
