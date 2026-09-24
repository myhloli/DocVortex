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
rounding/cluster means, HTML generation and public object construction. Rust owns
numeric batches and returns source ranges or indices. Original `Char` references and
copy-on-merge source-index behavior are retained. Small bounded caches contain only
immutable Unicode features; they do not retain documents or use object addresses.

## Kernels

- Shared script geometry: baseline clusters, component membership and script roles.
- Character geometry: clipping, rotation, visual run boundaries and canonical samples.
- Extracted-value geometry: PDFium reading stays in Python; coordinate conversion is
  batched after the same reads, within the existing resource scope.
- Deduplication: paint buckets, translated-run evidence, source components and hidden
  OCR geometry. Unicode matching and final copies remain in Python.
- Tables: region selection, batched rule coverage and merging, grid connectivity,
  rectangular component checks and glyph-to-cell assignment. Candidate ordering,
  confidence thresholds, diagnostics, primitive limits and HTML remain unchanged.

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

## Benchmarks and regression gates

Use `PYTHONPATH=src` for a source checkout; omit it when validating an installed wheel.
Every output directory must be new. Run CPU benchmarks without concurrent tests/builds.

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
