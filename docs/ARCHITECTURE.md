# Repository architecture

DocVortex is a Python document engine with optional Rust acceleration. The Python
package owns the public SDK and document pipeline; the Rust crates implement private
batch computations and their Python bindings. They are built and released together.

## Repository layout

| Location | Responsibility |
| --- | --- |
| `src/docvortex/` | Importable Python package and runtime resources |
| `rust/docvortex-core/` | Python-independent numeric kernels and Rust contract tests |
| `rust/docvortex-python/` | PyO3 argument validation, conversion, bindings and PDFium bridge |
| Root `pyproject.toml`, `setup.py`, `MANIFEST.in` | Python metadata, optional extension build and source distribution |
| Root `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml` | Shared Rust workspace, dependencies and compiler |
| `tests/` | Python unit/integration tests, Python/Rust parity tests and benchmarks |
| `tools/` | Development, regression review and distribution verification tools |
| `docs/`, `schemas/`, `demo/` | Architecture/protocol documentation, JSON schemas and runnable examples |

The Python `src` layout separates importable code from repository tools. It does not
require other languages to live under `src`. The Cargo workspace at the repository
root manages both crates, so Python and Rust development commands use the same working
directory. Rust sources are included in the sdist; the compiled extension is installed
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
It is Python code and works without Rust. By contrast, `docvortex._native` is a private
compiled extension. These two uses of “native” describe different responsibilities.

## Optional compute backend

```text
Python document/PDF algorithms
  -> _compute_backend.get_native()
     -> Python reference implementation, or
     -> docvortex._native (PyO3 validation and conversion)
        -> docvortex-core (numeric batches)
        -> Python-owned objects and parsing decisions
```

Python retains Unicode/text interpretation, public objects, algorithmic decisions and
reference implementations. The core crate does not access Python objects or PDFium.
Bindings validate transport records and preserve Python error and object-reuse
semantics. Numeric work releases the GIL only at the existing explicit boundaries.

The PDFium bridge is a separate boundary: it borrows function pointers and a text-page
handle from the active pypdfium2 instance, under the existing Python guard and GIL.
It does not load another PDFium library, take ownership of handles or close them.
Subsequent numeric geometry conversion can release the GIL after reading finishes.

`DOCVORTEX_COMPUTE_BACKEND=python|rust|auto` selects the backend once per process.
`python` never imports the extension; `rust` requires a compatible extension; `auto`
falls back on a missing or incompatible extension. Computation errors propagate.
The current private protocol is **6**. Rebuild an editable extension after changing
native sources and restart processes that cached the backend. Module-only refactors
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

Run commands from the repository root in a virtual environment. For pure Python
development (no native compilation):

```sh
DOCVORTEX_BUILD_NATIVE=0 python -m pip install -e ".[test,dev]"
DOCVORTEX_COMPUTE_BACKEND=python python -m pytest -q
```

For release-mode native development:

```sh
DOCVORTEX_BUILD_NATIVE=1 python -m pip install -e ".[test,dev]"
DOCVORTEX_COMPUTE_BACKEND=rust python -m pytest -q tests/test_compute_backend.py tests/test_native_parity.py tests/test_native_statistics.py tests/test_native_round3.py tests/test_native_round4.py tests/test_native_round5.py tests/test_pdfium_bridge.py tests/test_architecture.py
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test -p docvortex-core --locked
```

These environment assignments use POSIX shell syntax. In PowerShell, set the matching
`$env:DOCVORTEX_BUILD_NATIVE` or `$env:DOCVORTEX_COMPUTE_BACKEND` before the command.
For source-only Python runs, `PYTHONPATH=src` is available; installed-wheel validation
must omit it and use a fresh environment without an editable DocVortex installation.
Disabling native compilation does not remove a binary left by an earlier editable
build; use the explicit Python runtime backend or a clean wheel installation.

Use the existing parity suites for transport, numeric and object-identity contracts,
and the core crate's tests for Python-independent kernels. Distribution checks must
cover a complete sdist, a pure Python wheel and a native wheel built from that sdist.
The existing CI owns the cross-platform matrix. See [Rust acceleration](rust-acceleration.md)
for installed-wheel smoke checks, real-document comparisons and benchmark commands.

## Layout rationale

Keep `src/docvortex` and `rust` while Python remains the public product. Renaming `rust`
to `crates` would not change these boundaries. Independent `python` and `rust` product
roots become useful if Rust gains its own supported SDK and release lifecycle; that
is not the current distribution model.

References: [Python src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/),
[Cargo workspaces](https://doc.rust-lang.org/cargo/reference/workspaces.html),
[setuptools-rust mixed packages](https://setuptools-rust.readthedocs.io/en/latest/).
