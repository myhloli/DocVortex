# Architecture and ownership

The engine has one pipeline: document input → ModelJson and assets → deterministic
postprocessing → MiddleJson and assets → rendering/export. Public document values
are owned by DocGale. They do not open files, render documents, or import MinerU.

- `schema` and `foundation` define values and leaf operations.
- `document` owns input preparation, metadata and low-level document lifetimes.
- `document.pdf` owns PDFium access, explicit classification and materialized text
  evidence. Text units retain original character indices and raw Unicode evidence;
  surrogate decoding does not renumber the PDFium source. Loose/tight/origin
  evidence remains separate from the geometry used by layout algorithms.
- `analyzers.native` owns format-specific analysis. Shared native PDF operations
  are exposed through `analyzers.native.pdf.shared` and the text/style interfaces.
- `content` owns shared span, table and tree operations. `postprocess` uses these
  operations to create a valid semantic document without LLM services.
- `codecs` owns versioned JSON and canonical HTML wire representations.
- `assets`, `render` and `export` separate resource ownership, content generation
  and filesystem transactions. Rendering plans operate on copies.
- `api`, `result` and `cli` compose these stages without creating a second engine.

MinerU owns tier selection, OCR/VLM/Hybrid inference, LLM clients and enhancements,
service configuration, jobs, Doclib and UI. Its compatibility facades pass explicit
options and translate neutral metadata to the existing schema 2.0 envelope.

There is one PDFium lock and one rendering pool per process. Native handles are
created, used and closed under their owning runtime. Cross-process work transfers
PDF bytes and materialized data, not handles. Future Rust work must preserve this
runtime ownership and use coarse stage or page boundaries.

The initial extraction deliberately preserves current content, page-index and
coordinate semantics. Replacing pdftext and upgrading PDFium are tested separately.
The source baseline is recorded in `migration.md`.
