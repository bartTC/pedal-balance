# pedal-balance

Checks and fixes the left/right balance of dual-sided power meter pedals such as Garmin Rally. After a wrong zero offset or temperature drift, one pedal can read too high for a section of the ride until the pedals are re-zeroed, which inflates power, the power curve, eFTP and load. `pedal-balance` finds such sections in your rides on intervals.icu or in local FIT files, corrects a faulty section at the start of a ride in place (no re-encoding, developer fields stay intact) and can replace the activity on intervals.icu.

![Balance chart in intervals.icu: the first 15 minutes sit at R 55–70, after the auto-pause at 15:14 the balance is around 50/50](https://raw.githubusercontent.com/bartTC/pedal-balance/main/docs/images/balance-step.png)

*A typical case in intervals.icu: the pedals were zeroed indoors, and outside the right pedal reads too high, more so as it cools down. Re-zeroed in the auto-pause at 15:14, balanced afterwards. `pedal-balance check` reports `STEP Start–15:14 -21%`.*

## Install

Python ≥ 3.10.

```sh
uvx pedal-balance check            # run without installing
uv tool install pedal-balance      # or install the command
pipx install pedal-balance
```

## Setup

`check` and `fix` with an activity ID need intervals.icu credentials (intervals.icu → Settings → Developer):

```
INTERVALS_ICU_API_KEY=...
INTERVALS_ICU_ATHLETE_ID=...
```

They are looked up in this order: the environment, then `.env` in the current folder, then `~/.config/pedal-balance/.env` (`$XDG_CONFIG_HOME` is respected). Local FIT files need no credentials.

Downloaded and corrected files go to `~/.cache/pedal-balance` (`$XDG_CACHE_HOME` is respected). Change it with `--cache DIR` before the command (`pedal-balance --cache DIR check`) or with `PEDAL_BALANCE_CACHE`.

## Commands

### `check [days]`

```sh
pedal-balance check          # rides of the last 7 days on intervals.icu
pedal-balance check 30       # last 30 days
pedal-balance check --file ride.fit other.fit
```

Corrects nothing. Shows a table with one row per ride (Date, Bike, Pedals, Balance, Dropout, Finding, intervals.icu link) and below it a panel with the details for every ride with a STEP or BIAS. Findings read `ok`, `STEP <span> <pct>` or `BIAS ±<pct>`, plus `(corrected)` for files written by `fix` (suffix `_corrected.fit`, or the older `_korrigiert.fit`). Rides without power are hidden. Original files are downloaded once and then read from the cache.

### `fix TARGET`

```sh
pedal-balance fix i123456789
pedal-balance fix https://intervals.icu/activities/i123456789
pedal-balance fix ride.fit                     # local only: writes ride_corrected.fit
pedal-balance fix i123456789 --until 15m       # boundary by hand
```

1. For an intervals.icu activity: download the original file to the cache. It stays there as a backup.
2. Look for a step like `check`. If it is at the start, its boundary becomes the end of the faulty section. Without a step, with a step in the middle of the ride, or with only a BIAS (see [Why a bias is never corrected](#why-a-bias-is-never-corrected)), `fix` stops. `--until` sets the boundary by hand.
3. Correct in memory and show a before/after table: section (avg, NP, max, balance, watts per heartbeat), whole ride (avg, NP, max, IF, TSS), changed laps and power curve (5 s to 60 min). Then the preconditions. If one is not met, the cross-check with heart rate and speed is shown, and you can still confirm. `-v` shows every detail of the correction.
4. Ask `[y/n]`, default no. Nothing is written before that.
5. On yes: verify the corrected file (size, CRC, only patched fields changed, decodes with fitdecode and the Garmin FIT SDK) and write it. For a local file that is `<name>_corrected.fit` next to the original (`-o` for another name, `--force` to overwrite). For an intervals.icu activity: upload `<id>_corrected.fit` as a new activity, carry over name, description, type, sub type, bike, RPE, feel, tags, commute/trainer/race flags and the paired workout, then delete the old activity.

Upload comes before delete. If intervals.icu creates nothing (it detects duplicates by file hash) or carrying over the details fails, the old activity stays. A manually set load is not carried over, because it was set due to the wrong data in the first place. intervals.icu recomputes load, NP and zones from the records with the FTP from its settings.

Tuning options of `fix`:

| Option | Default | Effect |
|---|---|---|
| `--until 15m` | from the step | End of the faulty section from the first record (`15m`, `1h`, `900`). Snaps to an auto-pause within ±2 min. |
| `--no-snap` | off | With `--until`: do not snap to an auto-pause. |
| `--ref 50` | rest of the ride | Reference balance in %: power-weighted rest of the ride, rounded to 0.5. |
| `--ref-min-power 80` | 80 W | Data points below this do not count for the balance averages. |
| `--trust auto\|stored\|other` | auto | Pedal that measures correctly: the side of the stored value or the other one. auto = the side whose share dropped in the section. |
| `--balance invalid\|ref` | invalid | Balance in the corrected section: invalid (0xFF) or the reference value. |
| `--fallback 0.85` | mean factor | Factor for records without balance or with implausible balance. |
| `--max-dev 15` | 15 | A trusted-pedal share this many points above the reference counts as an outlier. |
| `--min-jump 4` | 4 | Minimum balance jump at the boundary for the preconditions. |
| `--mass 85` | 85 kg | Rider + bike, only for the speed cross-check. |

### `analyze FILE`

Per-minute table (pedaling seconds, balance, power, heart rate, speed, temperature) and the balance in the 5 minutes before and after every auto-pause, with a suggestion for `--until`. For looking at a ride by hand.

## How detection works

Only steady pedaling counts (above 80 W, above 60 rpm), power-weighted. An average over everything would be too coarse, because sprints, coasting and real differences between the legs shift it.

- **STEP:** A section with at least 5 min of steady pedaling differs by ≥ 5 points from the rest, and its boundary is at an auto-pause or a dropout. The zero offset itself is not recorded in the file (only timer events), but it happens while stopped. A fairly reliable sign of a measurement error. Shown: the section, the balance right before and after the boundary, the approximate correction (as with `--trust auto`) and watts per heartbeat compared with the rest. The search covers all pairs of stops. Candidates are scored like a two-sample test, and the shorter part is reported.
- **BIAS:** The ride (with a step: the rest) is ≥ 5 points away from 50/50. This is only a suspicion, because your legs don't have to pedal 50/50. A bias is deliberately never corrected, see below.
- Also: dropouts (≥ 3 s without power while the timer runs, shown from 30 s in total) and power without balance (from 5% of pedaling time).

5 points of balance are roughly 10% wrong total power.

`fix` can only correct a step at the **start**. `check` still reports a step in the middle of the ride.

### Why a bias is never corrected

This is intentional and has to stay that way. The balance only shows that the two pedals disagree. It does not show which one is wrong, or whether it reads too high or too low. The "±14%" from the check is therefore not a known error but two equally good explanations. Example: a ride on 2026-05-23, balance 57/43, NP 176 W:

| What happened | True power | Correction |
|---|---|---|
| The pedal with 57% reads too high | 2 × 43% × 176 ≈ 151 W | −14% |
| The pedal with 43% reads too low | 2 × 57% × 176 ≈ 201 W | +14% |

Both cases produce exactly the same file. The way the balance changes with power doesn't tell them apart either: with a fixed error o on one pedal, the share in both cases is ½ + o ÷ (2 × measured power). Picking the wrong direction leaves you 28% off instead of 14%.

A **step** is different. The correct rest of the same ride provides the reference, and heart rate and speed from the same ride show the direction (in both cases in [docs/cases.md](docs/cases.md): watts per heartbeat +18% and +37%, so too high).

For a bias only outside hints remain, and they are weak. On 2026-05-23 the EF (NP ÷ heart rate) according to intervals.icu was 1.29, rides of similar length are at about 1.5. With −14% it would be 1.11, the lowest value of all shorter rides. With +14% it would be 1.47, which is normal. So that day the pedal more likely read **too low**, and a flat −13% would have made the ride even worse. Heart rate also depends on heat, fatigue and terrain, though, so this is no proof.

On top of that:

- The correction always assumes that one pedal reads too high and only lowers power. It cannot represent the "too low" case at all.
- 50/50 is only an assumption as well. Clean rides are between 47.5 and 52.5, which alone is ±5% on the correction.

A bias is therefore only corrected by hand. The direction has to be justified from outside, for example by a second power meter, an indoor trainer or a clear comparison with heart rate.

## How the correction calculates

Basic assumption: one pedal measures correctly. The true left/right split in the section equals the one in the rest of the ride. For every record with power:

```
power_new = power × share_trusted_pedal / reference_share_trusted_pedal
```

Decisions and why (Case 1 and 2 are described in [docs/cases.md](docs/cases.md)):

1. **Per data point via the trusted pedal, no fixed watt deduction.** A zero-offset error shifts the *torque* by a fixed amount. In watts the error therefore grows with cadence. In Case 2 it also grew with power. Calculating via the trusted pedal covers both.
2. **Side:** auto picks the side whose share dropped in the section. That is only right if the other pedal reads too high, and not this one too low. The cross-check (point 9) tests that.
3. **The boundary snaps to the auto-pause.** The zero offset happens while stopped, i.e. in an auto-pause. In Case 2, "900 s" would have left the last 14 wrong seconds before the pause untouched.
4. **The balance in the section becomes invalid (0xFF).** Otherwise intervals.icu shows a clean balance that was never measured. In Case 1 it was still set to 48 (old version).
5. **Outliers:** If the share of the trusted pedal is more than 15 points above the reference, it is a stuck or broken value. The trusted pedal cannot have a clearly larger share than normal while the other one reads too high. These records get the mean factor. Example Case 2: 656 W at balance 72 would otherwise have become 945 W, an invented lap maximum.
6. **Never more than before:** If one pedal reads too high, the true power cannot be above the measured value. The formula is capped at the original value (Case 2: 18 seconds affected).
7. **Summaries in lap/session:** The device value is scaled by the ratio corrected/original, both computed from the records. The Karoo computes from higher-resolution data, so recomputing from 1 Hz records differs (Case 2, lap 1: Karoo 171 W, records 131 W; session NP 251 vs. 247 W). Replacing the values would distort unchanged ones. IF is scaled with the NP ratio, TSS with its square. max_power stays unless the maximum was in the corrected part. Balance in lap/session is recomputed from the valid values, otherwise set invalid. Torque effectiveness and pedal smoothness are set invalid because they cannot be recomputed cleanly.
8. **accumulated_power** (if present) counts continuously. From the first corrected record on, all following ones are adjusted.
9. **Preconditions (safety brake):** The balance jumps by ≥ 4 points at the boundary **and** the original power is ≥ 8% too high for heart rate or speed (and too low nowhere). Real differences between the legs don't jump at a zero offset. Real sprints match heart rate and speed. `fix` shows the result before asking; if a precondition is not met, it says so and shows the cross-check, and you decide.
   - Heart rate: watts per heartbeat in the section vs. the rest. Caveat: during the warm-up the heart rate lags, so the value is naturally somewhat higher.
   - Speed: model `P = a·v³ + b·v + m·g·v·grade`, a and b fitted on the rest of the ride (30 s blocks from 14.4 km/h, mass 85 kg). Wind and drafting are not in the model. The spread between the first and second half of the rest is shown too (Case 2: 0.90–1.09, out-and-back with wind).
10. **In-place patch instead of re-encoding:** The Karoo writes developer fields (battery, tire pressure, gears, suspension …). Re-encoding with fit-tool or similar risks losing something. `fix` only overwrites field bytes and checks before writing that every changed byte lies in a patched field, that size and message sequence are unchanged and that the CRC is correct.

## FIT details worth knowing

- `record.left_right_balance` is one byte. Bit 0x80 means "value refers to the right side", the lower 7 bits are the percentage. **In the Karoo files the flag is never set.** According to the FIT profile that means "side unknown", and intervals.icu reads the value as the left share. For the power this doesn't matter: power × value / reference gives the same result whichever side the value belongs to. Files recorded by Garmin devices do set the flag.
- In lap/session the field is called `left_right_balance_100` (uint16, hundredths, flag 0x8000). The Karoo does not write it. Also absent from the Karoo files so far: `total_work`, torque effectiveness, pedal smoothness, `accumulated_power`. They are handled anyway in case another device writes them (untested with real files).
- Records only exist while the timer runs. Auto-pauses are gaps in the timestamps.
- Karoo summaries: avg = sum ÷ timer time (incl. 0 W). NP ≈ 30 s average, time-based, pauses as 0 W. TSS = timer time × NP² ÷ FTP² ÷ 3600 × 100 with the unrounded internal NP. FTP is stored as `threshold_power` in the session.
- `record.grade` is sint16 in 1/100 %. A scaling error here had initially made the speed model useless.
- The Karoo defines 16 developer fields (battery, tire pressure, e-bike, skin temperature, gears, suspension). Hence the in-place patch.

## Limitations

- Only one faulty section at the **start** of the ride, no range in the middle of the ride.
- No bias across the whole ride, because the direction is unknown (see [Why a bias is never corrected](#why-a-bias-is-never-corrected)).
- The calculation via one pedal is noisier per second than the sum of both. NP in the section is therefore ~2% higher than with a smooth model (Case 1: 208 vs. 203 W). Short peaks in the section are correspondingly uncertain.
- The reference assumes that your own split in the section was the same as in the rest. It depends somewhat on power (Case 1: 46.6 at 100–200 W, 48–49 at 300–450 W), which is ±2–3%.
- `segment_lap`/`split` messages are not adjusted (`fix` warns).
- Chained FIT files are not supported.

## Development

```sh
just test                 # pytest: unit and regression tests
just lint                 # ruff with all rules (config in pyproject.toml)
just check 30             # run the CLI from the repo
just fix i123456789
just build                # sdist and wheel into dist/
```

The Justfile sets `PEDAL_BALANCE_CACHE` to `./fits` (gitignored), so downloads stay in the repo instead of `~/.cache/pedal-balance`.

The fixtures in `tests/fixtures` are real rides, anonymized with `uv run python -m tests.anonymize FILE`: GPS positions and serial numbers are set to the FIT invalid value, everything else stays byte-identical, so a fixture and its corrected counterpart still compare byte for byte. New fixtures must go through it.

## Background

[docs/cases.md](docs/cases.md) describes the two rides this tool was built from, the error pattern (zero offset indoors, then cooling down outside) and a trial run of the check over half a year of rides.

## License

MIT
