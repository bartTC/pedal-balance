"""
Minimal FIT reader/patcher that remembers the byte position of every field.

The file is never re-encoded: fields are overwritten in place and the CRC is
recomputed, so definitions, developer fields and message order stay
byte-identical (the Karoo writes 16 developer fields that a re-encoder might
drop).
"""
import struct
from pathlib import Path
from typing import Any

FIELD_TIMESTAMP = 253
MSG_FILE_ID, MSG_SESSION, MSG_LAP, MSG_RECORD, MSG_DEVICE_INFO = 0, 18, 19, 20, 23
OTHER_POWER_MSGS = {142: "segment_lap", 312: "split", 313: "split_summary"}

REC = {"timestamp": 253, "power": 7, "lrb": 30, "cadence": 4, "heart_rate": 3,
       "temperature": 13, "speed": 6, "enhanced_speed": 73, "grade": 9,
       "accumulated_power": 29}
# Torque effectiveness / pedal smoothness cannot be recomputed cleanly -> invalid
REC_TE_PS = {43: "left_torque_effectiveness", 44: "right_torque_effectiveness",
             45: "left_pedal_smoothness", 46: "right_pedal_smoothness",
             47: "combined_pedal_smoothness"}
LAP = {"start_time": 2, "total_timer_time": 8, "avg_power": 19, "max_power": 20,
       "normalized_power": 33, "left_right_balance": 34, "total_work": 41}
LAP_TE_PS = {91: "avg_left_torque_effectiveness", 92: "avg_right_torque_effectiveness",
             93: "avg_left_pedal_smoothness", 94: "avg_right_pedal_smoothness",
             95: "avg_combined_pedal_smoothness"}
SES = {"start_time": 2, "total_timer_time": 8, "avg_power": 20, "max_power": 21,
       "normalized_power": 34, "training_stress_score": 35, "intensity_factor": 36,
       "left_right_balance": 37, "threshold_power": 45, "total_work": 48}
SES_TE_PS = {101: "avg_left_torque_effectiveness", 102: "avg_right_torque_effectiveness",
             103: "avg_left_pedal_smoothness", 104: "avg_right_pedal_smoothness",
             105: "avg_combined_pedal_smoothness"}

# FIT base types: struct format and invalid value
BASE = {0x00: ("B", 0xFF), 0x01: ("b", 0x7F), 0x02: ("B", 0xFF), 0x0D: ("B", 0xFF),
        0x83: ("h", 0x7FFF), 0x84: ("H", 0xFFFF), 0x85: ("i", 0x7FFFFFFF),
        0x86: ("I", 0xFFFFFFFF), 0x0A: ("B", 0), 0x8B: ("H", 0), 0x8C: ("I", 0)}

CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


Record = dict[str, Any]   # one record message, see records()


