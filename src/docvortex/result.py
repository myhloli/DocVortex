"""Complete documentation of results and rendering products; convenience methods to explicitly delegate to the corresponding processing layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assets import AssetStore
from .schema import DocumentMetadata, MiddleJson, ModelJson
from .render.contracts import RenderFormat, RenderOptions


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Logs locatable, non-fatal document processing information."""

    code: str
    message: str
    page_index: int | None = None


@dataclass(slots=True)
class MetadataResult:
    """Returns source document properties that are read independently and diagnostics that do not affect text parsing."""

    metadata: DocumentMetadata
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(slots=True)
class AnalysisResult:
    """Native analysis output, allowing independent post-processing or delivery to other consumers."""

    model_json: ModelJson
    assets: AssetStore = field(default_factory=AssetStore)
    diagnostics: tuple[Diagnostic, ...] = ()
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Returns the actual written main file and material path."""

    path: Path
    asset_paths: tuple[Path, ...] = ()


@dataclass(slots=True)
class RenderArtifact:
    """The contents of the main file and all side files can be written independently."""

    content: bytes
    output_format: RenderFormat
    mime_type: str
    assets: AssetStore = field(default_factory=AssetStore)
    diagnostics: tuple[Diagnostic, ...] = ()

    def write(self, path: str | Path, *, overwrite: bool = False) -> ExportResult:
        """Atomic commit after prefetching all files, no file writing is performed during rendering."""
        from .export.files import write_artifact

        return write_artifact(self, Path(path), overwrite=overwrite)


@dataclass(slots=True)
class DocumentResult:
    """It holds semantic documents and materials, and there is no need to retain the source documents after parsing."""

    middle_json: MiddleJson
    assets: AssetStore = field(default_factory=AssetStore)
    model_json: ModelJson | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def export(
        self,
        path: str | Path,
        *,
        output_format: RenderFormat | str = RenderFormat.MARKDOWN,
        options: RenderOptions | None = None,
        overwrite: bool = False,
    ) -> ExportResult:
        """Reuse the current parsing results to render the specified format without re-analyzing the input."""
        from .api import render

        return render(self.middle_json, output_format, assets=self.assets, options=options).write(path, overwrite=overwrite)

    def save_bundle(self, path: str | Path, *, overwrite: bool = False) -> ExportResult:
        """Save the current document, optional analysis results, and materials into a portable results package."""
        from .export.bundle import save_bundle

        return save_bundle(self, Path(path), overwrite=overwrite)

    def to_dict(self) -> dict[str, Any]:
        """Only semantically neutral protocols are returned; material is saved via result packets or explicit interfaces."""
        return self.middle_json.to_dict()


__all__ = ["Diagnostic", "MetadataResult", "AnalysisResult", "DocumentResult", "RenderArtifact", "ExportResult"]
