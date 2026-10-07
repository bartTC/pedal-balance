"""
Corrects a faulty section at the start of a ride via the pedal that measures correctly.

Core assumption: one pedal measures correctly. The true left/right split in the
faulty section equals the one in the rest of the ride. Per record:

    power_new = power × share_of_trusted_pedal / reference_share_of_trusted_pedal

Because every data point is recomputed from the trusted pedal, it does not
matter whether the error is a fixed torque error (in watts growing with cadence)
or a scaling error. Nothing here prints; `correct` returns a `Correction` that
report.py renders.
"""
import io
from dataclasses import dataclass, field

import fitdecode
from garmin_fit_sdk import Decoder, Stream

from .detect import steady
from .fitfile import (
    LAP,
    LAP_TE_PS,
    MSG_LAP,
    MSG_SESSION,
    OTHER_POWER_MSGS,
    REC,
    REC_TE_PS,
    SES,
    SES_TE_PS,
    Fit,
    Msg,
    Record,
    fit_crc,
    pauses,
    records,
)
from .metrics import (
    SpeedModel,
    best_avg,
    blocks30,
    mmss,
    normalized_power,
    power_series,
    rnd,
    watts_per_beat,
    weighted_share,
)

SNAP_S = 120          # max distance of an auto-pause from --until to snap to it
MIN_SPEED_BLOCKS = 20  # 30 s blocks needed to fit the speed model on the rest of the ride
TOO_HIGH = 1.08       # original power this much above heart rate or speed = reads too high
TOO_LOW = 0.95        # ... and nowhere below this
# Best efforts shown in the before/after comparison
CURVE = ((5, "5 s"), (60, "1 min"), (300, "5 min"), (1200, "20 min"), (3600, "60 min"))

Row = tuple[str, float | None, float | None, float | None]   # cross-check row


class VerifyError(Exception):
    """The patched file differs from the original in an unexpected way."""


@dataclass
class Options:
    until: int                      # end of the faulty section, seconds from the first record
    snap: bool = True               # snap to an auto-pause within ±SNAP_S
    ref: float | None = None        # reference balance (default: rest of the ride, rounded to 0.5)
    ref_min_power: float = 80       # data points below this do not count for the balance averages
    trust: str = "auto"             # auto | stored | other
    balance: str = "invalid"        # invalid | ref: balance in the corrected section
    fallback: float | None = None   # factor for records without/with implausible balance
    max_dev: float = 15             # trusted share this far above the reference = outlier
    min_jump: float = 4             # minimum balance jump at the boundary (precondition)
    mass: float = 85                # rider + bike in kg, only for the speed cross-check


@dataclass
class Correction:
    t0: int = 0                     # timestamp of the first record
    boundary: int = 0
    snapped: bool = False
    how: str = ""
    n_before: int = 0
    n_after: int = 0
    s_bad: float = 0.0
    s_good: float = 0.0
    ref: float = 0.0
    trust: str = ""
    stored_side: str = ""
    n_ok: int = 0
    capped: int = 0
    n_fallback: int = 0
    outliers: int = 0
    factor: float = 1.0
    n_zero: int = 0
    outlier_limit: float = 0.0
    te_ps_cleared: int = 0
    acc_delta: int | None = None
    warnings: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)       # summary fields: "lap 2 avg_power: 206 → 162"
    cross_check: list[Row] = field(default_factory=list)   # label, W/beat, rel. to rest, power ÷ speed model
    spread: list[float] = field(default_factory=list)      # speed model ratio on both halves of the rest
    checks: list[tuple[str, bool, bool]] = field(default_factory=list)   # text, ok, required

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, required in self.checks if required)


def find_boundary(recs: list[Record], until: int, *, snap: bool = True) -> tuple[int, bool, str]:
    t0 = recs[0]["ts"]
    target = t0 + until
    if snap:
        near = [(a, b) for a, b in pauses(recs) if a <= target + SNAP_S and b >= target - SNAP_S]
        if near:
            a, b = min(near, key=lambda p: min(abs(p[0] - target), abs(p[1] - target)))
            return b, True, f"snapped to auto-pause {mmss(a - t0)}–{mmss(b - t0)}"
    return target, False, "exact (no auto-pause nearby)"


