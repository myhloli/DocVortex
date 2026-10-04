"""HTML Flash parsed stable exception type."""


class HtmlParseError(ValueError):
    """Indicates that the HTML bytes cannot form a usable static DOM."""


class HtmlResourceLimitError(HtmlParseError):
    """Indicates that the HTML input, DOM, or resource exceeds the fixed security budget."""


__all__ = ["HtmlParseError", "HtmlResourceLimitError"]
