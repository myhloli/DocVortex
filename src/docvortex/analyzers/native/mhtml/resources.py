"""使用 MIME 内嵌图片和样式的 HTML 资源适配器。"""

from __future__ import annotations

from docvortex.content.markup import ResolvedMarkupImage
from docvortex.document.mhtml import ArchivePart, MhtmlArchive, MhtmlParseError
from docvortex.foundation._svg_raster import looks_like_svg_payload, serialize_svg_image

from ..html.constants import MAX_HTML_IMAGE_BYTES
from ..html.errors import HtmlResourceLimitError
from ..html.resources import HtmlResourceContext, _image_data_uri


class MhtmlResourceContext(HtmlResourceContext):
    """仅从归档读取资源，复用 HTML 的链接、图片校验和字节预算。"""

    def __init__(self, archive: MhtmlArchive, *, base_href: str | None = None) -> None:
        """绑定归档并初始化以 MIME 部件为键的去重缓存。"""
        super().__init__(archive.source_context, base_href=base_href)
        self.archive = archive
        self._archive_images: dict[ArchivePart, str | None] = {}
        self._archive_stylesheets: dict[ArchivePart, str | None] = {}

    def _payload(self, part: ArchivePart, reference: str) -> bytes | None:
        """可选部件损坏时记录诊断；资源限额错误仍向调用方传播。"""
        try:
            return self.archive.decode(part)
        except MhtmlParseError:
            self.archive.report("mhtml_resource_invalid", reference)
            return None

    def resolve_image(self, source: str, *, alt: str = "") -> ResolvedMarkupImage | None:
        """优先恢复归档图片，缺失时沿用安全外链或替代文本降级。"""
        if not source.strip() or source.strip().casefold().startswith("data:"):
            return super().resolve_image(source, alt=alt)
        part = self.archive.find(source, base_href=self.base_href)
        if part is None:
            self.archive.report("mhtml_resource_missing", source)
            return super().resolve_image(source, alt=alt)
        if part not in self._archive_images:
            payload = self._payload(part, source)
            data_uri = None
            if payload is not None:
                if len(payload) > MAX_HTML_IMAGE_BYTES:
                    raise HtmlResourceLimitError(f"MHTML image exceeds max_html_image_bytes={MAX_HTML_IMAGE_BYTES}")
                if looks_like_svg_payload(payload):
                    data_uri = serialize_svg_image(payload)
                else:
                    data_uri = _image_data_uri(payload)
                if data_uri is not None:
                    self._charge_image_bytes(len(payload))
                else:
                    self.archive.report("mhtml_resource_invalid", source)
            self._archive_images[part] = data_uri
        if data_uri := self._archive_images[part]:
            return ResolvedMarkupImage(image_base64=data_uri, alt=alt)
        return super().resolve_image(source, alt=alt)

    def load_stylesheet(self, href: str) -> str | None:
        """按 HTML 源顺序读取归档 CSS，仅解释现有静态样式子集。"""
        part = self.archive.find(href, base_href=self.base_href)
        if part is None:
            self.archive.report("mhtml_resource_missing", href)
            return None
        if part in self._archive_stylesheets:
            return self._archive_stylesheets[part]
        payload = self._payload(part, href)
        text = None
        if payload is not None:
            if part.message.get_content_type() != "text/css":
                self.archive.report("mhtml_resource_invalid", href)
            else:
                self._charge_stylesheet_bytes(len(payload))
                try:
                    text = payload.decode(part.message.get_content_charset() or "utf-8-sig", errors="replace")
                except LookupError:
                    self.archive.report("mhtml_resource_invalid", href)
                    text = payload.decode("utf-8-sig", errors="replace")
        self._archive_stylesheets[part] = text
        return text
