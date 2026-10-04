"""Parse StyleTextPropAtom and TextMasterStyleAtom."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..legacy.binary import get_i16, get_u16, get_u32


@dataclass(frozen=True, slots=True)
class ParagraphRun:
    """Paragraph attributes corresponding to a UTF-16 text range."""

    count: int
    depth: int
    bullet: bool | None = None
    ordered: bool | None = None
    start: int | None = None


@dataclass(frozen=True, slots=True)
class CharacterRun:
    """Character attributes corresponding to a UTF-16 text range."""

    count: int
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    baseline: int | None = None
    pp9rt: int = 0


@dataclass(frozen=True, slots=True)
class MasterLevel:
    """An inheritable default value for the master's indentation level."""

    bullet: bool | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    baseline: int | None = None


@dataclass(slots=True)
class StyleRuns:
    """A sequence of paragraph and character attributes for the same text shape."""

    paragraphs: list[ParagraphRun] = field(default_factory=list)
    characters: list[CharacterRun] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _CharacterStyle:
    """TextCFException solved tri-state character attributes."""

    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    baseline: int | None = None
    pp9rt: int = 0


def _parse_paragraph_exception(
    body: bytes,
    position: int,
) -> tuple[bool | None, int] | None:
    """Parses TextPFException and returns explicit bullet status and next offset."""

    mask = get_u32(body, position)
    if mask is None:
        return None
    position += 4
    bullet = None
    if mask & 0x000F:
        flags = get_u16(body, position)
        if flags is None:
            return None
        if mask & 0x0001:
            bullet = bool(flags & 0x0001)
        position += 2
    fixed_sizes = (
        (0x0080, 2),
        (0x0010, 2),
        (0x0040, 2),
        (0x0020, 4),
        (0x0800, 2),
        (0x1000, 2),
        (0x2000, 2),
        (0x4000, 2),
        (0x0100, 2),
        (0x0400, 2),
        (0x8000, 2),
    )
    for property_mask, size in fixed_sizes:
        if mask & property_mask:
            position += size
    if mask & 0x0010_0000:
        tab_count = get_u16(body, position)
        if tab_count is None:
            return None
        position += 2 + tab_count * 4
    if mask & 0x0001_0000:
        position += 2
    if mask & 0x000E_0000:
        position += 2
    if mask & 0x0020_0000:
        position += 2
    if position > len(body):
        return None
    return bullet, position


def _parse_character_exception(
    body: bytes,
    position: int,
) -> tuple[_CharacterStyle, int] | None:
    """Parse TextCFException and retain the tri-state value of each inheritable property."""

    mask = get_u32(body, position)
    if mask is None:
        return None
    position += 4
    bold = italic = underline = strike = None
    pp9rt = 0
    if mask & 0xFFFF:
        flags = get_u16(body, position)
        if flags is None:
            return None
        if mask & 0x0001:
            bold = bool(flags & 0x0001)
        if mask & 0x0002:
            italic = bool(flags & 0x0002)
        if mask & 0x0004:
            underline = bool(flags & 0x0004)
        # Some producers write strikethrough to the extended style bit; inheritance continues when undeclared.
        if mask & 0x0100:
            strike = bool(flags & 0x0100)
        # The 4-bit pp9rt of fontStyle selects the StyleTextProp9 array entry.
        pp9rt = (flags >> 10) & 0xF
        position += 2
    for property_mask in (0x0001_0000, 0x0020_0000, 0x0040_0000, 0x0080_0000):
        if mask & property_mask:
            position += 2
    if mask & 0x0002_0000:
        position += 2
    if mask & 0x0004_0000:
        position += 4
    baseline = None
    if mask & 0x0008_0000:
        baseline = get_i16(body, position)
        if baseline is None:
            return None
        position += 2
    if position > len(body):
        return None
    return (
        _CharacterStyle(
            bold=bold,
            italic=italic,
            underline=underline,
            strike=strike,
            baseline=baseline,
            pp9rt=pp9rt,
        ),
        position,
    )


def parse_style_text(body: bytes, text_utf16_length: int) -> StyleRuns:
    """Parses a StyleTextPropAtom by UTF-16 unit length."""

    runs = StyleRuns()
    position = 0
    covered = 0
    while covered <= text_utf16_length:
        count = get_u32(body, position)
        depth = get_u16(body, position + 4)
        if count is None or depth is None:
            break
        position += 6
        parsed = _parse_paragraph_exception(body, position)
        if parsed is None:
            return runs
        bullet, position = parsed
        runs.paragraphs.append(ParagraphRun(count=int(count), depth=min(int(depth), 8), bullet=bullet))
        covered += int(count)
        if count == 0:
            break

    covered = 0
    while covered <= text_utf16_length:
        count = get_u32(body, position)
        if count is None:
            break
        position += 4
        parsed = _parse_character_exception(body, position)
        if parsed is None:
            break
        style, position = parsed
        runs.characters.append(
            CharacterRun(
                count=int(count),
                bold=style.bold,
                italic=style.italic,
                underline=style.underline,
                strike=style.strike,
                baseline=style.baseline,
                pp9rt=style.pp9rt,
            )
        )
        covered += int(count)
        if count == 0:
            break
    return runs


def parse_master_style(body: bytes, instance: int) -> list[MasterLevel]:
    """Parse the layer-by-layer default character and list attributes of TextMasterStyleAtom."""

    level_count = get_u16(body, 0)
    if level_count is None:
        return []
    position = 2
    result: list[MasterLevel] = []
    for _ in range(min(int(level_count), 10)):
        if instance >= 5:
            position += 2
        parsed_paragraph = _parse_paragraph_exception(body, position)
        if parsed_paragraph is None:
            break
        bullet, position = parsed_paragraph
        parsed_character = _parse_character_exception(body, position)
        if parsed_character is None:
            break
        style, position = parsed_character
        result.append(
            MasterLevel(
                bullet=bullet,
                bold=style.bold,
                italic=style.italic,
                underline=style.underline,
                baseline=style.baseline,
            )
        )
    return result
