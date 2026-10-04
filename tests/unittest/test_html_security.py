from __future__ import annotations

import base64
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup, Tag

from docvortex.foundation import _image_payload as image_payload
from docvortex.render._internal.html.sanitizer import (
    is_supported_html_markup,
    sanitize_html_fragment,
    sanitize_image_source,
    sanitize_link_url,
)

_SAFE_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2l9sAAAAASUVORK5CYII="


def _generated_svg_data_uri(extra_markup: str = "") -> str:
    """Construct minimum MinerU safe SVG data URI with PNG fallback."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1" '
        'data-metafile-render="wmf-emf">'
        f'<metadata id="metafile-render-raster-fallback" data-mime="image/png">{_SAFE_PNG_BASE64}</metadata>'
        '<path d="M 0 0 L 1 0 L 1 1 Z" fill="#000000"/>'
        f"{extra_markup}</svg>"
    ).encode()
    return f"data:image/svg+xml;base64,{base64.b64encode(svg).decode('ascii')}"


@pytest.mark.parametrize(
    ("current", "obsolete"),
    [
        (b"data-metafile-render", b"data-mineru-generated"),
        (b"metafile-render-raster-fallback", b"mineru-raster-fallback"),
        (b"metafile-render-clip-1", b"mineru-clip-1"),
    ],
)
def test_generated_svg_accepts_new_contract_and_rejects_obsolete_markers(current: bytes, obsolete: bytes) -> None:
    """The consumer only accepts the new SVG identifier for standalone packages and does not retain old root attributes, metadata or clipping references."""
    data_uri = _generated_svg_data_uri(
        '<defs><clipPath id="metafile-render-clip-1"><path d="M 0 0 L 1 0 L 1 1 Z"/></clipPath></defs>'
        '<g clip-path="url(#metafile-render-clip-1)"><path d="M 0 0 L 1 1"/></g>'
    )
    payload = base64.b64decode(data_uri.split(",", 1)[1])
    fallback, width, height = image_payload.extract_generated_svg_fallback(payload)
    assert fallback.startswith(b"\x89PNG")
    assert (width, height) == (1, 1)
    with pytest.raises(ValueError):
        image_payload.extract_generated_svg_fallback(payload.replace(current, obsolete))


@pytest.mark.parametrize(
    "content",
    [
        "<table><tr><td>A</td></tr></table>",
        "<eq>x</eq>",
        "<script>alert(1)</script>",
        '<img src="images/a.png">',
    ],
)
def test_supported_html_markup_detects_renderable_and_active_tags(content: str) -> None:
    """Verify that renderable tags and active tags that need to be deleted entirely will enter the security layer."""
    assert is_supported_html_markup(content)


@pytest.mark.parametrize(
    "content",
    [
        "plain text",
        "<local_dir> p <0.05 and x > 0",
        "<custom-wrapper>visible</custom-wrapper>",
        "literal <b> token",
        "Use <img> tag",
        "Example <script> tag",
        "&lt;table&gt;not markup&lt;/table&gt;",
    ],
)
def test_supported_html_markup_keeps_ordinary_angle_brackets_as_text(content: str) -> None:
    """Verify that unknown angle bracket text is not misinterpreted as HTML."""
    assert not is_supported_html_markup(content)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("docs/page.html", "docs/page.html"),
        ("/docs/page.html", "/docs/page.html"),
        ("#section", "#section"),
        ("?page=2", "?page=2"),
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("http://example.com", "http://example.com"),
        ("mailto:user@example.com", "mailto:user@example.com"),
        ("tel:+8610000", "tel:+8610000"),
    ],
)
def test_sanitize_link_url_allows_document_links(url: str, expected: str) -> None:
    """Verify that in-document links and explicitly allowed external protocols are preserved."""
    assert sanitize_link_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "javascript&colon;alert(1)",
        "java\nscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "blob:https://example.com/id",
        "//evil.example/path",
        "\\evil.example\\path",
        "http:relative",
        "https://",
        "mailto:",
        "https://example.com/\ud800",
        "",
    ],
)
def test_sanitize_link_url_rejects_dangerous_or_invalid_urls(url: str) -> None:
    """Verify executable, local file, protocol relative and invalid URL rejected."""
    assert sanitize_link_url(url) is None


def test_sanitize_image_source_rewrites_only_safe_relative_sidecars() -> None:
    """Verify that only secure relative sidecar paths apply to the resource root address."""
    base = "https://cdn.example/doc"

    assert sanitize_image_source("images/a b.png", asset_base_url=base) == ("https://cdn.example/doc/images/a%20b.png")
    assert sanitize_image_source("/shared/a.png", asset_base_url=base) == "/shared/a.png"
    assert sanitize_image_source("https://images.example/a.png", asset_base_url=base) == ("https://images.example/a.png")


@pytest.mark.parametrize(
    "source",
    [
        "../secret.png",
        "images/../secret.png",
        "images/%2e%2e/secret.png",
        "images/%252e%252e/secret.png",
        r"images\..\secret.png",
        "file:///tmp/a.png",
        "blob:https://example.com/id",
        "//evil.example/a.png",
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,AAAA",
        "data:image/png;base64,not-base64!",
        "#image",
        "?image=1",
        "",
    ],
)
def test_sanitize_image_source_rejects_unsafe_sources(source: str) -> None:
    """Validation path escapes, active content, and non-raster data URI rejected."""
    assert sanitize_image_source(source, asset_base_url="assets") is None


@pytest.mark.parametrize(
    "source",
    [
        "data:image/png;base64,iVBORw0KGgo=",
        "data:image/jpeg;base64,/9j/",
        "data:image/gif;base64,R0lGODlh",
        "data:image/webp;base64,UklGRgAAAABXRUJQ",
    ],
)
def test_sanitize_image_source_allows_strict_raster_data_uris(source: str) -> None:
    """Common raster plots base64 data URI that verify correct syntax are retained."""
    assert sanitize_image_source(source, asset_base_url="https://cdn.example/doc") == source


@pytest.mark.parametrize(
    "asset_base_url",
    [
        "javascript:alert(1)",
        "data:text/html,boom",
        "../escape",
        "//evil.example/assets",
        r"\evil.example\assets",
        "https://cdn.example/%252e%252e/escape",
        "https://cdn.example/assets?redirect=evil",
    ],
)
def test_sanitize_image_source_rejects_malicious_asset_bases(asset_base_url: str) -> None:
    """Verify that the malicious resource root address is not spliced to the secure sidecar path."""
    assert sanitize_image_source("images/a.png", asset_base_url=asset_base_url) is None


def test_sanitize_html_fragment_keeps_structure_and_bounded_attributes() -> None:
    """Verify that table, list, rich text, and legal numeric attributes are preserved."""
    markup = (
        "<div><blockquote><p><b>B</b><strong>S</strong><i>I</i><em>E</em><u>U</u>"
        "<s>D</s><sub>1</sub><sup>2</sup><code>C</code><br><span>T</span></p></blockquote>"
        '<table><colgroup span="1000"><col span="0"></colgroup><thead><tr>'
        '<th colspan="2" rowspan="1001">H</th></tr></thead><tbody><tr><td rowspan="1">A</td>'
        "</tr></tbody><tfoot><tr><td>B</td></tr></tfoot></table>"
        '<ol start="-1000000"><li value="+0002">two</li><li value="1000001">bad</li></ol>'
        "<ul><li>bullet</li></ul></div>"
    )

    soup = BeautifulSoup(sanitize_html_fragment(markup), "html.parser")

    assert soup.find("table") is not None
    assert soup.find("blockquote") is not None
    assert not soup.find("colgroup").has_attr("span")
    assert not soup.find("col").has_attr("span")
    assert soup.find("th")["colspan"] == "2"
    assert not soup.find("th").has_attr("rowspan")
    assert soup.find("td")["rowspan"] == "1"
    assert soup.find("ol")["start"] == "-1000000"
    assert soup.find_all("ol")[0].find_all("li")[0]["value"] == "2"
    assert not soup.find_all("ol")[0].find_all("li")[1].has_attr("value")


def test_sanitize_html_fragment_strips_source_presentation_and_clobbering_attributes() -> None:
    """Verify that the source style, event, DOM, clobbering and data attributes are all removed."""
    markup = (
        '<table id="location" name="cookie" class="evil" style="position:fixed" data-x="1">'
        '<tr><td onclick="alert(1)"><span id="forms" class="x" style="color:red">safe</span></td></tr>'
        '</table><a href="/docs" target="_blank" onclick="alert(1)" id="x">docs</a>'
        '<img src="images/a.png" alt="preview" class="x" style="x" onerror="alert(1)" width="999">'
    )

    rendered = sanitize_html_fragment(markup, asset_base_url="assets")
    soup = BeautifulSoup(rendered, "html.parser")

    assert soup.find("a").attrs == {"href": "/docs"}
    assert soup.find("img").attrs == {"alt": "preview", "src": "assets/images/a.png"}
    for tag in soup.find_all(True):
        assert not ({"class", "id", "name", "style"} & tag.attrs.keys())
        assert not any(name.startswith("data-") or name.startswith("on") for name in tag.attrs)


def test_sanitize_html_fragment_removes_active_elements_with_contents() -> None:
    """Validate activity, form and media tags along with content removal."""
    paired_tags = [
        "audio",
        "button",
        "canvas",
        "form",
        "iframe",
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
    ]
    markup = "".join(f"<{tag}>payload-{tag}</{tag}>" for tag in paired_tags)
    markup += '<embed src="x"><input value="payload-input"><p>safe</p>'

    rendered = sanitize_html_fragment(markup)

    assert rendered == "<p>safe</p>"


def test_sanitize_html_fragment_unwraps_unknown_safe_elements() -> None:
    """Verification Unknown Inactive wrapper Only removes tags without losing visible content."""
    markup = '<custom-wrapper onclick="alert(1)">before<strong>safe</strong>after</custom-wrapper>'

    assert sanitize_html_fragment(markup) == "before<strong>safe</strong>after"


def test_sanitize_html_fragment_repairs_list_colgroup_and_image_content_models() -> None:
    """Verify that orphaned list items, illegal direct children, colgroup span and missing alt are normalized."""
    markup = (
        "<li>orphan</li><ol>text<div>block</div><li>ok</li></ol>"
        "<span>before<li>phrasing orphan</li>after</span>"
        '<table><colgroup span="2"><col></colgroup><tr><td><img src="images/a.png"></td></tr></table>'
    )
    soup = BeautifulSoup(sanitize_html_fragment(markup), "html.parser")

    orphan = soup.find("li", string="orphan")
    assert orphan.parent.name == "ul"
    phrasing_orphan = soup.find("li", string="phrasing orphan")
    assert phrasing_orphan.parent.name == "ul"
    assert phrasing_orphan.find_parent("span") is None
    assert all(isinstance(child, Tag) and child.name == "li" for child in soup.find("ol").children)
    assert not soup.find("colgroup").has_attr("span")
    assert soup.find("img").attrs == {"alt": "", "src": "images/a.png"}


def test_unmarked_svg_data_uri_is_rejected() -> None:
    """Verifying that plain or spoofed SVG still cannot bypass the MinerU security subset."""
    assert sanitize_image_source("data:image/svg+xml;base64,PHN2Zz4=") is None


def test_mineru_generated_svg_data_uri_is_allowed() -> None:
    """Verification MinerU SVG with strict PNG fallback can be used with HTML pictures."""
    source = _generated_svg_data_uri()
    assert sanitize_image_source(source) == source


def test_mineru_generated_svg_rejects_dtd_beyond_prefix_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that DTD of any offset and encoding cannot bypass the HTML SVG security check."""
    original_fromstring = image_payload.ElementTree.fromstring
    parser_calls: list[object] = []

    def guarded_fromstring(payload: bytes, parser: object | None = None) -> object:
        """Asserting SVG The first XML parse has used parser which rejects DTD."""
        assert parser is not None
        parser_calls.append(parser)
        return original_fromstring(payload, parser=parser)  # type: ignore[arg-type,return-value]

    monkeypatch.setattr(image_payload.ElementTree, "fromstring", guarded_fromstring)
    safe_svg = base64.b64decode(_generated_svg_data_uri().split(",", 1)[1])
    late_doctype = (
        b" " * 4097
        + b'<!DOCTYPE svg [<!ENTITY injected "expanded">]>'
        + safe_svg.replace(b"</svg>", b'<text x="0" y="0" fill="#000">&injected;</text></svg>')
    )
    utf16_doctype = (
        '<!DOCTYPE svg [<!ENTITY injected "expanded">]>'
        + safe_svg.decode("utf-8").replace("</svg>", '<text x="0" y="0" fill="#000">&injected;</text></svg>')
    ).encode("utf-16")

    for payload in (late_doctype, utf16_doctype):
        source = f"data:image/svg+xml;base64,{base64.b64encode(payload).decode('ascii')}"
        assert sanitize_image_source(source) is None
    assert len(parser_calls) == 2


