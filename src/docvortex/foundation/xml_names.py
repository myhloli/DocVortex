"""Security normalization XML namespace and legacy HTML prefix tag names."""

from __future__ import annotations

from lxml import etree  # type: ignore[reportMissingImports]


def local_name(element: etree._Element) -> str:
    """Returns Clark notation, plain, or colon-prefixed lowercase local name of the label."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{") and "}" in tag:
        tag = tag.split("}", 1)[1]
    elif ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag.casefold()


__all__ = ["local_name"]
