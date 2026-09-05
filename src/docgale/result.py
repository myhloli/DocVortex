"""完整文档结果与渲染产物；便捷方法显式委托对应处理层。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assets import AssetStore
from .schema import MiddleJson, ModelJson
from .render.contracts import RenderFormat, RenderOptions


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """记录可定位的非致命文档处理信息。"""

    code: str
    message: str
    page_index: int | None = None


@dataclass(slots=True)
class AnalysisResult:
    """原生分析输出，允许独立进行后处理或交给其他消费者。"""

    model_json: ModelJson
    assets: AssetStore = field(default_factory=AssetStore)
    diagnostics: tuple[Diagnostic, ...] = ()
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ExportResult:
    """返回实际写出的主文件及素材路径。"""

    path: Path
    asset_paths: tuple[Path, ...] = ()


@dataclass(slots=True)
class RenderArtifact:
    """可独立写出的主文件内容和全部旁文件。"""

    content: bytes
    output_format: RenderFormat
    mime_type: str
    assets: AssetStore = field(default_factory=AssetStore)
    diagnostics: tuple[Diagnostic, ...] = ()

    def write(self, path: str | Path, *, overwrite: bool = False) -> ExportResult:
        """预检全部文件后原子提交，不在渲染期间执行文件写入。"""
        from .export.files import write_artifact

        return write_artifact(self, Path(path), overwrite=overwrite)


@dataclass(slots=True)
class DocumentResult:
    """持有语义文档与素材，解析结束后无需保留源文档。"""

    middle_json: MiddleJson
    assets: AssetStore = field(default_factory=AssetStore)
    model_json: ModelJson | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def export(self, path: str | Path, *, output_format: RenderFormat | str = RenderFormat.MARKDOWN,
               options: RenderOptions | None = None, overwrite: bool = False) -> ExportResult:
        """复用当前解析结果渲染指定格式，不重新分析输入。"""
        from .api import render

        return render(self.middle_json, output_format, assets=self.assets, options=options).write(path, overwrite=overwrite)

    def save_bundle(self, path: str | Path, *, overwrite: bool = False) -> ExportResult:
        """把当前文档、可选分析结果与素材保存成可移植结果包。"""
        from .export.bundle import save_bundle

        return save_bundle(self, Path(path), overwrite=overwrite)

    def to_dict(self) -> dict[str, Any]:
        """仅返回中性语义协议；素材通过结果包或显式接口保存。"""
        return self.middle_json.to_dict()


__all__ = ["Diagnostic", "AnalysisResult", "DocumentResult", "RenderArtifact", "ExportResult"]