def cross_check(bad_old: list[Record], bad_new: list[Record], rest: list[Record],
                mass: float) -> tuple[list[Row], list[float]]:
    """Does the power in the section match heart rate and speed, relative to the rest?"""
    wpb_rest = watts_per_beat(rest)
    rb = blocks30(rest)
    model = SpeedModel(rb, mass) if len(rb) >= MIN_SPEED_BLOCKS else None
    rows = []
    for label, rs in (("faulty, original", bad_old), ("faulty, corrected", bad_new),
                      ("rest of the ride", rest)):
        wpb = watts_per_beat(rs)
        rows.append((label, wpb, wpb / wpb_rest if wpb and wpb_rest else None,
                     model.ratio(blocks30(rs)) if model else None))
    spread = []
    if model:
        h = len(rb) // 2
        spread = [x for x in (model.ratio(rb[:h]), model.ratio(rb[h:])) if x is not None]
    return rows, spread


def _shown(v: int, scale: int | None) -> str:
    """Raw summary value as text; scale None = balance (percent × 100, 0x8000 = right)."""
    return f"{(v & 0x3FFF) / 100:g} %" if scale is None else f"{v / scale:g}"


def recompute(fit: Fit, msg: Msg, fmap: dict[str, int], te_ps: dict[int, str], *,
              recs_new: list[Record], recs_old: list[Record], label: str) -> list[str]:
    """
    Adjust the power summary of a lap/session to the corrected records.

    The Karoo computes its summaries from data with a finer resolution than the
    1 Hz records; recomputed avg and NP differ by a few watts depending on the
    ride, much more for very short laps. So the device value is scaled by the
    ratio new/old (both computed from the records) instead of being replaced by
    our own calculation: unchanged values stay exact, the correction applies in
    full. IF is scaled with the NP ratio, TSS with its square.
    """
    start, end = fit.get(msg, fmap["start_time"]), msg.ts
    timer = (fit.get(msg, fmap["total_timer_time"]) or 0) / 1000
    if start is None or end is None or timer <= 0:
        return []

    def stats(recs: list[Record]) -> dict | None:
        rs = [r for r in recs if start <= r["ts"] <= end]
        pw = [r["power"] or 0 for r in rs]
        if not pw:
            return None
        return {"avg": sum(pw), "max": max(pw), "np": normalized_power(power_series(recs, start, end)),
                "bal": weighted_share(rs)}

    old, new = stats(recs_old), stats(recs_new)
    if not old or not new:
        return []

    def ratio(k: str) -> float | None:
        return new[k] / old[k] if old.get(k) and new.get(k) is not None else None

    r_avg, r_np = ratio("avg"), ratio("np")
    flag = any(r["lrb"] is not None and r["lrb"] & 0x80 for r in recs_old)
    plan = {
        "avg_power": (r_avg, 1), "total_work": (r_avg, 1),
        "normalized_power": (r_np, 1), "intensity_factor": (r_np, 1000),
        "training_stress_score": (r_np ** 2 if r_np else None, 10),
    }
    changes = []
    for key in ("avg_power", "max_power", "normalized_power", "total_work",
                "left_right_balance", "intensity_factor", "training_stress_score"):
        if key not in fmap or fit.get(msg, fmap[key]) is None:
            continue
        before = fit.get(msg, fmap[key])
        if key == "left_right_balance":
            if new["bal"] is None:
                fit.invalidate(msg, fmap[key])
                changes.append(f"{label} {key}: invalid (only estimated values left)")
                continue
            raw, scale = rnd(new["bal"] * 100) | (0x8000 if flag else 0), None
        elif key == "max_power":
            # the device value stays unless the maximum was in the corrected part
            raw, scale = (before if new["max"] == old["max"] else new["max"]), 1
        else:
            factor, scale = plan[key]
            if factor is None:
                continue
            raw = rnd(before * factor)
        if raw == before:
            continue
        fit.put(msg, fmap[key], raw)
        changes.append(f"{label} {key}: {_shown(before, scale)} → {_shown(raw, scale)}")
    for num, name in te_ps.items():
        if fit.invalidate(msg, num):
            changes.append(f"{label} {name}: invalid")
    return changes


