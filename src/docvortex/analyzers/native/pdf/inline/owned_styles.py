"""Only fresh source page evidence uses the snapshot style kernel, and public variable input still uses the original entrance."""

import math
from functools import lru_cache

from ....._compute_backend import get_native
from ..models import _AxisLine, _LineItem
from . import detection as reference


@lru_cache(maxsize=4096)
def _snapshot_text_properties(text):
    """The host interpreter's match, visible, and bullet properties are evaluated only once per Unicode text value."""
    fragment = reference._normalize_match_fragment(text)
    return (
        fragment,
        text.isprintable() and not text.isspace(),
        text.isspace(),
        bool(fragment) and all(char in reference._PDF_LIST_MARKER_CHARS for char in fragment),
    )


def _snapshot_font_bold(name, flags, weight):
    """Reuse the original regular rules and font judgment, and characters with different font sizes but the same name/flags/weight share the results."""
    name = reference._PDF_FONT_SUBSET_PREFIX_RE.sub("", name)
    return "bold" in reference._font_styles_from_metadata(name, flags, weight if weight > 0 else None)


def _plain_box(box):
    """Private homologous data only has access to limited common coordinates, and special objects retain reference conversion and error semantics."""
    return (
        type(box) in (tuple, list)
        and len(box) == 4
        and all(type(v) is float and math.isfinite(v) and abs(v) <= 1e100 for v in box)
    )


def detect_owned_style_lines(owner, lines, drawings, identities):
    """Only the row box, member index and plot line are passed and the public style row is finally constructed; capability not applicable returns None."""
    native = get_native()
    if native is None or type(owner) is not native.NativeTextSnapshot or identities is None:
        return None
    rows = []
    for line in lines:
        if type(line) is not _LineItem or type(line.angle) is not int:
            return None
        if line.angle % 360:
            continue
        if type(line.source_index) is not int or not -(2**63) <= line.source_index < 2**63 or not _plain_box(line.bbox):
            return None
        if type(line.chars) is not list:
            return None
        members = [identities.get(id(char)) for char in line.chars]
        if any(index is None for index in members):
            return None
        rows.append((line.bbox, line.source_index, members))
    rules = []
    for drawing in drawings:
        if type(drawing) is not _AxisLine or type(drawing.orientation) is not str:
            return None
        if drawing.orientation != "horizontal":
            continue
        if not _plain_box(drawing.bbox) or type(drawing.width) not in (float, int) or not math.isfinite(drawing.width):
            return None
        rules.append((drawing.bbox, drawing.width))
    thresholds = (
        reference.TEXT_DECORATION_MIN_LENGTH_HEIGHT_RATIO,
        reference.UNDERLINE_BOTTOM_TOLERANCE_HEIGHT_RATIO,
        reference.STRIKETHROUGH_CENTER_TOLERANCE_HEIGHT_RATIO,
        reference.TEXT_DECORATION_MAX_WIDTH_HEIGHT_RATIO,
        reference.TEXT_DECORATION_MIN_TEXT_COVERAGE_RATIO,
        reference.TEXT_DECORATION_ENDPOINT_TOLERANCE_HEIGHT_RATIO,
        reference.UNDERLINE_FRACTION_MAX_GAP_HEIGHT_RATIO,
        reference.UNDERLINE_FRACTION_MIN_LOWER_LINE_COVERAGE,
    )
    result = owner.detect_style_lines(
        rows, rules, _snapshot_text_properties, _snapshot_font_bold, thresholds, reference.PDF_BOLD_MIN_COMPARABLE_CHAR_COUNT
    )
    if result is None:
        return None
    styles = {
        mask: tuple(style for bit, style in ((1, "bold"), (2, "underline"), (4, "strikethrough")) if mask & bit)
        for mask in range(8)
    }
    return [
        reference.PDFTextStyleLine(
            tuple(box),
            text,
            tuple(reference.PDFTextStyleRange(start, end, styles[mask]) for start, end, mask in ranges),
            source,
        )
        for box, text, ranges, source in result
    ]
