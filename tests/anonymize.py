"""
Strip personal data from a FIT file before it becomes a test fixture.

Overwrites GPS positions and serial numbers with the FIT invalid value. Everything
else stays byte-identical (same in-place patcher as the correction), so a fixture
and its corrected counterpart stay comparable byte for byte.

Usage:
    uv run python -m tests.anonymize ride.fit [more.fit ...]    # rewrites the files in place
"""
import sys
from pathlib import Path

from pedal_balance.fitfile import Fit

# message number → field numbers with personal data
PERSONAL = {
    0: (3,),                            # file_id: serial_number
    18: (3, 4, 29, 30, 31, 32, 38, 39),  # session: start/end position, bounding box
    19: (3, 4, 5, 6),                   # lap: start/end position
    20: (0, 1),                         # record: position_lat/long
    23: (3,),                           # device_info: serial_number
}


def anonymize(path: Path) -> int:
    fit = Fit.read(path)
    changed = sum(fit.invalidate(m, num) for m in fit.msgs for num in PERSONAL.get(m.gnum, ()))
    fit.finalize()
    path.write_bytes(fit.buf)
    return changed


if __name__ == "__main__":
    for path in sys.argv[1:]:
        print(f"{path}: {anonymize(Path(path))} fields invalidated")