def _reference(c: Correction, bad: list[Record], rest: list[Record], recs: list[Record],
               opts: Options) -> float:
    """Balance before/after, reference and trusted side. Returns the reference share of
    the trusted pedal."""
    s_bad, s_good = weighted_share(bad, opts.ref_min_power), weighted_share(rest, opts.ref_min_power)
    if s_bad is None or s_good is None:
        msg = "no balance data"
        raise ValueError(msg)
    c.s_bad, c.s_good = s_bad, s_good
    c.ref = opts.ref if opts.ref is not None else round(s_good * 2) / 2
    c.trust = opts.trust if opts.trust != "auto" else ("stored" if s_bad < s_good else "other")
    flag = any(r["lrb"] is not None and r["lrb"] & 0x80 for r in recs)
    c.stored_side = "right" if flag else "left (no 0x80 flag, as intervals.icu reads it)"
    ref_good = c.ref if c.trust == "stored" else 100 - c.ref
    c.outlier_limit = ref_good + opts.max_dev
    return ref_good


def _correct_powers(c: Correction, bad: list[Record], ref_good: float, opts: Options) -> None:
    """New power per record of the faulty section, via the trusted pedal."""
    fallback = []
    sum_old = sum_new = 0
    for r in bad:
        p = r["power"]
        if not p:
            c.n_zero += 1
            continue
        if r["lrb"] is None:
            fallback.append(r)
            continue
        v = r["lrb"] & 0x7F
        good_share = v if c.trust == "stored" else 100 - v
        if good_share > c.outlier_limit:
            # The trusted pedal cannot have a clearly HIGHER share than normal while
            # the other one reads too high: outlier/stuck value. Scaling it up would
            # invent peaks (e.g. 656 W at balance 72 → 945 W).
            fallback.append(r)
            c.outliers += 1
            continue
        # If one pedal reads too high, the true power cannot exceed the measured
        # value. Seconds where the formula gives more are balance noise.
        new_p = rnd(p * good_share / ref_good)
        if new_p > p:
            new_p = p
            c.capped += 1
        r["power"] = new_p
        sum_old += p
        sum_new += new_p
        c.n_ok += 1
    c.n_fallback = len(fallback)
    c.factor = opts.fallback if opts.fallback is not None else (sum_new / sum_old if sum_old else 1)
    for r in fallback:
        r["power"] = min(rnd(r["power"] * c.factor), r["power"])


def _write_records(fit: Fit, c: Correction, bad: list[Record], opts: Options) -> None:
    """Power, balance and torque effectiveness/pedal smoothness of the faulty section."""
    for r in bad:
        if r["power"] is not None and fit.get(r["msg"], REC["power"]) != r["power"]:
            fit.put(r["msg"], REC["power"], r["power"])
        if r["lrb"] is not None:
            if opts.balance == "invalid":
                # also at 0 W: none of the values in the section can be trusted
                fit.invalidate(r["msg"], REC["lrb"])
                r["lrb"] = None
            elif (r["power"] or 0) > 0:
                r["lrb"] = (r["lrb"] & 0x80) | rnd(c.ref)
                fit.put(r["msg"], REC["lrb"], r["lrb"])
        for num in REC_TE_PS:
            c.te_ps_cleared += fit.invalidate(r["msg"], num)


