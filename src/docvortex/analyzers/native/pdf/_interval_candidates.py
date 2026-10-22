"""在调用内保存线性规模的区间树，并按原行索引有界地产生同行候选。"""

from ...._compute_backend import get_native


class IntervalCandidates:
    """代替密集页面的全配对回退，保持下标查询与稳定右侧成员顺序。"""

    def __init__(self, bounds, groups, *, geometry=None):
        """冻结本轮数值区间；原生索引不持有行对象或 PDF 句柄。"""
        self.bounds = bounds
        self.group_ids = [0] * len(bounds)
        for group_id, indices in enumerate(groups.values()):
            for index in indices:
                self.group_ids[index] = group_id
        native = get_native()
        self.native = (
            native.BaselineGeometryCandidates(bounds, self.group_ids, *geometry)
            if geometry is not None
            else native.BaselineCandidates(bounds, self.group_ids)
        )
        self.cached_start = 0
        self.cached_rows = []

    def __len__(self):
        """返回源行数，供序列消费及存储规模检查。"""
        return len(self.bounds)

    def __getitem__(self, index):
        """按源行顺序查询；只缓存至多一批行对，异常下标明确失败。"""
        if not 0 <= index < len(self.bounds):
            raise IndexError(index)
        if not self.cached_start <= index < self.cached_start + len(self.cached_rows):
            self.cached_start = index
            self.cached_rows = self.native.rows(index, 64, 8192)
        return self.cached_rows[index - self.cached_start]
