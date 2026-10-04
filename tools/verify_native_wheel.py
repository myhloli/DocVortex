"""Verify true extensions, common parsing, and field-by-field Python/Rust consistency in a stand-alone installation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def capture(pdf: Path, output: Path, expected: str) -> None:
    """Requires loading the installer package instead of checkout and capturing models, post-processing, footage and diagnostics."""
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    import onnxruntime

    # Installation verification does not rely on telemetry, and background uploading is turned off to prevent the exit exception of SDK from covering up the parsing results.
    onnxruntime.disable_telemetry_events()
    from dataclasses import asdict
    from importlib.metadata import version
    import docvortex
    from docvortex._compute_backend import backend_info

    package = Path(docvortex.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert docvortex.__version__ == version("docvortex")
    assert (package.parent / "resources/unicode/EquivalentUnifiedIdeograph-17.0.0.txt").is_file()
    info = backend_info()
    assert info["backend"] == expected, info
    if expected == "rust":
        assert Path(info["extension"]).is_relative_to(package.parent), info
    result = docvortex.parse(pdf, keep_model_json=True)
    if expected == "rust":
        actual = backend_info()
        assert actual["pdfium_object_bridge_calls"] > 0, actual
        assert actual["pdfium_object_bridge_unavailable_reason"] is None, actual
        assert actual["native_text_snapshot_calls"] > 0 or actual["native_text_snapshot_empty_pages"] > 0, actual
        assert actual["native_text_snapshot_unavailable_reason"] is None, actual
        if actual["pdfium_bridge_calls"] > 0:
            assert actual["pdfium_record_batch_size"] == 1024, actual
    data = {
        "model": result.model_json.model_dump(mode="json"),
        "middle": result.middle_json.model_dump(mode="json"),
        "assets": {key: hashlib.sha256(value).hexdigest() for key, value in result.assets.items()},
        "diagnostics": [asdict(value) for value in result.diagnostics],
    }
    output.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def main() -> None:
    """Start the reference and native processes separately to avoid mutual contamination of the backend cache and imported modules."""
    if len(sys.argv) == 5 and sys.argv[1] == "--capture":
        capture(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        return
    if len(sys.argv) not in {2, 3}:
        raise SystemExit("Usage: verify_native_wheel.py PDF [python|rust]")
    selected = sys.argv[2] if len(sys.argv) == 3 else "rust"
    if selected not in {"python", "rust"}:
        raise SystemExit("Expected python or rust")
    pdf = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="docvortex-wheel-") as directory:
        root = Path(directory)
        backends = ("python", "rust") if selected == "rust" else ("python", "auto")
        for backend in backends:
            env = dict(os.environ, DOCVORTEX_COMPUTE_BACKEND=backend, LOGURU_LEVEL="WARNING")
            env.pop("PYTHONPATH", None)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--capture",
                    str(pdf),
                    str(root / f"{backend}.json"),
                    "python" if backend == "auto" else backend,
                ],
                cwd=root,
                env=env,
                check=True,
            )
        assert (root / f"{backends[0]}.json").read_bytes() == (root / f"{backends[1]}.json").read_bytes(), (
            "Wheel outputs differ"
        )
    print(f"Installed {selected} wheel and reference outputs verified", flush=True)


if __name__ == "__main__":
    main()