def _write_accumulated(fit: Fit, c: Correction, recs: list[Record], old_recs: list[Record]) -> None:
    """accumulated_power is cumulative: adjust every record from the first corrected one on."""
    if not any(r["acc"] is not None for r in recs):
        return
    delta = 0
    for r, o in zip(recs, old_recs, strict=True):
        delta += (o["power"] or 0) - (r["power"] or 0)
        if r["acc"] is not None and delta:
            fit.put(r["msg"], REC["accumulated_power"], max(0, r["acc"] - delta) & 0xFFFFFFFF)
    c.acc_delta = delta


def _preconditions(c: Correction, opts: Options) -> list[tuple[str, bool, bool]]:
    """Safety brake: jump at the boundary AND power does not match heart rate/speed."""
    _, _, hr_r, sp_r = c.cross_check[0]
    jump = abs(c.s_good - c.s_bad)
    evidence = [x for x in (hr_r, sp_r) if x is not None]
    return [
        (f"Balance jumps by {jump:.1f} points at the boundary (minimum {opts.min_jump:g})",
         jump >= opts.min_jump, True),
        ("Boundary is at an auto-pause (zero offset happens at a standstill)", c.snapped, False),
        ("Original power too high for heart rate or speed (≥ +8 %), nowhere too low",
         bool(evidence) and max(evidence) >= TOO_HIGH and min(evidence) > TOO_LOW, True),
    ]


def correct(fit: Fit, opts: Options) -> Correction:
    """
    Correct `fit` in place and finalize the CRC.

    Raises ValueError if the correction is impossible; preconditions are reported
    in Correction.checks.
    """
    recs = records(fit)
    c = Correction(t0=recs[0]["ts"])
    c.boundary, c.snapped, c.how = find_boundary(recs, opts.until, snap=opts.snap)
    bad = [r for r in recs if r["ts"] < c.boundary]
    rest = [r for r in recs if r["ts"] >= c.boundary]
    if not bad or not rest:
        msg = "boundary is outside the ride"
        raise ValueError(msg)
    c.n_before, c.n_after = len(bad), len(rest)
    c.warnings = [f"file contains {name} messages; their power values are not adjusted"
                  for gnum, name in OTHER_POWER_MSGS.items() if fit.of(gnum)]

    ref_good = _reference(c, bad, rest, recs, opts)
    old_recs = [dict(r) for r in recs]
    _correct_powers(c, bad, ref_good, opts)
    _write_records(fit, c, bad, opts)
    _write_accumulated(fit, c, recs, old_recs)
    for i, lap in enumerate(fit.of(MSG_LAP), 1):
        start = fit.get(lap, LAP["start_time"])
        if start is not None and start < c.boundary:
            c.changes += recompute(fit, lap, LAP, LAP_TE_PS, recs_new=recs, recs_old=old_recs,
                                   label=f"lap {i}")
    for s in fit.of(MSG_SESSION):
        c.changes += recompute(fit, s, SES, SES_TE_PS, recs_new=recs, recs_old=old_recs, label="session")
    fit.finalize()

    c.cross_check, c.spread = cross_check([o for o in old_recs if o["ts"] < c.boundary], bad, rest,
                                          opts.mass)
    c.checks = _preconditions(c, opts)
    return c


def verify(orig: bytes, fit: Fit) -> list[str]:
    """Check the patched file against the original. Raises VerifyError; returns the lines to show."""
    new = fit.buf
    problems = []
    if len(orig) != len(new):
        problems.append("file size changed")
    elif fit_crc(new[:fit.body_end + 2]) != 0:
        problems.append("CRC invalid")
    else:
        allowed = set(range(fit.body_end, fit.body_end + 2))
        for o, s in fit.patched:
            allowed.update(range(o, o + s))
        stray = [i for i in range(len(orig)) if orig[i] != new[i] and i not in allowed]
        if stray:
            problems.append(f"bytes changed outside patched fields: {stray[:10]}")
        if [m.gnum for m in Fit(new).msgs] != [m.gnum for m in fit.msgs]:
            problems.append("message order changed")
    if problems:
        raise VerifyError("; ".join(problems))

    changed = sum(1 for a, b in zip(orig, new, strict=True) if a != b)
    lines = [(f"Self-check OK: {len(fit.msgs)} messages, {changed} bytes changed, all inside patched "
              f"fields, CRC 0x{fit_crc(new[:fit.body_end]):04X}")]
    with fitdecode.FitReader(io.BytesIO(bytes(new)), check_crc=fitdecode.CrcCheck.RAISE) as fr:
        n = sum(1 for f in fr if isinstance(f, fitdecode.FitDataMessage))
    lines.append(f"fitdecode: OK ({n} messages, CRC checked)")
    if not Decoder(Stream.from_byte_array(bytearray(new))).check_integrity():
        msg = "Garmin FIT SDK: integrity check failed"
        raise VerifyError(msg)
    _, errors = Decoder(Stream.from_byte_array(bytearray(new))).read()
    lines.append(f"Garmin FIT SDK: integrity OK, errors: {errors or 'none'}")
    return lines


