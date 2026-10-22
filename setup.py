"""构建必需的 Rust 原生扩展，拒绝已移除的兼容后端开关。"""

import os
import sys
import sysconfig
import platform

from setuptools import setup
from setuptools_rust import Binding, RustExtension

for variable in ("DOCVORTEX_BUILD_NATIVE", "DOCVORTEX_COMPUTE_BACKEND"):
    if variable in os.environ:
        raise RuntimeError(f"{variable} was removed in DocVortex 0.6; unset it before building the required native extension")
supported = (
    platform.python_implementation() == "CPython"
    and not sysconfig.get_config_var("Py_GIL_DISABLED")
    and (3, 11) <= sys.version_info[:2] <= (3, 14)
)
if not supported:
    raise RuntimeError("Native wheels currently require GIL-enabled CPython 3.11-3.14")
setup(
    rust_extensions=[
        RustExtension(
            "docvortex._native",
            path="rust/docvortex-python/Cargo.toml",
            binding=Binding.PyO3,
            py_limited_api=True,
            optional=False,
            debug=False,
            cargo_manifest_args=["--locked"],
        )
    ],
    options={"bdist_wheel": {"py_limited_api": "cp311"}},
    zip_safe=False,
)
