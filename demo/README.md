# Native document examples

Install DocGale, then run from the repository root:

```bash
uv pip install .
python demo/demo.py --page-range 1 --output-dir output/demo
python demo/demo.py demo/office_docs/docx_01.docx --output-dir output/office
```

The example parses once, exports Markdown and saves a portable result bundle.
PDF input is expected to contain native text; DocGale does not perform OCR.
`small_ocr.pdf` is retained for classification and low-level PDF regression tests.
Use `PDFDocument.classify()` explicitly when a caller needs to route scanned PDFs.

These files were moved unchanged from MinerU. The complete PDF/Office corpus,
native regression manifests and benchmark now belong to this repository. MinerU
retains `demo1.pdf` and `demo2.pdf` for its own inference integration tests.

Run the native performance baseline with `python tests/benchmarks/flash_pdf.py --help`.
The benchmark uses POSIX process resource accounting. Examples and regression
fixtures are included in the source distribution, not the runtime wheel.
