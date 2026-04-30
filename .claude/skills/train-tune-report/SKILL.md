---
name: train-tune-report
description: >
  Use this skill to render a train-tune session as a human-readable markdown chain
  (chain.md). Reads all runs belonging to the session, extracts hypothesis / outcome /
  config diff per run, and assembles a structured narrative: headline, best-so-far
  curve, coverage map, decision timeline, confirmed/refuted distillation, open
  questions, and final recipe. Triggers when: /train-tune session closes (auto-invoked),
  user asks to re-render an existing session report, user asks for mid-session preview.
  Use for: "render the lr search report", "show me the chain.md for last train-tune
  session", "渲染 tune 报告", "总结一下这次搜索". This is a pure rendering skill — does not
  modify runs, does not run training.
---

# /train-tune-report — Render Tune Session Chain

Reads runs from a `train-tune` session and writes a markdown narrative summarizing
the search. The report is the **most valuable human artifact** of train-tune — agents
do reasoning, but humans read this to understand what happened.

**Pure rendering** — never mutates run.json or any source data. Reads only.

## Usage

| Invocation | Effect |
|---|---|
| `/train-tune-report` | render most recent tune session in this project's training stage |
| `/train-tune-report --session <id>` | render a specific session |
| `/train-tune-report --include-running` | include in-progress trials with current metrics (mid-session preview) |
| `/train-tune-report --output <path>` | override output path (default: `<session_dir>/chain.md`) |

## On entry

Follow CLAUDE.md Workflow State Protocol. Stage = `training`. Upstream check:
at least one run with `lineage.session = <id>` must exist.

## Step 1: Resolve Session

If `--session <id>` provided: validate `stages/training/tune_sessions/<id>/` exists.
Otherwise: find the most recent session by `started_at` in `tune_sessions/*/state.json`.

If no session exists at all: tell the user, exit. (Don't create one — that's `/train-tune`'s job.)

## Step 2: Collect Runs

Scan `stages/training/runs/*/run.json`, keep those with `lineage.session == <session_id>`.

Sort by `created_at` ascending. This gives the iteration timeline.

If `--include-running` not set: drop runs with `status != "completed"`.

If `--include-running` set: include them; mark explicitly in timeline ("running, current step ...").

## Step 3: Extract Per-Run Data

For each run, read:
- `run_id`, `created_at`, `status`
- `lineage.fork_of`, `lineage.variation_summary`
- `hypothesis` (free-form text, may include `[fill_grid|refine_best|add_axis|baseline]` decision tag prefix)
- `outcome` (free-form text written at completion)
- `metrics.<primary_metric>` (final value)
- `runtime_params` from `config_snapshot.json` (the actual values used)

If `hypothesis` is null and there's no decision tag: render as `[unlabeled]`.

## Step 4: Compute Sections

### 4a. Headline

```
**Best**: trial_<id> (<primary_metric>=<value>) | <N> trials | <wall_h>h wall | <status>
```

Status from `state.json`: running | converged | budget_exhausted | stopped | **no_signal**.

**`no_signal` headline override** — when `state.status == "no_signal"`, replace the entire headline with:

```
**⚠️ NO SIGNAL** — `<primary_metric>` = <value> across all <N> trials. The search produced no information to rank trials. Likely causes: training too short / metric saturated / NaN crash / wrong metric choice. Do not take any trial as production. | <N> trials | <wall_h>h wall
```

Use `state.no_signal_value` for `<value>` if present; else read from any trial. **Do not pick a "best" trial.** The "Best:" prefix and BEST highlight in the timeline must not appear at all in this mode — they would falsely imply ranking.

### 4b. Best-so-far sequence

For each iter, compute the best primary_metric value seen up to that iter. Render as a one-line numeric sequence:

```
Iter:   0     1     2     3     4     5     6     ...
Best:  .965  .965  .968  .969  .969  .971  .972
```

The information is in the numbers; readers can see plateaus and jumps without ASCII bars. (Earlier versions rendered an 8-level Unicode block bar — dropped because it added zero information beyond the numeric line and degenerated to a single character when the metric stayed flat.)

### 4c. Coverage map

For each axis observed in `variation_summary` across all runs:

| Axis | Range tested | Trials | Best @ | Coverage status |

Coverage status heuristic:
- `well-covered`: ≥ 5 trials, span ≥ 50% of search prior range, signal stable
- `sparse`: < 5 trials OR span < 30% of prior range
- `single-point`: 1 trial only
- `untested`: declared in research_goals priors but no trials

### 4d. Decision timeline

Each iter = one section:

```markdown
### Iter <N> · [<decision_tag>] · <run_id>
- **Hypothesis**: <text from run.json hypothesis, decision tag stripped>
- **Diff vs base**: <variation_summary>
- **Outcome**: <text from run.json outcome>
```

Decision tag extracted from hypothesis prefix (`[fill_grid] ...`) — see train-tune SKILL.md
for the four canonical tags. If no tag found, render as `[unlabeled]`.

### 4e. Confirmed / Refuted

Scan all `outcome` strings. Group by sentiment heuristic (look for keywords like
"confirmed", "refuted", "no signal", "validated"). Distill into bullet lists.

For ambiguous outcomes: agent re-reads run + outcome and judges, writing a one-line
summary in either Confirmed or Refuted bucket.

### 4f. Open questions

From the agent's final state in decisions (last few iters' analysis): list axes / regions
that haven't been fully explored. If user supplied `research_goals.md` with `search_priors`,
compare coverage map against priors and call out untested portions.

