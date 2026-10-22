"""构建必需的 Rust 原生扩展。"""

import sys
import sysconfig
import platform

from setuptools import setup
from setuptools_rust import Binding, RustExtension

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
