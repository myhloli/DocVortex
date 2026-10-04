"""HTML resource adapter using MIME embedded pictures and styles."""

from __future__ import annotations

from docvortex.content.markup import ResolvedMarkupImage
from docvortex.document.mhtml import ArchivePart, MhtmlArchive, MhtmlParseError
from docvortex.foundation._svg_raster import looks_like_svg_payload, serialize_svg_image

from ..html.constants import MAX_HTML_IMAGE_BYTES
from ..html.errors import HtmlResourceLimitError
from ..html.resources import HtmlResourceContext, _image_data_uri


class MhtmlResourceContext(HtmlResourceContext):
    """Read resources from archive only, reusing link, image checksum and byte budget of HTML."""

    def __init__(self, archive: MhtmlArchive, *, base_href: str | None = None) -> None:
        """Bind the archive and initialize the deduplication cache keyed by the MIME part."""
        super().__init__(archive.source_context, base_href=base_href)
        self.archive = archive
        self._archive_images: dict[ArchivePart, str | None] = {}
        self._archive_stylesheets: dict[ArchivePart, str | None] = {}

    def _payload(self, part: ArchivePart, reference: str) -> bytes | None:
        """Logging diagnostics when optional components are damaged; resource quota errors are still propagated to the caller."""
        try:
            return self.archive.decode(part)
        except MhtmlParseError:
            self.archive.report("mhtml_resource_invalid", reference)
            return None

    def resolve_image(self, source: str, *, alt: str = "") -> ResolvedMarkupImage | None:
        """Priority is given to restoring archived pictures, and when missing, safe external links or alternative text are used for downgrading."""
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
        """Read archive CSS in HTML source order, interpreting only a subset of existing static styles."""
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
