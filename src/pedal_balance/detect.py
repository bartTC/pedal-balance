"""
Finds balance errors: a step at a stop, a bias over the whole ride, dropouts.

Only steady pedaling counts (above 80 W and above 60 rpm), power-weighted.
Sprints, coasting and accelerations shift the balance more than a measurement
error otherwise would.
"""
import bisect
import math
from dataclasses import dataclass, field

from .fitfile import Fit, Record, pauses, records
from .metrics import mmss, watts_per_beat, weighted_share

MIN_POWER, MIN_CAD = 80, 60   # steady pedaling: above 80 W and above 60 rpm
MIN_SECTION_S = 300           # section of a step: at least 5 min of steady pedaling
STEP_PTS = 5                  # step: section vs. rest of the ride
BIAS_PTS = 5                  # bias: ride vs. 50/50
DROPOUT_S = 3                 # this many seconds without power count as a dropout
AROUND_S = 300                # balance right before/after a boundary: 5 min
# Files written by `pedal-balance fix`; "_korrigiert" is the suffix of the first (German) version
CORRECTED_SUFFIXES = ("_corrected.fit", "_korrigiert.fit")


def is_corrected(name: str | None) -> bool:
    return str(name or "").endswith(CORRECTED_SUFFIXES)


@dataclass
class Edge:
    """A stop or dropout that can bound a section."""

    kind: str      # "pause" or "dropout"
    last: int      # timestamp of the last record before it
    first: int     # timestamp of the first record after it
    label: str


@dataclass
class Step:
    sec: float               # balance in the section
    rest: float              # balance in the rest of the ride
    seconds: int             # steady pedaling in the section
    start: Edge | None       # None = start of the ride
    end: Edge | None         # None = end of the ride
    span: str = ""
    t_from: int = 0
    t_to: int = 0
    bounds: list[tuple[str, float | None, float | None]] = field(default_factory=list)  # label, before, after
    wpb: float | None = None                     # watts per heartbeat, section ÷ rest

    @property
    def correction(self) -> float:
        """
        Approximate correction in the section, like `fix --trust auto`: the side
        whose share dropped in the section measures correctly.
        """
        if self.sec < self.rest:
            return self.sec / self.rest - 1
        return (100 - self.sec) / (100 - self.rest) - 1

    @property
    def at_start(self) -> bool:
        return self.start is None and self.end is not None


@dataclass
class Finding:
    error: str | None = None
    share: float | None = None          # balance over the whole ride
    step: Step | None = None
    bias: float | None = None           # points off 50/50 (signed), if ≥ BIAS_PTS
    bias_base: float | None = None      # balance the bias was measured on
    dropouts: list[tuple[int, int]] = field(default_factory=list)   # (offset s, duration s)
    no_balance: float = 0.0             # share of pedaling time with power but no balance
    corrected: bool = False             # file written by `fix`

    @property
    def lost(self) -> int:
        return sum(d for _, d in self.dropouts)


def steady(r: Record) -> bool:
    return (r["power"] or 0) > MIN_POWER and (r["cad"] or 0) > MIN_CAD and r["lrb"] is not None


def dropouts(recs: list[Record]) -> list[tuple[int, int]]:
    """Runs without power while the timer runs (pedals not connected): [(first, last ts)]."""
    out, start = [], None
    for prev, r in zip([None, *recs], [*recs, None], strict=True):
        if r is not None and r["power"] is None:
            if start is None:
                start = r["ts"]
        elif start is not None:
            if prev["ts"] - start + 1 >= DROPOUT_S:
                out.append((start, prev["ts"]))
            start = None
    return out


