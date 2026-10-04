"""Pure data normalization of source attributes, without reading host configuration or inferring missing values."""

from __future__ import annotations

from datetime import date, datetime
import re

from ..schema import DocumentProperties


def property_text(value: object) -> str | None:
    """Normalizes actual text values and refuses to convert containers and arbitrary objects into pseudo properties."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip().replace("\x00", "") or None if isinstance(value, str) else None


def property_values(value: object) -> list[str]:
    """Convert a source scalar or sequence into a stable list without splitting author names or keyword strings."""
    values = value if isinstance(value, (list, tuple)) else [value]
    return list(dict.fromkeys(text for item in values if (text := property_text(item))))


def property_date(value: object, *, warnings: list[str] | None = None) -> str | None:
    """Preserves date precision and explicit time zone; supports PDF dates without compensating for missing times."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = property_text(value)
    if not text:
        return None
    if text.startswith("D:"):
        match = re.fullmatch(r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(Z|[+-]\d{2}'?(?:\d{2}'?)?)?", text)
        if match is None:
            if warnings is not None:
                warnings.append(f"Invalid declared date: {text[:120]}")
            return None
        year, month, day, hour, minute, second, zone = match.groups()
        text = year
        for separator, part in (("-", month), ("-", day), ("T", hour), (":", minute), (":", second)):
            if part is not None:
                text += separator + part
        if zone:
            zone = zone.replace("'", "")
            text += zone if zone == "Z" else zone[:3] + (":" + zone[3:] if zone[3:] else ":00")
    try:
        if re.fullmatch(r"\d{4}", text):
            date(int(text), 1, 1)
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            date(int(text[:4]), int(text[5:]), 1)
        elif "T" in text:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            date.fromisoformat(text)
    except ValueError:
        if warnings is not None:
            warnings.append(f"Invalid declared date: {text[:120]}")
        return None
    return text


def property_count(value: object) -> int | None:
    """Non-negative counts are accepted, illegal or missing declared values remain unknown."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, str)):
        return None
    try:
        count = int(value)
    except ValueError:
        return None
    return count if count >= 0 else None


def legacy_properties(properties: DocumentProperties) -> dict[str, object | None]:
    """For the existing interface to project basic attributes by format, there is still only one implementation for format reading."""
    return {
        "page_count": properties.page_count,
        "title": properties.title,
        "author": "; ".join(properties.authors) or None,
        "subject": properties.subject,
        "keywords": ", ".join(properties.keywords) or None,
    }


__all__ = ["property_text", "property_values", "property_date", "property_count", "legacy_properties"]
