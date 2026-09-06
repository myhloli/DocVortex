# Extraction validation

## Independent branding, MIT license and dependency bounds (2026-09-06)

Project code now uses the MIT License, retaining existing copyright ownership.
The distribution declares `MIT AND Apache-2.0` for the project code and bundled
Apache-2.0 portions. The root-level third-party summary has been removed; the
PDF text source retains its attribution and modification notices, and the Apache
license and original Droid font NOTICE remain in both wheel and sdist.

DocGale requires `pypdfium2>=5.10.1,<6`; MinerU requires
`docgale>=0.1.0,<1.0.0`. Requirement checks accept DocGale 0.1.0, 0.2.0 and 0.99.99
and reject 1.0.0; they accept PDFium 5.10.1 and 5.13.0 and reject 5.10.0 and 6.0.0.
Other dependency declarations, Python support ranges and package versions are
unchanged. The existing host and isolated environments replaced only DocGale
during installation, without upgrading other packages.

Current package verification:

- 47 DocGale checks passed against the rebuilt wheel: neutral and host protocol
  round trips, nine renderers, complete pipeline/bundles, architecture and the
  15-input-format matrix (Python 3.13.5, PDFium 5.10.1).
- 247 MinerU route, ParseResult, legacy adapter and HTML reader/writer checks
  passed against the rebuilt wheel. The pre-existing
  `test_doclib_compaction_rejects_unknown_legacy_schema` exclusion remains; this
  change does not claim to address it.
- An isolated Python 3.14.4 environment with PDFium 5.10.1, no MinerU/pdftext and
  no source-path import passed 29 protocol and complete-pipeline checks, including
  the nine renderers and portable bundles. Font runtime initialization succeeded.
- All 24 modified source files have identical executable ASTs after removing
  docstrings. Adapter imports, serialized markers, renderer defaults and parsing
  goldens remain unchanged. Ruff checks and formatting checks passed.
- Wheel/sdist metadata, license contents, local documentation links and dependency
  boundaries passed inspection; Twine accepted both archives. The wheel contains
  no demo/test corpus. Both archives retain the unchanged Droid font with SHA256
  `8a4dea0899424438af25a6f1f6eb61e5d111d85367eece0a5d30170861ae6b2e`.

This change does not rerun or replace the historical cross-platform geometry
baseline below. Current host integration and sample provenance are documented in
the [compatibility guide](COMPATIBILITY.md). Version 0.1.0 remains a release draft;
preparing archives is not publication to PyPI.

## Historical extraction reference

Reference MinerU commit: `23d6e51185b1aa8868965fb45d644ccc0071ce68`.
Reference PDF stack: pdftext 0.7.1 and pypdfium2 5.10.1.

## Independent package

- The native input/output matrix covers 15 input formats and all nine renderers.
- The package regression suite includes PDF classification, text/geometry,
  lifecycle, source-independent bundles, CSS, schema and import boundaries.
- A fresh wheel environment with no MinerU or pdftext passed the tests with
  pypdfium2 5.13.0, keeping other dependency versions aligned with the reference.
- GitHub Actions exercises Python 3.10, 3.11, 3.12, 3.13 and 3.14 on Linux, Windows and
  macOS, plus a separate PDFium 5.13.0 job and lint/format checks.

## Differential and application checks

- Eleven tracked PDF/Office documents have exact reference ModelJson and
  MiddleJson compatibility payloads, plus exact Markdown, HTML, LaTeX,
  Structured Content and both Content List outputs.
- Four real PDFs have exact native analysis results after replacing pdftext.
- Two rendered PDF comparisons cover 32 pages with identical Poppler pixels
  before and after the extraction. Existing source-text and reflow behavior is
  preserved; this is not a claim of lossless source-layout reproduction.
- Real Gradio desktop PDF upload/conversion, all three result tabs, and ZIP,
  HTML, DOCX, LaTeX bundle, EPUB and PDF downloads were exercised. Downloaded
  archives were inspected and the application JSON retained schema 2.0.
- Mobile viewport testing included uppercase CSV upload, Flash locking,
  conversion and the rendered table.

## Existing MinerU baseline failures

