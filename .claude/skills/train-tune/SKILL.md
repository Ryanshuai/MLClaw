---
name: train-tune
description: >
  Use this skill to run adaptive hyperparameter optimization on a fixed model + dataset.
  Triggers when user wants to find best hyperparameters via an agent-driven search loop:
  the agent reads prior runs, identifies coverage gaps, hypothesizes the next config,
  launches trials, observes outcomes, and iterates until budget exhausted or coverage
  sufficient. Trigger for: "tune lr / hyperparams", "find best config", "search hyperparams",
  "调超参", "tune 一下", "搜个 lr". This is the HPO loop skill — not for architecture search
  (that's /explore-*) or single-trial training (that's /train-run). Auto-invokes
  /train-tune-report at session close.
---

# /train-tune — Adaptive HPO Loop

Run an autonomous adaptive hyperparameter search. The agent itself decides what to vary
and when to stop. User starts with one command and walks away; comes back to a
chain.md report.

**Coverage-driven, not exploit-driven**: each iter the agent asks "where is evidence
weakest?" — fill gaps in axis ranges, refine around current best, or add a new axis —
not just "what's near current best". This avoids local optima trap.

**Train-stage contract assumed**: same code SHA + same dataset + same split. Variation
is in `runtime_params` only (lr / bs / warmup / etc.). For architecture search use
`/explore-*` (not yet implemented).

## Re-entry behavior

When invoked again, route by `--session <id>` arg (or auto-detect latest):

| State | Action |
|---|---|
| no session arg + no recent open session | Start new session |
| recent session in `running` status | Continue that session's loop |
| session in `done` / `converged` / `budget_exhausted` | Re-render report, do not relaunch |
| explicit `--session <id>` of a closed session | Same as above |

## On entry

Follow CLAUDE.md Workflow State Protocol. Stage = `training`. Upstream:
- `/train-init` done (`stages/training/config.json -> entry_command` non-empty)
- At least one prior `/train-run` completed (so we have a baseline configuration to fork from). If none, suggest user run a baseline first.

## Step 1: Resolve / Initialize Session

Each train-tune invocation operates on a **session** — a lightweight grouping with its
own directory and state.

```
stages/training/
  tune_sessions/
    <session_id>/
      state.json       ← session metadata (budget, status, started_at, ...)
      chain.md         ← report (rendered by /train-tune-report at close)
  runs/
    <run_id>/
      run.json         ← lineage.session = "<session_id>" links it to the session
```

**New session**: generate `session_id = <YYYYMMDD>_<HHMMSS>_<short_slug>` (slug from
optional `--name` arg or "tune"). Create dir + initial `state.json`:

```json
{
  "session_id": "...",
  "started_at": "...",
  "ended_at": null,
  "status": "running",
  "budget": { "max_trials": 20, "max_wall_hours": null, "max_concurrent": 1 },
  "best_run": null,
  "best_metric": null,
  "iteration": 0
}
```

**Continuing session**: load existing state.json, increment iteration, resume loop.

## Step 2: Read Project Guidance (Optional)

Look for `stages/training/research_goals.md`. If present, parse:

| Section | Effect |
|---|---|
| `fixed:` | Hard constraints — agent never modifies these `runtime_params` keys |
| `avoid:` | Soft constraints — agent should not propose these unless prior data refutes the avoidance reason |
| `search_priors:` | Per-axis range / scale priors (e.g., `lr: {scale: log, range: [1e-5, 5e-3]}`) — agent stays within these unless user override |
| `mode:` | `full` (default, full-epoch trials) or `screen_then_refine` (short trials first) |

If absent, agent uses domain-default priors (lr log-uniform 1e-5..1e-2, etc.) and no
hard constraints. Print a one-line note: "no research_goals.md, using domain defaults".

## Step 3: Read Prior Comparable Runs

Filter `stages/training/runs/*/run.json` by **train contract**:

```python
comparable = [
  r for r in all_runs
  if r.code.origin_commit  == query.code.origin_commit
  and r.cfg.data.dataset_id == query.cfg.data.dataset_id
  and r.cfg.data.split_seed == query.cfg.data.split_seed
]
```

Filter inline with Bash + jq, e.g.:
```bash
jq -r 'select(.code.origin_commit=="<sha>" and .lineage.session==null) | .run_id' \
   stages/training/runs/*/run.json
```

For each comparable run extract: `runtime_params`, `hypothesis`, `outcome`, primary
metric value, `lineage.fork_of`, `lineage.variation_summary`. **All session sources
allowed** — diff disentangles (see "Why no per-session isolation" in CLAUDE.md).

## Step 4: Adaptive Loop

```
loop:
  1. CHECK STOP CONDITIONS (Step 5)
     - if stopped → break to Step 6
  2. OBSERVATION
     - re-read all comparable runs (some may have completed since last iter)
     - extract per-axis: tested_values, density, current_best
     - identify gaps, plateaus, untested axes
  3. HYPOTHESIS
     - choose decision tag (see "Decision tags" below)
     - choose base run to fork from
     - choose runtime_params overrides (within fixed / avoid / priors)
     - write hypothesis text starting with [<tag>]
  4. LAUNCH
     - invoke /train-run as sub-skill with:
       - fork_of = <base run_id>
       - hypothesis = "<text>" (will be written to new run.json)
       - runtime_params overrides
     - new run inherits lineage.session = <self_session_id>
     - if max_concurrent > 1: launch up to that many in parallel (sync_batch)
  5. WAIT
     - block until all launched trials in this iter complete
     - read each trial's outcome (agent fills run.json.outcome based on metric vs hypothesis)
  6. UPDATE state.json
     - increment iteration
     - update best_run, best_metric if any trial beat them
  7. goto 1
```

