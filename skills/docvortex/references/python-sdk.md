# Python SDK recipes

Use the selected Python environment with DocVortex installed. Examples are
independent: replace `report.pdf` with a local native-text PDF and use a fresh
output directory for each run. Export calls create required parent directories.

## Contents

- [Parse once, export several formats](#parse-once-export-several-formats)
- [Select PDF pages and classify explicitly](#select-pdf-pages-and-classify-explicitly)
- [Read source metadata](#read-source-metadata)
- [Save and restore a portable Bundle](#save-and-restore-a-portable-bundle)
- [Compose the stages](#compose-the-stages)
- [Process several files](#process-several-files)

## Parse once, export several formats

```python
import docvortex

result = docvortex.parse("report.pdf")
for output_format, extension in (
    ("markdown", "md"),
    ("docx", "docx"),
    ("structured_content", "json"),
):
    exported = result.export(
        f"output/report.{extension}", output_format=output_format
    )
    print(exported.path, exported.asset_paths)

for diagnostic in result.diagnostics:
    print(diagnostic.code, diagnostic.message, diagnostic.page_index)
```

`parse()` performs analysis and postprocessing once. Each export reuses the result,
which owns the data needed after the source closes. For a single SDK conversion,
`docvortex.convert(source, output_path, output_format="markdown")` combines parsing,
rendering and writing, returning an `ExportResult`.

Path inputs, bytes and caller-owned `PDFDocument` objects are accepted. When byte
input needs an explicit format, supply `file_suffix`, for example:

```python
import docvortex

result = docvortex.parse(b"name,value\nalpha,1\nbeta,2\n", file_suffix="csv")
exported = result.export("output/table.md", output_format="markdown")
print(exported.path)
```

`result.to_dict()` returns the `middle_json` representation as a dictionary;
`structured_content` is a separate rendered export. Neither a standalone dictionary
nor document JSON alone replaces a Bundle containing required image assets.

## Select PDF pages and classify explicitly

```python
import docvortex
from docvortex.document.pdf import PDFDocument

with PDFDocument("report.pdf") as document:
    mode = document.classify()
    print("PDF mode:", mode)
    if mode == "txt":
        result = docvortex.parse(document, page_range="r1")
    else:
        result = None
        print("This PDF needs an external OCR or inference workflow.")

if result is not None:
    exported = result.export("output/last-page.md", output_format="markdown")
    print(exported.path)
    for diagnostic in result.diagnostics:
        print(diagnostic.code, diagnostic.message, diagnostic.page_index)
```

`page_range` uses one-based selections such as `1-5`, `1,3,5` and `r1`. Low-level
PDF page indices use zero-based numbering. Do not pass page selection for non-PDF
inputs. Classification is a caller decision; neither `parse()` nor `analyze()`
performs it automatically.

Parsing does not close a caller-owned document. The `with` block closes it here;
the parsed result remains usable afterward. Documents opened internally from a
path or bytes are closed by DocVortex.

## Read source metadata

```python
import docvortex

inspection = docvortex.extract_metadata("report.pdf")
properties = inspection.metadata.document
print(properties.title)
print(properties.authors)
print(properties.page_count, properties.page_count_kind)
for diagnostic in inspection.diagnostics:
    print(diagnostic.code, diagnostic.message)
```

This lightweight API reads source properties without body parsing, classification
or OCR. Missing properties stay missing; do not infer authors or dates from these
fields. A declared page count may differ from measured layout. PDF metadata refers
to the full source document even when body parsing selects only some pages.

## Save and restore a portable Bundle

```python
import docvortex

result = docvortex.parse("report.pdf", keep_model_json=True)
saved = result.save_bundle("output/report.bundle")
print("Bundle directory:", saved.path.parent)
print("Manifest:", saved.path)

restored = docvortex.load_bundle("output/report.bundle")
exported = restored.export("output/restored.html", output_format="html")
print(exported.path, exported.asset_paths)
for diagnostic in restored.diagnostics:
    print(diagnostic.code, diagnostic.message, diagnostic.page_index)
```

The directory contains `manifest.json`, `middle.json`, image assets and optional
`model.json`. Request `keep_model_json=True` only when analysis data is needed;
ordinary re-export does not require it. Pass the directory to `load_bundle()`, not
the manifest path. Loading verifies asset hashes, and restoring needs no original
source file. Supply missing external assets before saving; do not present an
incomplete Bundle as portable.

## Compose the stages

```python
from docvortex.api import analyze, postprocess, render

analysis = analyze("report.pdf", page_range="1-5")
result = postprocess(analysis)
artifact = render(result.middle_json, "docx", assets=result.assets)
exported = artifact.write("output/staged.docx")
print(exported.path, exported.asset_paths)
for diagnostic in result.diagnostics:
    print(diagnostic.code, diagnostic.message, diagnostic.page_index)
```

The stages are `analyze → AnalysisResult → postprocess → DocumentResult → render
→ RenderArtifact`. `render()` encodes in memory; `artifact.write()` writes files.
Retain `assets=result.assets` when rendering the intermediate representation.

These functions live in `docvortex.api`. Root aliases are `docvortex.analyze`,
`docvortex.postprocess_document` and `docvortex.render_artifact`. The lower-level
`docvortex.render` package has a different return-value contract; use the stage
API above when a `RenderArtifact` is required.

## Process several files

Use a sequential loop for a simple batch. This example selects DOCX inputs; adapt
the explicit input selection to the user's task. Separate output directories
prevent assets or identical stems from different input formats colliding.

```python
from pathlib import Path

import docvortex

sources = sorted(Path("input").glob("*.docx"))
if not sources:
    raise FileNotFoundError("No DOCX files found in input/")

failures = []
for source in sources:
    destination = Path("output/batch") / source.name / "document.md"
    try:
        result = docvortex.parse(source)
        exported = result.export(destination, output_format="markdown")
        print("Converted:", source, exported.path, exported.asset_paths)
        for diagnostic in result.diagnostics:
            print(source, diagnostic.code, diagnostic.message, diagnostic.page_index)
    except (ValueError, OSError) as error:
        failures.append((str(source), str(error)))
        print("Failed:", source, error)

if failures:
    raise RuntimeError(f"{len(failures)} conversion(s) failed: {failures}")
```

Report partial successes and failures separately. A PDF batch can classify each
input first when OCR routing is needed; never apply PDF page ranges to other formats.

All export examples preserve existing different content by default. Use
`overwrite=True` on `export()`, `convert()`, `save_bundle()` or `artifact.write()`
only when replacement is intended. Text and JSON exports may need sidecar image
files; DOCX, EPUB and PDF package assets into their main output. Check the actual
exported files and material diagnostics before reporting success.
