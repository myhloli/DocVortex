# Mixed-font PDF script repair

Chinese paper 4 now emits `12gpm` as body text and `［8］` as one superscript
reference. The repair uses existing character font metadata and geometry; it
does not add a unit dictionary, normalize raw characters early or alter PDFium.

## Rules

- Weak script clusters are rechecked before neighbor expansion using initial
  body references from the same font and contiguous ASCII/fullwidth alphanumeric
  run. Font keys preserve the original name, flags and weight, including subset
  identity. Missing information retains the original classification.
- References require at least two initial body characters and the existing
  bounded horizontal neighborhood. The local median origin and high-quantile
  tight height reuse existing shift/height thresholds. Only a downgrade to body
  is allowed; newly downgraded clusters never become reference seeds.
- Fullwidth digits participate in mathematical token recognition without changing
  their source Unicode or indices. A trusted numeric superscript suffix after a
  body word retains its existing boundary, preventing descenders inside author
  names from being reclassified during token refinement.
- Strong candidates, public APIs, schemas, font policy, geometry and output text
  remain unchanged. Native PDF, table script recovery and Hybrid share the fix.

## Validation (2026-09-07)

[CI](https://github.com/myhloli/DocVortex/actions/runs/34052061906) at
`be494c615dfa597457e950f43cc1dcc6b1cfc970` passed Python 3.10–3.14 on all three
platforms, PDFium 5.10.1/5.13.0 and the Pydantic minimum. Linux/macOS: 437 passed;
Windows: 436 passed and one POSIX-only skip. The existing sparse-table diagnostic
remains separate and is not counted as repaired.

- The target fixture records source SHA256, character/source indices, font keys
  and upright loose/tight/origin evidence. Width, rotations, missing fonts,
  reference boundaries, ordinary numeric references and author suffixes are tested.
- 79 focused engine script/table tests and 206 host native-text/style tests passed.
  Confirmed `R²`, `v_i`, `y_i/B_m`, compound table suffixes and author markers remain.
- MinerU full regression: 3958 passed, four skips and the same four historical
  exclusions. Independent Python 3.14 wheel: 51 API, script and bundle checks passed.
- Across 31 documents, only the target paper changes. Four character styles change
  in both ModelJson and MiddleJson: `1`, `2`, `m` lose subscript and `［` gains
  superscript. Text, other styles, block structure and table attributes match.
- Nine actual platform/document captures preserve raw geometry and all 78 PNGs.
  Windows/Linux HTML DOM checks and before/after screenshots verify the complete
  citation and plain unit, including preservation of the English author names.

See the [difference record](validation/pdf-script-repair.json). The source change
does not rewrite saved results or Doclib caches: restart the upgraded service and
use the existing `--force` reparse option for current styles. Archives are built
from a committed snapshot so unrelated worktree edits are excluded. Version 0.1.0
remains a draft; this task does not publish to PyPI.
