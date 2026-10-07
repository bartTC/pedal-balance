# Background: the cases behind pedal-balance

Two rides with Garmin Rally pedals on a Hammerhead Karoo, where one pedal read too high at the start until the pedals were re-zeroed. Both are in the test fixtures (anonymized): `tests/fixtures/case1.fit` and `tests/fixtures/case2.fit`, each with its corrected counterpart.

## Case 1: 2026-10-05

Start 09:30:43, 3:16:55 (moving time 3:09:20), 75.2 km, 3 laps. FTP in the file 230 W.

- Zero offset before the ride at about 23 °C, then a drop to 12 °C within ~15 min.
- Second zero offset in the auto-pause 59:37–60:59 (10:30:20–10:31:41). The boundary "start + 3600 s" fell exactly in this pause.
- Stored balance value (as intervals.icu reads it: left): first ~5 min still 44–46 (pedals still warm), then ~40. Power-weighted 41.4 in hour 1, 47.5 afterwards. **The right pedal read too high.**
- Nature of the error: in hour 1, at 100–350 W, the right pedal was almost constantly **+32 W** above the expected value, at ~74 rpm. That corresponds to roughly 4 Nm of torque error, typical of a zero-offset error.
- Corrected (version at the time: reference 47.5, balance set to 48, fallback 0.85, without outlier protection or capping, summaries fully recomputed):

| | Original | Corrected |
|---|---|---|
| Hour 1, avg while pedaling | 231 W | 202 W |
| Hour 1, NP | 232 W | 208 W |
| Session avg / NP | 200 / 223 W | 191 / 216 W |
| IF / TSS | 0.97 / 296.1 | 0.94 / 277.7 |
| Power curve 5 / 20 / 60 min | 257 / 226 / 212 W | 242 / 222 / 207 W |
| Watts per heartbeat hour 1 (afterwards 1.53 / 1.51 / 1.47) | 1.79 | 1.56 |

- The best hour used to be in minutes 18–79, right in the faulty section. Afterwards it is in minutes 90–153.
- Cross-check with a different model (subtract 32 W scaled with cadence): avg 199.5 instead of 201.5 W, NP 203 instead of 208 W. The calculation via one pedal is noisier per second and raises NP by ~4 W.
- Expected were NP ~200 W in hour 1 and 213 W / load 270 overall. The result was 208 W and 216 W / 278. Load 270 would have needed FTP ≈ 233 W. The differences can be explained and were not tuned to match.
- Re-corrected with the current version, the result is practically the same (TSS 275.6 instead of 277.7, NP 215 instead of 216, same power curve); the only real difference is that the balance in hour 1 becomes invalid instead of 48. The speed cross-check fails for this ride (power ÷ expected from speed 0.90 in hour 1, probably wind or terrain), heart rate says +18 %. So `pedal-balance fix` shows a failed precondition here and asks for confirmation.

## Case 2: 2026-10-06

Start 16:37 (14:37:10 UTC), 1:23:31 (moving time 1:20:56), 32.9 km, 7 laps.

- Zero offset before the ride, after 15 min (auto-pause 15:14–16:07) and after 30 min to be safe (pause 30:24–31:10).
- Temperature: 21 °C at the start, 15 °C after 15 min, then 13–14 °C.
- Stored balance value: minutes 0–1 still 45–48, then falling steadily with the temperature to 31–32 in minutes 14–15. Power-weighted 39.7 in the section, 50.0 afterwards. **Again the right pedal read too high** (intervals.icu: ~37/63).
- The zero offset after 30 min changed nothing (48.9 → 49.3). After the 15 min zero offset the pedals were stable.
- Nature of the error: this time **not constant**. The right pedal was ~49 W above the expected value at 150 W and ~65 W at 350–450 W. A fixed watt deduction as in Case 1 would be wrong here.
- Corrected with the current version (boundary 16:07, reference 50):
  - 697 records via the left pedal (18 of them capped at the original value), 10 with a flat factor × 0.784 (2 without balance, 8 outliers > 65%), 208 with 0 W unchanged.

| | Original | Corrected |
|---|---|---|
| Section 0–16:07, avg incl. 0 W | 205.9 W | 161.5 W (−21.6%) |
| Section NP | 272 W | 224 W |
| Lap 2 (0–15 min) avg / NP / max | 206 / 274 / 944 W | 162 / 226 / 868 W |
| Session avg / NP | 187 / 251 W | 179 / 242 W |
| IF / TSS | 1.092 / 159.1 | 1.051 / 147.3 |
| Watts per heartbeat (rest: 1.61) | 2.19 (+37%) | 1.73 (+7%) |
| Power ÷ expected from speed (rest: 1.00, spread 0.90–1.09) | 1.35 | 1.06 |

- An estimate made beforehand ("0.37 ÷ 0.50 = −26%") was too high. Power-weighted, the share is 39.7, because the fixed torque error matters relatively less at high power. The actual result is −21.6%.
- The claim "already 53/47 at 150 W" is not confirmed by the file. In the rest, the stored value is 49–51 across all power levels.

## Pattern across both rides

- Both times: zero offset at ≥ 21 °C (probably indoors), then cooled down outside by 6–11 °C within ~15 min. The error started near zero and grew minute by minute as the temperature fell, always on the **right** pedal. A zero offset at outdoor temperature fixed it both times, and it stayed stable afterwards.
- Temperature drift fits the data very well, but it is not proven. That it was the same side twice could also be down to the right pedal itself (temperature compensation). If it shows up without a temperature change, that would be a case for Garmin support.
- **In practice:** do the zero offset only outside, once the pedals are at outdoor temperature, i.e. after a few minutes of riding or after the bike has stood outside for a while. Watch the L/R balance on the head unit during the first minutes: if it drifts away from the normal value (~48–50), stop and re-zero.

## Trial run of the check

Both cases are detected as a step. Case 1: Start–59:37, 41.3 vs. 47.5, correction ≈ −13%, watts per heartbeat +18%. Case 2: Start–15:14, 39.3 vs. 50.0, ≈ −21%, +37%. The corrected files count as ok.

Across 29 Rally rides from April to October 2026 there was no other step. The multi-day rides with 50–80 stops don't trigger one either. A bias showed up on 10 rides from April to July: pedal set A at 43–45, pedal set B at 56–57. Artificially inserted steps of 6–7 points at pauses were found; 4 points and a step without a pause were not.

Looking at the balance by power level: on the bias rides of pedal set B the balance moved towards 50 as power rose (e.g. 61 at 80–120 W, 53 at 250–320 W), the signature of a fixed torque error on one pedal. Other bias rides were flat across all power levels (e.g. 44–45 from 80 W to over 320 W), which fits a scaling error or a real difference between the legs on that day just as well.
