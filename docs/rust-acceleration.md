# Required Rust native backend

See [Repository architecture](ARCHITECTURE.md) for the Python/Rust ownership boundary
and binding-module layout. The current private extension protocol is **36**, shared by
the Python loader and Rust core. Protocol 4, 5, 6, 7 and 8 references below describe historical
interfaces or rollout stages, not the current loader requirement. Rebuild editable
native installs and restart existing processes after updating native sources.

DocVortex keeps its Python API, document types, parsing decisions and PDFium runtime.
The private `docvortex._native` extension accelerates batches of already materialized
numbers and indices. It does not bundle or call a second PDFium library and does not
create a thread pool. The existing PDFium lock and handle lifetimes are unchanged.

## Runtime contract

DocVortex 0.6 always loads its compatible Rust extension. The Python SDK, CLI,
public document types and document orchestration remain available. The former
Python compute backend, runtime selection and silent extension fallback are removed.
The current source ignores `DOCVORTEX_COMPUTE_BACKEND` and `DOCVORTEX_BUILD_NATIVE`,
including empty strings, former valid values and arbitrary unknown values. It does not
modify these variables or issue deprecation warnings.
This compatibility fix is not released yet; the published 0.6.0 package still rejects these variables.
Missing, unloadable or incompatible extensions fail instead of parsing through a
second implementation. Rebuild editable extensions after native changes and restart
existing processes.

Python owns Unicode preparation, normalization, HTML generation and public object
construction. Rust owns native computations and returns source ranges or indices.
Canonical geometry reuses unchanged Python float objects and zero-rotation tuples.
Original character references and copy-on-merge source indices remain part of the
contract. Process-wide caches contain only immutable features; document preparation
caches do not retain native handles beyond their active operation.

## Kernels

- Ordered statistics: exact median clustering for baseline and body-height groups;
  stable first/last-cluster decisions return only source indices.
- Typography and lanes: bounded font metadata reuse and batched numeric statistics;
  original lane-member sorting and Python text/font-family classification remain.
- Table text: stable visual rows and lazy per-recovery occupancy reuse without changing
  candidates, interval boundary ownership or diagnostics.
- Shared script geometry: baseline clusters, component membership and script roles.
- Character geometry: clipping, rotation, visual run boundaries and canonical samples.
- Character extraction: the private bridge borrows function pointers from the active
  pypdfium2 instance and reads characters in one synchronous call under the original
  lock and GIL. It owns no PDFium handles or callbacks and loads no second library.
  Unsupported ctypes ABI/inputs fail with a diagnostic; errors after entering the
  bridge propagate. Coordinate conversion can then release
  the GIL. Raw numerical records stay within the call; Python records and coordinate
  results are materialized in batches of at most 1,024. Font sharing, object/source IDs
  and writing-direction assignment retain whole-page scope. The original protocol-4
  list reader remains compatible. Wheel smoke checks require an actually completed
  bridge call using bounded batches.
- Deduplication: paint buckets, translated-run evidence, source components and hidden
  OCR geometry. Unicode matching and final copies remain in Python.
- Tables: region selection, batched rule coverage and merging, grid connectivity,
  rectangular component checks and glyph-to-cell assignment. Candidate ordering,
  confidence thresholds, diagnostics, primitive limits and HTML remain unchanged.
- Dense pages: bounded interval queries replace the quadratic candidate-list fallback;
  candidate consumption preserves original line indices. Rule-table candidates retain
  compact interval drafts and share note statistics before stable score materialization.
  Short-tail attachment sweeps preceding rows without repeatedly scanning whole lanes.
- Visual assets: the existing rendering workers crop, orient and JPEG-encode images,
  returning indexed encoded blocks instead of full-page pixels. Scheduling limits,
  timeout/recovery, crop/encoding parameters and public asset bytes are preserved.
  After a crop task closes its images, the worker collects the youngest Python GC
  generation: closed pypdfium2 document/page reference cycles can otherwise retain
  whole PDF inputs across tasks. This does not change PDFium handle ownership or
  require allocator-specific memory trimming.

