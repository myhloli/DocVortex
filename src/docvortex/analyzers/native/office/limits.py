"""Flash Office Fixed security restrictions shared by binary, embedded objects and RTF parsing."""

from typing import Final

MAX_ENTRY_BYTES: Final = 128 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 512 * 1024 * 1024
MAX_ASSET_TOTAL_BYTES: Final = 128 * 1024 * 1024
# Formula detection is billed separately to prevent ordinary pictures from depleting the embedded material budget; the initial value is consistent with the material budget.
MAX_EQUATION_CANDIDATE_TOTAL_BYTES: Final = 128 * 1024 * 1024
MAX_GRID_SLOTS: Final = 4_000_000
MAX_RECORD_DEPTH: Final = 64
MAX_RECORDS: Final = 16_000_000
MAX_PICTURE_RECORDS: Final = 100_000
MAX_USER_EDIT_CHAIN: Final = 100
