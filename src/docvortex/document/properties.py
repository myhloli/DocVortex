"""源属性的纯数据规范化，不读取宿主配置或推断缺失值。"""

from __future__ import annotations

from datetime import date, datetime
import re

from ..schema import DocumentProperties


def property_text(value: object) -> str | None:
    """规范化实际文本值，拒绝把容器和任意对象转换成伪属性。"""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip().replace("\x00", "") or None if isinstance(value, str) else None


def property_values(value: object) -> list[str]:
    """将源标量或序列转成稳定列表，不拆分作者姓名或关键词字符串。"""
    values = value if isinstance(value, (list, tuple)) else [value]
    return list(dict.fromkeys(text for item in values if (text := property_text(item))))


def property_date(value: object, *, warnings: list[str] | None = None) -> str | None:
    """保留日期精度和显式时区；支持 PDF 日期而不补造缺失时间。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = property_text(value)
    if not text:
        return None
    if text.startswith("D:"):
        match = re.fullmatch(r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(Z|[+-]\d{2}'?\d{2}'?)?", text)
        if match is None:
            if warnings is not None:
                warnings.append(f"Invalid declared date: {text[:120]}")
            return None
        year, month, day, hour, minute, second, zone = match.groups()
        text = year
        for separator, part in (("-", month), ("-", day), ("T", hour), (":", minute), (":", second)):
            if part is not None:
                text += separator + part
        if zone:
            zone = zone.replace("'", "")
            text += zone if zone == "Z" else zone[:3] + ":" + zone[3:]
    try:
        if re.fullmatch(r"\d{4}", text):
            date(int(text), 1, 1)
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            date(int(text[:4]), int(text[5:]), 1)
        elif "T" in text:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            date.fromisoformat(text)
    except ValueError:
        if warnings is not None:
            warnings.append(f"Invalid declared date: {text[:120]}")
        return None
    return text


def property_count(value: object) -> int | None:
    """接受非负计数，非法或缺失的声明值保持未知。"""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, str)):
        return None
    try:
        count = int(value)
    except ValueError:
        return None
    return count if count >= 0 else None


def legacy_properties(properties: DocumentProperties) -> dict[str, object | None]:
    """供既有按格式接口投影基础属性，格式读取仍只有一份实现。"""
    return {
        "page_count": properties.page_count,
        "title": properties.title,
        "author": "; ".join(properties.authors) or None,
        "subject": properties.subject,
        "keywords": ", ".join(properties.keywords) or None,
    }


__all__ = ["property_text", "property_values", "property_date", "property_count", "legacy_properties"]
