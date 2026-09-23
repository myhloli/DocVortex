"""把 MHTML 主文档及归档资源投影为现有 HTML 语义块。"""

from __future__ import annotations

from typing import BinaryIO

from docvortex.document.contracts import HtmlSourceContext
from docvortex.document.mhtml import MAX_ARCHIVE_BYTES, MhtmlArchive
from docvortex.result import Diagnostic
from docvortex.schema import DocumentProperties

from ..html.converter import HtmlConverter
from ..html.document import HtmlDocument, parse_html_document
from ..html.metadata import read_html_properties
from .resources import MhtmlResourceContext


def read_archive_properties(archive: MhtmlArchive) -> tuple[DocumentProperties, list[str]]:
    """复用 HTML 声明元数据，仅用 MIME Subject 补充缺失标题。"""
    properties, warnings = read_html_properties(archive.html, archive.source_context)
    properties.title = properties.title or archive.subject
    return properties, warnings


class MhtmlConverter(HtmlConverter):
    """通过一次解包生成正文、元数据和可选资源诊断。"""

    def __init__(self) -> None:
        """初始化与单次转换绑定的元数据及诊断。"""
        super().__init__()
        self.properties = DocumentProperties()
        self.diagnostics: tuple[Diagnostic, ...] = ()

    def convert(self, file_binary: BinaryIO, *, source_context: HtmlSourceContext | None = None) -> None:
        """加载主 HTML 后调用共享投影，不合并 iframe 或其他 HTML 附件。"""
        archive = MhtmlArchive(file_binary.read(MAX_ARCHIVE_BYTES + 1), source_context)
        self.properties, warnings = read_archive_properties(archive)
        document = parse_html_document(archive.html, archive.source_context)
        if document.title is None and archive.subject:
            from dataclasses import replace

            document = replace(document, title=archive.subject)
        resources = MhtmlResourceContext(archive, base_href=document.base_href)
        _restore_picture_sources(document, archive)
        self._convert_document(document, resources)
        self.diagnostics = tuple(Diagnostic("read_metadata_failed", warning) for warning in warnings) + tuple(
            archive.diagnostics
        )


def _restore_picture_sources(document: HtmlDocument, archive: MhtmlArchive) -> None:
    """当回退图片未归档时采用已保存的 picture/srcset 候选，不发起外部请求。"""
    for image in document.body.iter("img"):
        if (image.get("src") or "").strip().casefold().startswith("data:"):
            continue
        if archive.find(image.get("src") or "", base_href=document.base_href) is not None:
            continue
        picture = next((parent for parent in image.iterancestors() if parent.tag == "picture"), None)
        sources = list(picture.iter("source")) if picture is not None else []
        for source in [*sources, image]:
            # 浏览器归档中的网络候选按逗号分隔；data URI 无需做归档匹配。
            candidates = (source.get("srcset") or "").split(",")
            selected = next(
                (
                    fields[0]
                    for candidate in candidates
                    if (fields := candidate.split()) and archive.find(fields[0], base_href=document.base_href) is not None
                ),
                None,
            )
            if selected:
                image.set("src", selected)
                break
