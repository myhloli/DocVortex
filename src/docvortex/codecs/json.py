"""Explicit read entry for the shared document protocol."""

from typing import Any
from ..schema import MiddleJson, ModelJson


def load_model(value: dict[str, Any]) -> ModelJson:
    """Read the new version of the analysis document without performing parsing, source rewriting or historical migration."""
    return ModelJson.from_dict(value)


def load_middle(value: dict[str, Any]) -> MiddleJson:
    """Read the new version of semantic documents without accessing source files and external materials."""
    return MiddleJson.from_dict(value)


__all__ = ["load_model", "load_middle"]
