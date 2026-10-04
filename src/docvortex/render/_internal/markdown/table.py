"""Markdown renderer's HTML table type determination and lossless GFM conversion."""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from ....options import LatexDelimitersConfig
from .assets import prefix_html_image_sources
from .inline import markdown_styles_require_html, render_styled_markdown_text

_INLINE_EQ_RE = re.compile(r"<eq>(?P<latex>.*?)</eq>", re.IGNORECASE | re.DOTALL)
_GFM_FORMULA_PIPE_RE = re.compile(r"(?P<slashes>\\*)\|")
_COMPLEX_CELL_TAGS = {
    "blockquote",
    "div",
    "dl",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "img",
    "li",
    "math",
    "object",
    "ol",
    "pre",
    "svg",
    "table",
    "ul",
}
_ALLOWED_INLINE_TAGS = {
    "a",
    "b",
    "br",
    "code",
    "em",
    "eq",
    "i",
    "p",
    "s",
    "span",
    "strong",
    "sub",
    "sup",
    "u",
}
_STYLE_TAGS = {
    "b": "bold",
    "strong": "bold",
    "em": "italic",
    "i": "italic",
    "u": "underline",
    "s": "strikethrough",
    "sup": "superscript",
    "sub": "subscript",
}


def _strip_embedded_images(markup: str) -> str:
    """Remove the HTML image taken over by the custom image renderer and identify the cleaned empty content."""
    soup = BeautifulSoup(markup, "html.parser")
    images = soup.find_all("img")
    if not images:
        return markup
    for image in images:
        image.decompose()
    if not soup.get_text(strip=True):
        return ""
    return str(soup)


