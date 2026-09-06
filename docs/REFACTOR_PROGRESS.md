# Progressive refactor

Base: `b0cdd9a`. Public APIs, serialized protocols, native parsing semantics and
MinerU integrations remain compatible. Each stage is independently committed.

1. Capture public-pipeline and native baselines, including renderer subprocess RSS.
2. Reuse table indexes and asset hashes; defer unused format detection imports.
3. Render only PDF pages with visual blocks, preserving physical page identities.
4. Optimize continuation planning, owned render copies and inline concatenation.
5. Move shared capabilities down and split converter responsibilities.
6. Reuse stable geometry within a stage; validate complete output and performance.

Validation results and any measured limitations will be recorded here as stages
complete. Generated baselines are stored under `.baseline/progressive-refactor/`.
