"""检查真实 Windows ARM64 原生 wheel 与 reportlab Python 实现，允许 pyclipper 本地构建。"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import platform
import struct
import sys
from pathlib import Path


def verify() -> None:
    """确认真实 ARM64 解释器、DocVortex 安装来源和原生二进制，并检查未启用 rl-accel。"""
    assert sys.platform == "win32" and platform.machine().lower() in {"arm64", "aarch64"}
    assert (3, 11) <= sys.version_info[:2] <= (3, 14), sys.version
    assert importlib.util.find_spec("_rl_accel") is None, "unexpected reportlab accelerator"
    try:
        importlib.metadata.distribution("rl-accel")
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        raise AssertionError("unexpected installed distribution: rl-accel")
    from docvortex._compute_backend import backend_info

    info = backend_info()
    assert info["backend"] == "rust", info
    extension = Path(info["extension"]).resolve()
    assert extension.is_relative_to(Path(sys.prefix).resolve()), extension
    data = extension.read_bytes()
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[:2] == b"MZ" and data[offset : offset + 4] == b"PE\0\0"
    assert struct.unpack_from("<H", data, offset + 4)[0] == 0xAA64
    from reportlab.lib import rl_accel

    assert not rl_accel._c_funcs, rl_accel._c_funcs
    print(f"Windows ARM64 CPython {sys.version_info.major}.{sys.version_info.minor} wheel installation verified")


if __name__ == "__main__":
    verify()
