<div align="center">

<img src="https://gcore.jsdelivr.net/gh/myhloli/DocVortex@main/docs/images/docvortex-logo.jpg" alt="DocVortex logo" width="200">

# DocVortex

**Native document parsing. One structure, many outputs.**

[![PyPI](https://img.shields.io/pypi/v/docvortex?color=008cff)](https://pypi.org/project/docvortex/)
[![Python](https://img.shields.io/pypi/pyversions/docvortex)](https://pypi.org/project/docvortex/)
[![Downloads](https://static.pepy.tech/badge/docvortex)](https://pepy.tech/project/docvortex)
[![Monthly Downloads](https://static.pepy.tech/badge/docvortex/month)](https://pepy.tech/project/docvortex)
[![CI](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml/badge.svg)](https://github.com/myhloli/DocVortex/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/myhloli/DocVortex/blob/main/LICENSE.md)

**English** · [简体中文](https://github.com/myhloli/DocVortex/blob/main/README_zh-CN.md)

[Performance](#pdf-performance) · [Quick start](#quick-start) · [Formats](#supported-formats) · [Documentation](#documentation)

</div>

## Documents in. Possibilities out.

DocVortex is a standalone Python engine for parsing and converting documents.
It reads native text and document structure into a unified representation,
then exports the result in the formats your workflow needs.

- **Multi-format input** — read text and scanned PDFs, Office files, OpenDocument files, EPUB, HTML, OFD, CSV and TSV.
- **Parse once, export many times** — reuse the same result for Markdown, HTML, LaTeX, DOCX, EPUB, PDF and structured JSON.
- **Portable results** — save document structure and image assets in a Bundle, then export again without the source file.
- **Composable APIs** — use the complete pipeline or integrate analysis, postprocessing and rendering separately.

Native parsing works without an OCR or VLM inference service. Use DocVortex directly through its CLI or Python SDK.

Scanned PDFs use local CPU OCR. The first OCR parse automatically downloads PP-DocLayoutV2 and PP-OCRv6 Tiny Det / Small Rec from Hugging Face, falling back to ModelScope, into `~/.docvortex/model/`. The six model and configuration files total about 239 MB; a complete cache works offline.

![DocVortex pipeline: native documents become a unified representation, then Markdown, HTML, LaTeX, DOCX, EPUB, PDF or structured JSON.](https://gcore.jsdelivr.net/gh/myhloli/DocVortex@main/docs/images/docvortex-overview.jpg)

## PDF performance

**Release 0.5.10 uses Rust acceleration: public parsing is 2.93× as fast as 0.4.25 on the tested corpus.**

| Pipeline | 0.4.25 python | 0.5.10 rust | Speedup | Time reduction | Peak RSS reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| DocVortex public `parse()` | 42.36 s | 14.48 s | **2.93×** | **65.82%** | **31.17%** |
| MinerU Flash | 44.50 s | 14.44 s | **3.08×** | **67.55%** | **29.84%** |
| MinerU shared PDF processing | 76.28 s | 12.85 s | **5.94×** | **83.16%** | **35.65%** |

Same-machine warm-run comparison on 32 PDFs / 299 pages (31 PDFs for Flash). The existing 0.4.25 measurements are reused. For 0.5.10, each document is warmed once and timed five times in each of three balanced rounds; times sum the medians of the three per-document medians. RSS reductions are medians of per-document process-tree peak RSS reductions. MinerU results include improvements in both projects. The shared pipeline excludes model computation; excluding the extreme dense-table sample, its speedup is **2.87×**.

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
The root-level `--log-level` option controls loguru output and defaults to `info`.
It must precede the command; `DOCVORTEX_LOG_LEVEL=warning` can also configure it.

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

### Native inputs · 20 formats

| Document family | File extensions |
| --- | --- |
| PDF with native text or scanned pages | `.pdf` |
| Word & rich text | `.doc`, `.docx`, `.rtf` |
| Presentations | `.ppt`, `.pptx` |
| Spreadsheets | `.xls`, `.xlsx`, `.csv`, `.tsv` |
| OpenDocument | `.odt`, `.ods`, `.odp` |
| E-books | `.epub` |
| Web documents | `.html`, `.htm`, `.shtml` |
| Web archives | `.mhtml`, `.mht` |
| Open Fixed-layout Document | `.ofd` |

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

- **PDF parsing:** `parse_mode="auto"` (default) classifies the selected PDF pages and chooses native text or local CPU OCR. Use `parse_mode="txt"` to force native parsing without classification or model downloads, or `parse_mode="ocr"` to force OCR. The CLI equivalent is `--parse-mode auto|txt|ocr`. Explicit modes apply only to PDF input.
- **PDF OCR:** layout supplies block regions and reading order; text blocks use Tiny Det / Small Rec. Display formulas, images, charts and seals retain screenshots. Inline formulas pass through ordinary text OCR. Tables contain spatially projected text and a screenshot, without structural cell recognition. No page orientation or unwarping model is used. A selected document uses one backend throughout.

If a mixed PDF is classified as `txt` but contains scanned pages, use `parse_mode="ocr"` (CLI: `--parse-mode ocr`) to OCR the entire selection.
- **PDF classification:** `docvortex classify report.pdf` returns `txt` or `ocr` without inference or networking. Metadata extraction and the low-level `PdfModel.predict()` native text API also remain offline. See [model provenance](licenses/models/README.md).
- **PDF export:** PDF sources with page geometry default to block layout restoration, with selectable text and HTML-based tables; charts retain region images. Other sources and older results use semantic reflow. Use `--pdf-layout original|reflow` to select explicitly; fonts, line breaks and drawing instructions are not reproduced losslessly. See [PDF output layout](docs/USAGE.md#pdf-output-layout).

## Documentation

| Guide | What you will find |
| --- | --- |
| [Usage](https://github.com/myhloli/DocVortex/blob/main/docs/USAGE.md) | Stage APIs, PDF pages, classification, images and Bundles |
| [Agent skill](skills/docvortex/SKILL.md) | CLI and Python SDK workflows for agents; copy the entire `skills/docvortex` folder to reuse |
| [Examples](https://github.com/myhloli/DocVortex/blob/main/demo/README.md) | Local samples for PDF, Office, EPUB, web documents and OFD with a runnable demo |
| [Metadata](https://github.com/myhloli/DocVortex/blob/main/docs/METADATA.md) | Source properties and per-format coverage |
| [JSON protocol](https://github.com/myhloli/DocVortex/blob/main/docs/JSON_PROTOCOL.md) | Document schemas, extensions and protocol migration |
| [HTML protocol](https://github.com/myhloli/DocVortex/blob/main/docs/HTML_PROTOCOL.md) | Semantic markers and round trips |
| [Public SDK & migration](https://github.com/myhloli/DocVortex/blob/main/docs/sdk-0.4.md) | Supported integration boundaries and the 0.4 upgrade |
| [Rendering ownership](https://github.com/myhloli/DocVortex/blob/main/docs/RENDER_OWNERSHIP.md) | DocVortex exports and MinerU-specific renderers |

## Development

See [Repository architecture](docs/ARCHITECTURE.md) for directory responsibilities,
the Python/Rust boundary, and pure Python or native development commands.

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
