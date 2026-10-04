"""Recognize common academic text roles and mathematical syntax without citing document names, variable names, or publisher names."""

import re
import unicodedata

_IDENTIFIER = r"[A-Za-z\u00c0-\u02af\u0370-\u03ff][A-Za-z0-9_\u00c0-\u02af\u0370-\u03ff]*"
_ATOM = r"(?:" + _IDENTIFIER + r"|\d+(?:\.\d+)?)(?:\s*[(\[{][^()\[\]{}\n]*[)\]}])*"
_EXPRESSION = re.compile(rf"{_ATOM}(?:\s*[+−\-*/=<>≤≥≠≈∝∈×÷^_]\s*{_ATOM})+")
_CALL = re.compile(rf"{_IDENTIFIER}\s*\([^()\n]*\)")
_ROLES = {
    "metadata": {"articleinfo", "articleinformation", "文章信息", "论文信息"},
    "abstract": {"abstract", "summary", "摘要", "内容摘要"},
    "affiliations": {"affiliations", "authoraffiliations", "作者单位", "作者机构", "机构信息"},
}
_FIELDS = {
    "received": r"^(?:received|收稿|收稿日期)",
    "revised": r"^(?:revised|修订|修回)",
    "accepted": r"^(?:accepted|录用|接受日期)",
    "published": r"^(?:available\s+online|published|出版日期|发表日期)",
    "keywords": r"^(?:key\s*words?|关键词|关键字)",
    "contact": r"(?:\bcorrespond(?:ing|ence)\b|通讯作者|通信作者|[\w.+-]+@[\w.-]+\.[a-z]{2,})",
}


def text_role(text: str) -> str | None:
    """Only independent character titles are normalized. Same words appearing in the text will not be upgraded to titles."""
    normalized = re.sub(r"[\s:：]+", "", unicodedata.normalize("NFKC", text)).casefold()
    return next((role for role, names in _ROLES.items() if normalized in names), None)


def metadata_field(text: str) -> str | None:
    """Extract independent metadata field categories. Multiple occurrences of the same date field are not counted as multiple roles."""
    return next((role for role, pattern in _FIELDS.items() if re.search(pattern, text.strip(), re.IGNORECASE)), None)


def publication_text(text: str) -> bool:
    """Publication identifiers and independent article genres provide margin context and do not identify specific journal or publisher names."""
    return bool(re.search(r"copyright|©|\bjournal\b|\bissn\b|\bdoi\b|出版|版权所有", text, re.IGNORECASE)) or (
        len(text.split()) <= 4
        and bool(
            re.search(r"\b(?:review|editorial|letter|research article|original article)\b|综述|研究论文", text, re.IGNORECASE)
        )
    )


def prose_residue(text: str) -> str:
    """Syntactically connected mathematical expressions are removed, leaving natural language; custom identifiers are identified by structure rather than name."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _EXPRESSION.sub(" ", normalized)
    return _CALL.sub(" ", normalized)


def has_prose(text: str, *, minimum_words: int = 3) -> bool:
    """Only continuous natural language outside mathematical expressions constitutes textual evidence; isolated identifiers still require spatial classification."""
    residue = prose_residue(text)
    return (
        bool(re.search(r"\b(?:where|with|when|from|that|then|the|this|these|which|is|are)\b", residue, re.IGNORECASE))
        or len(re.findall(r"(?<![A-Za-z\d])[A-Za-z]{2,}(?![A-Za-z\d])", residue)) >= minimum_words
        or len(re.findall(r"[\u3400-\u9fff]", residue)) >= minimum_words
    )
