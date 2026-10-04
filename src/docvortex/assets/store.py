"""The resulting collection of footage bytes, with all paths relative to the output directory."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from hashlib import sha256

from ..foundation._image_payload import validate_image_sidecar_path


class AssetStore(Mapping[str, bytes]):
    """Read assets with stable relative paths and share bytes objects with the same content."""

    def __init__(self, files: Mapping[str, bytes] | None = None) -> None:
        """By copying the material index, subsequent modifications to the dictionary by the caller will not pollute the results."""
        self._files: dict[str, bytes] = {}
        self._content: dict[str, bytes] = {}
        for path, payload in (files or {}).items():
            self.add(path, payload)

    def add(self, path: str, payload: bytes) -> None:
        """Verify relative paths and reject content with different names to avoid overwriting existing assets."""
        path = validate_image_sidecar_path(path)
        if not isinstance(payload, bytes):
            raise TypeError("Asset payload must be bytes")
        if path in self._files and self._files[path] != payload:
            raise ValueError(f"Conflicting asset payload: {path}")
        payload = self._content.setdefault(sha256(payload).hexdigest(), payload)
        self._files[path] = payload

    def __getitem__(self, path: str) -> bytes:
        """Parses assets by validated relative paths, without reading host files or the network."""
        return self._files[validate_image_sidecar_path(path)]

    def __iter__(self) -> Iterator[str]:
        """Traverse the material path in registration order."""
        return iter(self._files)

    def __len__(self) -> int:
        """Returns the number of material references."""
        return len(self._files)

    def copy(self) -> AssetStore:
        """Build independent indexes and reuse immutable bytes."""
        copied = AssetStore()
        copied._files = self._files.copy()
        copied._content = self._content.copy()
        return copied


__all__ = ["AssetStore"]
