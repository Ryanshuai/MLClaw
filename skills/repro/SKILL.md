---
name: repro
description: >
  Answer whether a past run can still be reproduced, and when its number moves, say which axis
  moved it. Audits five axes of rot (data, code, env, params, upstream artifacts), then drives a
  loop: re-measure the number, judge it against a band this pipeline measured on itself, run a
  frozen probe set through both artifacts so a person can see whether the predictions agree, and
  pin one axis per iteration until the divergence is attributed. Composes /data-check, /eval-run,
  /infer-run and /train-run; executes nothing itself. Trigger for: reproducing a result, checking
  whether an old run still holds up, verifying a number somebody claims, a paper or handover
  number you want to confirm, "why did this get worse", and "can we still rebuild that model".
  Also trigger for Chinese requests like "复现一下", "这个结果还能复现吗", "三个月前那个跑不出来了",
  "为什么数字不一样", "验一下这个 mAP", "老板要复现那个实验", "这个 checkpoint 还能重现吗".
  Not for comparing two different configs (that is /train-compare) and not for refactoring against
  a paper target (use /refactor-run).
---

# /repro — Can this still be reproduced, and if not, what moved

**Running inference over a val set is not a reproduction, and this skill refuses to call it one.** Re-measuring a training run's artifact re-runs nothing about the training — a hyperparameter or dataset recorded wrongly, and a recipe that would no longer produce this model, are all invisible to it. That session is real and often what you want, but it closes as `remeasured*` and needs `--remeasure-only` typed at `open`, exactly as its expensive twin needs `--i-accept-the-cost`. Neither combination is the default, because which question this is cannot be defaulted. `references/verdicts.md` → "Re-measuring is not reproducing".

A run's record rots along five axes at once and reads as pristine the whole way down. Every piece of the reproduction contract is already written somewhere; **nothing has ever checked that it still holds.** `code_snapshot.py` writes `origin_commit` plus a dirty patch and calls that a reproduction contract — no verb re-reads it to ask whether the commit still resolves. `/data-freeze` pins membership, `/data-retire` stamps `data_retired` when the bytes go — nothing joins that back to the runs that cited it. `env.packages` is captured per run and only ever compared inside `/train-run`.

This skill is the join, plus the loop that turns a delta into a conclusion.

## What it composes, and what it never does

`/repro` **executes nothing of its own** — the same discipline as `/data-curate`. Every measurement is an ordinary run through the stage that owns it, so code snapshots, metric streams, retention and comparability filtering come for free, and every trial is visible to `list_runs.py` and `build_dag.py` like any other run.

| It calls | For |
|---|---|
| `/data-check` (`census.py resolve`) | rebuilding the data axis — resolving the cited snapshot into openable paths again |
| `/eval-run` | re-measuring the number. **The default**, including for training runs |
| `/infer-run` | the probe set — the same fixed inputs through both artifacts, so a person can look |
| `/train-run` | retraining. Only when the user asks for it, and `open` refuses without `--i-accept-the-cost` |

Four things it must not do:

- **Never modify the run being reproduced.** That record is the evidence under test. "Fixing" its null `mode` or back-filling a missing metric destroys the thing you were checking.
- **Never re-freeze, re-collect, or delete anything to make a reproduction work.** Report the axis and route to the skill that owns it. A repro that repairs its own preconditions has stopped being a measurement.
- **Never suggest `/data-retire`.** Nothing on this path frees space, and offering it here would make a deletion a step on the way to something else.
- **Never pin an axis that is `intact`.** It is a no-op that costs a full run, and `attribute` will not suggest one.

## On entry

Standard Workflow State Protocol: push to `stack`, append `started` to `history`. Requirements:

| Requirement | Check |
|---|---|
| project.json exists | `{PROJECT}/project.json` |
| a completed run to reproduce | `stages/*/runs/*/run.json` with `status: "completed"` and non-null `metrics.best.primary_metric_value` |
| the stage that will re-measure is initialized | `stages/evaluation/config.json → entry_command` non-empty for `measure_via: eval`; training's for `retrain` |

