"""共享文档协议的显式读取入口。"""

from typing import Any
from ..schema import MiddleJson, ModelJson


def load_model(value: dict[str, Any]) -> ModelJson:
    """读取新版分析文档，不执行解析、补写来源或历史迁移。"""
    return ModelJson.from_dict(value)


def load_middle(value: dict[str, Any]) -> MiddleJson:
    """读取新版语义文档，不访问源文件和外部素材。"""
    return MiddleJson.from_dict(value)


__all__ = ["load_model", "load_middle"]