The following tests also fail on the unchanged reference checkout and are kept
separate from the extraction regression results:

- `test_basic_extra_includes_preflight_runtime_dependencies`
- `test_standard_is_the_highest_model_runtime_extra`
- `test_doclib_compaction_rejects_unknown_legacy_schema`
- `test_pp_formulanet_fix_latex_uses_shared_mathring_repair`
- `test_demo_sparse_table_confidence_manifest`

The sparse-table bbox difference is the same in both implementations; its gold
manifest was not rewritten. Obsolete test-only call signatures and patch targets
were updated to the actual APIs without restoring removed runtime parameters.

## Demo and test ownership

All 24 original PDF/Office documents match the source MinerU SHA256 hashes.
The complete corpus, native sample tests, four gold manifests and the PDF
benchmark live in DocGale. MinerU retains only demo1.pdf and demo2.pdf; its
remaining Hybrid cases use 33 reproducible character/geometry snapshots from
seven documents, and Office routing cases generate their own minimal files.
Neither repository's ordinary tests need a sibling checkout or network fixtures.

Before changing dependency versions, the independent engine environment passed
303 tests with PDFium 5.10.1. The existing sparse-table gold discrepancy is
explicitly deselected in the acceptance matrix and runs separately in the
non-blocking `baseline-diagnostic` CI job; it is not counted as a passing test.
The affected MinerU Hybrid/table/routing group passed 265 tests. All 33 captured
inputs were re-extracted and compared exactly. The standalone demo exported
Markdown and a portable result bundle, and the native benchmark entrypoint ran.

## Python and dependency validation

DocGale declares Python `>=3.10,<3.15`. Both projects now declare
`pydantic>=2.12.5,<3`; all shared runtime dependency constraints match MinerU,
including `numpy>=1.21.6`. No Pydantic compatibility implementation was added.

Local wheel-install verification:

| Environment | Result |
| --- | --- |
| Python 3.13, Pydantic 2.12.5, PDFium 5.10.1 | 308 passed; 1 known baseline deselected |
| Python 3.14, Pydantic 2.12.5, PDFium 5.10.1 | 308 passed; 1 known baseline deselected |
| Python 3.13, Pydantic 2.13.5, PDFium 5.10.1 | 308 passed; 1 known baseline deselected |
| MinerU Python 3.13, Pydantic 2.12.5 | 3972 passed, 4 skipped, 4 existing baseline failures deselected |

The isolated Python 3.14 environment contains neither MinerU nor pdftext. Its
Office demo successfully exports Markdown and a result bundle. Upgrading the
existing isolated Python 3.13 environment changed only Pydantic 2.11.10 → 2.12.5
and pydantic-core 2.33.2 → 2.41.5; NumPy and other dependencies were unchanged.

The sdist includes all 24 original documents, their provenance and demo.py;
the runtime wheel includes neither demo files nor tests. Python 3.14 dependency
resolution succeeds for Linux x86_64, Windows x86_64 and macOS Apple Silicon 14+.
The macOS floor comes from the available ONNX Runtime wheels. CI runs the full
five-version, three-platform matrix and additional Pydantic 2.12.5 jobs on
Python 3.10 and 3.14, while the normal matrix resolves newer allowed versions.

An additional POSIX benchmark smoke test runs a real one-page sample and its
profiler in an independent Python 3.14 environment. It verifies that metadata
uses DocGale instead of pdftext and that profile paths are package-relative.
The CI suite now includes this test (skipped on Windows, where POSIX resource
accounting is unavailable).


### Historical system-font-sensitive PDF fixtures (before Droid)

The following observations describe commit `c2de3fa`, before the bundled font
policy. The platform relaxations described here have now been removed.

`中文论文3.pdf` and `中文论文4.pdf` contain non-embedded CJK fonts (verified with
pdffonts). PDFium substitutes system fonts, so their exact text block counts and
grouping differ on Linux, Windows and macOS. The original documents and gold
manifests remain unchanged. macOS retains all exact gold checks; Linux/Windows
still run these documents and verify page counts, image inventory, presence of
recovered tables, nonempty pages and stable title/DOI/site text anchors. Font
geometry also affects sparse-table grouping and heading boundaries, so exact
table counts and heading text concatenation remain part of the macOS gold.
Both documents additionally run the complete API, all nine renderers and an
offline bundle roundtrip on every platform.
Other sample inventories retain exact cross-platform comparisons. This does not
claim identical text geometry for PDFs with missing fonts on different systems.

