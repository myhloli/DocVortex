"""Parse DOC field instructions and safely restore hyperlinks, directories, and caption semantics."""

from __future__ import annotations

import re

from loguru import logger

from ..._shared.hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target
from .models import DocTextRun

_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\\\S|\S+')


def field_keyword(instruction: str) -> str:
    """Return the first keyword capital form of the field instruction."""

    tokens = _TOKEN_RE.findall(instruction.strip())
    return tokens[0].strip('"').upper() if tokens else ""


def is_toc_field(instruction: str) -> bool:
    """Determine whether the field is multi-paragraph TOC."""

    return field_keyword(instruction) == "TOC"


def is_caption_field(instruction: str) -> bool:
    """Determine whether the field is Word SEQ caption number."""

    return field_keyword(instruction) == "SEQ"


def is_chart_embed_field(instruction: str) -> bool:
    """Determine whether the EMBED field declares a Excel.Chart or MSGraph.Chart object."""

    tokens = _TOKEN_RE.findall(instruction.strip())
    if len(tokens) < 2 or _unquote(tokens[0]).casefold() != "embed":
        return False
    prog_id = _unquote(tokens[1]).casefold()
    return prog_id.startswith(("excel.chart", "msgraph.chart"))


def _unquote(token: str) -> str:
    """Decode backslash escapes allowed within field quotes."""

    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    return token.replace(r"\"", '"').replace(r"\\", "\\")


def hyperlink_target(instruction: str) -> str | None:
    """Read URL from HYPERLINK field with optional internal bookmark."""

    tokens = _TOKEN_RE.findall(instruction.strip())
    if not tokens or _unquote(tokens[0]).casefold() != "hyperlink":
        return None
    url: str | None = None
    anchor: str | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("\\"):
            switch = token[1:].casefold()
            argument: str | None = None
            if switch in {"l", "o", "t"} and index + 1 < len(tokens) and not tokens[index + 1].startswith("\\"):
                index += 1
                argument = _unquote(tokens[index]).strip()
            if switch == "l" and argument:
                anchor = argument
        elif url is None:
            url = _unquote(token).strip()
        index += 1
    if url and anchor:
        candidate = f"{url}#{anchor}"
    elif url:
        candidate = url
    elif anchor:
        candidate = f"#{anchor}"
    else:
        return None
    safe = sanitize_hyperlink_target(
        candidate,
        allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
        allow_relative=True,
        allow_fragment=True,
    )
    if safe is None:
        logger.warning(f"DOC hyperlink target was rejected: {candidate!r}")
    return safe


def apply_field_result(instruction: str, runs: list[DocTextRun]) -> list[DocTextRun]:
    """Bind the HYPERLINK target to the field result, and only keep the cached result for other fields."""

    if field_keyword(instruction) != "HYPERLINK":
        return runs
    target = hyperlink_target(instruction)
    if target is None:
        return runs
    return [DocTextRun(run.text, run.style, target, run.formula) for run in runs]
