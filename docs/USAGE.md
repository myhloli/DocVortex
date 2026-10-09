# DocVortex usage guide

[English README](../README.md) · [中文 README](../README_zh-CN.md)

Use the complete pipeline for everyday conversion, or compose its stages when
you need access to intermediate results. All examples use the public Python SDK.
Install with `pip install docvortex` and replace `report.pdf` with a local text PDF.
Each example can be run independently in a fresh output directory.

## Global CLI log level

The root-level `--log-level` option controls loguru output for CLI commands.
It defaults to `info`, supports `trace`, `debug`, `info`, `warning`, `error` and
`critical`, and must be placed before the subcommand:

```bash
docvortex --log-level debug convert report.pdf --format markdown --output output/report.md
```

`DOCVORTEX_LOG_LEVEL` provides the same setting through an environment variable.
An explicit `--log-level` value takes precedence. SDK imports do not change the
host application's existing loguru sinks.

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
| `analyze(source)` | `AnalysisResult` | Read document content into `model_json`; automatically route PDF OCR |
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

## PDF output layout

PDF output defaults to `PdfLayout.AUTO`. Newly parsed PDF sources preserve their
page dimensions, page boundaries, headers, footers, and block positions. Text is
selectable and reflows inside each source block; original fonts and line breaks
are approximate. Tables prefer parsed HTML with selectable text and vector borders;
charts retain region images, and equations prefer vector rendering. Content absent
from the parsed result cannot be restored.

Original-layout tables use content-based column widths and compact cell padding.
They first fit their source box, then may use safe space in the same column while
preserving page margins and a 2 pt gap from other blocks. Existing spanning tables
retain their span. Rotation is restored after layout in upright coordinates;
tables do not move to another page. Captions and notes remain separate when their
own coordinates are available, or share the parent box when child coordinates
are missing. If 6 pt text still cannot fit, the table is scaled further and
`pdf_layout_small_text` is reported. Small text alone never triggers an image
fallback. Missing, invalid, or unrenderable HTML falls back to the region image,
then text or a placeholder, with `pdf_table_fallback` and `pdf_table_source`
diagnostics. `pdf_table_layout` records the source, rotation, effective font size,
and original/drawing bounds. Reflow table layout is unchanged.

```python
import docvortex
from docvortex.render import PdfLayout, PdfRenderOptions

result = docvortex.parse("report.pdf")
artifact = docvortex.render_artifact(
    result.middle_json, "pdf", assets=result.assets,
    options=PdfRenderOptions(layout=PdfLayout.ORIGINAL),
)
print(artifact.diagnostics)
artifact.write("output/report.pdf")

# Keep the existing A4 semantic reflow explicitly.
result.export("output/reflow.pdf", output_format="pdf",
              options=PdfRenderOptions(layout=PdfLayout.REFLOW))
```

```bash
docvortex convert report.pdf --format pdf --pdf-layout original --output output/report.pdf
```

`AUTO` falls back to reflow for older PDF results without complete page geometry,
with a `pdf_layout_reflow_fallback` diagnostic. `ORIGINAL` rejects incomplete
geometry and non-PDF sources. OFD and Office sources continue to reflow under
`AUTO`; original layout currently supports PDF sources only. The lower-level
`render_pdf()` accepts the same `layout` enum and returns bytes; its diagnostics
are logged. The CLI reports diagnostics through the same logs.

Result bundles retain the geometry extension and assets, so rendering does not
reopen the source document. Body text and visual blocks fit independently inside
their source boxes; `continues_prev` does not move text between original boxes.
Pages and blank pages remain. Missing child coordinates use approximate layout
inside the parent box. Reflow retains its existing paragraph merging and styles.

Both PDF layouts use CJK line breaking for paragraphs containing Chinese,
Japanese or Korean text, including mixed-language annotations and table cells.
This avoids moving an entire space-delimited Chinese phrase to the next line.
Pure Latin text and literal code/algorithm blocks retain their existing wrapping;
no layout-only characters or hyphens are added to the source text.

Original-layout titles use the actual fitted body font as a reference. A chapter
title first looks for following same-page, same-column body text, then the nearest
body in that column, then the exported body's median size (10.5 pt if none exists).
The common target for each title type/level is the median reference size plus 2 pt,
rounded up to 0.1 pt; larger local body text raises only that title's target.
Document titles target 2 pt above the largest chapter target, or body plus 4 pt
without chapters, with the same local hierarchy protection. Original style sizes
are not hard caps, and a coverage percentage no longer reduces a whole group.

