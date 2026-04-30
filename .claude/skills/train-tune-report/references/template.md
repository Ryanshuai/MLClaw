# chain.md template (full worked example)

This is the canonical layout `/train-tune-report` should produce. All sections required.
Empty sections rendered with "—" (don't omit).

---

```markdown
# Train-tune session: 2026-04-28_lr_search

**Best**: trial_15 (val_acc=0.972) | **18 trials** | **22h wall** | **converged**

## Headline

- lr is the dominant axis. Best lr=2.5e-4; differences within [2e-4, 3e-4] are < 0.1%.
- warmup_ratio is a weak signal: 0.03-0.05 are nearly indistinguishable.
- weight_decay shows no signal in the tested range.

## Best-so-far sequence

```
Iter:   0     1     2     3     4     5     6     7     8     9     10    11    12    13    14    15    16    17
Best:  .965  .965  .968  .969  .969  .971  .972  .972  .972  .972  .972  .972  .972  .972  .972  .972  .972  .972
```

## Coverage map

| Axis | Range tested | Trials | Best @ | Coverage |
|---|---|---|---|---|
| lr | log[1e-5, 5e-3] | 8 | 2.5e-4 | ✅ well-covered |
| warmup_ratio | [0, 0.1] | 4 | 0.03 | ⚠️ sparse but signal flat |
| weight_decay | log[1e-6, 1e-2] | 3 | 1e-4 | ⚠️ sparse, no signal |

## Decision timeline

### Iter 0 · [baseline] · trial_0
- **Hypothesis**: Establish baseline. lr=1e-4 (domain default).
- **Diff vs base**: — (no fork)
- **Outcome**: val_acc=0.965

### Iter 1 · [fill_grid] · trial_1
- **Hypothesis**: Try lr=2e-4 to see if a larger lr is better.
- **Diff vs base**: lr 1e-4 → 2e-4
- **Outcome**: val_acc=0.963 (-0.2%) — refuted

### Iter 2 · [fill_grid] · trial_2
- **Hypothesis**: Try lr=5e-5 in the opposite direction; may be more stable.
- **Diff vs base**: lr 1e-4 → 5e-5
- **Outcome**: val_acc=0.968 (+0.3%) — confirmed, smaller lr is more stable.

### Iter 5 · [refine_best] · trial_5
- **Hypothesis**: lr=5e-5 beats baseline. Try [3e-5, 7e-5] to see if it goes lower still.
- **Diff vs base**: lr 5e-5 → 3e-5 (fork_of trial_2)
- **Outcome**: val_acc=0.969 (+0.001) — weak signal, plateau reached.

### Iter 8 · [add_axis] · trial_8
- **Hypothesis**: lr is well explored; best region [2e-4, 3e-4] confirmed. Introduce warmup to see if there's more room.
- **Diff vs base**: warmup 0 → 0.03 (lr fixed at 2.5e-4)
- **Outcome**: val_acc=0.971 (+0.2%) — confirmed, warmup helps.

### Iter 15 · [refine_best] · trial_15  ← BEST
- **Hypothesis**: lr=2.5e-4 + warmup=0.03 is current best. Try ±10% micro-tuning.
- **Diff vs base**: lr 2.5e-4 → 2.6e-4 (fork_of trial_8)
- **Outcome**: val_acc=0.972 — same as 2.5e-4, plateau detected.

(other iters omitted)

## Confirmed

- lr range [2e-4, 3e-4] is the sweet spot; internal differences < 0.1%.
- warmup_ratio=0.03 beats 0 (weak signal).

## Refuted

- lr ≥ 5e-3: numerically unstable; loss diverges around epoch 30.
- warmup_ratio ≥ 0.1: slow early convergence; final val_acc lags.
- weight_decay = 1e-2: val_acc drops 2%.

## Open questions

- weight_decay narrow range [1e-7, 1e-5] not tested.
- batch_size held fixed at 256.
- **Single-seed result** — best config has not been verified across multiple seeds.

## Recipe (locked in by this session)

```yaml
lr: 2.5e-4
lr_scheduler: cosine
warmup_ratio: 0.03
weight_decay: 1e-4
```

**Recommend**: run 3-seed verification on trial_15's config before deploying.
```

---

## Decision-tag conventions

The hypothesis text on each run starts with one of these brackets (set by `/train-tune`):

| Tag | When agent uses it |
|---|---|
| `[baseline]` | iter 0 only — establishing the reference point |
| `[fill_grid]` | adding new value within an existing axis's range to fill gaps |
| `[refine_best]` | densifying around current best, smaller deltas |
| `[add_axis]` | introducing a new axis for the first time |
| `[verify]` | re-running a prior config (often with different seed) for verification |
| `[unlabeled]` | rendered when hypothesis text has no tag prefix (legacy / manual run) |

Render the tag in `### Iter <N> · [<tag>] · <run_id>` heading. If hypothesis text begins
with the tag, strip it from the body so it doesn't appear twice.

## Edge cases

- **No completed runs in session**: render headline + state, then "No completed trials yet"
  in timeline, skip Coverage / Confirmed / Refuted / Recipe sections (replace with —).
- **Single trial session**: same headline format, timeline has one entry, no curve
  (a single bar is meaningless).
- **Best is from a `[verify]` run**: note in headline "(verified, multi-seed N=3)".
- **Outcome field is null** (run completed but agent hasn't filled it): render
  "_outcome not yet annotated_" as a hint to invoke /train-tune to fill it.
- **Mid-session preview** (`--include-running`): mark in-progress trials with
  `(running, current val_acc=X at step Y/Z)`.
