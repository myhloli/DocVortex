"""HTML Flash Resolves the source context contract used."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HtmlSourceContext:
    """Save the source context required for relative link resolution and HTML decoding.

    Remote images are never downloaded, only restricted HTTP (S) external links are retained in the results, and the parsing process maintains zero network requests.
    """

    source_uri: str | None = None
    local_resource_root: Path | None = None
    transport_encoding: str | None = None


__all__ = ["HtmlSourceContext"]
