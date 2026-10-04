"""Flash Office Caller binary stream reading capability for converter multiplexing."""

from typing import BinaryIO


def rewind_stream(file_stream: BinaryIO) -> bool:
    """Moves the resettable binary stream to the starting point; returns False if not resettable."""
    try:
        file_stream.seek(0)
    except (AttributeError, OSError, ValueError):
        return False
    return True


def read_stream_bytes_from_start(file_stream: BinaryIO) -> bytes:
    """Read complete bytes from the beginning of the stream; non-resettable streams read the remaining bytes from the current position."""
    rewind_stream(file_stream)
    return file_stream.read()
