---
name: docvortex
description: Parse native documents and convert them with the DocVortex CLI or Python SDK. Use for text PDFs, Office, OpenDocument, EPUB, HTML, OFD and CSV conversion, source metadata extraction, or reusable document Bundles. Does not provide OCR or guidance for developing DocVortex itself.
---

# DocVortex

Use DocVortex to read native document content into a reusable semantic document
and export it to the user's requested formats. It runs locally without OCR or
VLM inference services.

## Choose the workflow

| Request | Entry point |
| --- | --- |
| Convert one local file to one format | `docvortex convert` |
| Decide whether a PDF needs OCR | Explicit `docvortex classify` |
| Export several formats or process several files | Python `parse()` and `result.export()` |
| Read title, authors or other source properties | Python `extract_metadata()` |
| Save a result for later exports without its source | Python `save_bundle()` and `load_bundle()` |
| Inspect intermediate results or render in memory | Stage APIs in `docvortex.api` |

For Python tasks, read [Python SDK recipes](references/python-sdk.md). It contains
independent examples for each workflow; load the sections relevant to the task.

## Environment and quick conversion

Use the task's Python environment. Check both its version and the imported package
before running examples, especially when a source checkout is also present:

```bash
python --version
python -c "import docvortex; print(docvortex.__version__); print(docvortex.__file__)"
```

DocVortex supports Python 3.10–3.14. If the package is missing, install it into the
chosen environment with `python -m pip install docvortex`. The examples use the
public SDK available in DocVortex 0.4.1. For a repository task, verify that imports
resolve to the intended checkout rather than an older installed copy.

Convert a local file to Markdown:

```bash
docvortex convert report.pdf --format markdown --output output/report.md
```

Replace the source and output paths with the user's paths. Set the format
explicitly; the default is Markdown, regardless of the output filename extension.
If the `docvortex` executable is missing or belongs to a different environment,
use `python -m docvortex.cli` with the same arguments.

### Supported formats

| Native input | Extensions |
| --- | --- |
| PDF with native text | `pdf` |
| Word and rich text | `doc`, `docx`, `rtf` |
| Presentations | `ppt`, `pptx` |
| Spreadsheets and delimited tables | `xls`, `xlsx`, `csv` |
| OpenDocument | `odt`, `ods`, `odp` |
| Books, HTML documents and fixed-layout documents | `epub`, `html`, `ofd` |

| Output format argument | Typical extension |
| --- | --- |
| `markdown` | `.md` |
| `html` | `.html` |
| `latex` | `.tex` |
| `docx` | `.docx` |
| `epub` | `.epub` |
| `pdf` | `.pdf` |
| `structured_content` | `.json` |

Input support does not imply output support: PPT/PPTX and XLS/XLSX are inputs
only. `structured_content` is a rendered JSON export; it is not the `middle_json`
intermediate representation. Use `result.to_dict()` to inspect that representation,
or a Bundle when it must travel with its assets.

## PDF decisions

DocVortex does not classify PDFs automatically or switch to OCR. When native text
availability is uncertain, classify explicitly:

```bash
docvortex classify report.pdf
```

The output is `txt` or `ocr`. An `ocr` result indicates an external OCR or inference
workflow is needed; report that limitation rather than treating empty native
extraction as complete. Classification itself never starts inference.

Select PDF pages with one-based page numbers:

```bash
docvortex convert report.pdf --pages 1-5 --format markdown --output output/pages.md
```

Use `1,3,5` for selected pages, `r1` for the last page, `r3-r1` for the last three,
and `all` or an omitted option for the whole PDF. Selections are sorted and
deduplicated; out-of-bounds pages are omitted. Reversed ranges and negative numbers
are invalid. Omit page selection for other formats: they are parsed whole, and a
nonempty PDF page selection is rejected for them.

PDF export reflows semantic content. It does not preserve the original page layout
and drawing instructions losslessly. If exact visual reproduction is essential,
explain this limit before choosing PDF export as the solution.

## Write and verify results

- Prefer `result.export()` for SDK exports; it supplies the result's assets.
  Direct stage rendering requires `assets=result.assets`, followed by
  `artifact.write()` to write the main file and any sidecar assets.
- Preserve default overwrite protection. Identical existing bytes may be reused;
  different content requires `--overwrite` or `overwrite=True`. Use replacement
  only when the user intends it; otherwise choose a distinct output path.
- A Bundle is a directory, even when named `report.bundle`. Save and load the
  directory, retaining its manifest, document JSON and image assets together.
- Inspect representative text, tables and images in the actual output. For visual
  deliverables, open or render the produced document using available tools; a
  nonempty file alone does not establish layout quality.
- Review SDK `result.diagnostics` or metadata inspection diagnostics. Distinguish
  optional metadata warnings from missing body content, and report material gaps.
- Return the actual main-file paths and necessary sidecar paths. SDK exports expose
  these as `ExportResult.path` and `asset_paths`. For Bundles, report the directory;
  `save_bundle()` returns the manifest path in `ExportResult.path`.

Use public imports shown in the recipes. No private modules, host application
installation or repository-specific paths are needed to use this skill.
