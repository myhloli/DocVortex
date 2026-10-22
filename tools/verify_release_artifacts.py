"""发布前检查完整的五平台原生 wheel 集合，拒绝兼容包和源码发行物。"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from pathlib import Path
import zipfile


EXPECTED_PLATFORMS = frozenset(
    {
        "manylinux_2_28_x86_64",
        "manylinux_2_28_aarch64",
        "win_amd64",
        "win_arm64",
        "macosx_14_0_arm64",
    }
)


def verify_artifacts(directory: Path, release_tag: str) -> list[Path]:
    """核对文件集合、平台标签、版本元数据和扩展载荷，防止错误产物进入发布步骤。"""
    expected_version = release_tag.removeprefix("v")
    artifacts = sorted(directory.iterdir())
    assert len(artifacts) == len(EXPECTED_PLATFORMS), artifacts
    observed = set()
    for wheel in artifacts:
        assert wheel.is_file() and wheel.suffix == ".whl", f"Only native wheels may be published: {wheel}"
        parts = wheel.name.removesuffix(".whl").split("-")
        assert len(parts) == 5, wheel.name
        name, version, interpreter, abi, platform = parts
        assert name == "docvortex" and version == expected_version, (wheel.name, expected_version)
        assert (interpreter, abi) == ("cp311", "abi3"), wheel.name
        assert platform in EXPECTED_PLATFORMS and platform not in observed, wheel.name
        observed.add(platform)
        with zipfile.ZipFile(wheel) as archive:
            members = archive.namelist()
            assert len(members) == len(set(members)), f"Duplicate wheel members: {wheel.name}"
            metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in members if name.endswith(".dist-info/WHEEL")]
            assert len(metadata_names) == len(wheel_names) == 1, wheel.name
            expected_info = f"docvortex-{expected_version}.dist-info/"
            assert metadata_names[0] == expected_info + "METADATA", metadata_names
            assert wheel_names[0] == expected_info + "WHEEL", wheel_names
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            wheel_metadata = BytesParser().parsebytes(archive.read(wheel_names[0]))
            assert metadata["Name"] == "docvortex" and metadata["Version"] == expected_version, wheel.name
            assert wheel_metadata["Root-Is-Purelib"] == "false", wheel.name
            assert wheel_metadata.get_all("Tag") == [f"cp311-abi3-{platform}"], wheel.name
            binaries = [name for name in members if name.startswith("docvortex/_native.") and name.endswith((".so", ".pyd"))]
            assert len(binaries) == 1 and archive.getinfo(binaries[0]).file_size > 0, (wheel.name, binaries)
            assert binaries[0].endswith(".pyd" if platform.startswith("win_") else ".so"), binaries
    assert observed == EXPECTED_PLATFORMS, observed
    return artifacts


def main() -> None:
    """读取待发布目录与标签，并打印已验证的具体 wheel 文件名。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("release_tag")
    arguments = parser.parse_args()
    for wheel in verify_artifacts(arguments.directory, arguments.release_tag):
        print(f"Verified release artifact: {wheel.name}")


if __name__ == "__main__":
    main()
