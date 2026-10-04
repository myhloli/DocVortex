"""Deterministic validation rules for shared document link targets."""

from ..foundation._hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target

__all__ = ["OFFICE_EXTERNAL_HYPERLINK_SCHEMES", "sanitize_hyperlink_target"]
