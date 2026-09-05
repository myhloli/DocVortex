# DocGale

A fast, multi-format document parsing and conversion engine.

DocGale owns the native document pipeline extracted from MinerU:

```text
Document -> ModelJson + Assets -> MiddleJson + Assets -> Render / Export
```

Native inputs include text PDFs, DOC/DOCX, PPT/PPTX, XLS/XLSX, RTF, ODT/ODS/ODP,
EPUB, HTML, OFD and CSV. Output formats include Markdown, HTML, LaTeX, DOCX,
EPUB, PDF, structured content and both content-list formats.

DocGale does not implement OCR or VLM inference and does not depend on MinerU.
PDF classification is an explicit document operation; native analysis does not
silently classify the document or select another inference backend.

The code and its third-party attributions retain their applicable licenses.
