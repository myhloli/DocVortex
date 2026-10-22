"""以来源索引连接批量统计与 Python 对象，保留非有限输入的参考行为。"""

from ...._compute_backend import get_native


def ordered_clusters(values, tolerance, *, relative=0.0, last_only=False):
    """由原生内核聚类，输入转换保留整数坐标与非有限值的排序语义。"""
    return get_native().ordered_clusters([float(value) for value in values], float(tolerance), float(relative), last_only)
