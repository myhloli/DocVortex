"""在独立安装环境验证真实扩展、公共解析及逐字段 Python/Rust 一致性。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def capture(pdf: Path, output: Path, expected: str) -> None:
    """要求加载安装包而非 checkout，并捕获模型、后处理、素材和诊断。"""
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
        assert actual["pdfium_bridge_calls"] > 0, actual
        assert actual["pdfium_bridge_unavailable_reason"] is None, actual
    data = {
        "model": result.model_json.model_dump(mode="json"),
        "middle": result.middle_json.model_dump(mode="json"),
        "assets": {key: hashlib.sha256(value).hexdigest() for key, value in result.assets.items()},
        "diagnostics": [asdict(value) for value in result.diagnostics],
    }
    output.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def main() -> None:
    """分别启动参考和原生进程，避免后端缓存及已导入模块相互污染。"""
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
