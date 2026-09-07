# DocVortex 0.1.0 upgrade

DocVortex is the new name of the standalone engine previously developed as
DocGale. The GitHub repository is `myhloli/DocVortex`; the Python distribution,
import and command are all `docvortex`. The existing repository history and release
draft are retained. Version 0.1.0 remains a draft, not a PyPI publication.

## Shared PDF layout visualization in 0.2.2

`docvortex.visualization.render_layout_pdf(pdf_bytes, pages, *, page_indices=None)`
returns PDF bytes with colored layout outlines and `block_type: index` labels. Pass current `MiddleJson.pages`;
the renderer uses normalized `blocks/content` geometry without changing the input
document. It preserves page dimensions, CropBox offsets and right-angle rotations.
This overlays the source PDF; it does not reflow semantic content like `render_pdf`.

```python
from pathlib import Path
import docvortex
from docvortex.visualization import render_layout_pdf

source = Path("report.pdf")
result = docvortex.parse(source)
Path("layout.pdf").write_bytes(render_layout_pdf(source.read_bytes(), result.middle_json.pages))
```

For a cropped or reordered PDF, `page_indices[i]` must name the original zero-based
page index of its i-th page. Omit the mapping only for an original full PDF. A
mapping with the wrong length raises `ValueError`; missing result pages remain
unmarked rather than borrowing another page's boxes. For example, a PDF containing
only original pages 2 and 5 requires `page_indices=(1, 4)`.

Page footnotes use teal (`#008080`); headers, footers, page numbers and aside text
use gray (`#9E9E9E`). Other block colors retain the current palette. All layout
rectangles use 1 pt outlines without interior fill. Each outlined block has a
matching 7 pt Helvetica label on a small white background above its top-left corner.
Labels show the block's own zero-based `index`, including gaps and duplicate
parent/child values; a missing child index is shown as `-`. Visual parent containers
are not outlined or labeled separately. Code and algorithm parents still share
`type="code"`, distinguished by `sub_type`; algorithm children retain
`algorithm_body` and the same purple as `code_body`.

Labels stay upright after page rotation, shift left at the right edge and move
inside the box when there is no room above it. Overlapping labels move up by rows
first, then down from the box top; if no non-overlapping row remains on the page,
the renderer chooses the row with the least label overlap. Labels add extractable
text to the output PDF; the input PDF and Middle JSON are not modified.
Label avoidance checks other labels, not source text. Where adjacent blocks have
little vertical spacing, a label's white background can cover nearby source text
visually; that text remains in the PDF content stream.

MinerU's Gradio preview delegates to this renderer and retains its artifact names.
`PDFDocument.draw_layout_bbox(pages, output_path,
page_indices=None)` remains a file-writing wrapper (the mapping is keyword-only),
now using current `PageInfo` rather than removed legacy layout fields. Legacy
filled boxes and separately renumbered red reading-order markers are not retained. Private helpers from
`docvortex.document.pdf.diagnostics` have been removed.

## On-demand PDF crops in 0.2.1

`docvortex.document.pdf.visuals.attach_visual_block_images_from_pdf(document,
model_list, *, window_size=64, timeout=None, threads=None)` adds visual crops in
place. The model list must cover every physical page of the supplied PDF, including
pages without visual blocks. The function does not select pages, classify the PDF,
or close the caller's document.

Only pages containing image, chart, table, or equation blocks are rendered, after
image containers are collapsed. Consecutive pages are batched within the supplied
window boundary and a 32 MiB pixel budget, preserving the existing rendering
resolution and crop encoding. Returned page images are closed even when cropping
fails. Explicit timeout and worker settings override the engine defaults; `None`
uses the existing `DOCVORTEX_PDF_RENDER_*` configuration.

The native `analyze()` API uses this same implementation. MinerU passes its own
window, timeout, and worker settings when running Flash native text parsing.

## Package and protocol names

Stop old services/interpreters before switching packages. Remove the old package
from the environment, then install DocVortex; do not reuse an initialized old
PDFium runtime across the change.

```bash
pip uninstall docgale
pip install docvortex
docvortex convert report.pdf --format html --output output/report.html
```

```python
import docvortex

result = docvortex.parse("report.pdf", keep_model_json=True)
result.export("output/report.html", output_format="html")
result.save_bundle("output/report.bundle")
```

Native JSON uses `docvortex.model`, `docvortex.middle` and `docvortex.bundle`, all
at schema version `2.0`. Native parsing records `metadata.producer` as
`docvortex` version `0.2.0`; constructors require explicit metadata.
Only these native schema identities are accepted. Old DocGale JSON and bundles
are rejected explicitly, with no automatic migration or legacy import/CLI aliases.
Files are not rewritten on load. A producer explicitly supplied in an otherwise
valid new protocol remains unchanged, as does user text containing old names.

HTML uses `docvortex-*`, `data-docvortex-html-version="1"` and
`data-docvortex-latex`. The codec exports `DOCVORTEX_HTML_VERSION` and
`decode_docvortex_html_wire`. Old DocGale/MinerU HTML goes through ordinary webpage
parsing without a guarantee of exact semantic reconstruction. EPUB XHTML/CSS and
Markdown embedded HTML share the new namespace. PDF, DOCX and LaTeX generated
titles, metadata, styles and macros use DocVortex. Supported explicit user titles
and authors retain their existing precedence.

MinerU requires `docvortex>=0.2.2,<1.0.0`. Both projects share the new envelope,
with `metadata.file_suffix`, `metadata.producer`, and optional `extensions.mineru`
containing actual tier and resolved parse mode. Old JSON adapters and historical
page conversion are removed. Python top-level metadata attributes have no aliases.
See [JSON_PROTOCOL.md](JSON_PROTOCOL.md) for migration and cache behavior.
OCR/VLM/Hybrid routing and UI identity remain unchanged.

## PDF text and font policies

PDF model output additionally converts `：．／＼－＿％＋＝＠＃＆＊` to the corresponding
ASCII characters, alongside fullwidth Latin letters and digits. Chinese punctuation
such as `，、。；！？（）`, square brackets, quotes, tildes, spaces and unlisted Unicode
symbols remain unchanged. The operation is one-to-one and idempotent.

Only natural-language text and visible table-cell text are processed. Formula/code
payloads, URL targets, HTML attributes and raw character/geometry evidence remain
protected. Other input formats do not automatically receive PDF text normalization.
See [the PDF text policy](PDF_TEXT_NORMALIZATION.md) for the public shared entrypoint.

The Droid font bytes, `droid-cjk-v1` policy and PDFium runtime ownership are
unchanged. Fixed fallback covers recognized non-embedded CJK requests; non-CJK
fonts still use the default provider. It is not a promise of identical whole-page
pixels for every missing font or viewer. Semantic PDF export retains its existing
font strategy. Python 3.10–3.14 and `pypdfium2>=5.10.1,<6` remain supported.

## Publishing configuration

The release workflow remains `.github/workflows/publish.yml`, guarded by its CI
validation job. Configure a PyPI pending Trusted Publisher with these values
before publishing the first version:

| Setting | Value |
| --- | --- |
| PyPI project | `docvortex` |
| GitHub owner | `myhloli` |
| Repository | `DocVortex` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

Updating repository configuration does not itself configure the PyPI account or
publish a package. The draft contains the verified `docvortex-0.1.0` wheel/sdist.
MIT project licensing, existing copyright ownership, Apache-2.0 source attribution
and the original Droid font NOTICE remain in place.
