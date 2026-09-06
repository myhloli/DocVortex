# Application compatibility and migration

DocGale owns native analysis, PDF access and classification, shared document
types, deterministic postprocessing, assets, rendering and export. It does not
import or require MinerU. Applications retain their inference services, routing,
configuration and caches.

## MinerU integration

`docgale.compat.mineru` reads and writes MinerU schema 2.0 envelopes around
DocGale's neutral ModelJson/MiddleJson schema 1.0. `MinerUMetadata` validates
`effort`, `parse_mode` and `mineru_version`, stored in `extensions["mineru"]`.
The existing `docgale.compat.legacy_schema_adapter` retains its supported legacy
result-reading boundary. These adapters do not import the host application.

MinerU's unified analysis entrypoint preserves the following routing:

| PDF route | Classification | Analysis |
| --- | --- | --- |
| Flash + txt | None | DocGale native PDF model |
| Flash + auto | Shared `PDFDocument.classify()` | txt uses DocGale; ocr uses MinerU Flash OCR |
| Flash + ocr | None | MinerU Flash OCR |
| Other tiers | Existing auto-mode rules | Existing Hybrid/VLM inference |

Other tiers reuse the shared PDF foundations, schema, postprocessing and
rendering. DocGale native analysis trusts the caller's selection and does not
add classification or an OCR fallback. Optional LLM enhancement runs in MinerU
after DocGale's deterministic postprocessing.

MinerU calls `normalize_pdf_model_text()` after PDF content assembly, Span
construction and host metadata cleanup. Flash, Hybrid and OCR/VLM therefore use
the same PDF text cleanup; repeated Flash cleanup is idempotent. Host tests cover
delegation and routing, while DocGale maintains the native text and geometry tests.

Saved results and Doclib caches are not rewritten automatically. After upgrading,
restart running services and use the existing `mineru parse ... --force` option
to regenerate results with the current font and text policies.

MinerU declares `docgale>=0.1.0,<1.0.0`. DocGale declares
`pypdfium2>=5.10.1,<6`; this constraint also applies when MinerU installs its own
direct PDFium dependency. Other shared dependency floors remain aligned, including
`pydantic>=2.12.5,<3` and `numpy>=1.21.6`.

## Existing serialized formats

The HTML wire protocol retains its existing `mineru-*` classes,
`data-mineru-html-version`, formula carriers and reader/writer behavior. Related
output style identifiers and renderer defaults remain unchanged. These names
preserve existing integrations and do not imply a dependency on MinerU.

Independent branding and licensing do not rename public APIs, schema identities,
adapter imports or serialized fields. Existing JSON/HTML round trips continue
to use the same contracts.

## Source and sample history

Native analyzers, semantic types, deterministic postprocessing and renderers
were extracted from MinerU commit
`23d6e51185b1aa8868965fb45d644ccc0071ce68`. Project code is now distributed under
the [MIT License](../LICENSE.md); existing copyright ownership is preserved.
Apache-2.0 portions remain identified in the PDF text source, and the Droid font
retains its original NOTICE and provenance manifest.

The full demo corpus was moved unchanged from MinerU, preserving filenames and
the recorded hashes and regression manifests. DocGale maintains native parsing,
geometry, semantic and export regressions; MinerU retains `demo1.pdf` and
`demo2.pdf` for inference integration tests. Other host-specific geometry inputs
are frozen as JSON fixtures in MinerU. Neither test suite requires an adjacent
checkout or runtime corpus download.
