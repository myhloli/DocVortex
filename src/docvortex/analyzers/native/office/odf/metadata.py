"""读取 OpenDocument meta.xml 与结构页数。"""

from __future__ import annotations

from typing import BinaryIO, Final

from lxml import etree  # type: ignore[reportMissingImports]

from .....schema import DocumentProperties
from .....document.properties import legacy_properties, property_date

from .constants import OdfSuffix, qname
from .package import OdfPackage
from .styles import OdfStyles


MAX_ODT_METADATA_PAGE_COUNT: Final = 10_000


def _first_text(root: etree._Element | None, *tags: str) -> str | None:
    """返回多个候选标签中首个非空文本。"""
    if root is None:
        return None
    for tag in tags:
        element = root.find(f".//{tag}")
        if element is not None:
            value = "".join(element.itertext()).strip()
            if value:
                return value
    return None


def _all_text(root: etree._Element | None, tag: str) -> list[str]:
    """保留文档内重复声明的多值属性，顺序和去重由共享类型维护。"""
    if root is None:
        return []
    return [value for element in root.iter(tag) if (value := "".join(element.itertext()).strip())]


def _odt_page_count(meta_root: etree._Element | None) -> int | None:
    """读取 ODT 生产者记录的布局页数，缺失或非法时返回空。"""
    if meta_root is None:
        return None
    statistic = meta_root.find(f".//{qname('meta', 'document-statistic')}")
    if statistic is None:
        return None
    try:
        value = int(statistic.get(qname("meta", "page-count"), ""))
    except ValueError:
        return None
    return min(value, MAX_ODT_METADATA_PAGE_COUNT) if value >= 1 else None


def _visible_sheet_count(body: etree._Element, styles: OdfStyles) -> int:
    """统计未被 table:display 或表格样式隐藏的 ODS 工作表。"""
    count = 0
    for sheet in body:
        if sheet.tag != qname("table", "table"):
            continue
        if sheet.get(qname("table", "display"), "true").casefold() == "false":
            continue
        if styles.table_is_visible(sheet.get(qname("table", "style-name"))):
            count += 1
    return count


def read_odf_properties(data: bytes, suffix: OdfSuffix) -> tuple[DocumentProperties, list[str]]:
    """提取 ODF 标题作者等元数据及稳定文档页数。"""
    package = OdfPackage(data)
    warnings: list[str] = []
    try:
        content_root = package.validate_document(suffix)
        styles_root = package.xml_part("styles.xml")
        styles = OdfStyles(styles_root, content_root)
        body = package.body_element(content_root, suffix)
        try:
            meta_root = package.xml_part("meta.xml", required=package.has_part("meta.xml"))
        except Exception as exc:
            from .errors import OdfResourceLimitError

            if isinstance(exc, OdfResourceLimitError):
                raise
            meta_root = None
            warnings.append(f"ODF meta.xml: {exc}")
        keywords = []
        if meta_root is not None:
            for keyword in meta_root.iter(qname("meta", "keyword")):
                value = "".join(keyword.itertext()).strip()
                if value:
                    keywords.append(value)
        if suffix == "odt":
            page_count = _odt_page_count(meta_root)
        elif suffix == "odp":
            page_count = sum(
                1 for child in body if child.tag == qname("draw", "page") and styles.drawing_page_is_visible(child)
            )
        else:
            page_count = _visible_sheet_count(body, styles)
        return DocumentProperties(
            page_count=page_count,
            page_count_kind=("declared" if suffix == "odt" else "slide" if suffix == "odp" else "sheet")
            if page_count is not None
            else None,
            title=_first_text(meta_root, qname("dc", "title")),
            authors=_all_text(meta_root, qname("dc", "creator")) or _all_text(meta_root, qname("meta", "initial-creator")),
            subject=_first_text(meta_root, qname("dc", "subject")),
            keywords=keywords,
            description=_first_text(meta_root, qname("dc", "description")),
            languages=_all_text(meta_root, qname("dc", "language")),
            identifiers=_all_text(meta_root, qname("dc", "identifier")),
            publisher=_first_text(meta_root, qname("dc", "publisher")),
            created_at=property_date(_first_text(meta_root, qname("meta", "creation-date")), warnings=warnings),
            modified_at=property_date(_first_text(meta_root, qname("dc", "date")), warnings=warnings),
            creator_application=_first_text(meta_root, qname("meta", "generator")),
        ), warnings
    finally:
        package.close()


def extract_odf_metadata(file_binary: BinaryIO, suffix: OdfSuffix) -> dict[str, object | None]:
    """保留既有基础属性接口的分页回退，源属性本身不补造页数。"""
    properties, _ = read_odf_properties(file_binary.read(), suffix)
    values = legacy_properties(properties)
    values["page_count"] = properties.page_count or 1
    return values


__all__ = ["extract_odf_metadata", "read_odf_properties"]