def render_html_table(
    content: str,
    *,
    asset_base_url: str,
    delimiters: LatexDelimitersConfig,
) -> str | None:
    """Output the original HTML or convert HTML table into the GFM table according to complexity."""
    prefixed = prefix_html_image_sources(content, asset_base_url)
    soup = BeautifulSoup(prefixed, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return None
    table = tables[0]
    if len(tables) != 1 or _is_complex_table(table):
        return format_embedded_html(prefixed, asset_base_url="", delimiters=delimiters).strip()
    markdown = _convert_simple_table(table, delimiters)
    if markdown is not None:
        return markdown
    return format_embedded_html(prefixed, asset_base_url="", delimiters=delimiters).strip()


def format_embedded_html(
    markup: str,
    *,
    asset_base_url: str,
    delimiters: LatexDelimitersConfig,
) -> str:
    """Unified processing of image addresses and inline formula tags embedded in HTML."""
    prefixed = prefix_html_image_sources(markup, asset_base_url)

    def _replace_inline_equation(match: re.Match[str]) -> str:
        """Replaces a single HTML eq tag with the configured inline formula delimiter."""
        return f" {delimiters.inline.left}{html.unescape(match.group('latex')).strip()}{delimiters.inline.right} "

    return _INLINE_EQ_RE.sub(_replace_inline_equation, prefixed)


def _is_complex_table(table: Tag) -> bool:
    """Determine whether the table contains structures that cannot be expressed losslessly by GFM."""
    if table.find("table") is not None:
        return True
    thead = table.find("thead")
    if thead is not None and len(thead.find_all("tr", recursive=False)) > 1:
        return True
    for cell in table.find_all(("th", "td")):
        if cell.has_attr("rowspan") or cell.has_attr("colspan"):
            return True
        if len(cell.find_all("p")) > 1:
            return True
        for descendant in cell.descendants:
            if not isinstance(descendant, Tag):
                continue
            if descendant.name in _COMPLEX_CELL_TAGS:
                return True
            if descendant.name not in _ALLOWED_INLINE_TAGS:
                return True
            if descendant.name == "span" and descendant.attrs:
                return True
    return False


def _convert_simple_table(table: Tag, delimiters: LatexDelimitersConfig) -> str | None:
    """Converts confirmed simple single layer HTML table to GFM."""
    rows = table.find_all("tr")
    if not rows:
        return None
    rendered_rows: list[list[str]] = []
    header_flags: list[list[bool]] = []
    for row in rows:
        cells = row.find_all(("th", "td"), recursive=False)
        if not cells:
            return None
        rendered_rows.append([_normalize_cell_text(_render_inline_children(cell, delimiters)) for cell in cells])
        header_flags.append([cell.name == "th" for cell in cells])

    width = max(len(row) for row in rendered_rows)
    for row in rendered_rows:
        row.extend([""] * (width - len(row)))
    header_index = _detect_header_index(table, header_flags)
    header = rendered_rows[header_index]
    body = rendered_rows[:header_index] + rendered_rows[header_index + 1 :]
    return "\n".join(
        [
            _format_markdown_row(header),
            _format_markdown_row(["---"] * width),
            *[_format_markdown_row(row) for row in body],
        ]
    )


def _detect_header_index(table: Tag, header_flags: list[list[bool]]) -> int:
    """Priority is given to selecting thead or all th rows, otherwise the first row is used as the GFM header."""
    thead = table.find("thead")
    if thead is not None and thead.find("tr", recursive=False) is not None:
        return 0
    for index, flags in enumerate(header_flags):
        if flags and all(flags):
            return index
    return 0


def _render_inline_children(
    node: Tag,
    delimiters: LatexDelimitersConfig,
    inherited_styles: tuple[str, ...] = (),
) -> str:
    """Recursively render safe inline HTML in simple cells."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            escaped = _escape_cell_text(str(child))
            parts.append(render_styled_markdown_text(escaped, inherited_styles))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        style = _STYLE_TAGS.get(name)
        styles = tuple(dict.fromkeys((*inherited_styles, style))) if style is not None else inherited_styles
        rendered = _render_inline_children(child, delimiters, styles)
        if name in {"p", "span"}:
            parts.append(rendered)
        elif name == "br":
            parts.append("<br>")
        elif name == "code":
            parts.append(_render_inline_code(child.get_text()))
        elif name == "eq":
            latex = html.unescape(child.get_text()).strip()
            escaped_latex = _escape_gfm_formula_pipes(latex)
            parts.append(f"{delimiters.inline.left}{escaped_latex}{delimiters.inline.right}" if escaped_latex else "")
        elif name == "a":
            href = str(child.get("href", "")).strip()
            if not href:
                parts.append(rendered)
            elif _node_has_complex_text_style(child, inherited_styles):
                parts.append(f'<a href="{html.escape(href, quote=True)}">{rendered}</a>')
            else:
                parts.append(f"[{rendered}]({_escape_link_url(href)})")
        elif name in _STYLE_TAGS:
            parts.append(rendered)
        else:
            parts.append(rendered)
    return "".join(parts)


def _node_has_complex_text_style(node: Tag, inherited_styles: tuple[str, ...]) -> bool:
    """Determines whether a node's descendants contain a valid text style combination that must be expressed using HTML."""
    for child in node.children:
        if isinstance(child, NavigableString):
            if str(child) and markdown_styles_require_html(inherited_styles):
                return True
            continue
        if not isinstance(child, Tag):
            continue
        style = _STYLE_TAGS.get(child.name)
        styles = tuple(dict.fromkeys((*inherited_styles, style))) if style is not None else inherited_styles
        if _node_has_complex_text_style(child, styles):
            return True
    return False


def _render_inline_code(content: str) -> str:
    """Use long enough backticks to wrap code inside table cells."""
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(1, longest + 1)
    return f"{fence}{_escape_cell_text(content)}{fence}"


def _escape_gfm_formula_pipes(latex: str) -> str:
    """Escape formula vertical bars and ensure that the original number of backslashes is restored after GFM parsing."""

    def _replace(match: re.Match[str]) -> str:
        """Expand n backslashes before the vertical bar to 2n+1 Markdown backslashes."""
        slash_count = len(match.group("slashes"))
        return "\\" * (2 * slash_count + 1) + "|"

    return _GFM_FORMULA_PIPE_RE.sub(_replace, latex)


def _escape_link_url(url: str) -> str:
    """Escape spaces, backslashes, and parentheses in GFM table link targets."""
    return url.replace("\\", "%5C").replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("|", "%7C")


def _escape_cell_text(content: str) -> str:
    """Escape HTML, backslashes, and pipe characters in the GFM cell to prevent source text from being injected into the active label."""
    escaped_html = content.replace("&", "&amp;").replace("<", "&lt;")
    return escaped_html.replace("\\", "\\\\").replace("|", r"\|")


def _normalize_cell_text(content: str) -> str:
    """Compress normal whitespace while preserving explicit br newlines."""
    content = re.sub(r"[ \t\r\f\v]+", " ", content)
    content = re.sub(r" *\n+ *", " ", content)
    content = re.sub(r" *<br> *", "<br>", content)
    return content.strip()


def _format_markdown_row(row: list[str]) -> str:
    """Format a row of cells as row GFM."""
    return f"| {' | '.join(row)} |"


__all__ = ["format_embedded_html", "render_html_table"]
