"""在现有 setuptools 构建中选择原生扩展或可独立安装的纯 Python 包。"""

import os
import shutil

from setuptools import setup
from setuptools_rust import Binding, RustExtension

mode = os.environ.get("DOCVORTEX_BUILD_NATIVE", "auto")
if mode not in {"0", "1", "auto"}:
    raise ValueError("DOCVORTEX_BUILD_NATIVE must be 0, 1 or auto")
enabled = mode == "1" or (mode == "auto" and shutil.which("cargo") is not None)
setup(
    rust_extensions=[
        RustExtension(
            "docvortex._native",
            path="rust/docvortex-python/Cargo.toml",
            binding=Binding.PyO3,
            debug=False,
            cargo_manifest_args=["--locked"],
        )
    ]
    if enabled
    else [],
    options={"bdist_wheel": {"py_limited_api": "cp310"}} if enabled else {},
    zip_safe=False,
)