The migrated benchmark and geometry manifest now explicitly read UTF-8 JSON;
Windows locale defaults must not alter Chinese filenames in the fixture index.


Observed system-font differences (PDFium 5.10.1, unchanged PDF bytes):

| Document / raw blocks | macOS | Linux | Windows |
| --- | ---: | ---: | ---: |
| 中文论文3.pdf text | 56 | 67 | 56 |
| 中文论文3.pdf header | 12 | 12 | 11 |
| 中文论文4.pdf text | 45 | 120 | 55 |
| 中文论文4.pdf table | 5 | 3 | 5 |

These are observations, not replacement gold values. ASCII-only PDF fixtures
are explicitly marked binary in .gitattributes so Git cannot alter their bytes
through Windows newline conversion. JSON/source text uses LF and explicit UTF-8.

The synthetic `native_cjk_layout_synthetic.pdf` also references non-embedded
STSong-Light. Linux reports 13 text + 1 paragraph title instead of 12 + 2; its
combined natural-text count and every other block count remain exact, with the
existing detailed CJK text/script checks retained. No fixture bytes are changed.


## Bundled Droid CJK runtime

The font/provider, PDF access integration, and tests were committed separately.
The approved follow-up repairs mixed-font source rows and fragmented inline prose
formula components, preserving the existing caption, paragraph and formula
assertions. Horizontal source adjacency is checked against raw boxes, source
indices, font size and baselines; large overlapping fraction boxes do not qualify
as ordinary text rows.

The reviewed Droid baseline keeps all existing text/group assertions. The only
inventory update is paper 4's header count, 19 → 18: the first-page volume/issue
and date now form one header, whose complete text has an additional exact
assertion. The original PDFs, existing sparse-table diagnostic and historical
geometry manifest remain untouched. All prior OS-specific CJK gold relaxations
were removed, including the synthetic STSong fixture.

