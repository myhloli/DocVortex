"""PDF 布局扩展的页尺寸规则，供协议模型和 PDF 读写共用。"""

import math


LAYOUT_EXTENSION = "docvortex_layout"


def round_layout_dimension(value: object) -> int | None:
    """把有限正尺寸四舍五入为整数 pt；不足半点返回 0，无效输入返回 None。"""
    if type(value) is int:
        return value if value > 0 else None
    if type(value) is not float or not math.isfinite(value) or value <= 0:
        return None
    whole = int(value)
    # 单独比较小数部分，避免加 0.5 时把略低于半点的数提前进位。
    return whole + int(value - whole >= 0.5)


def normalize_layout_dimensions(layout: object) -> None:
    """在扩展副本中规范 v1 的有效页尺寸，保留未知字段和供读取端诊断的非法记录。"""
    if not isinstance(layout, dict) or type(layout.get("version")) is not int or layout["version"] != 1:
        return
    pages = layout.get("pages")
    if not isinstance(pages, list):
        return
    retained = []
    for page in pages:
        if isinstance(page, dict):
            width = round_layout_dimension(page.get("width_pt"))
            height = round_layout_dimension(page.get("height_pt"))
            if width is not None and height is not None:
                if width == 0 or height == 0:
                    continue
                page["width_pt"], page["height_pt"] = width, height
        retained.append(page)
    layout["pages"] = retained
