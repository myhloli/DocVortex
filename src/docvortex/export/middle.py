"""Atomic export of semantic documents and files next to images."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..schema import MiddleJson
from ._images import _materialize_images


@dataclass(frozen=True, slots=True)
class MiddleJsonExportResult:
    """Middle JSON The object copy and actual file path after the picture is externalized."""

    middle_json: MiddleJson
    json_path: Path
    image_paths: tuple[Path, ...]


def _prepare_export_copy(middle_json: MiddleJson) -> tuple[MiddleJson, dict[str, bytes]]:
    """Reuse the unified materialization process to provide document copies and image bytes for atomic file export."""
    document, assets = _materialize_images(middle_json)
    return document, dict(assets)


def _resolve_export_target(output_root: Path, relative_path: str) -> Path:
    """Verify export relative paths and ensure that the parsed target is still within the document output directory."""
    from ..foundation._image_payload import validate_image_sidecar_path

    safe_path = validate_image_sidecar_path(relative_path)
    if output_root.is_symlink():
        raise ValueError(f"Export directory must not be a symlink: {output_root}")
    if output_root.exists() and not output_root.is_dir():
        raise ValueError(f"Export directory must be a directory: {output_root}")
    root = output_root.resolve()
    target = root / safe_path
    current_parent = root
    for path_part in Path(safe_path).parts[:-1]:
        current_parent /= path_part
        if current_parent.is_symlink():
            raise ValueError(f"Export path contains a symlink: {relative_path}")
        if current_parent.exists() and not current_parent.is_dir():
            raise ValueError(f"Export path parent is not a directory: {relative_path}")
    try:
        target.parent.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Export path escapes output directory: {relative_path}") from exc
    return target


def _commit_export_files(files: dict[Path, bytes], *, overwrite: bool) -> None:
    """Submit as a temporary file after preflight conflicts, and restore existing files when submission fails."""
    pending: dict[Path, bytes] = {}
    originals: dict[Path, bytes | None] = {}
    for target, payload in files.items():
        if target.is_symlink():
            raise ValueError(f"Export target must not be a symlink: {target}")
        if target.exists():
            if not target.is_file():
                raise ValueError(f"Export target is not a regular file: {target}")
            current = target.read_bytes()
            if current == payload:
                continue
            if not overwrite:
                raise FileExistsError(f"Export target already exists with different content: {target}")
            originals[target] = current
        else:
            originals[target] = None
        pending[target] = payload

    temp_paths: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for target, payload in pending.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="wb",
                prefix=".docvortex-export-",
                dir=target.parent,
                delete=False,
            ) as temp_file:
                temp_file.write(payload)
                temp_paths[target] = Path(temp_file.name)
        for target, temp_path in temp_paths.items():
            os.replace(temp_path, target)
            committed.append(target)
    except Exception:
        for temp_path in temp_paths.values():
            temp_path.unlink(missing_ok=True)
        for target in reversed(committed):
            original = originals[target]
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(original)
        raise


def _validate_export_path_relationships(relative_paths: list[str]) -> None:
    """Deny any exported file from occupying the parent directory of another file to avoid conflicts during the submission phase."""
    path_parts = {relative_path: Path(relative_path).parts for relative_path in relative_paths}
    for relative_path, parts in path_parts.items():
        for other_path, other_parts in path_parts.items():
            if relative_path == other_path or len(parts) >= len(other_parts):
                continue
            if other_parts[: len(parts)] == parts:
                raise ValueError(f"Export file path conflicts with a required directory: {relative_path} -> {other_path}")


def _export_middle_json(
    middle_json: MiddleJson,
    output_dir: Path,
    *,
    json_name: str,
    overwrite: bool,
) -> MiddleJsonExportResult:
    """Construct a complete export transaction to ensure that JSON and the picture use the same normalized object copy."""
    from ..foundation._image_payload import validate_image_sidecar_path

    exported, image_files = _prepare_export_copy(middle_json)
    json_text = exported.to_json(
        exclude_block_fields={"image_base64"},
    )
    if "data:image/" in json_text.lower():
        raise ValueError("Exported Middle JSON still contains an inline image data URI")
    json_bytes = json_text.encode("utf-8")
    safe_json_name = validate_image_sidecar_path(json_name)
    relative_files = dict(image_files)
    if safe_json_name in relative_files:
        raise ValueError(f"JSON path conflicts with an exported image: {safe_json_name}")
    relative_files[safe_json_name] = json_bytes
    _validate_export_path_relationships(list(relative_files))
    absolute_files = {
        _resolve_export_target(output_dir, relative_path): payload for relative_path, payload in relative_files.items()
    }
    _commit_export_files(absolute_files, overwrite=overwrite)
    json_path = _resolve_export_target(output_dir, safe_json_name)
    image_paths = tuple(_resolve_export_target(output_dir, relative_path) for relative_path in sorted(image_files))
    return MiddleJsonExportResult(
        middle_json=exported,
        json_path=json_path,
        image_paths=image_paths,
    )


def export_middle_json(
    middle_json: MiddleJson, output_dir: str | Path, *, json_name: str = "middle_json.json", overwrite: bool = False
) -> MiddleJsonExportResult:
    """Explicitly export semantic protocols and images, keeping the base schema without file system dependencies."""
    return _export_middle_json(middle_json, Path(output_dir), json_name=json_name, overwrite=overwrite)


__all__ = ["MiddleJsonExportResult", "export_middle_json"]
