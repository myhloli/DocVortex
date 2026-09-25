# Optional Rust PDF kernels

DocVortex keeps its Python API, document types, parsing decisions and PDFium runtime.
The private `docvortex._native` extension accelerates batches of already materialized
numbers and indices. It does not bundle or call a second PDFium library and does not
create a thread pool. The existing PDFium lock and handle lifetimes are unchanged.

## Runtime selection

Set `DOCVORTEX_COMPUTE_BACKEND` before the first PDF computation in a process:

| Value | Behavior |
| --- | --- |
| `auto` (default) | Load a compatible native extension when available; otherwise use Python. |
| `python` | Use the Python reference kernels without importing the extension. |
| `rust` | Require the extension and its matching private protocol; missing or incompatible binaries raise an error. |

Selection is cached for the process. Restart the process after changing the variable
or rebuilding an editable extension. Computation errors are never silently retried
in Python. The few explicit algorithmic reference paths preserve extreme coordinates,
unusual Python inputs, and script-reference ties whose choice depends on Python set
iteration. These paths do not weaken any parsing decision or output comparison.

Python owns Unicode classification, normalization, font equality, version-dependent
rounding, HTML generation and public object construction. Stable table-column means
use an incremental accumulator matching the running CPython version's float-only
`sum`: sequential for 3.10–3.11 and compensated for 3.12–3.14. Integer/mixed/custom
inputs, other runtimes and intermediate overflow retain Python computation. Rust owns
numeric batches and returns source ranges or indices. Canonical geometry reuses
unchanged Python float objects and zero-rotation tuples to bound retained memory. Original `Char` references and
copy-on-merge source-index behavior are retained. Small bounded caches contain only
immutable Unicode features; they do not retain documents or use object addresses.

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
  Unsupported ctypes ABI/inputs use the reference reader with a recorded reason;
  errors after entering the bridge propagate. Coordinate conversion can then release
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

## Development and distribution

The Cargo workspace contains a Python-independent core crate and a PyO3 binding crate.
`rust-toolchain.toml` pins the compiler; `Cargo.lock` pins dependencies. Build release
kernels even for editable performance testing:

```sh
DOCVORTEX_BUILD_NATIVE=1 python -m pip install -e .
DOCVORTEX_COMPUTE_BACKEND=rust python -m pytest tests/test_native_parity.py
cargo test -p docvortex-core --locked
cargo clippy --workspace --all-targets -- -D warnings
```

`DOCVORTEX_BUILD_NATIVE=0` builds a pure Python wheel. `auto` builds native code when
Cargo is available and otherwise builds pure Python. If a compiler is available but
compilation fails, installation fails visibly instead of publishing a broken binary.
Official binary builds always set `DOCVORTEX_BUILD_NATIVE=1`.

GIL-enabled CPython 3.10–3.14 uses `cp310-abi3` wheels for Linux x86_64/aarch64,
Windows x86_64 and macOS arm64/x86_64. Linux binary builds target manylinux_2_28;
older glibc systems can use the pure Python wheel. Other interpreters can build the
pure package; this does not extend compatibility guarantees of its dependencies.
Free-threaded native wheels are not part of this change.

The native wheel workflow tests installed artifacts against Python output and exercises
real parsing, both on Python 3.10 and 3.14. The regular CI matrix exercises Python and
Rust backends on Python 3.10–3.14. The release workflow retains the main-CI/tag gate,
then builds/tests every artifact before publishing all platform wheels, the pure wheel
and the source distribution. A local build is not evidence that remote matrix jobs passed.

On Intel macOS, the existing Magika/ONNX Runtime dependency stack currently prevents
full application installation on Python 3.14. That wheel is tested with full parsing
on Python 3.10 and 3.13, and its binary kernels are loaded and executed separately on
Python 3.14. This ABI check is not a claim that the complete dependency stack installs
there. The Rust change does not alter those existing dependency requirements.

## Benchmarks and regression gates

Use `PYTHONPATH=src` for a source checkout; omit it when validating an installed wheel.
Every output directory must be new. Run CPU benchmarks without concurrent tests/builds.
The independent process-tree memory auditor additionally requires `psutil`.

```sh
python tests/benchmarks/flash_pdf.py --backend python --runs 5 --profile --output output/rust/reference
python tests/benchmarks/flash_pdf.py --backend rust --runs 5 --profile --output output/rust/native --baseline output/rust/reference
python tests/benchmarks/native_pdf_table.py --backend python --runs 5 --output output/rust/table-python
python tests/benchmarks/native_pdf_table.py --backend rust --runs 5 --output output/rust/table-rust --baseline output/rust/table-python
python tests/benchmarks/rust_pdf.py --backend python --suite public --output output/rust/public-python
python tests/benchmarks/rust_pdf.py --backend rust --suite public --output output/rust/public-rust --baseline output/rust/public-python
python tests/benchmarks/rust_pdf.py --backend python --suite shared --flash-baseline output/rust/reference --output output/rust/shared-python
python tests/benchmarks/rust_pdf.py --backend rust --suite shared --flash-baseline output/rust/reference --output output/rust/shared-rust --baseline output/rust/shared-python
```

The public/shared benchmark defaults to `caibao1`, `demo1` and `demo2`. Use repeatable
`--path` arguments for other cases. Shared benchmarking consumes frozen baseline table
regions, so a different candidate set cannot masquerade as acceleration. Timings include
conversion into Rust and Python result construction. First invocation is separate from
five warmed runs. Output comparison and memory sampling are outside timing. Public
`parse` includes geometry, image materialization and postprocessing, but not rendering
an export. Shared timings separate extraction, reuse, evidence and table recovery;
they do not include model inference or represent MinerU end-to-end latency.

Acceptance requires unchanged full outputs, related historical/manual tests, host API
and primitive-reuse tests, and installed-wheel verification. Target a 30% reduction in
each demo's public parse and shared-entry timing. Investigate sustained regressions
above 5% in time or RSS. Never refresh a gold file or remove fields to achieve parity;
record any remaining performance gap explicitly.


Optional host replay lives outside the independent engine test tree:

```sh
python tools/mineru_native_replay.py --mineru-source ../Magic-PDF --backend python --flash-baseline output/rust/reference --output output/rust/host-python
python tools/mineru_native_replay.py --mineru-source ../Magic-PDF --backend rust --flash-baseline output/rust/reference --output output/rust/host-rust --baseline output/rust/host-python
python tests/benchmarks/compare_native_corpus.py --runs 5 --output output/rust/corpus
```

Host replay compares actual medium/high native stages with frozen model inputs and
records fallback inputs and per-page extraction counts; it does not measure live inference.


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
