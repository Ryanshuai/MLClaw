# The band, the verdicts, and every refusal

## The band

`band` takes the trials with nothing pinned, sorts their values, and reports the interval:

```
n        how many unpinned trials
min/max  the interval those repeats actually produced
spread   max - min
target   the value recorded on the run being reproduced
```

The question is **is `target` inside `[min, max]`**. Nothing is assumed about the distribution and nothing needs to be: "would this pipeline have produced that number again" is answered by whether it did.

| `metric_verdict` | Rule | What it means |
|---|---|---|
| `reproduced` | `min ≤ target ≤ max` | the recorded value is one this pipeline still produces |
| `inconclusive` | outside, but the gap to the nearest edge is ≤ `spread` | the interval is too narrow to answer either way — **run more repeats** |
| `diverged` | the gap exceeds the whole spread | outside by more than the pipeline's own scatter |

`inconclusive` is not a hedge, it is the loop's engine. Three repeats of a stable pipeline produce a tight interval, and a real value can sit a hair outside one by chance. Widening the interval with more repeats either swallows the target (`reproduced`) or leaves it further out in relative terms (`diverged`). This is what "run it repeatedly until there is a conclusion" means concretely.

Under 3 unpinned trials `band` refuses. **Two points are a range, not a band** — with n=2 the interval is whatever those two runs happened to do, and calling that noise is a guess dressed as a measurement.

### The declared tolerance is a foil, not a threshold

`open` records `--tolerance-pct` / `--tolerance-abs`, and `band` never judges against them. What it does instead is compare them to reality and say which way they were wrong:

- **wider than the measured spread** → "it would have accepted a divergence N× larger than real noise." This is the common case with the `±0.5%` default inherited from `/refactor-run`, and it is usually the most useful line the session produces.
- **tighter than the measured spread** → "judging on it would call this pipeline's own noise a divergence." This is how a reproducible pipeline gets declared broken.

Either way the note lands in `caveats` and travels with the verdict.

---

## Final verdicts

| Verdict | Requires | Reads as |
|---|---|---|
| `reproduced` | band says `reproduced`, **every** axis `intact`, probe predictions agree | the strongest claim available: same number, same conditions, same outputs |
| `reproduced_with_drift` | band says `reproduced`, probe run if declared | the number came back, but not from the same conditions. **A weaker fact that has to keep saying so** |
| `metric_ok_predictions_diverged` | band says `reproduced`, predictions differ | the dangerous cell — the average survived and the outputs did not |
| `diverged` | band says `diverged`, an axis attributed | the number moved and we know what moved it |
| `diverged_unattributed` | band says `diverged`, every suspect axis pinned | the number moved and nothing MLClaw records explains it. **A conclusion, not a failure to finish** |
| `not_reproducible` | an axis is `gone` | nothing was run and nothing could be |

There is no verdict for `inconclusive`. A session that reaches its budget still inconclusive should be left **open** with what it has — closing it would freeze a non-answer into the record as if it were one.

---

## Every refusal `close` raises

Exit 1 means the script worked and the answer is no. Pass these through; do not route around them.

| Refusal | Why |
|---|---|
| no band was measured | a verdict on a delta requires knowing the pipeline's own noise. Judging against a declared tolerance is the guess this whole loop replaces |
| `reproduced` while an axis `drifted` | the number came back from different conditions. Downgrade to `reproduced_with_drift`, which keeps saying so |
| `reproduced` while an axis is `unverifiable` | an axis nobody could check is not an axis that matched. `code.reproducible: false` in particular means the rebuilt tree is not the tree that ran, so a matching number is evidence and not proof |
| either reproduction verdict with a declared probe nobody ran | the probe is the *stronger* of the two checks. Closing without it reports the weaker one as if both had passed |
| either reproduction verdict while `metric_verdict` isn't `reproduced` | the recorded value is not inside the measured interval; this is not a reproduction whatever else is true |
| predictions differ, verdict asserts reproduction | that is `metric_ok_predictions_diverged` — the whole reason the probe exists |
| `diverged` with no `--attributed-to` | if no axis explains it, the honest verdict is `diverged_unattributed` |
| session already closed | a closed session's trials are its evidence; adding to them afterwards changes what the verdict was based on |

`trial` refuses separately, and for the reason that matters most: a **mode or scope mismatch** against the target. A debug trial judged against a production target is a fake comparison — nothing errors, no data is missing, and a wrong conclusion is drawn from correctly-recorded numbers.

---

## A worked session

Target: `training/run_20260315_120000`, `val_mAP = 48.5`, five months old.

```
check                         data intact · code intact · env DRIFTED · params intact · artifacts intact
                              → reproducible_with_drift (ceiling stated up front)

open --probe datasets/coco@probe_50 --band-trials 3
trial ×3 (nothing pinned)     48.42 · 48.55 · 48.48
band                          [48.42, 48.55]  spread 0.13  target 48.5 INSIDE
                              → metric_verdict: reproduced
                              ⚠ declared ±0.5% (=±0.2425) is 1.9× WIDER than real noise —
                                it would have passed a divergence twice the size of noise

infer probe × 2 artifacts     side by side, 50 units
                              → person judges: small objects missing in 6 of 50
trial --predictions-differ

close --verdict reproduced             REFUSED → the probe predictions disagree
close --verdict metric_ok_predictions_diverged
                              caveats: [tolerance note, "env drifted between the original
                                        run and every trial here"]
```

The number reproduced and the model did not. Judging on the declared tolerance alone — one run, no band, no probe — would have closed this as a clean pass in about four minutes.

The follow-up is Step 5 against the *prediction* divergence rather than the metric: pin `env` (the one drifted axis), re-run the probe, and see whether the small objects come back. That is the same loop, and the thing it is now explaining is the cell the metric could not see.
