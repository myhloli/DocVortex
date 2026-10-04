"""A bounded OLE2/CFB read-only wrapper based on olefile."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import olefile  # type: ignore[reportMissingModuleSource]

from ..errors import LegacyOfficeMalformedError, LegacyOfficeMissingPartError, LegacyOfficeResourceLimitError
from ..limits import MAX_ENTRY_BYTES, MAX_TOTAL_BYTES


class BoundedOleReader:
    """Limits input, single-stream, and cumulative reads, and provides case-independent access to stream."""

    def __init__(self, file_bytes: bytes) -> None:
        """Verify the input size and open the OLE2 container in memory."""

        if not isinstance(file_bytes, bytes):
            raise TypeError("legacy Office input must be bytes")
        if len(file_bytes) > MAX_TOTAL_BYTES:
            raise LegacyOfficeResourceLimitError(f"input exceeds max_total_bytes={MAX_TOTAL_BYTES}")
        try:
            self._ole: Any = olefile.OleFileIO(BytesIO(file_bytes), raise_defects=olefile.DEFECT_FATAL)
        except Exception as exc:
            raise LegacyOfficeMalformedError(f"not a readable OLE2 compound file: {exc}") from exc
        self._total_read = 0
        self._stream_names = self._build_stream_name_map()

    def _build_stream_name_map(self) -> dict[str, tuple[str, ...]]:
        """Create a case-independent index of the complete stream name."""

        names: dict[str, tuple[str, ...]] = {}
        try:
            for parts in self._ole.listdir(streams=True, storages=False):
                normalized = tuple(str(part) for part in parts)
                names["/".join(normalized).casefold()] = normalized
        except Exception as exc:
            self.close()
            raise LegacyOfficeMalformedError(f"cannot enumerate OLE streams: {exc}") from exc
        return names

    def has_stream(self, name: str) -> bool:
        """Returns whether the container contains the specified stream."""

        return name.casefold() in self._stream_names

    def stream_names(self, *, prefix: str | None = None) -> tuple[str, ...]:
        """Returns the full stream name; filterable by case-independent prefix."""

        normalized_prefix = prefix.casefold() if prefix is not None else None
        names = (
            "/".join(parts)
            for key, parts in self._stream_names.items()
            if normalized_prefix is None or key.startswith(normalized_prefix)
        )
        return tuple(sorted(names, key=str.casefold))

    def read_stream(self, name: str, *, required: bool = True) -> bytes:
        """Bounded read specifies stream; optional returns a null byte if stream does not exist."""

        parts = self._stream_names.get(name.casefold())
        if parts is None:
            if required:
                raise LegacyOfficeMissingPartError(f"missing required OLE stream: {name}")
            return b""
        try:
            size = int(self._ole.get_size(list(parts)))
        except Exception as exc:
            raise LegacyOfficeMalformedError(f"cannot read OLE stream size: {name}: {exc}") from exc
        if size > MAX_ENTRY_BYTES:
            raise LegacyOfficeResourceLimitError(f"stream {name!r} exceeds max_entry_bytes={MAX_ENTRY_BYTES}")
        if self._total_read + size > MAX_TOTAL_BYTES:
            raise LegacyOfficeResourceLimitError(f"OLE streams exceed max_total_bytes={MAX_TOTAL_BYTES}")
        try:
            with self._ole.openstream(list(parts)) as stream:
                payload = stream.read(MAX_ENTRY_BYTES + 1)
        except Exception as exc:
            raise LegacyOfficeMalformedError(f"cannot read OLE stream {name!r}: {exc}") from exc
        if len(payload) > MAX_ENTRY_BYTES:
            raise LegacyOfficeResourceLimitError(f"stream {name!r} exceeds max_entry_bytes={MAX_ENTRY_BYTES}")
        self._total_read += len(payload)
        return payload

    def metadata(self) -> Any | None:
        """Try our best to read SummaryInformation. Failure will not affect text parsing."""

        metadata, _ = self.metadata_with_diagnostics()
        return metadata

    def metadata_with_diagnostics(self) -> tuple[Any | None, list[str]]:
        """Optional attribute stream corruption olefile Retains read fields and returns specific diagnostics."""

        try:
            return self._ole.get_metadata(), []
        except Exception as exc:
            return getattr(self._ole, "metadata", None), [f"OLE property streams: {exc}"]

    def close(self) -> None:
        """Close the underlying olefile handle."""

        ole = getattr(self, "_ole", None)
        if ole is not None:
            ole.close()
            self._ole = None

    def __enter__(self) -> BoundedOleReader:
        """Returns the current bounded reader."""

        return self

    def __exit__(self, *_args: object) -> None:
        """Close the underlying handle when leaving the context."""

        self.close()
