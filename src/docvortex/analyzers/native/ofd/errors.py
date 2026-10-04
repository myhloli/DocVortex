"""OFD Native parsing error type."""


class OfdParseError(ValueError):
    """Indicates OFD The package structure or required content is illegal."""


class OfdEncryptedError(OfdParseError):
    """Indicates OFD The package or member uses unsupported encryption."""


class OfdResourceLimitError(OfdParseError):
    """Indicates that OFD input exceeds the fixed resource budget."""


__all__ = ["OfdEncryptedError", "OfdParseError", "OfdResourceLimitError"]
