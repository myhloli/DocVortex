# Third-party notices

The native analyzers, semantic schema, deterministic postprocessing and rendering
originate from MinerU, commit 23d6e51185b1aa8868965fb45d644ccc0071ce68.
Existing copyright headers and the MinerU Open Source License are retained.

PDF text grouping and geometry behavior incorporate selected algorithms from
pdftext 0.7.1 by Vik Paruchuri, licensed under Apache-2.0. DocGale replaces its
runtime dependency and data-container adapters with its own document text layer.
Changes include owned character types, source-index mappings, combined geometry
collection and direct dictionary-based grouping. The Apache-2.0 license is
included in licenses/Apache-2.0.txt.

## Droid Sans Fallback Full

The unchanged font at `docgale/resources/fonts/DroidSansFallbackFull.ttf` is
bundled from the Droid font directory at MuPDF commit `398b9126136fae6ffa78fb40bc768f2ebfdc4fa4`.
That directory carries the Android Open Source Project Apache-2.0 NOTICE,
reproduced alongside the font. No MuPDF code is included or required.
Font SHA256: `8a4dea0899424438af25a6f1f6eb61e5d111d85367eece0a5d30170861ae6b2e`.