## Development and distribution

`rust-toolchain.toml` pins the compiler and `Cargo.lock` pins dependencies. Install the
platform's C/C++ linker and build the required release extension even for editable work:

```sh
python -m pip install -e ".[test,dev]"
python -m pytest tests/test_native_parity.py
cargo test -p docvortex-core --locked
cargo clippy --workspace --all-targets -- -D warnings
uv build --wheel
```

Source checkout builds always require Rust. Missing compilers and build failures
stop installation. No build option produces a pure Python compatibility package.
Official releases publish exactly five `cp311-abi3` wheels and no sdist:

| Platform | Native wheel target |
| --- | --- |
| Linux x86_64 | manylinux_2_28_x86_64 |
| Linux aarch64 | manylinux_2_28_aarch64 |
| Windows x86_64 | win_amd64 |
| Windows ARM64 | win_arm64 |
| macOS Apple Silicon | macosx_14_0_arm64 |

The supported interpreter is GIL-enabled CPython 3.11–3.14. PyPy, free-threaded
Python, 32-bit systems, musl Linux and Intel macOS are outside this wheel matrix.
The required runtime stack includes pypdfium2 >=5.13.0,<6 and ONNX Runtime >=1.24.4;
Windows x64 selects DirectML, while the other supported platforms use the standard
ONNX Runtime package. macOS requires Apple Silicon and macOS 14 or newer.

The main CI tests native execution on Linux, Windows and macOS with Python 3.11/3.14.
The five-platform wheel workflow checks the installed artifact on both Python
endpoints, native ABI, real PDFs and a generated dense table with all 840 expected
cells. Windows ARM64 uses the actual ARM64 interpreter and verifies the PE architecture.
The release workflow retains the main-CI/tag gate and verifies every artifact's
version, ABI3 platform tag, extension payload and metadata before publishing.
A local build does not establish that remote matrix jobs passed.

Installed-wheel checks run outside the checkout with `PYTHONPATH` removed. They verify
package/extension provenance, SDK/CLI/producer versions, native bridge calls, nonempty
outputs, and fixed document semantics. An explicit full JSON baseline can additionally
check complete model/middle output, Markdown, diagnostics and asset hashes:

```sh
python tools/verify_native_wheel.py demo/pdfs/demo2.pdf --output output/wheel-demo2.json
python tools/generate_dense_pdf.py output/dense-table.pdf
python tools/verify_native_wheel.py output/dense-table.pdf --expectation dense-table
python tools/verify_native_wheel.py sample.pdf --baseline accepted-sample.json
```

Unknown inputs require `--baseline` or a JSON `--expectations` file specifying
`page_count`, `required_text` and optional `min_tables`/`min_assets` counts. Fixed demo
expectations are tied to the input SHA256. This is a semantic smoke gate, not a substitute
for the full revision regression corpus.

## Benchmarks and regression gates

Use `PYTHONPATH=src` for a source checkout; omit it when validating an installed wheel.
Every output directory must be new. Run CPU benchmarks without concurrent tests/builds.
The independent process-tree memory auditor additionally requires `psutil`.

```sh
python tests/benchmarks/flash_pdf.py --runs 5 --profile --output output/rust/native
python tests/benchmarks/native_pdf_table.py --runs 5 --output output/rust/table-native
python tests/benchmarks/rust_pdf.py --suite public --output output/rust/public-native
python tests/benchmarks/rust_pdf.py --suite shared --flash-baseline output/rust/native --output output/rust/shared-native
python tests/benchmarks/compare_pdf_revisions.py --reference-source OLD_CHECKOUT --output output/rust/revisions
```

