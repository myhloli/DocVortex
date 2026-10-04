"""Strict InlineSpan to safe LaTeX inline source conversion."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import re
from typing import Iterable

from ....content.inline import join_inline_spans
from ....schema import CodeInlineSpan, EquationInlineSpan, HyperlinkSpan, InlineSpan, InlineStyle, TextSpan

_INVALID_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TEXT_ESCAPES = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_URL_ESCAPES = {
    "\\": "/",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "%": r"\%",
    "~": r"\string~",
    "^": r"\string^",
}


@dataclass(slots=True)
class LatexAnchorRegistry:
    """Map the original anchor to the safe and stable target and only launch the first definition."""

    source_targets: set[str]
    emitted: set[str] = field(default_factory=set)

    def target_for(self, anchor: str | None) -> str | None:
        """Only stable TeX target names are returned for real visible targets."""
        normalized = (anchor or "").strip()
        if not normalized or normalized not in self.source_targets:
            return None
        digest = sha256(normalized.encode("utf-8")).hexdigest()[:24]
        return f"docvortex-{digest}"

    def emit_target(self, anchor: str | None) -> str:
        """hypertarget is output when it appears for the first time, and anchor is no longer defined repeatedly."""
        target = self.target_for(anchor)
        if target is None or target in self.emitted:
            return ""
        self.emitted.add(target)
        return rf"\hypertarget{{{target}}}{{}}"


def escape_latex_text(content: str, *, preserve_newlines: bool = True) -> str:
    """Escape all TeX control characters in normal text and preserve Unicode."""
    normalized = _INVALID_TEXT_CONTROL_RE.sub("\ufffd", content).replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for character in normalized:
        if character == "\n":
            parts.append(r"\DocVortexLineBreak{}" if preserve_newlines else " ")
        else:
            parts.append(_TEXT_ESCAPES.get(character, character))
    return "".join(parts)


def escape_latex_url(target: str) -> str:
    """Escapes the hyperref URL parameter while preserving the parsable structure of URL."""
    normalized = _INVALID_TEXT_CONTROL_RE.sub("", target).replace("\r", "").replace("\n", "")
    return "".join(_URL_ESCAPES.get(character, character) for character in normalized)


def render_inline_spans(spans: Iterable[InlineSpan], anchors: LatexAnchorRegistry) -> str:
    """Press span discriminator to render full inline semantics."""
    parts: list[str] = []
    for span in spans:
        if isinstance(span, TextSpan):
            rendered = escape_latex_text(span.content)
            parts.append(_apply_text_styles(rendered, span.styles))
        elif isinstance(span, EquationInlineSpan):
            parts.append(rf"\({span.content}\)")
        elif isinstance(span, CodeInlineSpan):
            parts.append(rf"\texttt{{{_escape_inline_code(span.content)}}}")
        elif isinstance(span, HyperlinkSpan):
            label = render_inline_spans(span.content, anchors)
            if span.url.startswith("#"):
                target = anchors.target_for(span.url[1:])
                parts.append(rf"\hyperlink{{{target}}}{{{label}}}" if target else label)
            else:
                parts.append(rf"\href{{{escape_latex_url(span.url)}}}{{{label}}}")
        else:
            raise TypeError(f"Unsupported InlineSpan type: {type(span).__name__}")
    return "".join(parts)


def render_joined_inline_contents(
    contents: Iterable[Iterable[InlineSpan]],
    anchors: LatexAnchorRegistry,
) -> str:
    """Merge and render multiple segments by shared physical segment boundary rules InlineSpan."""
    return render_inline_spans(join_inline_spans(contents), anchors)


def _apply_text_styles(content: str, styles: list[InlineStyle]) -> str:
    """Nest LaTeX text styles in a public fixed order."""
    wrappers = {
        "bold": r"\textbf{%s}",
        "italic": r"\textit{%s}",
        "underline": r"\uline{%s}",
        "emphasis": r"\CJKunderdot[format=\normalcolor]{%s}",
        "strikethrough": r"\sout{%s}",
        "superscript": r"\textsuperscript{%s}",
        "subscript": r"\textsubscript{%s}",
    }
    rendered = content
    for style in reversed(styles):
        wrapper = wrappers.get(style)
        if wrapper is None:
            raise TypeError(f"Unsupported inline style: {style}")
        rendered = wrapper % rendered
    return rendered


def _escape_inline_code(content: str) -> str:
    """Escape inline code and convert consecutive spaces and Tab into equal-width visible spaces."""
    expanded = content.replace("\t", "    ")
    return escape_latex_text(expanded).replace(" ", r"\ ")


__all__ = [
    "LatexAnchorRegistry",
    "escape_latex_text",
    "escape_latex_url",
    "render_inline_spans",
    "render_joined_inline_contents",
]
