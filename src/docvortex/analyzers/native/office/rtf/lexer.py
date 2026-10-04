"""Binary-safe, position-explicit RTF lexer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, TypeAlias, Union

from ..errors import LegacyOfficeMalformedError, LegacyOfficeResourceLimitError
from ..limits import MAX_RECORDS

MAX_RTF_GROUP_DEPTH = 256
MAX_RTF_CONTROL_PARAMETER_DIGITS = 10
MIN_RTF_CONTROL_PARAMETER = -(2**31)
MAX_RTF_CONTROL_PARAMETER = 2**31 - 1


@dataclass(frozen=True, slots=True)
class RtfOpen:
    """Represents an opening curly brace."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RtfClose:
    """Represents a right curly brace."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RtfControlWord:
    """Represents a control word that can take a signed integer argument."""

    name: str
    param: int | None
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RtfControlSymbol:
    """Represents a single character after the backslash control symbol."""

    symbol: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RtfHexByte:
    """Represents a ``\'hh`` hexadecimal byte."""

    value: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RtfTextBytes:
    """Represents contiguous bytes of raw text without characters in the RTF structure."""

    data: bytes
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RtfBinary:
    """Represents a raw binary payload of the length declared by ``\binN``."""

    data: bytes
    start: int
    end: int


RtfToken: TypeAlias = Union[
    RtfOpen,
    RtfClose,
    RtfControlWord,
    RtfControlSymbol,
    RtfHexByte,
    RtfTextBytes,
    RtfBinary,
]


def _is_ascii_letter(value: int) -> bool:
    """Determine whether a byte is RTF control word using ASCII letters."""
    return 65 <= value <= 90 or 97 <= value <= 122


def _is_ascii_digit(value: int) -> bool:
    """Determine whether a byte is a ASCII decimal number."""
    return 48 <= value <= 57


class RtfLexer:
    """Iterates RTF token by byte position and is responsible for binary length and resource caps."""

    def __init__(self, data: bytes) -> None:
        """Immutable input is saved and the actual scan is performed during iteration."""
        self._data = data

    def __iter__(self) -> Iterator[RtfToken]:
        """Generate token in source order, allowing parser to recover unbalanced root groups on its own."""
        data = self._data
        cursor = 0
        depth = 0
        token_count = 0
        while cursor < len(data):
            token: RtfToken
            start = cursor
            value = data[cursor]
            if value == ord("{"):
                cursor += 1
                depth += 1
                if depth > MAX_RTF_GROUP_DEPTH:
                    raise LegacyOfficeResourceLimitError(f"RTF group nesting exceeds max_group_depth={MAX_RTF_GROUP_DEPTH}")
                token = RtfOpen(start, cursor)
            elif value == ord("}"):
                cursor += 1
                depth = max(depth - 1, 0)
                token = RtfClose(start, cursor)
            elif value != ord("\\"):
                cursor += 1
                while cursor < len(data) and data[cursor] not in b"{}\\":
                    cursor += 1
                token = RtfTextBytes(data[start:cursor], start, cursor)
            else:
                cursor += 1
                if cursor >= len(data):
                    token = RtfControlSymbol("\\", start, cursor)
                elif data[cursor] in (10, 13):
                    if data[cursor] == 13 and cursor + 1 < len(data) and data[cursor + 1] == 10:
                        cursor += 2
                    else:
                        cursor += 1
                    token = RtfControlSymbol("\n", start, cursor)
                elif data[cursor] == ord("'"):
                    cursor += 1
                    if cursor + 2 <= len(data):
                        raw_hex = data[cursor : cursor + 2]
                        try:
                            hex_value = int(raw_hex.decode("ascii"), 16)
                        except (UnicodeDecodeError, ValueError):
                            token = RtfControlSymbol("'", start, cursor)
                        else:
                            cursor += 2
                            token = RtfHexByte(hex_value, start, cursor)
                    else:
                        token = RtfControlSymbol("'", start, cursor)
                elif _is_ascii_letter(data[cursor]):
                    name_start = cursor
                    while cursor < len(data) and _is_ascii_letter(data[cursor]):
                        cursor += 1
                    name = data[name_start:cursor].decode("ascii").lower()
                    sign = 1
                    if cursor < len(data) and data[cursor] == ord("-"):
                        sign = -1
                        cursor += 1
                    number_start = cursor
                    while cursor < len(data) and _is_ascii_digit(data[cursor]):
                        cursor += 1
                    param = None
                    if cursor > number_start:
                        digit_count = cursor - number_start
                        if digit_count > MAX_RTF_CONTROL_PARAMETER_DIGITS:
                            raise LegacyOfficeResourceLimitError(
                                f"RTF control parameter exceeds max_digits={MAX_RTF_CONTROL_PARAMETER_DIGITS}"
                            )
                        param = sign * int(data[number_start:cursor])
                        if param < MIN_RTF_CONTROL_PARAMETER or param > MAX_RTF_CONTROL_PARAMETER:
                            raise LegacyOfficeResourceLimitError("RTF control parameter exceeds signed 32-bit range")
                    if cursor < len(data) and data[cursor] == ord(" "):
                        cursor += 1
                    if name == "bin":
                        if param is None or param < 0:
                            raise LegacyOfficeMalformedError("RTF bin control requires a non-negative length")
                        payload_end = cursor + param
                        if payload_end < cursor or payload_end > len(data):
                            raise LegacyOfficeMalformedError(
                                f"RTF bin payload is truncated: declared={param}, available={len(data) - cursor}"
                            )
                        token = RtfBinary(data[cursor:payload_end], start, payload_end)
                        cursor = payload_end
                    else:
                        token = RtfControlWord(name, param, start, cursor)
                else:
                    symbol = chr(data[cursor])
                    cursor += 1
                    token = RtfControlSymbol(symbol, start, cursor)

            token_count += 1
            if token_count > MAX_RECORDS:
                raise LegacyOfficeResourceLimitError(f"RTF token count exceeds max_tokens={MAX_RECORDS}")
            yield token


__all__ = [
    "MAX_RTF_GROUP_DEPTH",
    "RtfBinary",
    "RtfClose",
    "RtfControlSymbol",
    "RtfControlWord",
    "RtfHexByte",
    "RtfLexer",
    "RtfOpen",
    "RtfTextBytes",
    "RtfToken",
]
