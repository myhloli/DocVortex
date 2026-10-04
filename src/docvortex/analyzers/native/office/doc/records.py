"""DOC Bounded integers, PLC and record budget tools used by binary structures."""

from __future__ import annotations

from dataclasses import dataclass
import struct

from ..errors import LegacyOfficeResourceLimitError
from ..limits import MAX_RECORDS


@dataclass(slots=True)
class DocBudget:
    """Limit the number of records and text units that DOC parses the cumulative access."""

    visited: int = 0

    def charge(self, amount: int = 1) -> None:
        """Counted into the current visit volume, stable failure occurs when the unified upper limit is exceeded."""

        if amount < 0 or self.visited + amount > MAX_RECORDS:
            raise LegacyOfficeResourceLimitError(f"DOC records exceed max_records={MAX_RECORDS}")
        self.visited += amount


def parse_plc(data: bytes, *, item_size: int, budget: DocBudget) -> tuple[list[int], list[bytes]]:
    """Parse a general PLC consisting of a CP array and fixed-length data items."""

    if item_size < 0 or len(data) < 4:
        return [], []
    denominator = 4 + item_size
    payload = len(data) - 4
    if denominator <= 0 or payload % denominator:
        return [], []
    count = payload // denominator
    budget.charge(count + 1)
    cp_bytes = (count + 1) * 4
    cps = [int(struct.unpack_from("<I", data, index * 4)[0]) for index in range(count + 1)]
    items = [data[cp_bytes + index * item_size : cp_bytes + (index + 1) * item_size] for index in range(count)]
    return cps, items
