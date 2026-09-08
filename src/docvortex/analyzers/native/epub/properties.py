"""OPF 源属性的唯一字段映射，供原生解析和独立元数据接口复用。"""

from __future__ import annotations
import lxml.etree as etree
from ....schema import DocumentProperties
from ....document.properties import property_date, property_text


def properties_from_opf(
    root: etree._Element,
    spine_count: int,
    *,
    warnings: list[str] | None = None,
) -> DocumentProperties:
    """映射 OPF 中显式的出版物信息，不读取正文或推断缺失字段。"""
    props = DocumentProperties(page_count=spine_count, page_count_kind="spine")
    dc = "{http://purl.org/dc/elements/1.1/}"
    scalar = {dc + "title": "title", dc + "description": "description", dc + "publisher": "publisher"}
    multiple = {dc + "creator": "authors", dc + "language": "languages", dc + "identifier": "identifiers"}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        value = property_text("".join(element.itertext()))
        if element.tag in scalar and value and not getattr(props, scalar[element.tag]):
            setattr(props, scalar[element.tag], value)
        elif element.tag in multiple and value:
            field = multiple[element.tag]
            setattr(props, field, [*getattr(props, field), value])
        elif element.tag == dc + "subject" and value:
            props.subject = props.subject or value
            props.keywords = [*props.keywords, value]
        elif element.tag == dc + "date":
            event = element.get("{http://www.idpf.org/2007/opf}event")
            if event == "creation":
                props.created_at = props.created_at or property_date(value, warnings=warnings)
            elif event == "modification":
                props.modified_at = props.modified_at or property_date(value, warnings=warnings)
        elif element.tag.rsplit("}", 1)[-1] == "meta":
            name = (element.get("property") or element.get("name") or "").casefold()
            value = property_text(element.get("content")) or value
            if name in {"keywords", "keyword"} and value:
                props.keywords = [*props.keywords, value]
            elif name == "dcterms:modified":
                props.modified_at = property_date(value, warnings=warnings) or props.modified_at
            elif name == "dcterms:created":
                props.created_at = property_date(value, warnings=warnings) or props.created_at
            elif name == "generator":
                props.creator_application = props.creator_application or value
    return props


__all__ = ["properties_from_opf"]
