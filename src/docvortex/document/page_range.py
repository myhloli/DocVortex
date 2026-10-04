"""Page range syntax, evaluation, and formatting shared by all portals; default paging policy determined by the caller."""

from __future__ import annotations

import re
from collections.abc import Iterable

from loguru import logger

from ..errors import InvalidRequestError

PAGE_RANGE_DESCRIPTION = (
    "PDF page selection: 1-based inclusive ranges, e.g. '1-5,8,r3-r1'; "
    "'r1' is the last page and 'all' selects every page. Blank means unspecified. "
    "Selections are sorted and deduplicated; out-of-bounds pages are omitted. "
    "Reversed ranges, '~' and negative page numbers are invalid."
)
_SEGMENT_PATTERN = re.compile(r"(r?[1-9][0-9]*)(?:\s*-\s*(r?[1-9][0-9]*))?")


def get_end_page_id(end_page_id: int | None, pdf_page_num: int) -> int:
    """Normalize the 0-based end page of the old CLI, and clamp to the last page when out of bounds."""
    normalized_end_page_id = end_page_id if end_page_id is not None and end_page_id >= 0 else pdf_page_num - 1
    if normalized_end_page_id > pdf_page_num - 1:
        logger.debug("end_page_id is out of range, use images length")
        normalized_end_page_id = pdf_page_num - 1
    return normalized_end_page_id


def _invalid_range(raw: str | None, reason: str) -> InvalidRequestError:
    """Unified generation of page number errors that are propagated across Python, CLI, and HTTP."""
    return InvalidRequestError("page_range_invalid", f"Invalid page range {raw!r}: {reason}", "page_range")


def _parse_endpoint(token: str) -> int:
    """Marks the reciprocal endpoint internally with a negative integer, external input only accepts rN."""
    return -int(token[1:]) if token.startswith("r") else int(token)


def _parse_segments(raw: str | None) -> list[tuple[int, int]] | None:
    """Parse the full expression; None means all, with the reciprocal endpoint left to be evaluated after the total number of pages has been obtained."""
    value = (raw or "").strip()
    if not value or value == "all":
        return None
    segments: list[tuple[int, int]] = []
    for part in value.split(","):
        match = _SEGMENT_PATTERN.fullmatch(part.strip())
        if match is None:
            raise _invalid_range(raw, "expected a page, an inclusive range, or 'all'")
        try:
            start = _parse_endpoint(match[1])
            end = _parse_endpoint(match[2]) if match[2] else start
        except ValueError as exc:
            raise _invalid_range(raw, "page number is too large") from exc
        if (start > 0) == (end > 0) and start > end:
            raise _invalid_range(raw, "reversed ranges are not supported")
        segments.append((start, end))
    return segments


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge evaluated ranges, sort, deduplicate, and join adjacent ranges without page-by-page expansion."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _format_endpoint(value: int) -> str:
    """Convert internal endpoint to positive integer or rN text."""
    return str(value) if value > 0 else f"r{-value}"


def _format_intervals(intervals: Iterable[tuple[int, int]]) -> str:
    """Generate range text without white space, and only output one endpoint on a single page."""
    return ",".join(
        _format_endpoint(start) if start == end else f"{_format_endpoint(start)}-{_format_endpoint(end)}"
        for start, end in intervals
    )


def normalize_page_range_input(raw: str | None) -> str:
    """Verify the input of unbound documents, clean up the blanks and retain all/rN; empty values are unified as unspecified."""
    segments = _parse_segments(raw)
    if segments is None:
        return "all" if (raw or "").strip() else ""
    if all(start > 0 and end > 0 for start, end in segments):
        segments = _merge_intervals(segments)
    return _format_intervals(segments)


def _resolved_intervals(raw: str | None, page_count: int) -> list[tuple[int, int]]:
    """Evaluate and crop based on the total number of pages in the document. First verify all intervals and then merge valid intersections."""
    segments = _parse_segments(raw)
    if page_count <= 0:
        raise _invalid_range(raw, "document has no available pages")
    if segments is None:
        return [(1, page_count)]
    intervals: list[tuple[int, int]] = []
    for start, end in segments:
        start = start if start > 0 else page_count + start + 1
        end = end if end > 0 else page_count + end + 1
        if start > end:
            raise _invalid_range(raw, "reversed ranges are not supported")
        lo, hi = max(1, start), min(page_count, end)
        if lo <= hi:
            intervals.append((lo, hi))
    if not intervals:
        raise _invalid_range(raw, "selection does not contain any available pages")
    return _merge_intervals(intervals)


def parse_page_range(raw: str, page_count: int) -> list[int]:
    """Converts the 1-based expression to a 0-based page index in deduplicated ascending order, with a null value selecting all."""
    return [page_idx for start, end in _resolved_intervals(raw, page_count) for page_idx in range(start - 1, end)]


def expand_page_range(raw: str | None, page_count: int) -> str:
    """Expands expressions containing all/rN to the positive integer specification range of the actual document."""
    return _format_intervals(_resolved_intervals(raw, page_count))


def _absolute_intervals(raw: str) -> list[tuple[int, int]]:
    """Read the evaluated range, compatible with historical half-width ~; empty text is an empty set, prohibited all/rN."""
    if not raw.strip():
        return []
    # Old delimiters are only compatible with result read boundaries; new requests and full-width tildes are still rejected by strict syntax.
    segments = _parse_segments(raw.replace("~", "-"))
    if segments is None or any(start < 0 or end < 0 for start, end in segments):
        raise _invalid_range(raw, "page count is required to resolve all/rN")
    return _merge_intervals(segments)


def normalize_result_page_range(raw: str) -> str:
    """Normalizes the old and new result ranges of the evaluation into hyphenated format, without modifying the persisted record or file name."""
    return _format_intervals(_absolute_intervals(raw))


def parse_page_range_set(raw: str) -> set[int]:
    """Convert the evaluated range into a set of 1-based page numbers for use by cache overrides and content filtering."""
    return {page_no for start, end in _absolute_intervals(raw) for page_no in range(start, end + 1)}


def count_pages_in_range(raw: str) -> int:
    """Counts the number of unique pages of the evaluated range, no page-by-page collection is allocated for the statistics."""
    return sum(end - start + 1 for start, end in _absolute_intervals(raw))


def format_page_range(page_numbers: Iterable[int]) -> str:
    """Format the 1-based page number collection into a continuous range in ascending order without duplication; an empty collection outputs an empty string."""
    return _format_intervals(_merge_intervals((page_no, page_no) for page_no in page_numbers))


__all__ = [
    "PAGE_RANGE_DESCRIPTION",
    "count_pages_in_range",
    "expand_page_range",
    "format_page_range",
    "get_end_page_id",
    "normalize_page_range_input",
    "normalize_result_page_range",
    "parse_page_range",
    "parse_page_range_set",
]
