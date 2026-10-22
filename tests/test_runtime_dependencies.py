"""验证安装产物中的运行时依赖路由，不依赖运行测试机器的平台。"""

from __future__ import annotations

from importlib import metadata

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
import pytest


@pytest.mark.parametrize("python_version", ("3.11", "3.12", "3.13", "3.14"))
@pytest.mark.parametrize(
    ("sys_platform", "machine", "expected"),
    (
        ("win32", "AMD64", "onnxruntime-directml"),
        ("win32", "amd64", "onnxruntime-directml"),
        ("win32", "x86_64", "onnxruntime-directml"),
        ("win32", "ARM64", "onnxruntime"),
        ("win32", "arm64", "onnxruntime"),
        ("win32", "aarch64", "onnxruntime"),
        ("win32", "x86", "onnxruntime"),
        ("win32", "i686", "onnxruntime"),
        ("win32", "", "onnxruntime"),
        ("darwin", "x86_64", "onnxruntime"),
        ("darwin", "arm64", "onnxruntime"),
        ("linux", "x86_64", "onnxruntime"),
        ("linux", "aarch64", "onnxruntime"),
    ),
)
def test_installed_metadata_selects_one_runtime(sys_platform: str, machine: str, expected: str, python_version: str) -> None:
    """以完整平台与 Python 矩阵检查实际发行元数据，保证两个运行时条件互斥且无遗漏。"""
    environment = {
        **default_environment(),
        "sys_platform": sys_platform,
        "platform_machine": machine,
        "python_version": python_version,
        "python_full_version": f"{python_version}.0",
    }
    requirements = [Requirement(value) for value in metadata.requires("docvortex") or ()]
    selected = [
        requirement
        for requirement in requirements
        if canonicalize_name(requirement.name).startswith("onnxruntime")
        and (requirement.marker is None or requirement.marker.evaluate(environment))
    ]
    assert len(selected) == 1, selected
    assert canonicalize_name(selected[0].name) == expected
    assert "1.24.4" in selected[0].specifier
    assert "1.24.3" not in selected[0].specifier
    assert "2" not in selected[0].specifier


def test_installed_metadata_requires_python_311_through_314() -> None:
    """检查实际发行物的 Python 支持边界，避免源码声明与安装包元数据脱节。"""
    package = metadata.metadata("docvortex")
    supported = SpecifierSet(package["Requires-Python"])
    assert all(version in supported for version in ("3.11", "3.12", "3.13", "3.14"))
    assert all(version not in supported for version in ("3.10", "3.15"))
    assert "Programming Language :: Python :: 3.10" not in package.get_all("Classifier", [])


def test_installed_metadata_requires_runtime_free_magika_fork() -> None:
    """确保宿主依赖新发行包，新包自身也不能再次引入任一种 ORT。"""
    requirements = [Requirement(value) for value in metadata.requires("docvortex") or ()]
    assert all(canonicalize_name(requirement.name) != "magika" for requirement in requirements)
    forks = [requirement for requirement in requirements if canonicalize_name(requirement.name) == "mineru-magika"]
    assert len(forks) == 1 and forks[0].marker is None
    assert "1.0.3" in forks[0].specifier
    assert "1.0.2" not in forks[0].specifier
    assert "1.1.0" not in forks[0].specifier
    fork_dependencies = [Requirement(value) for value in metadata.requires("mineru-magika") or ()]
    assert all(not canonicalize_name(requirement.name).startswith("onnxruntime") for requirement in fork_dependencies)


def test_installed_metadata_does_not_require_pdf_ocr_dependencies() -> None:
    """发行物不直接依赖已移除的 OCR 库，其他功能的传递依赖不受限制。"""
    requirements = [Requirement(value) for value in metadata.requires("docvortex") or ()]
    assert not {canonicalize_name(requirement.name) for requirement in requirements} & {"pyyaml", "filelock", "pyclipper"}
