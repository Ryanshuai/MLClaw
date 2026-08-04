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

Under 3 unpinned trials `band` refuses — unless a band comes from somewhere else, which is the next section. **Two points are a range, not a band**: with n=2 the interval is whatever those two runs happened to do, and calling that noise is a guess dressed as a measurement.

### A band has a source, and the source decides which way it can answer

Three eval trials cost two minutes. Three retrains cost three times the original run. Demanding the same number of both makes the expensive route pay up front for an ambiguity that may never arise — and **only the first trial can reveal whether it does**. So `--band-trials` defaults to what one costs (`eval` → 3, `retrain` → **1**), and the gap is filled by a second, weaker band.

| `band.source` | Built from | Reach |
|---|---|---|
| `trials` | ≥3 unpinned repeats of the whole procedure | **run-to-run spread.** Answers both ways |
| `run_history` | the target run's own **converged tail** — its per-epoch metric over the epochs after the schedule settled | **a lower bound.** Confirms; can never refute |

The tail is the *same* weights trajectory scored at nearby epochs: same seed, same data order, same init. It therefore does not contain the variation that makes two fresh runs differ — kernel selection under `deterministic: false`, dataloader order, initialisation. So it is a lower bound on run-to-run spread, and a lower bound is one-directional:

- **trial inside it** → sound. `delta ≤ lower_bound ≤ true noise`, so the delta *is* noise-sized whatever the true spread turns out to be. → `reproduced`
- **trial outside it** → **`inconclusive`, never `diverged`.** True noise may be wider than this band can see. → *this* is the case that buys repeats, and the only one.

With `run_history` the thing tested is the **fresh trial**, not the target — the interval already came from the target's own run — so `band` refuses a history band with no trial registered. It also refuses fewer than 5 history points: a handful is a range with extra steps, not a distribution. And it records `--history-what` (which epochs, and why those), because *which* epochs count as converged is a judgement, and a band whose window nobody wrote down cannot be checked later.

A declared tolerance is deliberately **not** a band source. `declared_tolerance` already holds a typed number and already states that it does not decide the verdict; giving it a path to become a band would undo that in one flag.

When both exist, `trials` decides and the tail is kept beside it as `also_measured`. That cross-check earns its place: **run-to-run spread cannot be smaller than within-run spread**, so a trials band narrower than the tail means the repeats held something fixed that the original run did not, and the interval they produced is too narrow to judge on. `band` says so.

### The recorded number may be the tail's max, and that bias runs one way

A best-checkpoint save picks the **best** epoch. If the target metric equals the extreme of its own converged tail, the recorded number is a selection, not a converged value — and a fresh run that lands on the tail **mean** has matched it while reading as short by roughly the tail's spread. Every time, in the same direction, with nothing anywhere raising.

Whenever a tail is supplied — even when the band came from trials — `band` checks for this and writes a caveat naming the tail mean. That is the number a reproduction should be judged against; the recorded one is the luckiest epoch.

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
| `remeasured` | as `reproduced`, but the target's **procedure was never re-run** | the artifact's number came back. Says **nothing** about whether the recipe still produces it |
| `remeasured_with_drift` | as above, ≥1 axis drifted | both weaker facts at once, and both keep saying so |
| `metric_ok_predictions_diverged` | band says `reproduced`, predictions differ | the dangerous cell — the average survived and the outputs did not |
| `diverged` | band says `diverged`, an axis attributed | the number moved and we know what moved it |
| `diverged_unattributed` | band says `diverged`, every suspect axis pinned | the number moved and nothing MLClaw records explains it. **A conclusion, not a failure to finish** |
| `not_reproducible` | an axis is `gone` | nothing was run and nothing could be |

There is no verdict for `inconclusive`. A session that reaches its budget still inconclusive should be left **open** with what it has — closing it would freeze a non-answer into the record as if it were one.

---

## Re-measuring is not reproducing, and one word was doing both jobs

`measure_via: eval` is the default **including for training runs**, and the cost argument for that is sound: re-measuring a surviving checkpoint answers *is the recorded number real* for the price of one eval, where retraining costs what the original cost. What was not sound was calling the result `reproduced`.

**Re-measuring a training run's artifact re-runs nothing about the training.** A hyperparameter recorded wrongly, a dataset recorded wrongly, a recipe that would no longer produce this model — every one of those is invisible to such a session, because the artifact is a *given* and only its number was checked. So the verdict has to say which question it answered:

| Target | `measure_via` | Verdict family | What it establishes |
|---|---|---|---|
| an **eval** or **infer** run | `eval` | `reproduced*` | **a full reproduction** — the run being reproduced *was* a measurement, so re-measuring it is re-running it |
| a **training** run | `eval` | `remeasured*` | the artifact still scores this. The recipe was not exercised |
| a **training** run | `retrain` | `reproduced*` | the training itself came back |

The split is keyed on the **target's stage**, not on a flag, because that is the fact that decides it.

**Why the distinction is load-bearing rather than tidy.** `skill-graph.md` makes a closed `reproduced*` session the only thing that moves an inherited checkpoint's `origin.confidence` off `claimed`. With one word for both, the weaker fact bought the stronger promotion — on precisely the inherited-checkpoint case that field exists to guard. This is the same defect `/discover` `references/searches.md` names under "Where the vocabulary breaks, and it is not cosmetic": *two words, same spelling, opposite bars.*

`close` refuses in both directions. `reproduced*` on a re-measurement is refused and told which word is available plus what retraining would cost; `remeasured*` on a session that really did re-run the procedure is refused too, because recording the weaker word loses a fact nobody can recover from the record later.

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

open --probe datasets/coco@probe_50   (eval → band_target_trials 3)
trial ×3 (nothing pinned)     48.42 · 48.55 · 48.48
band                          source trials · [48.42, 48.55] spread 0.13 · target 48.5 INSIDE
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
