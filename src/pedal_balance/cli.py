"""
pedal-balance: check and fix the left/right balance of dual-sided power meter pedals.

  check    Scan the rides of the last days on intervals.icu (or local FIT files)
           for a STEP at a stop or a BIAS over the whole ride.
  fix      Correct a step at the start of a ride: intervals.icu ID, activity URL
           or local FIT file. Shows before/after and asks before changing anything.
  analyze  Per-minute table and the balance around every pause of a FIT file.
"""
import argparse
import datetime
import re
import sys
import urllib.error
from pathlib import Path

from rich.markup import escape
from rich.prompt import Confirm

from . import __version__, report
from .correct import Options, VerifyError, before_after, correct, verify
from .detect import (
    BIAS_PTS,
    MIN_CAD,
    MIN_POWER,
    STEP_PTS,
    Finding,
    analyze,
    check,
    is_corrected,
)
from .fitfile import Fit, records
from .intervals import (
    ConfigError,
    Intervals,
    ReplaceError,
    activity_url,
    cache_dir,
    credentials,
    replace_activity,
)
from .metrics import mmss

console = report.console
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
EXIT_INTERRUPTED = 130


def parse_duration(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(h|m|min|s)?\s*", text)
    if not m:
        msg = f"cannot read duration: {text!r} (e.g. 15m, 1h, 900)"
        raise argparse.ArgumentTypeError(msg)
    v, unit = float(m.group(1)), (m.group(2) or "s")
    return int(v * {"h": 3600, "m": 60, "min": 60, "s": 1}[unit])


def parse_target(text: str) -> tuple[str | None, Path | None]:
    """(activity ID, None) or (None, local path)."""
    if Path(text).exists():
        return None, Path(text)
    m = re.search(r"activities/(i?\d+)", text) or re.fullmatch(r"\s*(i?\d+)\s*", text)
    if not m:
        msg = f"Neither a file nor an intervals.icu ID/URL: {text}"
        raise ConfigError(msg)
    return m.group(1), None


def read_fit(path: str | Path) -> Fit:
    try:
        fit = Fit.read(path)
    except (OSError, ValueError, KeyError, IndexError) as e:
        msg = f"{path}: not readable ({e})"
        raise ConfigError(msg) from e
    if not fit.crc_ok:
        msg = f"{path}: CRC invalid"
        raise ConfigError(msg)
    return fit


def short(path: Path) -> str:
    """Path relative to the current folder, or with ~ for the home folder."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path).replace(str(Path.home()), "~", 1)


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------
def ride_columns(a: dict, gear: dict[str, str]) -> list[str]:
    """Date, bike and pedals of an intervals.icu activity for the check table."""
    start = datetime.datetime.fromisoformat(a["start_date_local"])
    when = f"{WEEKDAYS[start.weekday()]} {MONTHS[start.month - 1]} {start:%d %H:%M}"
    bike = gear.get((a.get("gear") or {}).get("id")) or "–"
    serial = str(a.get("power_meter_serial") or "")
    # "GARMIN RALLY_200" → "Rally", "STAGES_CYCLING 3" → "Stages"
    pm = (a.get("power_meter") or "–").replace("GARMIN ", "").split()[0].split("_")[0].capitalize()
    return [when, bike, pm + (f" …{serial[-4:]}" if serial else "")]


def check_activity(api: Intervals, a: dict, cache: Path) -> Finding:
    if a.get("source") == "STRAVA" or (a.get("file_type") or "").lower() != "fit":
        return Finding(error="no FIT file available")
    try:
        f = check(read_fit(api.download(a["id"], cache)))
    except urllib.error.URLError as e:
        f = Finding(error=f"download failed ({e})")
    except ConfigError as e:
        f = Finding(error=str(e))
    f.corrected = is_corrected(a.get("external_id"))
    return f


def cmd_check(args: argparse.Namespace) -> int:
    if args.file:
        rows = []
        for path in args.file:
            try:
                f = check(read_fit(path))
            except ConfigError as e:
                f = Finding(error=str(e))
            f.corrected = is_corrected(path)
            rows.append(([Path(path).name], None, f))
        report.findings(rows, ["File"])
        return 0

    api = Intervals(*credentials())
    newest = datetime.datetime.now().astimezone().date()
    oldest = newest - datetime.timedelta(days=args.days)
    all_rides = api.rides(oldest, newest)
    rides = [a for a in all_rides if a.get("device_watts")]
    gear = api.gear_names() if rides else {}
    with console.status("Loading rides …"):
        rows = [(ride_columns(a, gear), activity_url(a["id"]), check_activity(api, a, args.cache))
                for a in rides]

    hidden = len(all_rides) - len(rides)
    console.print(f"[bold]intervals.icu {oldest} – {newest}[/]: {len(rides)} rides with power"
                  + (f" [dim]({hidden} without hidden)[/]" if hidden else ""))
    console.print(f"[dim]Balance = stored value as shown by intervals.icu, power-weighted, only "
                  f"> {MIN_POWER} W and > {MIN_CAD} rpm[/]\n")
    report.findings(rows, ["Date", "Bike", "Pedals"])
    return 0


# --------------------------------------------------------------------------
# fix
# --------------------------------------------------------------------------
def find_until(fit: Fit, args: argparse.Namespace) -> tuple[int, bool] | None:
    """(--until in s, snap) from the step at the start, or from --until; None = nothing to fix."""
    f = check(fit)
    if f.error:
        msg = f"Cannot check: {f.error}"
        raise ConfigError(msg)
    if args.until is not None:
        console.print(f"Boundary by hand: --until {mmss(args.until)}"
                      + ("" if f.step else " [dim](the check finds no step)[/]"))
        return args.until, not args.no_snap
    if not f.step:
        console.print(f"No step: no section at a stop differs by ≥ {STEP_PTS} points from the rest "
                      f"(balance over the whole ride {f.share:.1f}). Without a correct part as a "
                      f"reference there is nothing to correct against.")
        if f.bias is not None:
            console.print(f"[yellow]Bias[/]: {abs(f.bias):.1f} points off 50/50 over the whole ride. "
                          f"Cannot be corrected automatically: one pedal reading too high and the other "
                          f"reading too low look the same in the data.")
        console.print("[dim]Boundary by hand with --until.[/]")
        return None
    console.print(report.step_line(f.step))
    if not f.step.at_start:
        msg = "The step is not at the start of the ride; only a faulty start can be corrected."
        raise ConfigError(msg)
    t0 = records(fit)[0]["ts"]
    edge = f.step.end
    if edge.kind == "pause":
        return edge.last - t0, True     # snaps to this pause
    return edge.first - t0, False       # dropout: exactly there, no snapping


def ask(question: str) -> bool:
    try:
        return Confirm.ask(question, default=False, console=console)
    except EOFError:
        return False


def local_output(path: Path, args: argparse.Namespace) -> Path:
    out = Path(args.output) if args.output else path.with_name(path.stem + "_corrected.fit")
    if out.resolve() == path.resolve():
        msg = "Target file must not be the original file."
        raise ConfigError(msg)
    if out.exists() and not args.force:
        msg = f"{out} already exists (--force to overwrite)."
        raise ConfigError(msg)
    return out


def cmd_fix(args: argparse.Namespace) -> int:
    act_id, path = parse_target(args.target)
    api, old = None, None
    if act_id:
        api = Intervals(*credentials())
        old = api.activity(act_id)
        console.print(f"[bold]{escape(old.get('name') or act_id)}[/] · "
                      f"{old.get('start_date_local', '')[:16]} · {old.get('power_meter') or '–'} · "
                      f"{activity_url(act_id)}")
        if is_corrected(old.get("external_id")):
            console.print("[yellow]This activity is already a corrected file.[/]")
        path = api.download(act_id, args.cache)
        out = Path(args.cache) / f"{act_id}_corrected.fit"
    else:
        console.print(f"[bold]{escape(str(path))}[/]")
        out = local_output(path, args)

    fit = read_fit(path)
    found = find_until(fit, args)
    if found is None:
        return 0
    orig = bytes(fit.buf)
    opts = Options(until=found[0], snap=found[1], ref=args.ref, ref_min_power=args.ref_min_power,
                   trust=args.trust, balance=args.balance, fallback=args.fallback,
                   max_dev=args.max_dev, min_jump=args.min_jump, mass=args.mass)
    try:
        c = correct(fit, opts)
    except ValueError as e:
        msg = f"Cannot correct: {e}"
        raise ConfigError(msg) from e
    console.print()
    report.comparison(before_after(orig, fit.buf, c.boundary))
    report.correction(c, opts, verbose=args.verbose)
    console.print()

    if not c.ok:
        console.print("[bold red]Caution:[/] preconditions not met. Only confirm if you are sure the "
                      "section was measured wrongly.")
    if act_id:
        console.print("Planned:\n"
                      f"  1. write and verify {short(out)}\n"
                      f"  2. upload as a new activity, carry over name, description, type, bike, RPE, feel, "
                      f"tags and workout\n"
                      f"  3. [bold]delete the old activity[/]: {activity_url(act_id)}\n"
                      f"     [dim]The original stays in {short(path)}. A manually set load is lost.[/]")
    if not ask("Replace in intervals.icu?" if act_id else f"Write {short(out)}?"):
        console.print("Cancelled, nothing changed.")
        return 0

    for line in verify(orig, fit):   # raises VerifyError before anything is written
        console.print(line, style="dim")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(fit.buf)
    console.print(f"Written: {short(out)}")
    if api and old:
        new_id = replace_activity(api, old, out, say=console.print)
        console.print(f"\n[bold green]Done:[/] {activity_url(new_id)}")
    return 0


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------
def cmd_analyze(args: argparse.Namespace) -> int:
    report.analysis(*analyze(records(read_fit(args.file))))
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pedal-balance", description=__doc__.strip().split("\n\n")[0],
                                 epilog=__doc__.strip().split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--cache", type=Path, default=cache_dir(),
                    help="folder for downloaded and corrected files (default: %(default)s, "
                         "or $PEDAL_BALANCE_CACHE)")
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("check", help="find steps and bias in recent rides or local files",
                       description=f"Finds a STEP (a section of ≥ 5 min that differs by ≥ {STEP_PTS} points "
                                   f"from the rest, bounded by a stop or dropout) and a BIAS (the ride ≥ "
                                   f"{BIAS_PTS} points off 50/50). Only steady pedaling counts (> {MIN_POWER} W, "
                                   f"> {MIN_CAD} rpm).")
    p.add_argument("days", nargs="?", type=int, default=7, help="this many days back (default: 7)")
    p.add_argument("--file", nargs="+", metavar="FIT", help="check local FIT files instead of intervals.icu")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("fix", help="correct a step at the start of a ride (asks first)",
                       description="Finds the step at the start, corrects it via the pedal that measures "
                                   "correctly, shows before/after and asks. For an intervals.icu activity it "
                                   "then uploads the corrected file, carries over the details and deletes "
                                   "the old activity.")
    p.add_argument("target", help="intervals.icu ID (i123456789), activity URL or local FIT file")
    p.add_argument("--until", type=parse_duration,
                   help="boundary by hand instead of from the step, e.g. 15m (snaps to an auto-pause ±2 min)")
    p.add_argument("--no-snap", action="store_true", help="with --until: do not snap to an auto-pause")
    p.add_argument("--ref", type=float,
                   help="reference balance in %% (default: power-weighted rest of the ride, rounded to 0.5)")
    p.add_argument("--ref-min-power", type=float, default=80,
                   help="data points below this do not count for the balance averages (default: 80 W)")
    p.add_argument("--trust", choices=("auto", "stored", "other"), default="auto",
                   help="pedal that measures correctly: the side of the stored value or the other "
                        "(auto: the side whose share dropped in the section)")
    p.add_argument("--balance", choices=("invalid", "ref"), default="invalid",
                   help="balance in the corrected section: invalid (default) or the reference value")
    p.add_argument("--fallback", type=float,
                   help="factor for records without or with implausible balance (default: mean factor)")
    p.add_argument("--max-dev", type=float, default=15,
                   help="trusted share this many points above the reference = outlier (default: 15)")
    p.add_argument("--min-jump", type=float, default=4,
                   help="minimum balance jump at the boundary for the preconditions (default: 4)")
    p.add_argument("--mass", type=float, default=85,
                   help="rider + bike in kg, only for the speed cross-check (default: 85)")
    p.add_argument("-o", "--output", help="local file only: target file (default: <name>_corrected.fit)")
    p.add_argument("--force", action="store_true", help="local file only: overwrite an existing target")
    p.add_argument("-v", "--verbose", action="store_true", help="show every detail of the correction")
    p.set_defaults(func=cmd_fix)

    p = sub.add_parser("analyze", help="per-minute table and balance around pauses of a FIT file")
    p.add_argument("file", type=Path)
    p.set_defaults(func=cmd_analyze)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ReplaceError, VerifyError) as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 1
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
