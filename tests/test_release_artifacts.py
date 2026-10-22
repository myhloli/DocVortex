"""验证 native-only 发布门禁能够拒绝缺失、混入或伪装的平台发行物。"""

from pathlib import Path
import zipfile

import pytest

from tools.verify_release_artifacts import EXPECTED_PLATFORMS, verify_artifacts


def write_wheel(directory: Path, platform: str, *, version: str = "0.6.0", native: bool = True, pure: bool = False) -> Path:
    """构造最小 wheel 载荷，只模拟发行物守卫需要检查的元数据和扩展条目。"""
    path = directory / f"docvortex-{version}-cp311-abi3-{platform}.whl"
    prefix = f"docvortex-{version}.dist-info/"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(prefix + "METADATA", f"Metadata-Version: 2.4\nName: docvortex\nVersion: {version}\n")
        archive.writestr(
            prefix + "WHEEL",
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {str(pure).lower()}\nTag: cp311-abi3-{platform}\n",
        )
        if native:
            suffix = ".pyd" if platform.startswith("win_") else ".abi3.so"
            archive.writestr("docvortex/_native" + suffix, b"native fixture")
    return path


@pytest.fixture
def release_directory(tmp_path: Path) -> Path:
    """为每条失败场景创建独立且完整的五平台 wheel 集合。"""
    for platform in EXPECTED_PLATFORMS:
        write_wheel(tmp_path, platform)
    return tmp_path


def test_complete_native_release_is_accepted(release_directory: Path) -> None:
    """完整 ABI3 发行物集合接受带 v 的发布标签。"""
    assert len(verify_artifacts(release_directory, "v0.6.0")) == 5


@pytest.mark.parametrize("filename", ["docvortex-0.6.0.tar.gz", "docvortex-0.6.0-py3-none-any.whl"])
def test_source_or_compatibility_artifact_is_rejected(release_directory: Path, filename: str) -> None:
    """源码包和纯 Python wheel 均不能混入待发布集合。"""
    (release_directory / filename).write_bytes(b"unexpected artifact")
    with pytest.raises(AssertionError):
        verify_artifacts(release_directory, "v0.6.0")


def test_missing_platform_is_rejected(release_directory: Path) -> None:
    """一个平台缺失时不能用其余成功产物完成发布。"""
    next(release_directory.glob("*win_arm64.whl")).unlink()
    with pytest.raises(AssertionError):
        verify_artifacts(release_directory, "v0.6.0")


@pytest.mark.parametrize("field", ["native", "pure", "version", "platform"])
def test_invalid_platform_wheel_is_rejected(release_directory: Path, field: str) -> None:
    """检查扩展缺失、纯包标志、旧版本及旧 macOS 标签这四种伪装。"""
    next(release_directory.glob("*macosx_14_0_arm64.whl")).unlink()
    write_wheel(
        release_directory,
        "macosx_11_0_arm64" if field == "platform" else "macosx_14_0_arm64",
        version="0.5.0" if field == "version" else "0.6.0",
        native=field != "native",
        pure=field == "pure",
    )
    with pytest.raises(AssertionError):
        verify_artifacts(release_directory, "v0.6.0")
