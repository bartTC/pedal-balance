"""All terminal output (rich)."""
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .correct import Correction, Options
from .detect import BIAS_PTS, Finding, Step
from .metrics import mmss

console = Console(highlight=False)

SHOW_DROPOUT_S = 30      # dropouts shorter than this in total are not worth a mention
SHOW_NO_BALANCE = 0.05   # power without balance above this share of the pedaling time
LONGEST_DROPOUTS = 3     # dropouts listed in the details
CLEAR_JUMP = 4           # analyze: balance jump at a pause that suggests a zero offset
UNCHANGED = 0.0005       # relative change below this shows as "="

Rows = list[tuple[list[str], str | None, Finding]]   # leading columns, intervals.icu link, finding


def _num(x: float | None, spec: str = ".1f") -> str:
    return format(x, spec) if x is not None else "–"


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------
def summary(f: Finding) -> str:
    """Short finding for the table."""
    if f.error:
        text = f"[dim]{f.error}[/]"
    else:
        parts = []
        if f.step:
            parts.append(f"[bold red]STEP[/] {f.step.span} {f.step.correction:+.0%}")
        if f.bias is not None:
            parts.append(f"[yellow]BIAS[/] ±{2 * abs(f.bias):.0f}%")
        text = ", ".join(parts) or "[green]ok[/]"
    return text + (" [dim](corrected)[/]" if f.corrected else "")


def balance_cell(f: Finding) -> str:
    if f.share is None:
        return "[dim]–[/]"
    return f"[yellow]{f.share:.1f}[/]" if abs(f.share - 50) >= BIAS_PTS else f"{f.share:.1f}"


def dropout_cell(f: Finding) -> str:
    return mmss(f.lost) if f.lost >= SHOW_DROPOUT_S else ""


def details(f: Finding) -> list[str]:
    """Detailed finding as lines."""
    lines = []
    s = f.step
    if s:
        lines.append(f"[bold red]Step[/] {s.span}: balance {s.sec:.1f} vs. {s.rest:.1f} in the rest "
                     f"({s.sec - s.rest:+.1f} points, {s.seconds // 60} min of steady pedaling)")
        lines.append(f"  correction there ≈ [bold]{s.correction:+.0%}[/]")
        lines.extend(f"  boundary at {label}: 5 min before {_num(before)} → after {_num(after)}"
                     for label, before, after in s.bounds)
        if s.wpb:
            lines.append(f"  watts per heartbeat in the section {s.wpb - 1:+.0%} vs. the rest")
    if f.bias is not None:
        what = "Rest of the ride" if s else "Whole ride"
        lines.append(f"[yellow]Bias[/]: {what} {f.bias_base:.1f}, {abs(f.bias):.1f} points off 50/50 "
                     f"→ ≈ {2 * abs(f.bias):.0f} % wrong power, direction unknown [dim](suspicion only)[/]")
    if f.lost >= SHOW_DROPOUT_S:
        longest = sorted(f.dropouts, key=lambda x: -x[1])[:LONGEST_DROPOUTS]
        lines.append(f"Dropouts: {mmss(f.lost)} without power ("
                     + ", ".join(f"{mmss(d)} from {mmss(a)}" for a, d in sorted(longest))
                     + (", …" if len(f.dropouts) > LONGEST_DROPOUTS else "") + ")")
    # in corrected files the balance in the section is invalid on purpose
    if f.no_balance >= SHOW_NO_BALANCE and not f.corrected:
        lines.append(f"Power without balance: {f.no_balance:.0%} of the pedaling time")
    return lines


def findings(rows: Rows, headers: list[str]) -> None:
    """Table with short findings, then a panel with details for each conspicuous ride."""
    table = Table(header_style="bold", box=None, pad_edge=False)
    for h in headers:
        table.add_column(h, no_wrap=True)
    table.add_column("Balance", justify="right", no_wrap=True)
    lost = any(dropout_cell(f) for _, _, f in rows)
    if lost:
        table.add_column("Dropout", justify="right", no_wrap=True)
    table.add_column("Finding", no_wrap=True)
    links = [link for _, link, _ in rows if link]
    if links:
        # never truncate, a cut-off link is useless
        table.add_column("intervals.icu", no_wrap=True, style="blue", min_width=max(map(len, links)))
    for cols, link, f in rows:
        cells = [*map(escape, cols), balance_cell(f), *([dropout_cell(f)] if lost else []), summary(f)]
        table.add_row(*cells, *([link or ""] if links else []))
    console.print(table)

    for cols, link, f in rows:
        if f.error or (not f.step and f.bias is None):
            continue
        body = "\n".join(details(f) + ([f"[blue]{link}[/]"] if link else []))
        console.print(Panel(body, title=escape(" · ".join(c for c in cols if c and c != "–")),
                            title_align="left", border_style="red" if f.step else "yellow", expand=False))


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------
def analysis(minutes: list[tuple], jumps: list[tuple], flags: list[bool]) -> None:
    console.print(f"Balance flag 0x80 (value is for the right side) set: {flags}. Without the flag "
                  f"intervals.icu reads the value as the LEFT share.\n")
    t = Table(box=None, header_style="bold", pad_edge=False)
    for h in ("Min", "n pedaling", "Balance", "avg W pedaling", "HR", "km/h", "°C"):
        t.add_column(h, justify="right")
    for minute, n, bal, watts, hr, kmh, temp in minutes:
        t.add_row(str(minute), str(n), _num(bal), _num(watts, ".0f"), _num(hr), _num(kmh), _num(temp))
    console.print(t)

    console.print("\n[bold]Auto-pauses[/] (candidates for a zero offset), balance ≥ 80 W over 5 min "
                  "before → after:")
    best = None
    for a, b, before, after in jumps:
        jump = after - before if before is not None and after is not None else 0
        if best is None or abs(jump) > abs(best[2]):
            best = (a, b, jump)
        console.print(f"  {mmss(a)} – {mmss(b)}  ({b - a - 1:3d} s)  {_num(before, '5.1f')} → "
                      f"{_num(after, '5.1f')}")
    if best and abs(best[2]) >= CLEAR_JUMP:
        console.print(f"\nLargest jump at the pause {mmss(best[0])}–{mmss(best[1])}. "
                      f"Correct e.g. with: [bold]--until {best[0] // 60}m[/]")
    else:
        console.print("\nNo clear balance jump at a pause found.")


