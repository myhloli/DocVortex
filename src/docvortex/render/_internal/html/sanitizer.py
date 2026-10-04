"""HTML renderer uses embedded fragments that are handled safely with URL."""

from __future__ import annotations

import html
import re
from urllib.parse import quote, unquote, urlsplit

import nh3
from bs4 import BeautifulSoup, NavigableString, Tag

from ....foundation._image_payload import extract_generated_svg_fallback, parse_image_data_uri_strict

_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "col",
    "colgroup",
    "div",
    "em",
    "eq",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_ACTIVE_CONTENT_TAGS = {
    "audio",
    "button",
    "canvas",
    "embed",
    "form",
    "iframe",
    "input",
    "math",
    "noscript",
    "object",
    "script",
    "select",
    "style",
    "svg",
    "template",
    "textarea",
    "video",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "col": {"span"},
    "colgroup": {"span"},
    "img": {"alt", "src", "title"},
    "li": {"value"},
    "ol": {"start"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}
_ALLOWED_URL_SCHEMES = {"data", "http", "https", "mailto", "tel"}
_LINK_SCHEMES = {"http", "https", "mailto", "tel"}
_EQ_PLACEHOLDER_TAG = "docvortex-eq-placeholder"
_DATA_IMAGE_RE = re.compile(
    r"\Adata:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<payload>[a-z0-9+/]*={0,2})\Z",
    re.IGNORECASE,
)
_SCHEME_RE = re.compile(r"\A[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_INTEGER_RE = re.compile(r"\A[+-]?\d+\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
_INVALID_HTML_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_HTML_TAG_TOKEN_RE = re.compile(
    r"<\s*(?P<closing>/)?\s*(?P<name>[A-Za-z][A-Za-z0-9:-]*)\b(?P<attrs>[^>]*)>",
    re.DOTALL,
)
_VOID_MARKUP_TAGS = {"br"}
_SOURCE_MARKUP_TAGS = _ALLOWED_TAGS | _ACTIVE_CONTENT_TAGS
_PHRASING_CONTAINER_TAGS = {"a", "b", "code", "em", "i", "p", "s", "span", "strong", "sub", "sup", "u"}
_TABLE_STRUCTURE_TAGS = {"colgroup", "table", "tbody", "tfoot", "thead", "tr"}


def is_supported_html_markup(content: str) -> bool:
    """Determine whether the content contains the HTML tag that needs to be handed over to the security layer for processing.

    Only whitelisted tags or active tags that must be deleted in their entirety count as HTML to avoid
    Ordinary text such as ``<local_dir>`` and mathematical inequalities are misidentified as tags.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if "<" not in content or ">" not in content:
        return False
    tokens = list(_HTML_TAG_TOKEN_RE.finditer(content))
    closing_names = {
        match.group("name").lower()
        for match in tokens
        if match.group("closing") and match.group("name").lower() in _SOURCE_MARKUP_TAGS
    }
    for match in tokens:
        if match.group("closing"):
            continue
        name = match.group("name").lower()
        if name not in _SOURCE_MARKUP_TAGS:
            continue
        if name in _VOID_MARKUP_TAGS or name in closing_names:
            return True
        if name in {"img", "embed"} and re.search(r"\bsrc\s*=", match.group("attrs"), re.IGNORECASE):
            return True
    return False


def sanitize_link_url(url: str) -> str | None:
    """Verify link addresses, retaining only safe relative addresses and explicitly allowed protocols."""
    normalized = _normalize_url_text(url, parameter_name="url")
    if normalized is None:
        return None
    if normalized.startswith(("//", "\\")):
        return None

    scheme_match = _SCHEME_RE.match(normalized)
    if scheme_match is None:
        return _quote_document_url(normalized)

    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _LINK_SCHEMES:
        return None
    if scheme in {"http", "https"}:
        try:
            if not parsed.netloc or parsed.hostname is None:
                return None
        except ValueError:
            return None
    elif not parsed.path:
        return None
    return _quote_document_url(normalized)


def sanitize_image_source(source: str, *, asset_base_url: str = "") -> str | None:
    """Verify the source of the image and add the resource root address for a safe relative sidecar path."""
    normalized = _normalize_url_text(source, parameter_name="source")
    if normalized is None:
        return None

    data_source = _sanitize_raster_data_uri(normalized)
    if data_source is not None:
        return data_source
    if normalized.lower().startswith("data:"):
        return None

    absolute_source = _sanitize_absolute_image_url(normalized)
    if absolute_source is not None:
        return _quote_image_url(absolute_source)
    if _SCHEME_RE.match(normalized) is not None or normalized.startswith(("//", "\\", "#", "?")):
        return None
    if not _is_safe_image_path(normalized):
        return None
    if normalized.startswith("/") or not asset_base_url:
        return _quote_image_url(normalized)

    safe_base = _sanitize_asset_base_url(asset_base_url)
    if safe_base is None:
        return None
    return _quote_image_url(f"{safe_base.rstrip('/')}/{normalized.lstrip('/')}")


def sanitize_html_fragment(markup: str, *, asset_base_url: str = "") -> str:
    """Clean embed HTML, preserving table semantics, secure media, and ``eq`` tags to be rendered."""
    if not isinstance(markup, str):
        raise TypeError("markup must be a string")
    if not isinstance(asset_base_url, str):
        raise TypeError("asset_base_url must be a string")

    soup = BeautifulSoup(_INVALID_HTML_TEXT_RE.sub("\ufffd", markup), "html.parser")
    _remove_active_content(soup)
    _normalize_fragment_elements(soup, asset_base_url=asset_base_url)
    _protect_equation_elements(soup)
    prepared = str(soup)
    cleaned = nh3.clean(
        prepared,
        tags=_ALLOWED_TAGS | {_EQ_PLACEHOLDER_TAG},
        clean_content_tags=_ACTIVE_CONTENT_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_sanitized_attribute,
        link_rel=None,
        url_schemes=_ALLOWED_URL_SCHEMES,
    )
    restored = cleaned.replace(f"<{_EQ_PLACEHOLDER_TAG}>", "<eq>").replace(f"</{_EQ_PLACEHOLDER_TAG}>", "</eq>")
    cleaned_soup = BeautifulSoup(restored, "html.parser")
    _normalize_cleaned_content_models(cleaned_soup)
    return str(cleaned_soup)


def _normalize_url_text(value: str, *, parameter_name: str) -> str | None:
    """Decodes the HTML entity and rejects control characters that could be used to obfuscate the protocol."""
    if not isinstance(value, str):
        raise TypeError(f"{parameter_name} must be a string")
    normalized = value.strip()
    for _ in range(8):
        decoded = html.unescape(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    normalized = normalized.strip()
    if not normalized or _CONTROL_RE.search(normalized) is not None:
        return None
    return normalized


def _sanitize_absolute_image_url(source: str) -> str | None:
    """Only HTTP(S) image addresses with valid hostnames are allowed."""
    if source.startswith(("//", "\\")):
        return None
    try:
        parsed = urlsplit(source)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    try:
        if not parsed.netloc or parsed.hostname is None:
            return None
    except ValueError:
        return None
    return source


def _sanitize_raster_data_uri(source: str) -> str | None:
    """Verify raster plot or renderer generated security SVG data URI."""
    match = _DATA_IMAGE_RE.fullmatch(source)
    if match is None:
        return None
    try:
        payload, extension = parse_image_data_uri_strict(source)
    except ValueError:
        return None
    if extension == "svg":
        try:
            extract_generated_svg_fallback(payload)
        except ValueError:
            return None
    return source


def _quote_image_url(source: str) -> str:
    """Encode spaces and parentheses according to existing render resource rules while preserving URL structural characters."""
    return quote(source, safe="/:#?&=%@+~,;!$'*-._")


def _quote_document_url(source: str) -> str:
    """Encodes spaces, backslashes, and parentheses in links while preserving URL structure characters."""
    return quote(source, safe="/:#?&=%@+~,;!$'*-._")


def _is_safe_image_path(source: str) -> bool:
    """Determines whether a relative or root relative image path will not escape the sidecar root directory."""
    if "\\" in source:
        return False
    try:
        parsed = urlsplit(source)
    except ValueError:
        return False
    if parsed.scheme or parsed.netloc or not parsed.path:
        return False

    return not _has_unsafe_path_segment(parsed.path)


def _sanitize_asset_base_url(asset_base_url: str) -> str | None:
    """Verify the resource root address used to splice sidecar."""
    normalized = _normalize_url_text(asset_base_url, parameter_name="asset_base_url")
    if normalized is None or normalized.startswith(("//", "\\", "#", "?")):
        return None
    absolute_url = _sanitize_absolute_image_url(normalized)
    if absolute_url is not None:
        parsed = urlsplit(absolute_url)
        if parsed.query or parsed.fragment or _has_unsafe_path_segment(parsed.path):
            return None
        return absolute_url
    if _SCHEME_RE.match(normalized) is not None:
        return None
    if not _is_safe_image_path(normalized):
        return None
    parsed = urlsplit(normalized)
    return None if parsed.query or parsed.fragment else normalized


def _has_unsafe_path_segment(path: str) -> bool:
    """Recursively decode URL paths, identifying control characters, backslashes, and parent directory escapes."""
    decoded_path = path
    for _ in range(8):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    if "\\" in decoded_path or _CONTROL_RE.search(decoded_path) is not None:
        return True
    return ".." in decoded_path.split("/")


def _remove_active_content(soup: BeautifulSoup) -> None:
    """Remove active tags and their entire contents before universal whitelist cleaning."""
    for tag in list(soup.find_all(_ACTIVE_CONTENT_TAGS)):
        if tag.parent is not None:
            tag.decompose()


def _normalize_fragment_elements(soup: BeautifulSoup, *, asset_base_url: str) -> None:
    """Override image, link and bounded numeric properties before nh3."""
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name == "a":
            if not _normalize_link_element(tag):
                tag.unwrap()
            continue
        if tag.name == "img":
            _normalize_image_element(tag, asset_base_url=asset_base_url)
            continue
        tag.attrs = _normalized_non_url_attributes(tag)


def _protect_equation_elements(soup: BeautifulSoup) -> None:
    """Use a temporary custom tag to protect ``eq`` to avoid nh3's special parsing of this historical tag."""
    for forged_placeholder in list(soup.find_all(_EQ_PLACEHOLDER_TAG)):
        forged_placeholder.unwrap()
    for equation in soup.find_all("eq"):
        latex = equation.get_text()
        equation.clear()
        equation.append(NavigableString(latex))
        equation.name = _EQ_PLACEHOLDER_TAG


def _normalize_cleaned_content_models(soup: BeautifulSoup) -> None:
    """Fix allowlist Lists, colgroup and pictures of HTML content model may still be violated after cleaning."""
    for image in soup.find_all("img"):
        if not image.has_attr("alt"):
            image["alt"] = ""

    for colgroup in soup.find_all("colgroup"):
        if colgroup.find("col", recursive=False) is not None:
            colgroup.attrs.pop("span", None)

    for list_tag in soup.find_all(("ul", "ol")):
        for child in list(list_tag.children):
            if isinstance(child, NavigableString):
                if not str(child).strip():
                    continue
                item = soup.new_tag("li")
                child.replace_with(item)
                item.append(child)
            elif isinstance(child, Tag) and child.name != "li":
                item = soup.new_tag("li")
                child.replace_with(item)
                item.append(child)

    for item in list(soup.find_all("li")):
        parent = item.parent
        if isinstance(parent, Tag) and parent.name in {"ul", "ol"}:
            continue
        while isinstance(parent, Tag) and parent.name in _PHRASING_CONTAINER_TAGS:
            parent.unwrap()
            parent = item.parent
        wrapper = soup.new_tag("ul")
        if isinstance(parent, Tag) and parent.name in _TABLE_STRUCTURE_TAGS:
            boundary = parent
            while isinstance(boundary.parent, Tag) and boundary.parent.name in _TABLE_STRUCTURE_TAGS:
                boundary = boundary.parent
            boundary.insert_before(wrapper)
            item.extract()
            wrapper.append(item)
        else:
            item.replace_with(wrapper)
            wrapper.append(item)


def _normalize_link_element(tag: Tag) -> bool:
    """Clean up link properties, dangerous or missing href expanded to normal text by the caller."""
    href = sanitize_link_url(_attribute_text(tag.get("href")))
    if href is None:
        return False
    attributes: dict[str, str] = {"href": href}
    title = _attribute_text(tag.get("title"))
    if title:
        attributes["title"] = title
    tag.attrs = attributes
    return True


def _normalize_image_element(tag: Tag, *, asset_base_url: str) -> None:
    """Clean image properties and replace dangerous images with visible and escaped text alt."""
    alt = _attribute_text(tag.get("alt"))
    source = sanitize_image_source(_attribute_text(tag.get("src")), asset_base_url=asset_base_url)
    if source is None:
        tag.replace_with(NavigableString(alt))
        return
    attributes: dict[str, str] = {"alt": alt, "src": source}
    title = _attribute_text(tag.get("title"))
    if title:
        attributes["title"] = title
    tag.attrs = attributes


def _normalized_non_url_attributes(tag: Tag) -> dict[str, str]:
    """Only bounded integer properties required for table and list semantics are retained."""
    attributes: dict[str, str] = {}
    if tag.name in {"td", "th"}:
        for name in ("colspan", "rowspan"):
            value = _bounded_integer(_attribute_text(tag.get(name)), minimum=1, maximum=1000)
            if value is not None:
                attributes[name] = value
        if tag.name == "th" and _attribute_text(tag.get("scope")) in {"col", "colgroup", "row", "rowgroup"}:
            attributes["scope"] = _attribute_text(tag.get("scope"))
    elif tag.name in {"col", "colgroup"}:
        value = _bounded_integer(_attribute_text(tag.get("span")), minimum=1, maximum=1000)
        if value is not None:
            attributes["span"] = value
    elif tag.name == "ol":
        value = _bounded_integer(_attribute_text(tag.get("start")), minimum=-1_000_000, maximum=1_000_000)
        if value is not None:
            attributes["start"] = value
    elif tag.name == "li":
        value = _bounded_integer(_attribute_text(tag.get("value")), minimum=-1_000_000, maximum=1_000_000)
        if value is not None:
            attributes["value"] = value
    return attributes


def _bounded_integer(value: str, *, minimum: int, maximum: int) -> str | None:
    """Parses and normalizes decimal integers within the specified closed range."""
    if _INTEGER_RE.fullmatch(value) is None:
        return None
    number = int(value)
    return str(number) if minimum <= number <= maximum else None


def _attribute_text(value: object) -> str:
    """Stable conversion of BeautifulSoup attribute value to string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _filter_sanitized_attribute(tag: str, attribute: str, value: str) -> str | None:
    """Verify URL again after re-parsing nh3 to prevent attribute bypass due to parsing differences."""
    if tag == "a" and attribute == "href":
        return sanitize_link_url(value)
    if tag == "img" and attribute == "src":
        return sanitize_image_source(value)
    return value


__all__ = [
    "is_supported_html_markup",
    "sanitize_html_fragment",
    "sanitize_image_source",
    "sanitize_link_url",
]