The public/shared benchmark defaults to `--corpus all`: the layout manifest, recursively
discovered `demo/pdfs` PDFs and native table fixtures, deduplicated by resolved path.
Use `--corpus demo` for all demo PDFs, or repeatable `--path` arguments for explicit
inputs. The revision comparison also accepts `--extra-path` for local external samples.
Shared benchmarking consumes frozen baseline table
regions, so a different candidate set cannot masquerade as acceleration. Timings include
conversion into Rust and Python result construction. First invocation is separate from
five warmed runs. Output comparison and memory sampling are outside timing. Public
`parse` includes geometry, image materialization and postprocessing, but not rendering
an export. Shared timings separate extraction, reuse, evidence and table recovery;
they do not include model inference or represent MinerU end-to-end latency.

Acceptance requires unchanged full outputs, related historical/manual tests, host API
and primitive-reuse tests, and installed-wheel verification. Freeze the timing targets
and aggregation method for each optimization round before measurement. Investigate sustained regressions
above 5% in time or RSS. Never refresh a gold file or remove fields to achieve parity;
record any remaining performance gap explicitly.


Optional host replay lives outside the independent engine test tree:

```sh
python tools/mineru_native_replay.py --mineru-source ../Magic-PDF --flash-baseline output/rust/native --output output/rust/host-native
```

Host replay compares actual medium/high native stages with frozen model inputs and
records fallback inputs and per-page extraction counts; it does not measure live inference.

MinerU revisions whose scheduling tests mock the old full-page raster entry can use
`python -m pytest -p tools.mineru_render_test_adapter -o addopts= HOST_TEST_PATH`.
This explicit test-only plugin forwards that mock to the encoded-crop worker boundary;
it retains page-selection, configuration, title-gating and image-closure assertions.
The frozen third-round source reproduces the unadapted mock failures too. Real worker
behavior is checked separately with engine tests and complete PDF/asset comparisons;
the plugin does not change MinerU application code or production dispatch.


## Historical migration notes (0.5 and earlier)

The following records describe earlier rollout stages and their then-current reference
paths. Their protocol numbers, backend switches and commands are historical evidence,
not configuration instructions for 0.6. Use the current commands above for new runs.

Fourth-round kernels require private protocol 5. Rebuild editable native installs after
updating Python sources; an older extension is rejected by forced Rust selection.
Statistics preserve even-median arithmetic and stable tie order. Non-finite inputs use
explicit reference paths. Occupancy caches are local to one table recovery and do not
retain documents. Font metadata reuse is limited to one typography call and primitive
dictionary values.

Dense table-column clustering preserves first-match decisions and strict-prefix reuse,
but no longer re-sums each cluster's complete history. Each corridor's anchors are
prepared once; repeated row appearances still count as separate query positions.
Table-note body heights use persistent counts indexed by vertical prefixes and stable
height ranks. A core-row range may skip extra exclusion only when all occurrences of
every source ID lie inside the exclusion band; other ranges use exact member filtering.
Marker interpretation remains Python-owned and cached within one candidate build.
Marker queries visit only untested rows in the current core range and stop on the
first match. Two compact row bitmaps record tested and matching positions, with at
most 128 recently used markers per corridor. Eviction recomputes evidence; it never
removes candidates. No dense source-by-marker matrix of false results is populated.
Existing first-source cache decisions are respected, including duplicate source IDs.
Non-contiguous row selections retain the reference path.

Row geometry indices preserve original ordering, including non-monotonic bottoms.
They return coordinate source indices so Python reuses the original float objects;
ties retain the earlier value, including signed zero. Candidate scoring, all interval
candidates, annotation stopping rules and merge ordering are unchanged. Contexts keep
strong row references only within the active call and are never global caches.

Installed-wheel checks include `tests/test_native_round4.py`: every prefix's float bits,
coverage, first-match clustering, quantile ranks, repeated sources, coordinate identity
and indexed annotation chains are checked against the independent reference. The tests
assert native state is actually used, not merely that an extension can be imported.

The experimental native mapping pre-grouping kernel remains disabled in production:
whole-entry measurements showed its packing cost exceeded its calculation benefit.

