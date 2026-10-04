"""Compatible with exporting shared hyperlink security policies located at the utils layer."""

from __future__ import annotations

from ....foundation._hyperlink import (
    DEFAULT_EXTERNAL_HYPERLINK_SCHEMES,
    OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
    sanitize_hyperlink_target,
)

__all__ = [
    "DEFAULT_EXTERNAL_HYPERLINK_SCHEMES",
    "OFFICE_EXTERNAL_HYPERLINK_SCHEMES",
    "sanitize_hyperlink_target",
]
