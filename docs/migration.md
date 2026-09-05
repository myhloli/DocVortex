# DocGale extraction baseline

Source: MinerU `23d6e51185b1aa8868965fb45d644ccc0071ce68`.

The approved migration preserves explicit `txt` and `ocr` routing. Only `auto`
requests classification. DocGale owns native analysis and the shared document
pipeline; OCR, VLM, Hybrid inference and LLM enhancement remain in MinerU.

The source environment uses pdftext 0.7.1 and pypdfium2 5.10.1. Capture reference
artifacts using `tools/capture_baseline.py` before changing the source checkout.
Large reference artifacts are kept outside the published package.

The migration stages are public schema and deterministic processing, independent
native parsing and PDF text extraction, MinerU integration, and release validation.
