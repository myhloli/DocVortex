"""RTF info 属性提取，仅读取前导信息，不构造正文语义树。"""

from __future__ import annotations

from io import BytesIO
import locale
import re

from .....document.properties import property_count, property_date, property_text, property_values
from .....schema import DocumentProperties
from .parser import _decode_group_text, _named_groups, parse_rtf_prelude, read_rtf_bytes


def _rtf_date(group: bytes, warnings: list[str]) -> str | None:
    """组合 info 日期控件，缺失的月日时间保持缺失。"""
    fields = dict(re.findall(rb"\\(yr|mo|dy|hr|min|sec)(\d+)", group))
    if b"yr" not in fields:
        return None
    value = fields[b"yr"].decode("ascii").zfill(4)
    for separator, name in (("-", b"mo"), ("-", b"dy"), ("T", b"hr"), (":", b"min"), (":", b"sec")):
        if name not in fields:
            break
        value += separator + fields[name].decode("ascii").zfill(2)
    return property_date(value, warnings=warnings)


def read_rtf_properties(data: bytes) -> tuple[DocumentProperties, list[str]]:
    """复用 RTF 编码和 info 解析，补充显式日期、软件及声明页数。"""
    warnings: list[str] = []
    data = read_rtf_bytes(BytesIO(data))
    prelude = parse_rtf_prelude(data)
    basic = prelude.metadata
    properties = DocumentProperties(
        title=basic.title,
        authors=property_values(basic.author),
        subject=basic.subject,
        keywords=property_values(basic.keywords),
    )
    infos = _named_groups(data, "info")
    if infos:
        info = infos[0].materialize()
        for field, name in (("created_at", "creatim"), ("modified_at", "revtim")):
            groups = _named_groups(info, name)
            if groups:
                setattr(properties, field, _rtf_date(groups[0].materialize(), warnings))
        comments = _named_groups(info, "doccomm")
        if comments:
            properties.description = property_text(_decode_group_text(comments[0].materialize(), prelude.default_encoding))
        pages = re.search(rb"\\nofpages(\d+)", info)
        if pages:
            properties.page_count = property_count(pages.group(1).decode("ascii"))
            properties.page_count_kind = "declared"
    generators = _named_groups(data, "generator")
    if generators:
        value = _decode_group_text(generators[0].materialize(), prelude.default_encoding)
        properties.creator_application = property_text(value.rstrip(";"))
    language = re.search(rb"\\deflang(\d+)", data[:4096])
    if language and (name := locale.windows_locale.get(int(language.group(1)))):
        properties.languages = [name.replace("_", "-")]
    return properties, warnings


__all__ = ["read_rtf_properties"]