Validation source: `89a3c2308a2c64435f75086e8942e42323b3e112`.
[GitHub Actions run](https://github.com/myhloli/docgale/actions/runs/34028564417)
covers Python 3.10–3.14 on Linux/Windows/macOS, Pydantic 2.12.5 floor jobs and
PDFium 5.13.0 separately. The main suite has 340 passing tests on Linux/macOS;
Windows skips only the POSIX benchmark smoke. The known sparse-table test remains
one explicit deselection and a separate failing, non-blocking diagnostic.

The platform artifact job records both the original system-provider and Droid
raw geometry, actual font byte hashes, PDFium identity, source SHA256, ModelJson,
MiddleJson, page images, layout annotations and HTML. Its downstream comparison
requires all new-policy character indices and Unicode to match, and applies a
0.001 PDF point tolerance to the substituted font's loose/tight/origin geometry.
The two papers and synthetic sample contain 8,702 substituted-font character
records; the measured maximum CJK geometry difference across the three platforms
is **0.0 points**. Block types, ordered text and table HTML are exactly equal.
Unchanged default Latin-font geometry differences are recorded separately.

A font-less Linux system baseline can omit CJK character records as well as
synthesize different spaces. Those incomplete old CJK results are not treated as
correct Unicode gold. The boundary tests protect embedded/non-CJK font bytes,
Unicode and geometry exactly, verify the bundled bytes for each substituted font,
and check that existing CJK character content is retained. The new-policy
cross-platform check remains strict for every raw index and Unicode value.

Both real papers pass all nine render formats, repeated rendering without tree
mutation, and source-independent bundle restore. Standalone wheel environments
contain neither MinerU nor pdftext. The font is present and SHA256-verified in both
wheel and sdist, together with its Apache-2.0 NOTICE. Runtime dependencies and
Python declarations are unchanged. The font is 5,074,864 bytes; its wheel ZIP
entry is 2,382,564 bytes (2.27 MiB compressed).

The matching Python 3.14.4 / PDFium 5.10.1 local before/after benchmark contains
31 documents: 28 full ModelJson/MiddleJson outputs are exactly unchanged, and
only the three font-affected fixtures differ. Per-document timing, peak RSS,
source/output hashes and reviewed Droid page fingerprints are preserved in
[the machine-readable baseline](validation/droid-cjk-v1.json).
The median warm time ratio is 0.935 and median peak-RSS ratio 1.038; each document
has one measured warm run, so these observations are not a speedup claim.
Five fresh-process startup samples put the font initializer median at 2.95 ms.

MinerU's source suite passed 3,972 tests, with four pre-existing failures excluded
and four skips. The worker initializer test verifies that parent-exit monitoring
still precedes font initialization. In an isolated Doclib home, a real Flash CLI
parse completed, its repeat hit the cache, and `--force` created parse ID 2 with
`cache_hit=false`; all three Markdown files had identical hashes. The isolated
server was stopped after the check. Schema 2.0 and routing remain unchanged.

The downloadable Windows/Linux viewer was checked in Chromium: all 18 layout
images and four HTML documents loaded without failed resources or browser errors.
The original system-font viewer is archived separately. Linux CI reported an
empty `fc-list :lang=zh` inventory, confirming installation without system CJK fonts.


## PDF fullwidth alphanumeric normalization

Validation source: `0afb1607452ebd79a1cae70c7d06fe5bede83cc7`.
[CI run](https://github.com/myhloli/docgale/actions/runs/34034656162) passed the
Python 3.10–3.14 three-platform matrix, Pydantic floor checks and PDFium 5.13.0.
The suite passed 382 tests on Linux/macOS and 381 on Windows, where the POSIX
benchmark smoke is skipped. One known sparse-table test remains deselected and
runs separately as a non-blocking diagnostic. The previous font validation
remains applicable.

`docgale.content.normalize_pdf_model_text()` now runs at the public PDF model
output boundary and at MinerU's final PDF content boundary. Only fullwidth Latin
letters and digits in natural language and table-cell text are converted;
formulas, code, link targets, punctuation, source characters and geometry are
preserved. All fourteen other input formats assert that this PDF-only cleanup is
never called. ModelJson construction/loading, explicit postprocessing, rendering
and saved-bundle loading preserve historical text.

The shared cleaning tests moved out of MinerU. DocGale additionally tests formula
boundaries across spans/HTML nodes, entities, table structure and attributes,
styles, nested tables, idempotence, raw geometry, public exports and old bundles.
MinerU retains delegation timing, phonetic/Span conversion, and Flash/Hybrid/VLM
routing tests. Its full suite passed 3,954 tests, with the same four skips and four
pre-existing failures excluded; the count changed because the shared tests moved.

For `中文论文4.pdf`, 1,992 fullwidth alphanumeric characters in natural-language
content and 398 in table content are converted. The post-conversion ModelJson,
HTML and Markdown contain none of these target characters. The two papers and
synthetic PDF retain exactly the same raw geometry, non-content model fields and
local source/layout PNG bytes. Per-document counts and output hashes are recorded
in [the text baseline](validation/pdf-text-normalization.json). No layout, table
structure or character-geometry gold was weakened. The native benchmark now uses
`PdfModel.predict()` so its output baseline includes the public cleanup stage.

See [PDF text normalization](PDF_TEXT_NORMALIZATION.md) for the public interface
and boundaries. Existing Doclib caches are refreshed only with an explicit
`--force` request; source PDF images retain their original glyphs.

The refreshed 31-document public-model baseline has one changed full output
(`中文论文4.pdf`) and 30 byte-equivalent ModelJson/MiddleJson outputs. Every
document retains its non-content model fields. See the
[public output fingerprints](validation/pdf-text-output-baseline.json); this
run records functional outputs rather than a new warm performance measurement.

The refreshed browser viewer passed 18 Windows/Linux page-image checks and four
HTML document checks with no failed resources or browser errors. Both rendered
HTML documents were checked for remaining fullwidth Latin letters/digits.
MiddleJson hierarchy and non-content fields are also unchanged in the three
reviewed PDF fixtures.