def fit_crc(data: bytes | bytearray, crc: int = 0) -> int:
    for byte in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ CRC_TABLE[byte & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ CRC_TABLE[(byte >> 4) & 0xF]
    return crc


class Msg:
    __slots__ = ("endian", "fields", "gnum", "ts")

    def __init__(self, gnum: int, endian: str) -> None:
        self.gnum, self.endian = gnum, endian
        self.fields: dict[int, tuple[int, int, int]] = {}   # number → (offset, size, base type)
        self.ts: int | None = None


class Fit:
    def __init__(self, buf: bytes | bytearray) -> None:
        self.buf = bytearray(buf)
        self.patched: list[tuple[int, int]] = []  # (offset, size) of all overwritten fields
        self.hsize = self.buf[0]
        self.body_end = self.hsize + struct.unpack_from("<I", self.buf, 4)[0]
        if self.buf[8:12] != b".FIT":
            msg = "not a FIT file"
            raise ValueError(msg)
        if len(self.buf) > self.body_end + 2:
            msg = "chained FIT files are not supported"
            raise ValueError(msg)
        self.msgs = self._parse()

    @classmethod
    def read(cls, path: str | Path) -> "Fit":
        return cls(Path(path).read_bytes())

    @property
    def crc_ok(self) -> bool:
        return fit_crc(self.buf[:self.body_end + 2]) == 0

    def _parse(self) -> list[Msg]:
        buf, defs, msgs = self.buf, {}, []
        pos, last_ts = self.hsize, None
        while pos < self.body_end:
            h = buf[pos]
            pos += 1
            ts = None
            if h & 0x80:  # compressed timestamp header
                local, toff = (h >> 5) & 0x3, h & 0x1F
                if last_ts is not None:
                    ts = (last_ts & ~0x1F) + toff
                    if toff < (last_ts & 0x1F):
                        ts += 0x20
                    last_ts = ts
                is_def = has_dev = False
            else:
                local, is_def, has_dev = h & 0x0F, bool(h & 0x40), bool(h & 0x20)
            if is_def:
                endian = ">" if buf[pos + 1] else "<"
                gnum = struct.unpack_from(endian + "H", buf, pos + 2)[0]
                nf = buf[pos + 4]
                pos += 5
                fdefs = [(buf[pos + 3 * i], buf[pos + 3 * i + 1], buf[pos + 3 * i + 2])
                         for i in range(nf)]
                pos += 3 * nf
                dsize = 0
                if has_dev:
                    nd = buf[pos]
                    pos += 1
                    dsize = sum(buf[pos + 3 * i + 1] for i in range(nd))
                    pos += 3 * nd
                defs[local] = (gnum, endian, fdefs, dsize)
                continue
            gnum, endian, fdefs, dsize = defs[local]
            m = Msg(gnum, endian)
            for num, size, btype in fdefs:
                m.fields[num] = (pos, size, btype)
                pos += size
            pos += dsize
            if FIELD_TIMESTAMP in m.fields:
                last_ts = m.ts = self.get(m, FIELD_TIMESTAMP)
            elif ts is not None:
                m.ts = ts
            msgs.append(m)
        return msgs

    def _fmt(self, m: Msg, num: int) -> tuple[int, str | None, int | None]:
        o, size, btype = m.fields[num]
        fmt, invalid = BASE.get(btype, (None, None))
        if fmt is None or struct.calcsize(fmt) != size:
            return o, None, None
        return o, fmt, invalid

    def get(self, m: Msg, num: int) -> int | None:
        if num not in m.fields:
            return None
        o, fmt, invalid = self._fmt(m, num)
        if fmt is None:
            return None
        v = struct.unpack_from(m.endian + fmt, self.buf, o)[0]
        return None if v == invalid else v

    def put(self, m: Msg, num: int, value: int) -> None:
        o, fmt, invalid = self._fmt(m, num)
        if fmt is None or invalid is None:
            msg = f"field {num} has an unsupported base type"
            raise ValueError(msg)
        if not 0 <= value < invalid:
            msg = f"field {num}: {value} is out of range"
            raise ValueError(msg)
        struct.pack_into(m.endian + fmt, self.buf, o, value)
        self.patched.append((o, struct.calcsize(fmt)))

    def invalidate(self, m: Msg, num: int) -> bool:
        """Set a field to the invalid value of its base type. True if it changed."""
        if num not in m.fields or self.get(m, num) is None:
            return False
        o, fmt, invalid = self._fmt(m, num)
        struct.pack_into(m.endian + fmt, self.buf, o, invalid)
        self.patched.append((o, struct.calcsize(fmt)))
        return True

    def of(self, gnum: int) -> list[Msg]:
        return [m for m in self.msgs if m.gnum == gnum]

    def finalize(self) -> int:
        crc = fit_crc(self.buf[:self.body_end])
        struct.pack_into("<H", self.buf, self.body_end, crc)
        return crc


def records(fit: Fit) -> list[Record]:
    """Record messages as dicts; "msg" points back to the message for patching."""
    out = []
    for m in fit.of(MSG_RECORD):
        if m.ts is None:
            continue
        speed = fit.get(m, REC["enhanced_speed"])
        if speed is None:
            speed = fit.get(m, REC["speed"])
        grade = fit.get(m, REC["grade"])
        out.append({"msg": m, "ts": m.ts, "power": fit.get(m, REC["power"]),
                    "lrb": fit.get(m, REC["lrb"]), "cad": fit.get(m, REC["cadence"]),
                    "hr": fit.get(m, REC["heart_rate"]), "temp": fit.get(m, REC["temperature"]),
                    "v": speed / 1000 if speed is not None else None,
                    "grade": grade / 10000 if grade is not None else 0.0,  # sint16, 1/100 %
                    "acc": fit.get(m, REC["accumulated_power"])})
    return out


def pauses(recs: list[Record]) -> list[tuple[int, int]]:
    """Gaps between records = auto-pauses: (last record before, first record after)."""
    return [(recs[i - 1]["ts"], recs[i]["ts"]) for i in range(1, len(recs))
            if recs[i]["ts"] - recs[i - 1]["ts"] > 1]
