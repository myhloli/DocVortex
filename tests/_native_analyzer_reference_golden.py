"""读取 0.6 迁移前冻结版本生成的语义期望，不加载或执行旧后端代码。"""

from __future__ import annotations

import gzip
import importlib
import json
import os
from functools import lru_cache
from pathlib import Path

_CURRENT_TEST = None
_CALL_COUNTS: dict[str, int] = {}


@lru_cache(maxsize=1)
def _data():
    """一次解压固定 JSON 数据，源结果来自协议 35 与 pypdfium2 5.13.0。"""
    path = Path(__file__).parent / "fixtures" / "native_analyzer_reference_06_golden.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


def _decode(value, memo=None):
    """重建固定数据及共享引用，不支持执行代码的序列化格式。"""
    if not isinstance(value, dict):
        return value
    memo = {} if memo is None else memo
    if "@ref" in value:
        return memo[value["@ref"]]
    if "@dict" in value:
        result = {_decode(key, memo): _decode(item, memo) for key, item in value["@dict"]}
    elif "@bbox" in value:
        from docvortex.document.pdf.text._contracts import Bbox

        coordinates, ensure_nonzero_area = value["@bbox"]
        result = Bbox(_decode(coordinates, memo), ensure_nonzero_area=ensure_nonzero_area)
    elif "@class" in value:
        module, name = value["@class"].split(":")
        if not module.startswith("docvortex.") or "." in name:
            raise ValueError("Unsupported golden data type")
        cls = getattr(importlib.import_module(module), name)
        result = cls(**{key: _decode(item, memo) for key, item in value["fields"].items()})
    elif "@bytes" in value:
        result = bytes.fromhex(value["@bytes"])
    else:
        for name, constructor in (("tuple", tuple), ("list", list), ("set", set), ("frozenset", frozenset)):
            if f"@{name}" in value:
                result = constructor(_decode(item, memo) for item in value[f"@{name}"])
                break
        else:
            raise ValueError("Unknown golden record")
    memo[value["@id"]] = result
    return result


def native_analyzer_golden(label):
    """按原测试位置和参数读取独立副本，循环中的多次期望保持生成顺序。"""
    global _CURRENT_TEST
    node = os.environ["PYTEST_CURRENT_TEST"].split(" (")[0]
    if node != _CURRENT_TEST:
        _CURRENT_TEST = node
        _CALL_COUNTS.clear()
    key = f"{node}|{label}"
    index = _CALL_COUNTS.get(label, 0)
    _CALL_COUNTS[label] = index + 1
    data = _data()
    return _decode(data["values"][data["calls"][key][index]])
