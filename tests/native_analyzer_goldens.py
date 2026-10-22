"""0.6 迁移前固定输入的语义预期；只保留数据，不加载历史实现。"""

import dataclasses
import gzip
import json
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest


def semantic_value(value):
    """保留公开字段、类型、顺序和正负零，忽略声明为不参与比较的内部缓存。"""
    if dataclasses.is_dataclass(value):
        return {
            "type": type(value).__name__,
            "fields": {field.name: semantic_value(getattr(value, field.name)) for field in dataclasses.fields(value) if field.compare},
        }
    if isinstance(value, dict):
        return {"dict": [[semantic_value(key), semantic_value(item)] for key, item in value.items()]}
    if isinstance(value, (set, frozenset)):
        return {"set": sorted([semantic_value(item) for item in value], key=lambda item: json.dumps(item, sort_keys=True))}
    if isinstance(value, tuple):
        return {"tuple": [semantic_value(item) for item in value]}
    if isinstance(value, list):
        return [semantic_value(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if type(value).__name__ == "Bbox":
        return {"Bbox": semantic_value(value.bbox)}
    if isinstance(value, SimpleNamespace):
        return {"namespace": semantic_value(vars(value))}
    return value


@lru_cache(maxsize=1)
def _expected_values():
    """读取在 protocol 35、PDFium 5.13 下冻结的语义结果。"""
    with gzip.open(Path(__file__).parent / "fixtures/native_analyzer_06_golden.json.gz", "rt", encoding="utf-8") as source:
        return json.load(source)


@pytest.fixture
def native_golden(request):
    """按原测试名和参数定位固定预期，逐次验证循环中的每个输出。"""
    params = getattr(getattr(request.node, "callspec", None), "params", {})
    key = json.dumps([Path(request.module.__file__).stem, request.node.originalname, params], sort_keys=True, separators=(",", ":"))
    expected = _expected_values()[key]
    cursor = 0

    def check(actual):
        """验证当前结果并推进到下一份固定预期。"""
        nonlocal cursor
        assert json.dumps(semantic_value(actual), sort_keys=True, ensure_ascii=True) == json.dumps(expected[cursor], sort_keys=True, ensure_ascii=True)
        cursor += 1

    return check
