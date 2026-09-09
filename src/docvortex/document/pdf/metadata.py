"""读取原始 PDF 属性，复用调用方 PDFium 生命周期与共享锁。"""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

from ...schema import DocumentProperties
from ..properties import property_date, property_text, property_values

if TYPE_CHECKING:
    from ._document import PDFDocument


def read_pdf_properties(document: PDFDocument) -> tuple[DocumentProperties, list[str]]:
    """优先保留 Info 字段，用 XMP 补缺；可选属性损坏不丢失物理页数。"""
    properties = DocumentProperties(page_count=document.page_count, page_count_kind="physical")
    warnings: list[str] = []
    try:
        info = document.metadata
        properties.title = property_text(info.get("Title"))
        properties.authors = property_values(info.get("Author"))
        properties.subject = property_text(info.get("Subject"))
        properties.keywords = property_values(info.get("Keywords"))
        properties.created_at = property_date(info.get("CreationDate"), warnings=warnings)
        properties.modified_at = property_date(info.get("ModDate"), warnings=warnings)
        properties.creator_application = property_text(info.get("Creator"))
        properties.producer_application = property_text(info.get("Producer"))
    except Exception as exc:
        warnings.append(f"PDF Info: {exc}")
    try:
        from pypdf import PdfReader
        from pypdf.generic import DictionaryObject

        reader = PdfReader(BytesIO(document.bytes), strict=False)
        catalog = reader.trailer["/Root"]
        if isinstance(catalog, DictionaryObject):
            properties.languages = property_values(catalog.get("/Lang"))
        xmp = reader.xmp_metadata
        if xmp is not None:
            # 日期直接读取原始 XMP 文本，避免库将无时区日期隐式解释为 UTC。
            mappings = {
                "title": ("dc_title", False),
                "authors": ("dc_creator", True),
                "subject": ("dc_subject", False),
                "keywords": ("pdf_keywords", True),
                "description": ("dc_description", False),
                "languages": ("dc_language", True),
                "identifiers": ("dc_identifier", True),
                "publisher": ("dc_publisher", False),
                "creator_application": ("xmp_creator_tool", False),
                "producer_application": ("pdf_producer", False),
            }
            for field, (attribute, multiple) in mappings.items():
                if getattr(properties, field):
                    continue
                try:
                    value = getattr(xmp, attribute, None)
                    if isinstance(value, dict):
                        value = value.get("x-default") or next(iter(value.values()), None)
                    values = property_values(value)
                    if field == "keywords" and not values:
                        values = property_values(xmp.dc_subject)
                    setattr(properties, field, values if multiple else (values[0] if values else None))
                except Exception as exc:
                    warnings.append(f"PDF XMP {attribute}: {exc}")
            import lxml.etree as etree

            root = etree.fromstring(xmp.stream.get_data(), etree.XMLParser(resolve_entities=False, no_network=True))
            namespace = "{http://ns.adobe.com/xap/1.0/}"
            for field, name in (("created_at", "CreateDate"), ("modified_at", "ModifyDate")):
                if getattr(properties, field):
                    continue
                for element in root.iter():
                    value = element.text if element.tag == namespace + name else element.get(namespace + name)
                    if normalized := property_date(value, warnings=warnings):
                        setattr(properties, field, normalized)
                        break
    except Exception as exc:
        warnings.append(f"PDF XMP: {exc}")
    return properties, warnings


__all__ = ["read_pdf_properties"]
