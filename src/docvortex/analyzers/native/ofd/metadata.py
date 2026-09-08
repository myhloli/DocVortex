"""从 OFD.xml 与 Document.xml 提取 Doclib 基础元数据。"""

from __future__ import annotations

from typing import BinaryIO

from ....schema import DocumentProperties
from ....document.properties import legacy_properties, property_date, property_values

from .constants import MAX_PAGE_COUNT, MAX_TOTAL_BYTES
from .errors import OfdResourceLimitError
from .package import OfdPackage, first_descendant, local_name


def read_ofd_properties(file_bytes: bytes) -> tuple[DocumentProperties, list[str]]:
    """返回全部 DocBody 的总页数和首个非空文档元数据。"""
    if len(file_bytes) > MAX_TOTAL_BYTES:
        raise OfdResourceLimitError(f"OFD resource limit exceeded: max_total_bytes={MAX_TOTAL_BYTES}")
    warnings: list[str] = []
    with OfdPackage(file_bytes) as package:
        refs = package.document_refs()
        page_count = 0
        keywords: list[str] = []
        metadata: dict[str, str | None] = {
            "title": None,
            "author": None,
            "subject": None,
            "keywords": None,
            "creator_application": None,
            "producer_application": None,
            "created_at": None,
            "modified_at": None,
            "identifier": None,
            "description": None,
        }
        key_map = {
            "Title": "title",
            "Author": "author",
            "Subject": "subject",
            "Keywords": "keywords",
            "Creator": "creator_application",
            "CreatorVersion": "producer_application",
            "CreationDate": "created_at",
            "ModDate": "modified_at",
            "DocID": "identifier",
            "Abstract": "description",
        }
        for ref in refs:
            if not keywords:
                keywords = property_values(ref.keywords) or property_values(ref.metadata.get("Keywords"))
            document_root = package.xml_part(ref.document_part, required=True)
            assert document_root is not None
            pages = first_descendant(document_root, "Pages")
            if pages is not None:
                for child in pages:
                    if local_name(child.tag) != "Page":
                        continue
                    page_count += 1
                    if page_count > MAX_PAGE_COUNT:
                        raise OfdResourceLimitError(f"OFD resource limit exceeded: max_page_count={MAX_PAGE_COUNT}")
            for source_key, target_key in key_map.items():
                if metadata[target_key] is None and ref.metadata.get(source_key):
                    metadata[target_key] = ref.metadata[source_key]
        creator = metadata["creator_application"]
        creator_version = metadata["producer_application"]
        return DocumentProperties(
            page_count=page_count,
            page_count_kind="physical",
            title=metadata["title"],
            authors=property_values(metadata["author"]),
            subject=metadata["subject"],
            keywords=keywords,
            description=metadata["description"],
            identifiers=property_values(metadata["identifier"]),
            creator_application=(f"{creator} {creator_version}" if creator and creator_version else creator),
            created_at=property_date(metadata["created_at"], warnings=warnings),
            modified_at=property_date(metadata["modified_at"], warnings=warnings),
        ), warnings


def extract_ofd_metadata(file_binary: BinaryIO) -> dict[str, object | None]:
    """既有基础接口委托统一属性提取，保留原有返回字段。"""
    properties, _ = read_ofd_properties(file_binary.read(MAX_TOTAL_BYTES + 1))
    return {**legacy_properties(properties), "is_image_based": 0}


__all__ = ["extract_ofd_metadata", "read_ofd_properties"]