Sixth-round inline geometry uses private protocol 7. Ordinary script classification
batches contain at most 64 independent lines or 8,192 characters; a longer individual
line is processed alone without splitting its classification scope. Formula segments,
citation protection, first-match ties and final Python token materialization remain.
Fraction filtering uses directional character indices and each cell's own character
set and median height. Post-semantic line candidates preserve both same-baseline and
edge-alignment paths before the original business predicate runs.

Rule-band reuse requires identical, ordered, contiguous row references and strictly
ordered finite rules. Repeated rules and unsupported inputs retain the reference path.
At most 16 corridor band indices and 8,192 prepared marker lines / 16,384 glyphs are
retained within one candidate build. Eviction recomputes evidence. Candidate enumeration,
scoring, boundary double ownership, first-source choices and final conflict resolution
remain unchanged.
It is retained for differential coverage. No additional environment switch is exposed.

For four-way revision acceptance, freeze the old checkout with its matching binary and
write `benchmark-revision.json` containing its `commit`. Then run:

```sh
python tests/benchmarks/compare_pdf_revisions.py --reference-source OLD_CHECKOUT --extra-path LOCAL_EXTERNAL.pdf --output output/rust/revisions
python tools/generate_dense_pdf.py output/dense-table.pdf
python tools/verify_native_wheel.py output/dense-table.pdf
```

The revision benchmark compares complete ModelJson, MiddleJson, diagnostics and asset
hashes, records real source fingerprints, checkpoints every timing run, samples process
tree RSS separately, and reverses order for regressions above 5%. Local external PDFs
are identified by SHA256 and are not uploaded to CI. The wheel matrix instead generates
a deterministic 120-row synthetic table and exercises both pypdfium2 5.10.1 and 5.13.0.

Validation tools set the upstream `ORT_DISABLE_TELEMETRY=1` before importing ONNX Runtime
and record the setting. API suppression alone cannot retract its initialization event
([upstream details](https://github.com/microsoft/onnxruntime/blob/main/docs/Privacy.md)).
This prevents a telemetry SDK shutdown race observed on the
local macOS stack from masking completed PDF results; production runtime settings are
unchanged. Failed runs remain excluded, and formal comparisons use the same telemetry
setting for both revisions and backends. Regression retests reverse only affected pairs.

RSS is collected in a separate process after one entrypoint warmup. Public parsing is
sampled before model serialization; shared sampling retains the evidence snapshots but
does not encode or hash JSON. Output equality is checked after sampling. The complete
descendant tree includes rendering workers and multiprocessing helpers. This avoids
mistaking allocator retention from full-output JSON validation for parser memory use.
Existing timing runs can be audited without discarding their original memory records:

```sh
python tests/benchmarks/pdf_memory.py --timing-report output/rust/revisions/report.json --output output/rust/memory-audit
```

## Native kernel migration (protocol 11)

The same-library PDFium adapter is now a separate Python-independent crate. Object
traversal, clipping and path decoding run there; character geometry stays in Rust
until bounded output batches. The synchronized font provider handles ASCII/CJK
requests natively and preserves Python normalization for uncommon non-ASCII names.
The full Flash document pipeline is not yet a standalone Rust engine.

`PDFPage.get_snapshot()` explicitly shares detached page evidence; public mutable
character access still materializes isolated Python data. `PDFDocument.get_render_session()`
reuses mapped input and directed workers. `DOCVORTEX_PDF_RENDER_BACKEND=auto` is the default and selects `session` rendering
in DocVortex and the matching MinerU checkout. Sessions use Rust when the compute
backend provides it and support Python when it does not. Rendering errors propagate
without retrying through the legacy renderer.
Set `DOCVORTEX_PDF_RENDER_BACKEND=legacy` to explicitly use the previous renderer.
The Python reference setting `DOCVORTEX_COMPUTE_BACKEND=python` describes pre-0.6
development; it does not select a Python backend in the current source. See
[pdf-native-kernel.md](pdf-native-kernel.md) for migration and evidence boundaries.
