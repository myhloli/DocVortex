"""OOXML 与 OLE 属性读取，不实例化正文转换器。"""

from __future__ import annotations

from io import BytesIO
from datetime import datetime, timezone
import posixpath
from zipfile import ZipFile

import lxml.etree as etree

from ....document.properties import property_count, property_date, property_text, property_values
from ....errors import DocumentError
from ....schema import DocumentProperties
from .limits import MAX_ENTRY_BYTES, MAX_TOTAL_BYTES

_DC = "{http://purl.org/dc/elements/1.1/}"
_DCT = "{http://purl.org/dc/terms/}"
_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_APP = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"


def _xml_part(package: ZipFile, name: str) -> etree._Element | None:
    """读取有界 ZIP XML part，禁用实体扩展和外部网络访问。"""
    if name not in package.namelist():
        return None
    if package.getinfo(name).file_size > MAX_ENTRY_BYTES:
        raise DocumentError("resource_limit", f"Metadata part too large: {name}")
    with package.open(name) as stream:
        data = stream.read(MAX_ENTRY_BYTES + 1)
    if len(data) > MAX_ENTRY_BYTES:
        raise DocumentError("resource_limit", f"Metadata part too large: {name}")
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))


def _element_value(root: etree._Element | None, tag: str) -> str | None:
    """读取指定命名空间的首个非空属性元素。"""
    if root is None:
        return None
    for element in root.iter(tag):
        if value := property_text("".join(element.itertext())):
            return value
    return None


def read_ooxml_properties(data: bytes, suffix: str) -> tuple[DocumentProperties, list[str]]:
    """读取 OOXML 核心属性和结构计数，坏的可选属性不阻止其他读取。"""
    properties = DocumentProperties()
    warnings: list[str] = []
    with ZipFile(BytesIO(data)) as package:
        infos = package.infolist()
        if len(infos) > 100_000 or sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
            raise DocumentError("resource_limit", "OOXML metadata package exceeds resource limits")
        if len({info.filename for info in infos}) != len(infos):
            raise DocumentError("open_failed", "Duplicate OOXML package members")
        if _xml_part(package, "[Content_Types].xml") is None:
            raise DocumentError("open_failed", "Missing OOXML content types")
        parts = {"core-properties": "docProps/core.xml", "extended-properties": "docProps/app.xml"}
        main = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}[suffix]
        relationships = _xml_part(package, "_rels/.rels")
        if relationships is not None:
            for relation in relationships:
                if relation.get("TargetMode") == "External":
                    continue
                name = (relation.get("Type") or "").rsplit("/", 1)[-1]
                target = posixpath.normpath((relation.get("Target") or "").lstrip("/"))
                if target.startswith("../") or "\\" in target:
                    raise DocumentError("open_failed", "Invalid OOXML relationship target")
                if name == "officeDocument":
                    main = target
                elif name in parts:
                    parts[name] = target
        if main not in package.namelist():
            raise DocumentError("open_failed", f"Missing OOXML document part: {main}")
        for name, path in parts.items():
            try:
                root = _xml_part(package, path)
                if name == "core-properties":
                    properties.title = _element_value(root, _DC + "title")
                    properties.authors = property_values(_element_value(root, _DC + "creator"))
                    properties.subject = _element_value(root, _DC + "subject")
                    properties.description = _element_value(root, _DC + "description")
                    properties.keywords = property_values(_element_value(root, _CP + "keywords"))
                    properties.languages = property_values(_element_value(root, _DC + "language"))
                    properties.identifiers = property_values(_element_value(root, _DC + "identifier"))
                    properties.created_at = property_date(_element_value(root, _DCT + "created"), warnings=warnings)
                    properties.modified_at = property_date(_element_value(root, _DCT + "modified"), warnings=warnings)
                else:
                    properties.creator_application = _element_value(root, _APP + "Application")
                    if suffix == "docx":
                        properties.page_count = property_count(_element_value(root, _APP + "Pages"))
                        if properties.page_count is not None:
                            properties.page_count_kind = "declared"
            except Exception as exc:
                if isinstance(exc, DocumentError):
                    raise
                warnings.append(f"OOXML {path}: {exc}")
        if suffix in {"pptx", "xlsx"}:
            root = _xml_part(package, main)
            assert root is not None
            ns = "http://schemas.openxmlformats.org/"
            if suffix == "pptx":
                tags = {f"{{{ns}presentationml/2006/main}}sldId", "{http://purl.oclc.org/ooxml/presentationml/main}sldId"}
            else:
                tags = {f"{{{ns}spreadsheetml/2006/main}}sheet", "{http://purl.oclc.org/ooxml/spreadsheetml/main}sheet"}
            properties.page_count = sum(1 for element in root.iter() if element.tag in tags)
            properties.page_count_kind = "slide" if suffix == "pptx" else "sheet"
    return properties, warnings


def read_ole_properties(data: bytes, suffix: str) -> tuple[DocumentProperties, list[str]]:
    """读取受限 OLE 属性流并按声明代码页解码旧 Office 文本。"""
    from .legacy.ole import BoundedOleReader

    properties = DocumentProperties()
    warnings: list[str] = []
    with BoundedOleReader(data) as reader:
        # 先通过同一读取预算校验属性流，防止 get_metadata 绕过单流上限。
        for name in ("\x05SummaryInformation", "\x05DocumentSummaryInformation"):
            if reader.has_stream(name):
                reader.read_stream(name)
        metadata, messages = reader.metadata_with_diagnostics()
        warnings.extend(messages)
        if metadata is None:
            warnings.append("Cannot read OLE property streams")
            return properties, warnings
        codepage = getattr(metadata, "codepage", None)
        encoding = f"cp{codepage}" if isinstance(codepage, int) and codepage > 0 else "cp1252"
        fields = {"title": "title", "subject": "subject", "creator_application": "creating_application"}
        for target, source in fields.items():
            setattr(properties, target, _ole_text(getattr(metadata, source, None), encoding))
        properties.authors = property_values(_ole_text(metadata.author, encoding))
        properties.keywords = property_values(_ole_text(metadata.keywords, encoding))
        properties.description = _ole_text(metadata.comments, encoding)
        doc_codepage = getattr(metadata, "codepage_doc", None)
        doc_encoding = f"cp{doc_codepage}" if isinstance(doc_codepage, int) and doc_codepage > 0 else encoding
        properties.languages = property_values(_ole_text(metadata.language, doc_encoding))
        # OLE FILETIME 明确定义为 UTC，即使 olefile 返回的是无 tzinfo 的 datetime。
        created, modified = metadata.create_time, metadata.last_saved_time
        properties.created_at = property_date(
            created.replace(tzinfo=timezone.utc) if isinstance(created, datetime) else created, warnings=warnings
        )
        properties.modified_at = property_date(
            modified.replace(tzinfo=timezone.utc) if isinstance(modified, datetime) else modified, warnings=warnings
        )
        properties.page_count = property_count(metadata.slides if suffix == "ppt" else metadata.num_pages)
        if properties.page_count is not None:
            properties.page_count_kind = "declared"
    return properties, warnings


def _ole_text(value: object, encoding: str) -> str | None:
    """优先使用 OLE 代码页，不让未解码字节进入共享 JSON。"""
    if isinstance(value, bytes):
        try:
            value = value.decode(encoding, errors="replace")
        except LookupError:
            value = value.decode("cp1252", errors="replace")
    return property_text(value)


__all__ = ["read_ooxml_properties", "read_ole_properties"]
