"""A linearly scaled interval tree is saved within the call and peer candidates are generated bounded by the original row index."""

from bisect import bisect_right

from ...._compute_backend import get_native


class IntervalCandidates:
    """Instead of full-pair fallback for dense pages, keep subscripted queries with stable right-hand member order."""

    def __init__(self, bounds, groups, *, geometry=None):
        """Freezes the value range for this round; the native index does not hold row objects or PDF handles."""
        self.bounds = bounds
        self.group_ids = [0] * len(bounds)
        for group_id, indices in enumerate(groups.values()):
            for index in indices:
                self.group_ids[index] = group_id
        native = get_native()
        self.native = None
        if native is not None:
            self.native = (
                native.BaselineGeometryCandidates(bounds, self.group_ids, *geometry)
                if geometry is not None
                else native.BaselineCandidates(bounds, self.group_ids)
            )
        self.cached_start = 0
        self.cached_rows = []
        self.groups = []
        if self.native is not None:
            return
        for indices in groups.values():
            order = sorted(indices, key=lambda i: (bounds[i][0], i))
            width = 1 << max(0, (len(order) - 1).bit_length())
            maxima = [float("-inf")] * (2 * width)
            for position, index in enumerate(order):
                maxima[width + position] = bounds[index][1]
            for node in range(width - 1, 0, -1):
                maxima[node] = max(maxima[2 * node], maxima[2 * node + 1])
            self.groups.append((order, [bounds[i][0] for i in order], maxima, width))

    def __len__(self):
        """Returns the number of source rows for sequence consumption and storage scale checking."""
        return len(self.bounds)

    def __getitem__(self, index):
        """Query in source row order; only cache at most one batch of row pairs, exception subscript explicitly fails."""
        if not 0 <= index < len(self.bounds):
            raise IndexError(index)
        if self.native is not None:
            if not self.cached_start <= index < self.cached_start + len(self.cached_rows):
                self.cached_start = index
                self.cached_rows = self.native.rows(index, 64, 8192)
            return self.cached_rows[index - self.cached_start]
        low, high = self.bounds[index]
        order, lows, maxima, width = self.groups[self.group_ids[index]]
        end = bisect_right(lows, high)
        stack = [(1, 0, width)]
        result = []
        while stack:
            node, left, right = stack.pop()
            if left >= end or maxima[node] < low:
                continue
            if right - left == 1:
                if (other := order[left]) > index:
                    result.append(other)
            else:
                middle = (left + right) // 2
                stack.append((2 * node + 1, middle, right))
                stack.append((2 * node, left, middle))
        return sorted(result)