# --------------------------------------------------------------------------
# fix
# --------------------------------------------------------------------------
def step_line(s: Step) -> str:
    return (f"[bold red]Step[/] {s.span}: balance {s.sec:.1f} vs. {s.rest:.1f} in the rest"
            + "".join(f", boundary at {label} ({before:.1f} → {after:.1f})"
                      for label, before, after in s.bounds if before is not None and after is not None))


def _value(v: float | list | None, fmt: str) -> str:
    if v is None:
        return "[dim]invalid[/]" if fmt == "bal" else "–"
    if fmt == "lap":
        return " / ".join("–" if x is None else str(x) for x in v)
    spec = {"W": ".0f", "bal": ".1f", "pct": "+.0%", "if": ".3f", "tss": ".1f"}[fmt]
    return format(v, spec) + (" W" if fmt == "W" else "")


def _change(old: float | list | None, new: float | list | None, fmt: str) -> str:
    if fmt == "lap":
        old, new = old[0], new[0]
    if fmt in ("bal", "pct") or old in (None, 0) or new is None:
        return ""
    d = new / old - 1
    if abs(d) < UNCHANGED:
        return "[dim]=[/]"
    return f"[{'red' if d < 0 else 'green'}]{d:+.1%}[/]"


def comparison(groups: list[tuple[str, list[tuple]]]) -> None:
    t = Table(box=None, header_style="bold", pad_edge=False)
    t.add_column("")
    t.add_column("Before", justify="right")
    t.add_column("After", justify="right")
    t.add_column("Change", justify="right")
    for title, rows in groups:
        t.add_row(f"[bold]{title}[/]")
        for label, old, new, fmt in rows:
            t.add_row("  " + label, _value(old, fmt), _value(new, fmt), _change(old, new, fmt))
        t.add_row()
    console.print(t)


def cross_check(c: Correction) -> None:
    t = Table(box=None, header_style="dim bold", pad_edge=False, style="dim")
    for h, j in (("Cross-check", "left"), ("W/beat", "right"), ("rel. to rest", "right"),
                 ("power ÷ expected from speed", "right")):
        t.add_column(h, justify=j, style="dim")
    for label, wpb, hr, sp in c.cross_check:
        t.add_row(label, _num(wpb, ".2f"), _num(hr, ".2f"), _num(sp, ".2f"))
    console.print(t)
    if len(c.spread) > 1:
        console.print(f"[dim](speed model spreads between {min(c.spread):.2f} and {max(c.spread):.2f} on "
                      f"the rest: wind, drafting. Heart rate lags during the warm-up.)[/]")


def _say(text: str) -> None:
    console.print(escape(text), style="dim")


def correction(c: Correction, opts: Options, *, verbose: bool = False) -> None:
    """How the correction went, then the preconditions."""
    _say(f"Boundary: {mmss(c.boundary - c.t0)} after start ({c.how}); "
         f"{c.n_before} records before, {c.n_after} after")
    _say(f"Stored balance value, power-weighted from {opts.ref_min_power:g} W: before {c.s_bad:.1f}, "
         f"after {c.s_good:.1f} → reference {c.ref:g}")
    if verbose:
        _say(f"Stored value = {c.stored_side} share. Assumed to measure correctly: "
             f"{'this side' if c.trust == 'stored' else 'the other side'} "
             f"(its share {'dropped' if c.trust == 'stored' else 'rose'} in the section).")
    _say(f"records: {c.n_ok} corrected via the trusted pedal ({c.capped} of them capped at the original "
         f"value), {c.n_fallback} with a flat factor × {c.factor:.3f} ({c.n_fallback - c.outliers} without "
         f"balance, {c.outliers} with implausible balance > {c.outlier_limit:g} %), {c.n_zero} at 0 W "
         f"unchanged")
    for w in c.warnings:
        console.print(f"[yellow]Warning:[/] {escape(w)}")
    if verbose:
        balance = "invalid (0xFF)" if opts.balance == "invalid" else f"set to {c.ref:g}"
        _say(f"Balance in the section: {balance}"
             + (f"; {c.te_ps_cleared} torque effectiveness/pedal smoothness values invalid"
                if c.te_ps_cleared else ""))
        if c.acc_delta is not None:
            _say(f"accumulated_power: all following records reduced by {c.acc_delta} W·s")
        _say("Summaries (device value × ratio corrected/original):")
        for line in c.changes:
            _say("  " + line)
    if verbose or not c.ok:
        cross_check(c)   # the reason when a precondition fails
    console.print("[bold]Preconditions[/]")
    for text, ok, required in c.checks:
        mark = "[green]✓[/]" if ok else "[red]✗[/]" if required else "[yellow]✗[/]"
        console.print(f"  {mark} {escape(text)}" + ("" if ok or required else " [dim](informational)[/]"))