def before_after(orig: bytes, new: bytes, boundary: int) -> list[tuple[str, list[tuple]]]:
    """Groups of rows (label, before, after, format) comparing two versions of a ride."""
    f_old, f_new = Fit(orig), Fit(new)
    r_old, r_new = records(f_old), records(f_new)
    t0 = r_old[0]["ts"]

    def pw(rs: list[Record]) -> list[int]:
        return [r["power"] or 0 for r in rs]

    def sec(rs: list[Record]) -> list[Record]:
        return [r for r in rs if r["ts"] < boundary]

    wpb_rest = watts_per_beat([r for r in r_old if r["ts"] >= boundary])

    def rel(rs: list[Record]) -> float | None:
        wpb = watts_per_beat(rs)
        return wpb / wpb_rest - 1 if wpb_rest and wpb else None

    a, b = sec(r_old), sec(r_new)
    groups = [(f"Section Start–{mmss(a[-1]['ts'] - t0)}", [
        ("avg power (incl. 0 W)", sum(pw(a)) / len(a), sum(pw(b)) / len(b), "W"),
        ("NP", normalized_power(power_series(r_old, t0, boundary - 1)),
         normalized_power(power_series(r_new, t0, boundary - 1)), "W"),
        ("max", max(pw(a)), max(pw(b)), "W"),
        ("balance", weighted_share([r for r in a if steady(r)]),
         weighted_share([r for r in b if steady(r)]), "bal"),
        ("watts per heartbeat vs. rest", rel(a), rel(b), "pct"),
    ])]

    s_old, s_new = f_old.of(MSG_SESSION), f_new.of(MSG_SESSION)
    if s_old and s_new:
        rows = []
        for key, label, scale, fmt in (("avg_power", "avg power", 1, "W"),
                                       ("normalized_power", "NP", 1, "W"),
                                       ("max_power", "max", 1, "W"),
                                       ("intensity_factor", "IF", 1000, "if"),
                                       ("training_stress_score", "TSS", 10, "tss")):
            v_old, v_new = f_old.get(s_old[0], SES[key]), f_new.get(s_new[0], SES[key])
            if v_old is not None:
                rows.append((label, v_old / scale, v_new / scale if v_new is not None else None, fmt))
        groups.append(("Whole ride (values in the file)", rows))

    laps = []
    for i, (lo, ln) in enumerate(zip(f_old.of(MSG_LAP), f_new.of(MSG_LAP), strict=True), 1):
        keys = ("avg_power", "normalized_power", "max_power")
        before = [f_old.get(lo, LAP[k]) for k in keys]
        after = [f_new.get(ln, LAP[k]) for k in keys]
        if before != after:
            laps.append((f"lap {i}  avg / NP / max", before, after, "lap"))
    if laps:
        groups.append(("Changed laps", laps))

    p_old, p_new = pw(r_old), pw(r_new)
    groups.append(("Power curve (best efforts)", [
        (label, best_avg(p_old, w), best_avg(p_new, w), "W") for w, label in CURVE
        if best_avg(p_old, w) is not None]))
    return groups
