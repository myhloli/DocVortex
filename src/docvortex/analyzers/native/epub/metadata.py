"""从 EPUB OPF 读取源出版物属性和 spine 计数。"""

from __future__ import annotations
from typing import BinaryIO
from ....schema import DocumentProperties
from ....document.properties import legacy_properties
from .package import EpubPackage


def read_epub_properties(data: bytes) -> tuple[DocumentProperties, list[str]]:
    """读取完整多值 OPF 属性，不解析 spine 正文或加载资源。"""
    package = EpubPackage(data)
    try:
        return package.document_properties.model_copy(deep=True), list(package.metadata_warnings)
    finally:
        package.close()


def extract_epub_metadata(file_binary: BinaryIO) -> dict[str, object | None]:
    """保留既有基础属性接口，统一复用出版物属性读取器。"""
    properties, _ = read_epub_properties(file_binary.read())
    return legacy_properties(properties)


__all__ = ["extract_epub_metadata", "read_epub_properties"]