**Resume before starting.** `repro.py status --project {PROJECT} --open-only`. A repro session is long-lived by construction — three band trials plus an attribution walk is many runs across days — so an open session is the normal state to find, not an anomaly. Report where it stands (trials so far, whether a band exists, which axes have been pinned) and continue it rather than opening a second one against the same target.

## Step 1: Audit the five axes

```bash
python <mlclaw_root>/lifecycle/scripts/repro/repro.py check --project {PROJECT} --run <stage>/<run_id>
```

Records only: local files and local `git`, no network and no ssh. Writes a dated observation to `stages/<stage>/runs/<run_id>/repro/check_<ts>.json` — dated because it is an observation of the world, like a census, and re-taking it next month gives a different answer. Never exits 1; reporting that a reproduction is dead is not a refusal.

Per axis: `intact` / `drifted` / `gone` / `unverifiable`. Read `references/axes.md` for what each probe reads and why. The fourth value is the load-bearing one — **a probe that could not run never collapses into `intact`.** "The commit resolves" and "no commit was recorded" are different facts and only the first is evidence.

**Report the verdict before proposing anything.** Three of the four outcomes end the conversation differently:

| Overall | Means | Say |
|---|---|---|
| `reproducible` | every axis intact | open a session |
| `reproducible_with_drift` | ≥1 axis drifted | open a session, **and the ceiling is already `reproduced_with_drift`** — say that up front rather than at close |
| `reproducible_unverifiably` | nothing known to have changed, something couldn't be checked | open a session, same ceiling, and name what couldn't be checked |
| `not_reproducible` | an axis is `gone` | **stop.** `open` refuses. Deliver `you_can_still` |

`not_reproducible` is a real answer, not a failure. When training data has been retired, the honest report is "this training run cannot be reproduced, and here is what you can still verify: the surviving checkpoint's number, through `/eval-run`." That second half matters — a dead training reproduction usually leaves a live eval one, and it is what the user actually wanted.

## Step 2: Open the session — declare everything first

```bash
python <mlclaw_root>/lifecycle/scripts/repro/repro.py open --project {PROJECT} \
    --run training/run_20260315_120000 --name lr1e4 \
    --probe 'datasets/coco@probe_50' \
    --remeasure-only                       # or: --measure-via retrain --i-accept-the-cost
```

`open` reports `verdict_ceiling` and `reproduces_the_procedure` — **read them before spending the trials.** Learning at `close` that the best available word was never the one you wanted means the runs are already gone.

Everything that could be chosen to flatter the result is fixed here, before a single trial runs:

- **The probe set.** A fixed, small, ideally frozen (`datasets/<id>@<snapshot>`) set of inputs. Declared now and never afterwards — a probe set picked once the result is known is a test written to its own answer. 20–50 units is plenty; this is a look, not an eval.
- **`measure_via`, and it decides which verdict you can close with.** `eval` by default, **including for training runs** — but re-measuring a training run's artifact re-runs nothing about the training, so such a session closes as **`remeasured*`, never `reproduced*`**. `close` refuses the stronger word and says so. A hyperparameter or dataset recorded wrongly is invisible to it; the artifact is a given and only its number was checked. When the target is itself an eval or infer run, measuring it *is* re-running it and `reproduced*` is correct. Full table: `references/verdicts.md` → "Re-measuring is not reproducing". Retraining to verify a number costs what the original cost and answers a fuzzier question, because nondeterminism means the best possible answer is a band rather than a match. Re-measuring the surviving checkpoint through eval answers "is the recorded number real" for the price of one eval. `retrain` answers a different and rarer question — "does this recipe still produce a model this good" — and needs `--i-accept-the-cost`.
- **The declared tolerance.** Recorded, but it does **not** decide the verdict. It is kept so Step 3 can compare it against reality and tell you when your default would have passed a real divergence.

`open` refuses a run that is not `completed`, has no metric value, or has `mode: null` — that last one because metrics are comparable only within the same `mode` and an equivalent `scope`, so a null mode cannot be matched by any trial.

