# DocVortex

A fast, multi-format document parsing and conversion engine.

DocVortex provides a complete, standalone document pipeline:

```text
Document -> ModelJson + Assets -> MiddleJson + Assets -> Render / Export
```

Native inputs include text PDFs, DOC/DOCX, PPT/PPTX, XLS/XLSX, RTF, ODT/ODS/ODP,
EPUB, HTML, OFD and CSV. Output formats include Markdown, HTML, LaTeX, DOCX,
EPUB, PDF and structured content. Content List V1/V2 are provided by MinerU.

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
services. PDF access uses `pypdfium2>=5.10.1,<6`; the
compatibility matrix also exercises 5.13.0.

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
version `1.0`, and neutral producer metadata. Definitions are in `schemas/`.
Application-specific metadata belongs in `extensions`. See the
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

DocVortex project code is licensed under the [MIT License](LICENSE.md). Bundled
third-party portions retain their own licenses: the PDF text layer includes
Apache-2.0 portions attributed in its source, and the bundled Droid font retains
its [original NOTICE](src/docvortex/resources/fonts/NOTICE). The
[Apache-2.0 license](licenses/Apache-2.0.txt) ships with both distributions; package
metadata therefore declares `MIT AND Apache-2.0`.

Dependencies include `pydantic>=2.12.5,<3` and `numpy>=1.21.6`; the
installer selects versions compatible with the active Python interpreter.
On Apple Silicon, Python 3.14 installation requires macOS 14 or newer because
of the ONNX Runtime dependency used by file-type detection.

See [the standalone example](demo/README.md) for native parsing and portable
result bundles, and [the validation record](docs/validation.md) for test coverage.

PDFium uses a bundled, pinned CJK fallback font for non-embedded CJK fonts;
no system font installation is required. See [PDF font policy](docs/PDF_FONTS.md)
for initialization, diagnostics and replacement boundaries.

PDF output normalizes fullwidth Latin letters, digits and selected technical
symbols in natural-language text and table cells, preserving Chinese punctuation,
formulas, code and link targets. See
[PDF text normalization](docs/PDF_TEXT_NORMALIZATION.md) for scope and API usage.

See [the DocVortex upgrade guide](docs/DOCVORTEX_UPGRADE.md) for package, protocol,
PDF text rules and publishing configuration changes.

See [rendering ownership](docs/RENDER_OWNERSHIP.md) for the seven engine targets,
MinerU Content List integration and public fragment helpers.

See [refactor validation](docs/REFACTOR_PROGRESS.md) for the staged internal
refactoring, compatibility checks, corpus comparisons and measured performance.
