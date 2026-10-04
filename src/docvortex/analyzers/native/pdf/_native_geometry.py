"""Extract lightweight inputs for batch numerical kernels, preserving the Unicode and object semantics of Python."""

from functools import lru_cache
from ....document.pdf.text._contracts import Bbox


def raw_bbox(value):
    """Read the underlying value of own Bbox, leaving other iterable objects to the original validator."""
    return value.bbox if type(value) is Bbox else value


@lru_cache(maxsize=4096)
def glyph_flags(text: str) -> int:
    """Only caches categories of immutable strings and holds no page or character records."""
    return int(text.isprintable() and not text.isspace()) | (int(text.isspace()) << 1)
