"""OFD fixed layout Flash analysis entry."""

from .errors import OfdEncryptedError, OfdParseError, OfdResourceLimitError
from .metadata import extract_ofd_metadata
from .package import detect_ofd, detect_ofd_path

__all__ = [
    "OfdEncryptedError",
    "OfdParseError",
    "OfdResourceLimitError",
    "detect_ofd",
    "detect_ofd_path",
    "extract_ofd_metadata",
]
