# Application compatibility and migration

DocVortex owns native analysis, PDF access and classification, shared document
types, deterministic postprocessing, assets, rendering and export. It does not
import or require MinerU. Applications retain their inference services, routing,
configuration and caches.

## MinerU integration

DocVortex and MinerU use the same `docvortex.model` / `docvortex.middle` protocol
at version `2.0`, with `metadata.file_suffix` and `metadata.producer`.
`mineru.integrations.docvortex` only validates the optional product extension:
actual `tier` and resolved `parse_mode`. DocVortex preserves this JSON without
interpreting MinerU enums. There are no legacy envelope adapters or import aliases.
See [the shared JSON protocol](JSON_PROTOCOL.md) for the direct migration.

MinerU's unified analysis entrypoint preserves the following routing:

| PDF route | Classification | Analysis |
| --- | --- | --- |
| Flash + txt | None | DocVortex native PDF model |
| Flash + auto | Shared `PDFDocument.classify()` | txt uses DocVortex; ocr uses MinerU Flash OCR |
| Flash + ocr | None | MinerU Flash OCR |
| Other tiers | Existing auto-mode rules | Existing Hybrid/VLM inference |

Other tiers reuse the shared PDF foundations, schema, postprocessing and
rendering. DocVortex native analysis trusts the caller's selection and does not
add classification or an OCR fallback. Optional LLM enhancement runs in MinerU
after DocVortex's deterministic postprocessing.

MinerU calls `normalize_pdf_model_text()` after PDF content assembly, Span
construction and host metadata cleanup. Flash, Hybrid and OCR/VLM therefore use
the same PDF text cleanup; repeated Flash cleanup is idempotent. Host tests cover
delegation and routing, while DocVortex maintains the native text and geometry tests.

Saved results and Doclib caches are not rewritten automatically. After upgrading,
restart running services and use the existing `mineru parse ... --force` option
to regenerate results with the current font and text policies.

MinerU declares `docvortex>=0.1.0,<1.0.0`. DocVortex declares
`pypdfium2>=5.10.1,<6`; this constraint also applies when MinerU installs its own
direct PDFium dependency. Other shared dependency floors remain aligned, including
`pydantic>=2.12.5,<3` and `numpy>=1.21.6`.

## Existing serialized formats

HTML output now uses `docvortex-*`, `data-docvortex-html-version="1"` and
`data-docvortex-latex`. EPUB XHTML/CSS and Markdown's embedded HTML use the same
namespace. Only DocVortex HTML markers receive exact decoding; old DocGale- or MinerU-marked
HTML follows ordinary webpage parsing, without an exact semantic round-trip
guarantee. No legacy codec aliases or marker compatibility branches are provided.
See the [HTML protocol](HTML_PROTOCOL.md) for the current contract.

Saved files are never rewritten on load. Both applications require the new shared
schema identities and version 2.0. Old DocGale/DocVortex native artifacts and
historical MinerU envelopes are rejected and must be regenerated from the source.
PDF/DOCX/LaTeX generated identifiers now use DocVortex; MinerU's UI markers and
product identity remain unchanged.

## Source and sample history

Native analyzers, semantic types, deterministic postprocessing and renderers
were extracted from MinerU commit
`23d6e51185b1aa8868965fb45d644ccc0071ce68`. Project code is now distributed under
the [MIT License](../LICENSE.md); existing copyright ownership is preserved.
Apache-2.0 portions remain identified in the PDF text source, and the Droid font
retains its original NOTICE and provenance manifest.

The full demo corpus was moved unchanged from MinerU, preserving filenames and
the recorded hashes and regression manifests. DocVortex maintains native parsing,
geometry, semantic and export regressions; MinerU retains `demo1.pdf` and
`demo2.pdf` for inference integration tests. Other host-specific geometry inputs
are frozen as JSON fixtures in MinerU. Neither test suite requires an adjacent
checkout or runtime corpus download.

## Content List ownership

Content List V1/V2 and their options live in MinerU. DocVortex only accepts its
seven general rendering targets, including when exporting a restored bundle.
Applications needing Content List can pass the shared MiddleJson to MinerU's
public render_content_list/render_content_list_v2 functions. The two projects
have separate RenderFormat enums; import the enum belonging to the API you call.
Shared options and RenderMode remain the same types. MinerU ContentType and
ContentTypeV2 constants are defined in mineru.types.
