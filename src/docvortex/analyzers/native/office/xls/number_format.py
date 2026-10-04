"""Excel Numeric format parsing and stable display text generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
import math
import re


BUILTIN_NUMBER_FORMATS: dict[int, str] = {
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    12: "# ?/?",
    13: "# ??/??",
    14: "mm-dd-yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    48: "##0.0E+0",
    49: "@",
}

_COLOR_NAMES = {
    "black",
    "blue",
    "cyan",
    "green",
    "magenta",
    "red",
    "white",
    "yellow",
}
_CONDITION_RE = re.compile(r"^(<=|>=|<>|=|<|>)([-+]?(?:\d+(?:\.\d*)?|\.\d+))$")


@dataclass(frozen=True, slots=True)
class _Condition:
    """A comparison condition of numeric format section."""

    operator: str
    operand: float

    def matches(self, value: float) -> bool:
        """Determine whether the value meets the current comparison conditions."""

        return {
            "<": value < self.operand,
            "<=": value <= self.operand,
            ">": value > self.operand,
            ">=": value >= self.operand,
            "=": value == self.operand,
            "<>": value != self.operand,
        }[self.operator]


@dataclass(frozen=True, slots=True)
class _Section:
    """A section that has been split but still retains the Excel format syntax."""

    pattern: str
    condition: _Condition | None


def builtin_number_format(format_id: int) -> str | None:
    """Returns the determined built-in format code, no guessing is required for region-related formats."""

    return BUILTIN_NUMBER_FORMATS.get(int(format_id))


def format_general(value: float) -> str:
    """Press Excel to output an unformatted floating point number with 15 significant digits."""

    if not math.isfinite(value):
        return str(value)
    if value == 0:
        return "0"
    rounded = float(f"{value:.15g}")
    if rounded.is_integer() and abs(rounded) < 1e15:
        return str(int(rounded))
    return format(rounded, ".15g")


def _split_sections(code: str) -> list[str] | None:
    """Split semicolons without cutting quotes, escapes, and square brackets sections."""

    sections: list[str] = []
    current: list[str] = []
    quote = False
    bracket_depth = 0
    escaped = False
    for char in code:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and not quote:
            current.append(char)
            escaped = True
            continue
        if char == '"':
            current.append(char)
            quote = not quote
            continue
        if not quote and char == "[":
            bracket_depth += 1
        elif not quote and char == "]":
            bracket_depth -= 1
            if bracket_depth < 0:
                return None
        if char == ";" and not quote and bracket_depth == 0:
            sections.append("".join(current))
            current = []
        else:
            current.append(char)
    if quote or bracket_depth != 0 or escaped:
        return None
    sections.append("".join(current))
    return sections if len(sections) <= 4 else None


def _strip_brackets(pattern: str) -> tuple[str, _Condition | None, bool]:
    """Removed color/condition/region markers while recognizing elapsed time format."""

    output: list[str] = []
    condition: _Condition | None = None
    elapsed = False
    cursor = 0
    while cursor < len(pattern):
        if pattern[cursor] != "[":
            output.append(pattern[cursor])
            cursor += 1
            continue
        end = pattern.find("]", cursor + 1)
        if end < 0:
            return pattern, condition, elapsed
        inner = pattern[cursor + 1 : end]
        match = _CONDITION_RE.fullmatch(inner)
        if match and condition is None:
            condition = _Condition(match.group(1), float(match.group(2)))
        elif inner.casefold() in _COLOR_NAMES or re.fullmatch(r"Color\d+", inner, re.I):
            pass
        elif inner.casefold() in {"h", "hh", "m", "mm", "s", "ss"}:
            elapsed = True
            output.append(f"[{inner}]")
        elif inner.startswith("$"):
            currency = inner[1:].split("-", 1)[0]
            output.append(currency)
        else:
            output.append(f"[{inner}]")
        cursor = end + 1
    return "".join(output), condition, elapsed


def _parse_sections(code: str) -> list[_Section] | None:
    """Split the format and extract the optional conditions for each section."""

    raw_sections = _split_sections(code)
    if raw_sections is None or not raw_sections:
        return None
    sections: list[_Section] = []
    for raw in raw_sections:
        pattern, condition, _ = _strip_brackets(raw)
        sections.append(_Section(pattern=pattern, condition=condition))
    if sum(section.condition is not None for section in sections) > 2:
        return None
    return sections


def _choose_numeric_section(
    sections: list[_Section],
    value: float,
) -> tuple[_Section, float, bool] | None:
    """section Selects a numerical value for rendering by condition or positive and negative zero position."""

    numeric = sections[:-1] if len(sections) == 4 else sections
    if not numeric:
        return None
    if any(section.condition is not None for section in numeric):
        for section in numeric:
            if section.condition is None or section.condition.matches(value):
                return section, value, True
        return None
    if len(numeric) == 1:
        return numeric[0], value, True
    if len(numeric) == 2:
        return (numeric[1], abs(value), False) if value < 0 else (numeric[0], value, False)
    if value > 0:
        return numeric[0], value, False
    if value < 0:
        return numeric[1], abs(value), False
    return numeric[2], value, False


def _literalize(pattern: str) -> str:
    """Revert quote, escape, underscore, and fill syntax to display literals."""

    output: list[str] = []
    cursor = 0
    quote = False
    while cursor < len(pattern):
        char = pattern[cursor]
        if char == '"':
            quote = not quote
            cursor += 1
            continue
        if not quote and char == "\\" and cursor + 1 < len(pattern):
            output.append(pattern[cursor + 1])
            cursor += 2
            continue
        if not quote and char == "_" and cursor + 1 < len(pattern):
            output.append(" ")
            cursor += 2
            continue
        if not quote and char == "*" and cursor + 1 < len(pattern):
            cursor += 2
            continue
        output.append(char)
        cursor += 1
    return "".join(output)


def _syntax_view(pattern: str) -> str:
    """After removing the literal, a syntax view that only determines the format type is returned."""

    output: list[str] = []
    cursor = 0
    quote = False
    while cursor < len(pattern):
        char = pattern[cursor]
        if char == '"':
            quote = not quote
            cursor += 1
            continue
        if not quote and char in {"\\", "_", "*"} and cursor + 1 < len(pattern):
            cursor += 2
            continue
        if not quote:
            output.append(char)
        cursor += 1
    return "".join(output)


def _date_parts(pattern: str) -> tuple[bool, bool, bool] | None:
    """Determines whether the format represents a date, time, or elapsed duration."""

    syntax = _syntax_view(pattern).casefold()
    elapsed = bool(re.search(r"\[(?:h+|m+|s+)\]", syntax))
    syntax_without_elapsed = re.sub(r"\[(?:h+|m+|s+)\]", "", syntax)
    has_year_or_day = bool(re.search(r"y|d", syntax_without_elapsed))
    has_hour_or_second = bool(re.search(r"h|s|am/pm|a/p", syntax_without_elapsed))
    has_month_or_minute = "m" in syntax_without_elapsed
    if not (elapsed or has_year_or_day or has_hour_or_second or has_month_or_minute):
        return None
    if not re.search(r"[ydhms]", syntax_without_elapsed) and not elapsed:
        return None
    if has_month_or_minute:
        minute_context = has_hour_or_second or bool(re.search(r"m+\s*:\s*s+|h+\s*:\s*m+", syntax))
        has_date = has_year_or_day or not minute_context
        has_time = has_hour_or_second or minute_context
    else:
        has_date = has_year_or_day
        has_time = has_hour_or_second
    if elapsed:
        has_time = True
        if not has_year_or_day:
            has_date = False
    return has_date, has_time, elapsed


def _render_serial(value: float, *, date1904: bool, parts: tuple[bool, bool, bool]) -> str:
    """Output Excel serial according to the determined ISO date/time policy."""

    has_date, has_time, elapsed = parts
    if elapsed:
        total_seconds = int(round(abs(value) * 86_400))
        sign = "-" if value < 0 else ""
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{sign}{hours}:{minutes:02}:{seconds:02}"
    if not has_date:
        total_seconds = int(round(abs(value % 1) * 86_400)) % 86_400
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    days = math.floor(value)
    if not date1904 and days == 60:
        return format_general(value)
    try:
        if date1904:
            resolved = datetime(1904, 1, 1) + timedelta(days=value)
        else:
            corrected = value - 1 if value >= 60 else value
            resolved = datetime(1899, 12, 31) + timedelta(days=corrected)
    except (OverflowError, ValueError):
        return format_general(value)
    output = resolved.strftime("%Y-%m-%d")
    if has_time:
        output += resolved.strftime(" %H:%M:%S")
    return output


def _decimal_quantize(value: float, places: int) -> Decimal:
    """The half-up rule to Excel rounds to the specified number of digits."""

    decimal_value = Decimal(format(value, ".15g"))
    quantum = Decimal(1).scaleb(-places)
    return decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)


def _extract_number_span(pattern: str) -> tuple[str, str, str]:
    """Remove the number placeholder body and its surrounding literals."""

    literalized = _literalize(pattern)
    general_match = re.search("general", literalized, re.I)
    if general_match is not None:
        return (
            literalized[: general_match.start()],
            literalized[general_match.start() : general_match.end()],
            literalized[general_match.end() :],
        )
    positions = [index for index, char in enumerate(literalized) if char in "0#?@"]
    if not positions:
        return literalized, "", ""
    start, end = min(positions), max(positions) + 1
    numeric_syntax = set("0123456789#?.,%Ee+-/")
    while start > 0 and literalized[start - 1] in numeric_syntax:
        start -= 1
    while end < len(literalized) and literalized[end] in numeric_syntax:
        end += 1
    return literalized[:start], literalized[start:end], literalized[end:]


def _render_scientific(value: float, pattern: str) -> str | None:
    """Press the Excel scientific notation placeholder to output mantissa and exponent."""

    match = re.search(r"([0#?]+)(?:\.([0#?]+))?[Ee]([+-])([0#?]+)", pattern)
    if match is None:
        return None
    int_places = len(match.group(1))
    frac_places = len(match.group(2) or "")
    exponent_places = len(match.group(4))
    if value == 0:
        exponent = 0
        mantissa = 0.0
    else:
        raw_exponent = math.floor(math.log10(abs(value)))
        exponent = math.floor(raw_exponent / max(int_places, 1)) * max(int_places, 1)
        mantissa = value / (10**exponent)
    rounded = _decimal_quantize(mantissa, frac_places)
    if abs(rounded) >= 10**int_places:
        exponent += max(int_places, 1)
        rounded /= Decimal(10 ** max(int_places, 1))
    mantissa_text = f"{rounded:.{frac_places}f}"
    exponent_sign = "+" if exponent >= 0 else "-"
    if match.group(3) == "-" and exponent >= 0:
        exponent_sign = ""
    exponent_text = str(abs(exponent)).rjust(exponent_places, "0")
    return f"{mantissa_text}E{exponent_sign}{exponent_text}"


def _render_fraction(value: float, pattern: str) -> str | None:
    """Outputs the nearest fraction by fixed or placeholder denominator."""

    match = re.search(r"([#0?]*)\s*([0#?]+)\/([0-9#?]+)", pattern)
    if match is None:
        return None
    integer_pattern, numerator_pattern, denominator_pattern = match.groups()
    whole = math.floor(abs(value)) if integer_pattern else 0
    fraction_value = abs(value) - whole
    fixed_denominator = int(denominator_pattern) if denominator_pattern.isdigit() else None
    if fixed_denominator:
        denominator = fixed_denominator
        numerator = int(Decimal(format(fraction_value * denominator, ".15g")).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    else:
        max_denominator = max(1, 10 ** len(denominator_pattern) - 1)
        fraction = Fraction(fraction_value).limit_denominator(max_denominator)
        numerator, denominator = fraction.numerator, fraction.denominator
    if numerator == 0:
        return str(whole) if whole else ""
    if numerator >= denominator and integer_pattern:
        whole += numerator // denominator
        numerator %= denominator
    numerator_text = str(numerator).rjust(len(numerator_pattern), " " if "?" in numerator_pattern else "0")
    denominator_text = str(denominator).rjust(
        len(denominator_pattern),
        " " if "?" in denominator_pattern else "0",
    )
    fraction_text = f"{numerator_text}/{denominator_text}"
    return f"{whole} {fraction_text}" if whole else fraction_text


def _render_decimal(value: float, pattern: str) -> str:
    """Output values as integers, decimals, grouping, scaling, and percent sign placeholders."""

    percent_count = pattern.count("%")
    core = pattern.replace("%", "")
    placeholder_positions = [index for index, char in enumerate(core) if char in "0#?"]
    trailing_commas = 0
    if placeholder_positions:
        suffix = core[max(placeholder_positions) + 1 :]
        trailing_commas = suffix.count(",") if not suffix.strip(", ") else 0
        if trailing_commas:
            remove_from = max(placeholder_positions) + 1
            core = core[:remove_from] + core[remove_from:].replace(",", "")
    decimal_pos = core.find(".")
    integer_pattern = core if decimal_pos < 0 else core[:decimal_pos]
    fractional_pattern = "" if decimal_pos < 0 else core[decimal_pos + 1 :]
    scaled = value * (100**percent_count) / (1000**trailing_commas)
    integer_placeholders = "".join(char for char in integer_pattern if char in "0#?")
    fractional_placeholders = "".join(char for char in fractional_pattern if char in "0#?")
    decimal_places = len(fractional_placeholders)
    rounded = _decimal_quantize(abs(scaled), decimal_places)
    absolute_text = f"{rounded:.{decimal_places}f}"
    integer_text, _, fraction_text = absolute_text.partition(".")
    required_integer = integer_placeholders.count("0")
    if len(integer_text) < required_integer:
        integer_text = integer_text.rjust(required_integer, "0")
    if rounded == 0 and not required_integer and integer_placeholders:
        integer_text = ""
    if "," in integer_pattern and integer_text:
        integer_text = f"{int(integer_text):,}"
    if integer_placeholders.startswith("?") and integer_text:
        integer_text = integer_text.rjust(len(integer_placeholders), " ")
    visible_fraction = list(fraction_text)
    for index in range(len(fractional_placeholders) - 1, -1, -1):
        placeholder = fractional_placeholders[index]
        if placeholder == "0":
            break
        if index >= len(visible_fraction) or visible_fraction[index] != "0":
            break
        visible_fraction[index] = " " if placeholder == "?" else ""
    rendered_fraction = "".join(visible_fraction)
    show_decimal = decimal_pos >= 0 and (rendered_fraction != "" or not fractional_placeholders)
    result = integer_text
    if show_decimal:
        result += "." + rendered_fraction
    if scaled < 0:
        result = "-" + result
    return result + ("%" * percent_count)


def _render_pattern(pattern: str, value: float, auto_minus: bool) -> str:
    """Renders a selected value section."""

    if not pattern:
        return ""
    prefix, core, suffix = _extract_number_span(pattern)
    if core.casefold() == "general":
        body = format_general(abs(value) if not auto_minus else value)
        if value < 0 and not auto_minus:
            body = format_general(abs(value))
        return prefix + body + suffix
    if not any(char in core for char in "0#?"):
        return prefix + suffix
    if re.search(r"[Ee][+-][0#?]", core):
        body = _render_scientific(abs(value), core) or format_general(abs(value))
    elif "/" in core:
        body = _render_fraction(abs(value), core) or format_general(abs(value))
    else:
        body = _render_decimal(abs(value), core)
    if auto_minus and value < 0:
        body = "-" + body
    return prefix + body + suffix


def format_number(value: float, format_code: str | None, *, date1904: bool) -> str:
    """Use Excel format code to render the value, and return to General if it cannot be parsed."""

    if format_code is None or not math.isfinite(value):
        return format_general(value)
    sections = _parse_sections(format_code)
    if sections is None:
        return format_general(value)
    chosen = _choose_numeric_section(sections, value)
    if chosen is None:
        return format_general(value)
    section, selected_value, auto_minus = chosen
    pattern, _, _ = _strip_brackets(section.pattern)
    parts = _date_parts(pattern)
    if parts is not None:
        return _render_serial(selected_value, date1904=date1904, parts=parts)
    try:
        return _render_pattern(pattern, selected_value, auto_minus)
    except (InvalidOperation, OverflowError, ValueError, ZeroDivisionError):
        return format_general(value)


def format_text(text: str, format_code: str | None) -> str:
    """The fourth text section is applied; without text section the original text is retained."""

    if format_code is None:
        return text
    sections = _parse_sections(format_code)
    if sections is None:
        return text
    if len(sections) == 4:
        pattern = _literalize(sections[3].pattern)
        return pattern.replace("@", text)
    if len(sections) == 1 and "@" in sections[0].pattern:
        return _literalize(sections[0].pattern).replace("@", text)
    return text
