"""快速多格式文档解析与转换引擎。"""

from .version import __version__
from .api import analyze, parse, convert, extract_metadata
from .api import postprocess as postprocess_document, render as render_artifact
from .export.bundle import load_bundle
from .result import MetadataResult, AnalysisResult, DocumentResult, RenderArtifact, ExportResult

__all__ = [
    "__version__",
    "analyze",
    "extract_metadata",
    "MetadataResult",
    "postprocess_document",
    "parse",
    "render_artifact",
    "convert",
    "load_bundle",
    "AnalysisResult",
    "DocumentResult",
    "RenderArtifact",
    "ExportResult",
]
