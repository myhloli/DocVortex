"""保存及读取不依赖源文件、网络或宿主缓存的结果包。"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from ..assets import AssetStore
from ..codecs.json import load_middle, load_model
from ..foundation.image_payload import INLINE_IMAGE_DATA_URI_RE, parse_image_data_uri_strict
from ..result import Diagnostic, DocumentResult, ExportResult
from .files import materialize_middle, validate_materialized_assets
from .middle import _commit_export_files, _resolve_export_target, _validate_export_path_relationships


def _externalize_model(value: Any, assets: AssetStore) -> Any:
    """在原始分析副本中外置图片及 HTML 内嵌素材，保持可独立重放。"""
    if isinstance(value, list):
        return [_externalize_model(item, assets) for item in value]
    if isinstance(value, dict):
        result = {key: _externalize_model(item, assets) for key, item in value.items()}
        image = value.get("image_base64")
        if isinstance(image, str):
            result["image_path"] = _add_image(image, assets)
            result.pop("image_base64", None)
        return result
    if isinstance(value, str) and "data:image/" in value and not value.startswith("data:image/"):
        return INLINE_IMAGE_DATA_URI_RE.sub(lambda match: _add_image(match.group(0), assets), value)
    return value


def _add_image(data_uri: str, assets: AssetStore) -> str:
    """按内容摘要登记分析阶段素材，避免产生不稳定文件名。"""
    payload, extension = parse_image_data_uri_strict(data_uri)
    path = f"images/{sha256(payload).hexdigest()}.{extension}"
    assets.add(path, payload)
    return path


def save_bundle(result: DocumentResult, path: Path, *, overwrite: bool = False) -> ExportResult:
    """将中间协议与素材清单放入同一文件事务。"""
    middle, assets = materialize_middle(result.middle_json, result.assets)
    validate_materialized_assets(middle, assets)
    files: dict[str, bytes] = {"middle.json": middle.to_json(skip_defaults=False).encode("utf-8")}
    manifest: dict[str, Any] = {
        "schema": "docvortex.bundle",
        "schema_version": "2.0",
        "middle": "middle.json",
        "diagnostics": [asdict(item) for item in result.diagnostics],
    }
    if result.model_json is not None:
        model = _externalize_model(result.model_json.to_dict(skip_defaults=False), assets)
        files["model.json"] = json.dumps(model, ensure_ascii=False, indent=2).encode("utf-8")
        manifest["model"] = "model.json"
    manifest["assets"] = [
        {"path": name, "size": len(payload), "sha256": sha256(payload).hexdigest()} for name, payload in sorted(assets.items())
    ]
    if {"manifest.json", "middle.json", "model.json"}.intersection(assets):
        raise ValueError("Bundle assets conflict with reserved document files")
    files.update(assets)
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    _validate_export_path_relationships(list(files))
    path = path.absolute()
    _commit_export_files({_resolve_export_target(path, name): value for name, value in files.items()}, overwrite=overwrite)
    return ExportResult(path / "manifest.json", tuple(path / name for name in assets))


def load_bundle(path: str | Path) -> DocumentResult:
    """校验结果包版本、路径及素材摘要后恢复可渲染结果。"""
    root = Path(path).absolute()
    manifest = json.loads(_resolve_export_target(root, "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "docvortex.bundle" or manifest.get("schema_version") != "2.0":
        raise ValueError("Unsupported DocVortex bundle schema")
    if manifest.get("middle") != "middle.json" or manifest.get("model") not in {None, "model.json"}:
        raise ValueError("Invalid bundle document paths")
    assets = AssetStore()
    for item in manifest["assets"]:
        if item["path"] in {"manifest.json", "middle.json", "model.json"}:
            raise ValueError("Bundle asset uses a reserved document path")
        payload = _resolve_export_target(root, item["path"]).read_bytes()
        if len(payload) != item["size"] or sha256(payload).hexdigest() != item["sha256"]:
            raise ValueError(f"Bundle asset integrity mismatch: {item['path']}")
        assets.add(item["path"], payload)
    middle = load_middle(json.loads(_resolve_export_target(root, "middle.json").read_text(encoding="utf-8")))
    validate_materialized_assets(middle, assets)
    model = (
        load_model(json.loads(_resolve_export_target(root, "model.json").read_text(encoding="utf-8")))
        if manifest.get("model")
        else None
    )
    diagnostics = tuple(Diagnostic(**item) for item in manifest.get("diagnostics", []))
    return DocumentResult(middle_json=middle, assets=assets, model_json=model, diagnostics=diagnostics)


__all__ = ["save_bundle", "load_bundle"]
