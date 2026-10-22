# Repository architecture

DocVortex is a Python document engine with a required Rust native backend. The Python
package owns the public SDK and document pipeline; the Rust crates implement private
batch computations and their Python bindings. They are built and released together.

## Repository layout

| Location | Responsibility |
| --- | --- |
| `src/docvortex/` | Importable Python package and runtime resources |
| `rust/docvortex-core/` | Python-independent numeric kernels and Rust contract tests |
| `rust/docvortex-pdfium/` | Same-library PDFium ABI, object/path reading and synchronized font callbacks |
| `rust/docvortex-python/` | PyO3 argument validation, conversion, bindings and PDFium bridge |
| Root `pyproject.toml`, `setup.py`, `MANIFEST.in` | Python metadata, mandatory extension build and source file inventory |
| Root `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml` | Shared Rust workspace, dependencies and compiler |
| `tests/` | Python unit/integration tests, native kernel contracts and revision benchmarks |
| `tools/` | Development, regression review and distribution verification tools |
| `docs/`, `schemas/`, `demo/` | Architecture/protocol documentation, JSON schemas and runnable examples |

The Python `src` layout separates importable code from repository tools. It does not
require other languages to live under `src`. The Cargo workspace at the repository
root manages the crates, so Python and Rust development commands use the same working
directory. Published releases contain platform wheels only; the compiled extension is installed
as `docvortex._native`, alongside the Python package, rather than as a second SDK.

## Document pipeline and ownership

The public entry points live in `docvortex.api` and are re-exported by `docvortex`:

```text
source -> document preparation -> analyzers -> ModelJson
       -> postprocess + asset materialization -> MiddleJson / DocumentResult
       -> render -> RenderArtifact -> export / saved files
```

`parse` combines analysis and postprocessing; `convert` additionally renders and
exports the result. The major package boundaries are:

- `document`: source preparation, document access and PDF primitives/handle lifetime.
- `analyzers`: format-specific interpretation and ModelJson construction.
- `postprocess`: deterministic transformation into the common document representation.
- `render`: output-format layout and rendering.
- `export`: materialized files and result bundles.
- `schema`, `result`, `assets`: document protocols, stage results and asset ownership.

`analyzers/native` means parsing information already present in the source document.
It contains Python document interpretation and orchestration. `docvortex._native` is
the required private compiled extension used by that pipeline. These two uses of
“native” describe different responsibilities.

## Required native backend

```text
Python document/PDF algorithms
  -> _compute_backend.get_native()
     -> docvortex._native (PyO3 validation and conversion)
        -> docvortex-core (numeric batches)
        -> Python-owned objects and parsing decisions
```

Python retains the public objects and top-level document pipeline.
Rust also owns continuous text grouping/visual preparation/typography and prepared table-corridor computations.
Unicode classifications are prepared using the active Python version before detached Rust computation. The core crate does not access Python objects or PDFium.
Bindings validate transport records and preserve Python error and object-reuse
semantics. Numeric work releases the GIL only at the existing explicit boundaries.

The PDFium bridge is a separate boundary: it borrows function pointers and a text-page
handle from the active pypdfium2 instance, under the existing Python guard and GIL.
It does not load another PDFium library, take ownership of handles or close them.
Subsequent numeric geometry conversion can release the GIL after reading finishes.

The extension is required; missing, unloadable or incompatible binaries fail with
installation guidance. `DOCVORTEX_COMPUTE_BACKEND` and `DOCVORTEX_BUILD_NATIVE` were
removed in 0.6: unset them entirely, including old `rust`, `1`, `auto` or empty values.
Computation errors propagate. The current private protocol is **36**. Rebuild an
editable extension after changing native sources and restart existing processes. Module-only refactors
preserve the protocol and all Python-visible registrations.

Within the binding crate, `lib.rs` declares modules and registers the existing flat
Python extension interface. Rust modules do not become Python submodules:

| Module | Responsibility |
| --- | --- |
| `geometry` | Character geometry, coordinate conversion, visual runs, anchor pairs and coordinate-object reuse |
| `statistics` | Ordered clustering, typography and lane-gap statistics |
| `tables` | Table kernels, column accumulation, row geometry and note-metric state |
| `spatial` | Baseline indices, title gaps, neighboring lines and annotation geometry |
| `scripts` | Script-role classification and inline pairing |
| `dedup` | Duplicate-paint, hidden-text and mapping-run candidates |
| `conversion` | Shared box extraction, tuple conversion and transport aliases |
| `pdfium` | Borrowed PDFium calls and bounded character batches |

Keep binding helpers internal to the crate. New functions and methods must include
Chinese comments explaining their responsibility. Place numeric kernels in the core
crate, Python transport handling in the bindings, and document decisions in Python.

## Development and verification

Run commands from the repository root in a virtual environment with GIL-enabled
CPython 3.11–3.14. Install the toolchain in `rust-toolchain.toml` and the platform's
C/C++ linker before building the required release-mode native extension:

```sh
python -m pip install -e ".[test,dev]"
python -m pytest -q tests/test_compute_backend.py tests/test_native_parity.py tests/test_native_statistics.py tests/test_native_round3.py tests/test_native_round4.py tests/test_native_round5.py tests/test_pdfium_bridge.py tests/test_architecture.py
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test -p docvortex-core --locked
```

`PYTHONPATH=src` can select the checkout only after its matching native extension has
been built. Installed-wheel validation must omit it and use a fresh environment without
an editable DocVortex installation. A missing compiler or extension never produces or
selects a pure Python compatibility package.

Use the native suites for transport, numeric and object-identity contracts,
and the core crate's tests for Python-independent kernels. Distribution checks must
cover exactly five `cp311-abi3` platform wheels, their installed extension and metadata,
CLI version, real document output, and rejection of pure Python wheels and source distributions.
The existing CI owns the cross-platform matrix. See [Rust acceleration](rust-acceleration.md)
for installed-wheel smoke checks, real-document comparisons and benchmark commands.

See [Windows ARM64](windows-arm64.md) for native ABI checks, reportlab compatibility,
and installation requirements when pyclipper needs a local build.

## Layout rationale

Keep `src/docvortex` and `rust` while Python remains the public product. Renaming `rust`
to `crates` would not change these boundaries. Independent `python` and `rust` product
roots become useful if Rust gains its own supported SDK and release lifecycle; that
is not the current distribution model.

References: [Python src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/),
[Cargo workspaces](https://doc.rust-lang.org/cargo/reference/workspaces.html),
[setuptools-rust mixed packages](https://setuptools-rust.readthedocs.io/en/latest/).