def find_step(recs: list[Record], st: list[Record]) -> Step | None:
    """
    Strongest section between two stops/dropouts (or start/end) that differs by
    at least STEP_PTS from the rest. Scored like a two-sample test
    (difference × √(n_section·n_rest/n)), so that a long clear section beats a
    short random one.
    """
    t0 = recs[0]["ts"]
    ts = [r["ts"] for r in st]
    cw, cwb = [0], [0]
    for r in st:
        cw.append(cw[-1] + r["power"])
        cwb.append(cwb[-1] + r["power"] * (r["lrb"] & 0x7F))
    n = len(st)

    # Boundary = first record after the stop; the section before ends with the stop.
    cuts = {}
    for a, b in pauses(recs):
        cuts.setdefault(bisect.bisect_left(ts, b),
                        Edge("pause", a, b, f"pause {mmss(a - t0)}–{mmss(b - t0)}"))
    for a, b in dropouts(recs):
        cuts.setdefault(bisect.bisect_left(ts, b + 1),
                        Edge("dropout", a, b + 1, f"dropout {mmss(a - t0)}–{mmss(b + 1 - t0)}"))
    edges = [(0, None), *sorted(cuts.items(), key=lambda kv: kv[0]), (n, None)]

    best, best_score = None, 0
    for i in range(len(edges) - 1):
        for j in range(i + 1, len(edges)):
            lo, hi = edges[i][0], edges[j][0]
            k = hi - lo
            if k < MIN_SECTION_S or n - k < MIN_SECTION_S:
                continue
            sec = (cwb[hi] - cwb[lo]) / (cw[hi] - cw[lo])
            rest = (cwb[n] - cwb[hi] + cwb[lo]) / (cw[n] - cw[hi] + cw[lo])
            if abs(sec - rest) < STEP_PTS:
                continue
            score = abs(sec - rest) * math.sqrt(k * (n - k) / n)
            start, end = edges[i][1], edges[j][1]
            # The deviating part is the shorter one. If the section touches the start
            # or the end, the rest is contiguous as well and is reported instead.
            # A long middle part does not count: its rest (start + end) is the
            # shorter part, but not a section (case 1: first hour wrong, pause
            # shortly before the end).
            if 2 * k > n:
                if (start is None) == (end is None):
                    continue
                start, end, sec, rest, k = end, start, rest, sec, n - k
            if best is None or score > best_score:
                best, best_score = Step(sec, rest, k, start, end), score
    if best is None:
        return None

    # Section in time: from the boundary after the stop to the start of the next stop
    best.t_from = best.start.first if best.start else recs[0]["ts"]
    best.t_to = best.end.last if best.end else recs[-1]["ts"] + 1
    best.span = (f"{'Start' if not best.start else mmss(best.t_from - t0)}–"
                 f"{'End' if not best.end else mmss(best.t_to - t0)}")
    for edge in (best.start, best.end):
        if edge:
            before = weighted_share([r for r in st if edge.last - AROUND_S < r["ts"] <= edge.last])
            after = weighted_share([r for r in st if edge.first <= r["ts"] < edge.first + AROUND_S])
            best.bounds.append((edge.label, before, after))
    inside = [r for r in recs if best.t_from <= r["ts"] < best.t_to]
    outside = [r for r in recs if not best.t_from <= r["ts"] < best.t_to]
    wpb_in, wpb_out = watts_per_beat(inside), watts_per_beat(outside)
    best.wpb = wpb_in / wpb_out if wpb_in and wpb_out else None
    return best


def check(fit: Fit) -> Finding:
    """Finding for a parsed FIT file."""
    recs = records(fit)
    st = [r for r in recs if steady(r)]
    if len(st) < 2 * MIN_SECTION_S:
        has_lrb = any(r["lrb"] is not None for r in recs)
        return Finding(error="too little data" if has_lrb else "no balance data")

    res = Finding(share=weighted_share(st), step=find_step(recs, st))
    base = res.share
    if res.step:
        base = weighted_share([r for r in st if not res.step.t_from <= r["ts"] < res.step.t_to])
    res.bias = base - 50 if abs(base - 50) >= BIAS_PTS else None
    res.bias_base = base
    res.dropouts = [(a - recs[0]["ts"], b - a + 1) for a, b in dropouts(recs)]
    pedaling = [r for r in recs if (r["power"] or 0) > 0]
    res.no_balance = sum(1 for r in pedaling if r["lrb"] is None) / len(pedaling)
    return res


def analyze(recs: list[Record]) -> tuple[list[tuple], list[tuple], list[bool]]:
    """Per-minute rows and the balance around every pause, for `pedal-balance analyze`."""
    t0 = recs[0]["ts"]
    def mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None
    minutes = []
    for minute in range((recs[-1]["ts"] - t0) // 60 + 1):
        rs = [r for r in recs if (r["ts"] - t0) // 60 == minute]
        if not rs:
            continue
        ped = [r for r in rs if (r["power"] or 0) > 0]
        minutes.append((minute, len(ped),
                        mean([r["lrb"] & 0x7F for r in ped if r["lrb"] is not None]),
                        mean([r["power"] for r in ped]),
                        mean([r["hr"] for r in rs if r["hr"]]),
                        mean([r["v"] * 3.6 for r in rs if r["v"] is not None]),
                        mean([r["temp"] for r in rs if r["temp"] is not None])))
    jumps = []
    for a, b in pauses(recs):
        before = weighted_share([r for r in recs if a - AROUND_S < r["ts"] <= a], MIN_POWER)
        after = weighted_share([r for r in recs if b <= r["ts"] < b + AROUND_S], MIN_POWER)
        jumps.append((a - t0, b - t0, before, after))
    flags = sorted({bool(r["lrb"] & 0x80) for r in recs if r["lrb"] is not None})
    return minutes, jumps, flags