### Decision tags

Every hypothesis begins with one bracketed tag chosen from:

| Tag | When |
|---|---|
| `[baseline]` | iter 0 only — establishing reference. Use the project's known good config or first reasonable default. |
| `[fill_grid]` | An axis has tested values with gaps in between. Fill gap to identify shape. |
| `[refine_best]` | Best is plateau / unstable. Densify around best with smaller deltas. |
| `[add_axis]` | Current axes are well-explored; introduce a new axis to vary. |
| `[verify]` | Re-run a prior config (typically with different seed) to assess noise. |

Agent picks based on observation:
- gap detected on axis X (large step between tested values) → `fill_grid` on X
- best metric stable across 3+ trials → `refine_best` to confirm or push for marginal gain
- best stable AND no obvious gaps → `add_axis` (new axis: warmup if not tried, etc.)
- best is single-seed and budget allows → `verify` (optional; the report will warn anyway, so launch only when budget permits and you genuinely want noise estimates)

## Step 5: Stop Conditions

Three paths to stop:

| Stop reason | Condition |
|---|---|
| `budget_exhausted` | iteration ≥ `budget.max_trials` OR wall-time ≥ `budget.max_wall_hours` |
| `converged` | Agent self-judges based on observation: best stable for last 3-5 iters AND coverage map shows no obvious gaps AND last few hypotheses' alternatives are all "minor variations". |
| `no_signal` | All completed trials produced **the exact same** `primary_metric` value (typical causes: training too short / metric saturated at 0 or NaN / broken eval / wrong metric choice). Agent does not pick a winner — see "No-signal handling" below. |

Convergence is **agent's call** — write the rationale into the final hypothesis or
chain.md, e.g., "stopping: best stable 4 iters, all axes covered ≥5 trials, marginal
gain expected < 0.1%."

Update state.json `status` accordingly.

### No-signal handling (Hard Rule)

At session close, before writing `state.best_run` / `state.best_metric`:

```python
values = {t.metrics["best"]["primary_metric_value"] for t in completed_trials}
if len(values) == 1:
    state["status"] = "no_signal"
    state["best_run"] = None
    state["best_metric"] = None
    state["no_signal_value"] = next(iter(values))   # the shared value, for the report
```

Strict equality, no epsilon — primary metric is a real number that won't accidentally collide across trials when there's actual signal. This catches the cases that matter: 0.0 (model never solved any sample), NaN (loss exploded), or a metric the user accidentally rounded to int.

Do not invent a tiebreaker. Do not silently pick `trials[0]`. The whole point is to surface "the search produced no information" as a user-visible result, not to pretend there is a winner. `/train-tune-report` renders a loud warning — see that skill's "No-signal report" section.

`no_signal` overrides both `budget_exhausted` and `converged`: if the values check trips, status is `no_signal` regardless of how the loop ended.

## Step 6: Render Report

Auto-invoke `/train-tune-report --session <session_id>` as sub-skill. It renders
`<session_dir>/chain.md` from runs + outcomes.

Print to stdout: report path + headline ("Best: trial_X val_acc=0.972; 18 trials, 22h").

## Hard Rules

These are non-negotiable behaviors `/train-tune` must enforce, regardless of agent's
own preferences:

1. **Read all comparable runs (including refuted)** every iter. Don't shortcut by
   only looking at "best surroundings" — refuted directions are valuable signal.
2. **Single-axis priority**. When multiple axes have low coverage, prefer to fully
   explore one before opening another. Multi-axis simultaneous variation only when
   user explicitly requests interaction study, or as `[refine_best]` micro-step.
3. **Hypothesis must include confidence**. End hypothesis with `(confidence: low|medium|high based on N comparable trials)`. This forces honest uncertainty estimates.

(Single-seed-best detection lives in `/train-tune-report`'s Open Questions section — it's a reporting concern that fires regardless of session status, not a stop-condition for the search loop. The agent can still proactively launch `[verify]` re-runs as a `refine_best` decision if budget allows, but it's no longer mandatory before stopping.)

## Failure Modes

| Symptom | Diagnosis | Action |
|---|---|---|
| First trial crashes (e.g., OOM at chosen lr) | Hypothesis exceeded hardware | Reduce batch_size / try smaller config; add to `avoid` list mentally for this session |
| Many trials in a row refuted | Search direction wrong | Switch decision tag (`fill_grid` → `add_axis`); agent should explicitly note "no progress on X axis, switching to Y" |
| Trial hangs > expected_duration × 2 | Probably hung / data issue | Cancel, mark trial as failed with note; continue with reduced sample of comparable runs |
| Wall-time approaching budget | Pre-emptive stop | Don't launch new trials whose ETA exceeds remaining budget; render partial report |

## Quick mode

When user provides axes inline (e.g., "tune lr from 1e-5 to 1e-3, 10 trials"):
1. Set `--max_trials 10` and parse override priors
2. Skip the agent's "what to search" phase
3. Go straight to `[fill_grid]` on the specified axis

## Notes for implementers

- **Session is just a directory + state.json**, no separate experiment.json
- **Per-session isolation is intentional**: prevents one session's reasoning trace from
  bleeding into another's, even when their underlying runs are diff-comparable
- **Auto-detect "latest session"** when re-invoked without --session: read all
  `tune_sessions/*/state.json`, pick max `started_at`
- `/train-tune` must NOT modify any prior session's state.json — only its own
- `chain.md` is **owned by /train-tune-report**, not written by /train-tune directly
