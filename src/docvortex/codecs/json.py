"""版本化 DocVortex JSON 的独立读取接口。"""

from __future__ import annotations

from typing import Any

from ..schema import MiddleJson, ModelJson


def _payload(value: dict[str, Any], expected_schema: str) -> dict[str, Any]:
    """先验证协议身份和版本，再交给严格类型校验内容。"""
    if value.get("schema") != expected_schema or value.get("schema_version") != "1.0":
        raise ValueError(f"Expected {expected_schema} schema version 1.0")
    if "producer" not in value:
        raise ValueError("Missing document producer")
    return {key: item for key, item in value.items() if key not in {"schema", "schema_version"}}


def load_model(value: dict[str, Any]) -> ModelJson:
    """读取原生分析协议，不执行解析或后处理。"""
    return ModelJson.model_validate(_payload(value, "docvortex.model"))


def load_middle(value: dict[str, Any]) -> MiddleJson:
    """读取语义文档协议，不访问源文件或素材。"""
    return MiddleJson.model_validate(_payload(value, "docvortex.middle"))


__all__ = ["load_model", "load_middle"]
