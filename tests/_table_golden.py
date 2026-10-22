"""读取协议 35 冻结表格预期，仅负责序列化，不包含参考算法。"""

from collections.abc import Mapping, Set
import dataclasses
from functools import lru_cache
import hashlib
import json
from pathlib import Path


@lru_cache(maxsize=1)
def table_golden():
    """加载由绝对冻结源码路径及原扩展采集的参数化语义预期。"""
    return json.loads((Path(__file__).parent / "fixtures/native_table_semantics.json").read_text(encoding="utf-8"))


def encode_table_value(value):
    """保留数值位、对象字段和稳定集合顺序，避免摘要掩盖有符号零差异。"""
    if isinstance(value, float):
        return {"float": value.hex()}
    if dataclasses.is_dataclass(value):
        return {field.name: encode_table_value(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): encode_table_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode_table_value(item) for item in value]
    if isinstance(value, Set) or hasattr(value, "members") or type(value).__name__ == "_NativeCoreLineSet":
        return sorted((encode_table_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or type(value) in (bool, int, str):
        return value
    raise TypeError(type(value))


def table_value_hash(value):
    """比较完整冻结输出，所有模型字段和坐标类型均参与摘要。"""
    return hashlib.sha256(json.dumps(encode_table_value(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fragment_row_value(rows):
    """比较组行的完整来源、框类型及视觉编号，均值由用例按运行时逐位核验。"""
    return [
        {
            "members": [fragment.line_index for fragment in row.fragments],
            "bbox": encode_table_value(row.bbox),
            "visual": row.visual_row_id,
        }
        for row in rows
    ]
