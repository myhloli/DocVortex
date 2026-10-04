"""Maintain historical import identities of relocated types and independently parsable annotations."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import get_type_hints


def preserve_type_module(cls: type, module: str) -> None:
    """First parse the annotations in the definition module, and then retain the old pickle path to avoid relying on facades that have not yet been imported."""
    annotations = get_type_hints(cls)
    cls.__annotations__ = annotations
    if is_dataclass(cls):
        for field in fields(cls):
            if field.name in annotations:
                field.type = annotations[field.name]
    cls.__module__ = module


__all__ = ["preserve_type_module"]
