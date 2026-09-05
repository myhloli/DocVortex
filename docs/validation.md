# Extraction validation

Reference MinerU commit: `23d6e51185b1aa8868965fb45d644ccc0071ce68`.
Reference PDF stack: pdftext 0.7.1 and pypdfium2 5.10.1.

## Independent package

- The native input/output matrix covers 15 input formats and all nine renderers.
- The package regression suite includes PDF classification, text/geometry,
  lifecycle, source-independent bundles, CSS, schema and import boundaries.
- A fresh wheel environment with no MinerU or pdftext passed the tests with
  pypdfium2 5.13.0, keeping other dependency versions aligned with the reference.
- GitHub Actions exercises Python 3.10, 3.11, 3.12 and 3.13 on Linux, Windows and
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
