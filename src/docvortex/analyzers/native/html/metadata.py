"""从静态 HTML head 元素读取源属性，不解释正文或访问资源。"""

from __future__ import annotations

from ....document.contracts import HtmlSourceContext
from ....document.properties import property_date, property_text
from ....schema import DocumentProperties
from .document import parse_html_document


def read_html_properties(
    data: bytes,
    source_context: HtmlSourceContext | None = None,
) -> tuple[DocumentProperties, list[str]]:
    """按标准 meta、Dublin Core、Open Graph 的顺序读取显式元数据。"""
    document = parse_html_document(data, source_context=source_context, metadata_only=True)
    warnings: list[str] = []
    values: dict[str, list[str]] = {}
    title = None
    for element in document.root.iter():
        if not isinstance(element.tag, str):
            continue
        if any(
            isinstance(parent.tag, str) and parent.tag.rsplit("}", 1)[-1].lower() in {"body", "script", "template", "noscript"}
            for parent in element.iterancestors()
        ):
            continue
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag == "title":
            title = title or property_text(" ".join(element.itertext()))
        elif tag == "meta":
            name = (element.get("name") or element.get("property") or element.get("http-equiv") or "").casefold()
            if value := property_text(element.get("content")):
                values.setdefault(name, []).append(value)
    props = DocumentProperties(page_count=1, page_count_kind="logical")
    mappings = {
        "title": ("dc.title", "dcterms.title", "og:title"),
        "authors": ("author", "dc.creator", "dcterms.creator", "article:author"),
        "subject": ("subject", "dc.subject", "dcterms.subject"),
        "keywords": ("keywords", "dc.subject", "article:tag"),
        "description": ("description", "dc.description", "dcterms.description", "og:description"),
        "languages": ("language", "content-language", "dc.language", "og:locale"),
        "identifiers": ("dc.identifier", "dcterms.identifier", "og:url"),
        "publisher": ("publisher", "dc.publisher", "dcterms.publisher"),
        "creator_application": ("generator",),
        "created_at": ("dcterms.created", "dc.date.created"),
        "modified_at": ("dcterms.modified", "dc.date.modified", "article:modified_time"),
    }
    for field, names in mappings.items():
        found = next((values[name] for name in names if values.get(name)), [])
        if field in {"authors", "keywords", "languages", "identifiers"}:
            setattr(props, field, found)
        elif field in {"created_at", "modified_at"}:
            setattr(props, field, property_date(found[0], warnings=warnings) if found else None)
        else:
            setattr(props, field, found[0] if found else None)
    props.title = title or props.title
    language = document.root.get("lang") or document.root.get("{http://www.w3.org/XML/1998/namespace}lang")
    if language:
        props.languages = [language]
    return props, warnings


__all__ = ["read_html_properties"]
