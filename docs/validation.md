# Extraction validation

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
