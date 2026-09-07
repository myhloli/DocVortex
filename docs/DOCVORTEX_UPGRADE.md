# DocVortex 0.1.0 upgrade

DocVortex is the new name of the standalone engine previously developed as
DocGale. The GitHub repository is `myhloli/DocVortex`; the Python distribution,
import and command are all `docvortex`. The existing repository history and release
draft are retained. Version 0.1.0 remains a draft, not a PyPI publication.

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

MinerU requires `docvortex>=0.2.1,<0.3.0`. Both projects share the new envelope,
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
