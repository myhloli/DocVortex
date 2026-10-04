import re

DEFAULT_CODE_LANGUAGE = "txt"
_INVALID_SURROGATES = re.compile("[\ud800-\udfff]")


def remove_invalid_surrogates(text: str) -> str:
    """Equivalent to removing surrogate code points; common legal Unicode text does not need to be scanned character by character Python."""
    return _INVALID_SURROGATES.sub("", text)


def _normalize_text_for_language_guess(code: str) -> str:
    """Removes orphan surrogate characters and restores legal surrogate pairs for use by code language recognition."""
    if not code:
        return ""
    normalized: list[str] = []
    index = 0
    while index < len(code):
        current_char = code[index]
        current_ord = ord(current_char)
        if 0xD800 <= current_ord <= 0xDBFF:
            if index + 1 < len(code):
                next_char = code[index + 1]
                next_ord = ord(next_char)
                if 0xDC00 <= next_ord <= 0xDFFF:
                    pair = current_char + next_char
                    normalized.append(pair.encode("utf-16", "surrogatepass").decode("utf-16"))
                    index += 2
                    continue
            index += 1
            continue
        if 0xDC00 <= current_ord <= 0xDFFF:
            index += 1
            continue
        normalized.append(current_char)
        index += 1
    return "".join(normalized)


def guess_code_language(code: str) -> str:
    """Use Magika to infer the code block language, returning a plain text type on failure."""
    normalized_code = _normalize_text_for_language_guess(code)
    if not normalized_code:
        return DEFAULT_CODE_LANGUAGE
    try:
        from .magika import Magika

        lang = Magika().identify_bytes(normalized_code.encode("utf-8", errors="replace")).prediction.output.label
    except Exception:
        return DEFAULT_CODE_LANGUAGE
    return lang if lang != "unknown" else DEFAULT_CODE_LANGUAGE


__all__ = ["DEFAULT_CODE_LANGUAGE", "guess_code_language", "remove_invalid_surrogates"]