def test_svg_data_uri_rejects_encoded_oversize_before_xml_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SVG that clearly exceeds the verification limit is rejected before base64 is decoded and XML is parsed."""
    xml_parser = Mock(side_effect=AssertionError("oversized SVG must not reach XML parsing"))
    monkeypatch.setattr(image_payload, "MAX_GENERATED_SVG_BYTES", 1)
    monkeypatch.setattr(image_payload.ElementTree, "fromstring", xml_parser)

    assert sanitize_image_source(_generated_svg_data_uri()) is None
    xml_parser.assert_not_called()


@pytest.mark.parametrize(
    "extra_markup",
    [
        "<script>alert(1)</script>",
        '<image width="1" height="1" href="https://evil.example/a.png"/>',
        "<foreignObject><div>unsafe</div></foreignObject>",
    ],
)
def test_mineru_svg_marker_does_not_bypass_active_content_checks(extra_markup: str) -> None:
    """Verification MinerU marker Unable to release script, external link or foreignObject."""
    assert sanitize_image_source(_generated_svg_data_uri(extra_markup)) is None


def test_sanitize_html_fragment_preserves_safe_equation_as_plain_text() -> None:
    """Verify that eq is retained as a subsequent formula carrier, and the labels in it are only processed as plain text."""
    markup = "<p>before <eq>x&lt;y &lt;/script&gt;<b>z</b></eq> after</p>"

    rendered = sanitize_html_fragment(markup)
    soup = BeautifulSoup(rendered, "html.parser")

    assert soup.find("eq") is not None
    assert soup.find("eq").get_text() == "x<y </script>z"
    assert "&lt;/script&gt;" in rendered
    assert soup.find("eq").find(True) is None


def test_sanitize_html_fragment_degrades_dangerous_links_and_images_visibly() -> None:
    """Validating dangerous links leaves label, and dangerous images leaves escaped alt."""
    markup = (
        '<a href="javascript&colon;alert(1)"><strong>label</strong></a>'
        '<img src="data:image/svg+xml;base64,PHN2Zz4=" alt="&lt;unsafe&gt;">'
    )

    rendered = sanitize_html_fragment(markup)

    assert rendered == "<strong>label</strong>&lt;unsafe&gt;"
    assert "javascript" not in rendered
    assert "<img" not in rendered


def test_sanitize_html_fragment_handles_malformed_mutation_xss() -> None:
    """Verification malformation SVG/MathML/script and attribute combination does not bypass secondary parsing."""
    markup = (
        '<svg><style><img src="x" onerror="alert(1)"></style></svg>'
        '<math><mtext><img src="x" onerror="alert(2)"></mtext></math>'
        '<table><tr><td><a href="java&#x0A;script:alert(3)">link</a>'
        '<img src="images/a.png" onerror="alert(4)"></td></tr></table>'
        '<iframe srcdoc="&lt;script&gt;alert(5)&lt;/script&gt;">frame</iframe>'
        "<!-- <script>alert(6)</script> -->"
    )

    rendered = sanitize_html_fragment(markup, asset_base_url="assets")
    reparsed = BeautifulSoup(rendered, "html.parser")

    assert reparsed.get_text(strip=True) == "link"
    assert reparsed.find("a") is None
    assert reparsed.find("img")["src"] == "assets/images/a.png"
    lowered = rendered.lower()
    for forbidden in ("<script", "<style", "<svg", "<math", "<iframe", "onerror", "javascript"):
        assert forbidden not in lowered


def test_sanitize_html_fragment_escapes_malicious_titles_and_alt_text() -> None:
    """Validate quotes and labels in title/alt as attributes or visible text only."""
    markup = (
        '<a href="https://example.com" title="&quot; onmouseover=&quot;alert(1)">safe</a>'
        '<img src="javascript:alert(2)" alt="&lt;img src=x onerror=alert(3)&gt;">'
    )

    rendered = sanitize_html_fragment(markup)
    soup = BeautifulSoup(rendered, "html.parser")

    assert soup.find("a")["title"] == '" onmouseover="alert(1)'
    assert not soup.find("a").has_attr("onmouseover")
    assert soup.find("img") is None
    assert soup.get_text().endswith("<img src=x onerror=alert(3)>")
    assert "&lt;img src=x onerror=alert(3)&gt;" in rendered


def test_sanitize_html_fragment_rejects_invalid_argument_types() -> None:
    """Verify that the security layer does not implicitly accept non-string input."""
    with pytest.raises(TypeError, match="markup"):
        sanitize_html_fragment(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="asset_base_url"):
        sanitize_html_fragment("<p>x</p>", asset_base_url=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="url"):
        sanitize_link_url(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source"):
        sanitize_image_source(None)  # type: ignore[arg-type]