## Step 3: Measure the band — how many times depends on what a time costs

Launch `--band-trials` runs through `/eval-run` (or `/train-run`) that change **nothing**. Register each:

```bash
python <mlclaw_root>/lifecycle/scripts/repro/repro.py trial --project {PROJECT} \
    --session <sid> --run evaluation/<run_id>
python <mlclaw_root>/lifecycle/scripts/repro/repro.py band --project {PROJECT} --session <sid>
```

**This is the step people skip, and skipping it is why reproduction arguments never end.** A -0.3% delta is either this pipeline's own noise or a real divergence, and one run cannot tell you which. `band` measures the interval the repeats actually produced and asks whether the recorded value lies inside it. No distribution is assumed and none is needed: "would this pipeline have produced that number again" is answered by whether it did.

**But three of them is not one price.** Three eval trials are two minutes; three retrains are three times the original run — and that bill covers an ambiguity that may never arise, which **only the first trial can reveal**. So `--band-trials` defaults to `eval` → 3, `retrain` → **1**, and a one-trial session is not a session without a band:

```bash
python <mlclaw_root>/lifecycle/scripts/repro/repro.py band --project {PROJECT} --session <sid> \
    --from-history '[...the target's per-epoch metric over its converged tail...]' \
    --history-what 'epochs 101-140; mosaic closed at 100'
```

The target run's own converged tail is a band, and it is free — usually already sitting in the checkpoint or the training log. It is **weaker in one exact way and that way decides everything**: same trajectory, same seed, same data order, so it is a *lower bound* on run-to-run spread. Inside it → sound, the delta is noise-sized. Outside it → **`inconclusive`, never `diverged`** — and that is the one case worth buying repeats for. Rules, refusals, and the cross-check when both bands exist: `references/verdicts.md` → "A band has a source".

**Whenever you have the tail, pass it even if the band came from trials** — `band` uses it for a check nothing else can make: whether the recorded target number is the tail's *max*. A best-checkpoint save is a selection, and judged against it a fresh converged run reads as short by about the tail's spread, always in the same direction, with nothing raising. The caveat names the tail mean, which is the number to judge against.

**Set `lineage.repro_of` on every trial run** to `<stage>/<run_id>` of the target. Not `fork_of` — a fork intends to differ and carries a `variation_summary` of what it changed, while a trial intends to be identical and its empty diff is the point; conflating them makes every reproduction read as an experiment. Not `parents` either: a trial consumes no artifact of the target, it re-measures the same quantity.

`trial` refuses a mode or scope mismatch against the target. Pass that refusal through: it is the fake-comparison guard, and a debug trial judged against a production target produces a wrong conclusion from correctly-recorded numbers.

Three metric verdicts, and the middle one is the loop:

| `metric_verdict` | Meaning | Next |
|---|---|---|
| `reproduced` | the recorded value is inside the measured interval | Step 4 |
| `inconclusive` | outside, but within one spread of the edge | **more repeats.** The interval is not yet wide enough to answer either way — this is what "反复做" is for |
| `diverged` | further out than the whole spread | Step 5 |

`band` also reports how the declared tolerance compares to measured reality. Show that line to the user — it is usually the most surprising output of the whole session ("your ±0.5% default is 1.9× wider than this pipeline's real noise; it would have accepted a divergence twice the size of noise as a pass").

## Step 4: The probe — two runs can share a number and not a prediction

A matching aggregate metric is weaker evidence than it looks. The small objects can all get lost while the average lands in the same place. So run the probe set through **both** artifacts with `/infer-run` — the original checkpoint and the reproduction — and put the outputs side by side for the user.

Then let the person judge, and record what they judged:

```bash
python <mlclaw_root>/lifecycle/scripts/repro/repro.py trial --project {PROJECT} --session <sid> \
    --run evaluation/<run_id> --probe-run inference/<run_id> --predictions-differ
```