A title that fits its original box stays there. Otherwise it can borrow empty
space above, below and to the right within its column, keeping its left edge.
Column width comes from the matched body; without reliable evidence the original
width is retained. Original spanning titles retain their span. Other original
boxes remain occupied, with a 2 pt clearance and page boundaries enforced;
adjacent titles divide their gap at its midpoint. The original top edge is
preferred, shifting upward when needed. Only a title that cannot fit its safe
area shrinks, in 0.1 pt steps before the existing below-6-pt fallback. Script and
inline-formula ink extents are included in title measurement. Index references
and approximate parent groups do not participate; body text, captions and code
retain their original independent fitting results. Structured tables are placed
after titles and equations, respecting their final drawing bounds.

`pdf_title_layout_expanded` identifies expanded title areas.
`pdf_layout_font_exception` records local-body increases or insufficient-space
reductions, including reference/target/final sizes and original/drawing bounds.
`pdf_title_geometry_conflict` reports existing source overlaps, where the title
stays within its original occupancy. `pdf_title_clearance_unavailable` reports
source spacing too tight to provide a safe expansion. Only exported pages
contribute; the input schema and assets are not modified.
Text is fitted down to 6 pt, then proportionally scaled further if necessary;
`pdf_layout_scaled` and `pdf_layout_small_text` diagnostics identify these cases.
Individual rotated text, vertical writing, and character-level reconstruction
are outside this first version.

PDF text wrapping uses the existing ReportLab behavior. Code blocks retain their
literal text. Since 0.4.2, native PDF display equations
have empty `content` and retain their region images (including detected numbers),
so all renderers use the existing image fallback. Inline equations are unchanged.
Old results and caches are not rewritten; reparse to obtain the new output.

## PDF classification and local OCR

`analyze()`, `parse()` and `convert()` accept `parse_mode="auto"` (default),
`"txt"` or `"ocr"`. Auto mode classifies the selected pages after `page_range`
has been applied, then uses one backend for the whole selected document. Forced
text mode skips classification and OCR entirely. Non-PDF input accepts only
the default auto mode.

If a mixed PDF is classified as `txt` but contains scanned pages, select `ocr`
explicitly to OCR all selected pages. Auto mode does not switch per page.

```bash
docvortex convert scan.pdf -o output/scan.md
docvortex convert scan.pdf -o output/scan.html --parse-mode ocr --pages 1-3
docvortex convert report.pdf -o output/native.md --parse-mode txt
```

```python
import docvortex

result = docvortex.parse("scan.pdf", parse_mode="auto", keep_model_json=True)
result.export("output/scan.md")
native = docvortex.parse("report.pdf", parse_mode="txt")
```

OCR uses CPU ONNX Runtime with PP-DocLayoutV2 and PP-OCRv6 Tiny Det / Small Rec.
The first OCR parse downloads six fixed model/configuration files (about 239 MB)
into `~/.docvortex/model/MinerU-4_models_onnx/`, preserving the repository paths.
Downloads try Hugging Face first and fall back to ModelScope, validate SHA-256,
and install atomically under a file lock. Complete caches work without networking.
Failed downloads raise `DocumentError` with code `ocr_model_download_failed` and
the failed file and sources. Invalid models and inference failures use
`ocr_model_invalid` and `ocr_inference_failed`, respectively.

Layout supplies reading order and regions. Text blocks use local OCR, tables
retain spatially projected text and a screenshot, and display formulas retain
an image with empty content. Inline formulas pass through ordinary text OCR.
Images, charts and seals retain screenshots. No table structure, formula,
orientation classification or unwarping model is downloaded. Recognition uses
the single model's embedded character dictionary without a language selector.

Formula numbers merge with adjacent display equations in layout reading order,
so the equation screenshot includes its number. Trailing numbers take precedence;
unpaired numbers remain text. `header_image` and `footer_image` regions use OCR
and become `header` and `footer` text blocks, including empty recognition results.
They follow the usual page auxiliary filtering in default Markdown/HTML output.

Classification remains separately available without inference or networking:

```bash
docvortex classify report.pdf
```

The command returns `txt` or `ocr`. A caller can also inspect the classification:

```python
import docvortex
from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    mode = document.classify()
    result = docvortex.parse(document, parse_mode=mode)
    result.export("output/result.md", output_format="markdown")
```

Classification and metadata extraction do not start inference. The low-level
`docvortex.analyzers.native.PdfModel.predict()` continues to perform only native
text analysis, preserving explicit callers such as MinerU. OCR screenshots are
materialized into the normal result assets and remain usable after closing or
deleting the source. See [model provenance](MODELS.md).

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

`PDFDocument.from_image()` can wrap an image as a PDF document. Pass the resulting
document to `parse(..., parse_mode="ocr")` for OCR; low-level document operations
themselves do not run inference.

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
