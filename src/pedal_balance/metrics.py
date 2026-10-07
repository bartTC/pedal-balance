"""Power metrics on record dicts (see fitfile.records)."""
import math
from collections.abc import Iterator, Sequence

from .fitfile import Record

G = 9.81
HOUR = 3600
NP_WINDOW = 30               # seconds, rolling average for normalized power
MIN_HR_SAMPLES = 60          # watts per heartbeat needs at least a minute of pedaling with HR
BLOCK = 30                   # seconds per block for the speed model
MIN_BLOCK_SPEED = 4          # m/s, slower blocks are too noisy for the speed model

Block = tuple[float, float, float]   # avg power, avg speed, avg grade


def rnd(x: float) -> int:
    """Round half up (round() rounds 0.5 to the even number)."""
    return math.floor(x + 0.5)


def mmss(s: float) -> str:
    s = int(s)
    return f"{s // HOUR}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= HOUR else f"{s // 60}:{s % 60:02d}"


def power_series(recs: list[Record], t_start: int, t_end: int) -> list[int]:
    """1 Hz series t_start..t_end, missing seconds (auto-pause) = 0 W."""
    arr = [0] * (t_end - t_start + 1)
    for r in recs:
        if t_start <= r["ts"] <= t_end:
            arr[r["ts"] - t_start] = r["power"] or 0
    return arr


def rolling_sums(values: Sequence[float], win: int) -> Iterator[float]:
    """Sums of every window of `win` consecutive values."""
    s = sum(values[:win])
    yield s
    for i in range(win, len(values)):
        s += values[i] - values[i - win]
        yield s


def normalized_power(arr: Sequence[float], win: int = NP_WINDOW) -> float | None:
    if len(arr) < win:
        return None
    fourth = [(s / win) ** 4 for s in rolling_sums(arr, win)]
    return (sum(fourth) / len(fourth)) ** 0.25


def best_avg(powers: Sequence[float], win: int) -> float | None:
    """Best average power over `win` consecutive values (power curve)."""
    return max(rolling_sums(powers, win)) / win if len(powers) >= win else None


def weighted_share(recs: list[Record], min_power: float = 1) -> float | None:
    """Power-weighted stored balance value (lower 7 bits), as intervals.icu shows it."""
    num = den = 0
    for r in recs:
        if (r["power"] or 0) >= min_power and r["lrb"] is not None:
            num += r["power"] * (r["lrb"] & 0x7F)
            den += r["power"]
    return num / den if den else None


def watts_per_beat(recs: list[Record]) -> float | None:
    ped = [r for r in recs if (r["power"] or 0) > 0 and r["hr"]]
    if len(ped) < MIN_HR_SAMPLES:
        return None
    return sum(r["power"] for r in ped) / sum(r["hr"] for r in ped)


def blocks30(recs: list[Record]) -> list[Block]:
    """Contiguous 30 s blocks at speed ≥ 4 m/s: (P, v, grade)."""
    out = []
    for i in range(0, len(recs) - BLOCK + 1, BLOCK):
        b = recs[i:i + BLOCK]
        if b[-1]["ts"] - b[0]["ts"] != BLOCK - 1 or any(r["v"] is None for r in b):
            continue
        v = sum(r["v"] for r in b) / BLOCK
        if v >= MIN_BLOCK_SPEED:
            out.append((sum(r["power"] or 0 for r in b) / BLOCK, v, sum(r["grade"] for r in b) / BLOCK))
    return out


class SpeedModel:
    """
    P = a·v³ + b·v + m·g·v·grade, a and b fitted on the rest of the ride.

    Wind and drafting are not modeled; the spread between the first and second
    half of the rest shows how accurate the model is for this ride.
    """

    def __init__(self, blocks: list[Block], mass: float) -> None:
        self.mass = mass
        y = [p - mass * G * v * g for p, v, g in blocks]
        s33 = sum(v ** 6 for _, v, _ in blocks)
        s31 = sum(v ** 4 for _, v, _ in blocks)
        s11 = sum(v ** 2 for _, v, _ in blocks)
        t3 = sum(yy * v ** 3 for yy, (_, v, _) in zip(y, blocks, strict=True))
        t1 = sum(yy * v for yy, (_, v, _) in zip(y, blocks, strict=True))
        det = s33 * s11 - s31 ** 2
        self.a = (t3 * s11 - t1 * s31) / det
        self.b = (s33 * t1 - s31 * t3) / det

    def ratio(self, blocks: list[Block]) -> float | None:
        pred = sum(self.a * v ** 3 + self.b * v + self.mass * G * v * g for _, v, g in blocks)
        return sum(p for p, _, _ in blocks) / pred if pred > 0 and blocks else None
