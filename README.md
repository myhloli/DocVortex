<div align="center">

<img src="https://raw.githubusercontent.com/myhloli/DocVortex/main/docs/images/docvortex-logo.jpg" alt="DocVortex logo" width="200">

# DocVortex

**Native document parsing. One structure, many outputs.**

[![PyPI](https://img.shields.io/pypi/v/docvortex?color=008cff)](https://pypi.org/project/docvortex/)
[![Python](https://img.shields.io/pypi/pyversions/docvortex)](https://pypi.org/project/docvortex/)
[![CI](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml/badge.svg)](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/myhloli/DocVortex/blob/main/LICENSE.md)

**English** · [简体中文](https://github.com/myhloli/DocVortex/blob/main/README_zh-CN.md)

[Quick start](#quick-start) · [Formats](#supported-formats) · [Documentation](#documentation)

</div>

## Documents in. Possibilities out.

DocVortex is a standalone Python engine for parsing and converting documents.
It reads native text and document structure into a unified representation,
then exports the result in the formats your workflow needs.

- **Multi-format input** — read text PDFs, Office files, OpenDocument files, EPUB, HTML, OFD and CSV.
- **Parse once, export many times** — reuse the same result for Markdown, HTML, LaTeX, DOCX, EPUB, PDF and structured JSON.
- **Portable results** — save document structure and image assets in a Bundle, then export again without the source file.
- **Composable APIs** — use the complete pipeline or integrate analysis, postprocessing and rendering separately.

Native parsing works without an OCR or VLM inference service. Use DocVortex
directly through its CLI or Python SDK, independently of MinerU.

![DocVortex pipeline: native documents become a unified representation, then Markdown, HTML, LaTeX, DOCX, EPUB, PDF or structured JSON.](https://raw.githubusercontent.com/myhloli/DocVortex/main/docs/images/docvortex-overview.jpg)

## Quick start

Requires **Python 3.10–3.14**.

### Install

```bash
pip install docvortex
```

### Command line

Convert a text PDF to Markdown:

```bash
docvortex convert report.pdf --format markdown --output output/report.md
```

Replace `report.pdf` with a local file in any supported input format.
Use `--format` to choose the output; run `docvortex convert --help` for options.

### Python

Parse a document once and export it twice:

```python
import docvortex

result = docvortex.parse("report.pdf")
result.export("output/report.md", output_format="markdown")
result.export("output/report.docx", output_format="docx")
```

The result owns its document structure and assets, so further exports do not
reopen or reparse the source. Existing output files are protected by default;
use `overwrite=True` in Python or `--overwrite` in the CLI to replace them.

## Supported formats

### Native inputs · 15 formats

| Document family | Formats |
| --- | --- |
| PDF with native text | PDF |
| Word & rich text | DOC, DOCX, RTF |
| Presentations | PPT, PPTX |
| Spreadsheets | XLS, XLSX, CSV |
| OpenDocument | ODT, ODS, ODP |
| E-books & web documents | EPUB, HTML |
| Open Fixed-layout Document | OFD |

### Outputs · 7 formats

| Output | `--format` / `output_format` |
| --- | --- |
| Markdown | `markdown` |
| HTML | `html` |
| LaTeX | `latex` |
| Word document | `docx` |
| EPUB e-book | `epub` |
| PDF | `pdf` |
| Structured JSON | `structured_content` |

PPT/PPTX and XLS/XLSX are input formats only. Structured JSON is an export
format; the [document JSON protocol](https://github.com/myhloli/DocVortex/blob/main/docs/JSON_PROTOCOL.md)
separately defines the analysis and intermediate representations.

## Save now, export later

A Bundle packages the parsed document and its image assets for reuse across
processes or machines. Load it whenever you need another output format:

```python
import docvortex

result = docvortex.parse("report.pdf")
result.save_bundle("output/report.bundle")

restored = docvortex.load_bundle("output/report.bundle")
restored.export("output/report.epub", output_format="epub")
```

The restored result works without the original file. See the
[usage guide](https://github.com/myhloli/DocVortex/blob/main/docs/USAGE.md#portable-bundles)
for Bundle contents, asset handling and overwrite rules.

Need only the title, authors or other source properties?
[`docvortex.extract_metadata()`](https://github.com/myhloli/DocVortex/blob/main/docs/METADATA.md)
reads metadata without parsing the document body.

## Choose the right workflow

- **Text PDFs:** native parsing uses the document's existing text and structure. Scanned pages requiring OCR need an external OCR or inference service.
- **PDF classification:** `docvortex classify report.pdf` returns `txt` or `ocr`. Classification is explicit and does not start inference; parsing does not automatically switch backends.
- **PDF export:** output is a semantic reflow of the content. Original page layout and drawing instructions are not reproduced losslessly.

## Documentation

| Guide | What you will find |
| --- | --- |
| [Usage](https://github.com/myhloli/DocVortex/blob/main/docs/USAGE.md) | Stage APIs, PDF pages, classification, images and Bundles |
| [Examples](https://github.com/myhloli/DocVortex/blob/main/demo/README.md) | Local PDF and Office samples with a runnable demo |
| [Metadata](https://github.com/myhloli/DocVortex/blob/main/docs/METADATA.md) | Source properties and per-format coverage |
| [JSON protocol](https://github.com/myhloli/DocVortex/blob/main/docs/JSON_PROTOCOL.md) | Document schemas, extensions and protocol migration |
| [HTML protocol](https://github.com/myhloli/DocVortex/blob/main/docs/HTML_PROTOCOL.md) | Semantic markers and round trips |
| [Public SDK & migration](https://github.com/myhloli/DocVortex/blob/main/docs/sdk-0.4.md) | Supported integration boundaries and the 0.4 upgrade |
| [Rendering ownership](https://github.com/myhloli/DocVortex/blob/main/docs/RENDER_OWNERSHIP.md) | DocVortex exports and MinerU-specific renderers |

## Development

From a local checkout:

```bash
uv venv
uv pip install -e ".[test,dev]"
uv run --no-project python -m pytest -q
uv run --no-project ruff check src
uv run --no-project ruff format --check src
uv build
```

Bug reports and contributions are welcome. When reporting a parsing issue,
include a reproducible command and a sample document you can share in
[GitHub Issues](https://github.com/myhloli/DocVortex/issues).

## License

DocVortex project code is released under the
[MIT License](https://github.com/myhloli/DocVortex/blob/main/LICENSE.md).
