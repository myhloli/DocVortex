"""Flash Office Stable error type shared by binary, embedded objects and RTF parsing."""

from __future__ import annotations


class LegacyOfficeError(ValueError):
    """The old version of Office parses the error base class and carries stable error codes."""

    code = "legacy_office_error"


class LegacyOfficeMalformedError(LegacyOfficeError):
    """The input container or core binary records cannot form a valid document."""

    code = "malformed"


class LegacyOfficeMissingPartError(LegacyOfficeError):
    """OLE stream required to complete parsing is missing."""

    code = "missing_part"


class LegacyOfficeEncryptedError(LegacyOfficeError):
    """The input uses encryption that is not supported by the current pure Python parsing chain."""

    code = "encrypted"


class LegacyOfficeResourceLimitError(LegacyOfficeError):
    """Input exceeds fixed safety limits."""

    code = "resource_limit"