**Single-seed-best detection** (fires regardless of `state.status`, **except** when status is `no_signal` — there's no best to verify in that case):

```python
if state.status != "no_signal" and state.best_run is not None:
    best_params = state.best_run.runtime_params
    seeds_at_best = {
        t.runtime_params.seed for t in trials
        if {k: v for k, v in t.runtime_params.items() if k != "seed"}
        == {k: v for k, v in best_params.items() if k != "seed"}
    }
    if len(seeds_at_best) == 1:
        open_questions.append(
            f"**Single seed ({next(iter(seeds_at_best))})** — best config "
            f"({state.best_run}) was only run with one seed. The reported winner "
            f"may be a lucky outlier. Recommend re-running the same config with "
            f"2+ different seeds and confirming the metric stays within noise "
            f"before deployment."
        )
```

This is the canonical place for the warning — it fires for every status that produces a `best_run` (`converged`, `budget_exhausted`, manual stop, ...). `/train-tune` does not need to launch `[verify]` trials before stopping; the reminder in the report is enough to prompt a follow-up session if the user wants verification.

### 4g. Recipe

Final config dict from current best run's `runtime_params`, formatted as YAML.

Plus a one-line **Recommend** action: typically "run 3-seed verification" or "test on
held-out set".

**No-signal mode**: replace the recipe section entirely with a diagnostic block — there is no recipe to recommend, and printing one would imply confidence that doesn't exist.

```
## What to do next

The search did not produce comparable signal. Before re-running the search:

1. Verify training is actually learning at this scale — pick the strongest config from `runtime_params` (likely `[baseline]`) and run **once** with ≥3x the trials' epochs / training data. If the primary metric is still `<value>`, the bug is upstream of `/train-tune` (eval / data / metric definition).
2. Check the metric is sensible: `<primary_metric>` should be a continuous quantity that varies smoothly with model quality. If it's a strict-match accuracy and the model is far from solving any sample, expect 0.0 across reasonable hparams.
3. If you intended to stress-test the skill (not actually search), that's fine — just don't take any trial's `runtime_params` as production.
```

Skip 4f's "single-seed reminder" in `no_signal` mode — there is no best run to verify.

## Step 5: Write chain.md

Default output: `stages/training/tune_sessions/<session_id>/chain.md`. Override with
`--output`.

Print to stdout: report path + headline (so user can see at a glance even without
opening the file).

## Step 6: (Conditional) Re-invoke logic

If invoked manually mid-session and user has questions about specific iters: read those
iter's run.json + train_log.jsonl directly, surface the relevant snippet. Don't dump
entire jsonls.

## Format template (full example)

See `references/template.md` for a full worked example showing all sections and
decision-tag conventions.

## Notes

- **Pure rendering, idempotent**: re-running on the same session produces identical
  output (modulo newer trial completions if --include-running).
- **No state mutation**: never writes to run.json, state.json, or any non-output file.
- **Cheap**: ~5s render even for 30+ trial sessions.
- **Iteration-friendly**: format can be improved without touching `/train-tune` —
  just edit this skill.