**Why a person and not an epsilon.** "The boxes moved 0.3 px" and "the small objects vanished" are both numeric differences and no threshold on a prediction file separates them. That is the entire reason this step is a look. Per `/ask-human`'s rule, that judgement is a **claim** — record on what basis it was made (`--note`), and never upgrade it to something else.

This produces the 2×2 that pass/fail cannot express:

| | predictions agree | predictions differ |
|---|---|---|
| **metric inside the band** | `reproduced` | **`metric_ok_predictions_diverged`** — the dangerous cell. Never call it reproduced |
| **metric outside the band** | usually a membership/scope difference → look at the data axis first | `diverged` → Step 5 |

`close` enforces this: both `reproduced` and `reproduced_with_drift` refuse when a probe was declared and never run. Both assert the number came back, so both carry the full evidence bar.

## Step 5: Attribute — one axis per iteration, cheapest first

```bash
python <mlclaw_root>/lifecycle/scripts/repro/repro.py attribute --project {PROJECT} --session <sid>
```

It names the cheapest untested suspect. Pin that one axis back to what the original run recorded, re-run, and register with `--pinned <axis>`. Repeat. Order is `env → params → artifacts → data → code`, and it is **cost order, not likelihood order**: a wrong guess about likelihood costs one cheap iteration, a wrong guess about cost can cost three days of GPU.

Only `drifted` and `unverifiable` axes are suspects. `references/axes.md` has the concrete pin for each.

Two ways this ends, and both are conclusions:

- **An axis is implicated** — pinning it brought the number back into range and the others didn't. `close --verdict diverged --attributed-to <axis>`.
- **Every suspect pinned, still outside** — `diverged_unattributed`. Report it as a finding, not a failure: no axis MLClaw records explains the difference, which means either the nondeterminism is wider than three repeats measured (go widen the band) or something nobody recorded changed. Naming that honestly is worth more than a guess.

If **no** axis drifted and the number still diverges, do not spend a run pinning anything. `attribute` says so. That combination points at nondeterminism the repeats under-measured, or at an axis nothing here records — data order, host, a clock.

## Step 6: Close

```bash
python <mlclaw_root>/lifecycle/scripts/repro/repro.py close --project {PROJECT} --session <sid> \
    --verdict reproduced_with_drift --note "predictions judged by <who>, side-by-side on 50 probe units"
```

Read `references/verdicts.md` for the full vocabulary and every refusal `close` raises. The refusals are the point of the skill, so pass them through rather than routing around them (exit 1 = the answer is no; exit 2 = the script broke, do it by hand):

- **no band measured** → refused. A verdict on a delta without knowing the pipeline's own noise is the guess this loop exists to replace.
- **`reproduced` while any axis drifted or is unverifiable** → refused, downgraded to `reproduced_with_drift`. The number came back but not from the same conditions, and that weaker fact has to keep saying so every time it is read.
- **`reproduced*` with a declared probe nobody ran** → refused.
- **`diverged` with no attributed axis** → refused; that is `diverged_unattributed`.

Every drifted and unverifiable axis is stamped into `caveats` automatically, so the conclusion carries its own qualifications wherever it is quoted later. That is the whole reason this record exists: a repro verdict is written once and read by people who can no longer re-check it.

## What this makes possible elsewhere

- An inherited checkpoint's `artifacts.json → items.<name>.origin.confidence` moves from `claimed` to `verified` **only** by a closed session whose verdict asserts reproduction. That field's whole purpose is to not take an author's number on their word, and this is the thing that does the checking.
- `/data-retire`'s `data_retired` stamp finally gets read. It was written so a citation could resolve *and say the bytes were freed*, and until now nothing joined it back to the runs that cited it. Note what the stamp does **not** say: it names one location, so the verdict comes from joining it against a census taken since — surviving copies are `drifted`, no copies are `gone`, and no census since the deletion is `unverifiable`. `references/axes.md` → "A retirement stamp is not by itself a verdict".

## Done

Report the verdict, the band, the attributed axis if any, and the caveats. Suggest `/eval-report` when the probe set was an eval set and the user wants the comparison rendered. Pop the stack, append `completed`.
