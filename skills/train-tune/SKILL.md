---
name: train-tune
description: >
  Use this skill to run adaptive hyperparameter optimization on a model that is ALREADY
  SETTLED — the architecture, the code, the dataset and the split are no longer in
  question, and what is wanted is that model's best operating point. Triggers when user
  wants to find best hyperparameters via an agent-driven search loop: the agent reads
  prior runs, identifies coverage gaps, hypothesizes the next config, launches trials,
  observes outcomes, and iterates until budget exhausted or coverage sufficient. Trigger
  for: "tune lr / hyperparams", "find best config", "search hyperparams", "调超参",
  "tune 一下", "搜个 lr", "模型定了，调一下参", "在这个模型上调到最好". This is the HPO
  loop skill, and its unit is one point in runtime_params. NOT for deciding what the model
  should BE — architecture, components, network selection, or a parameter change that is
  itself the hypothesis ("是不是容量不够", "这个模块没用是不是 lr 太保守") — all of that is
  /explore, and it comes first. Not for single-trial training (that's /train-run).
  Auto-invokes /train-tune-report at session close.
---

# /train-tune — Adaptive HPO Loop

Run an autonomous adaptive hyperparameter search **on a model that is already settled**.
The agent itself decides what to vary and when to stop. User starts with one command and
walks away; comes back to a chain.md report.

**Coverage-driven, not exploit-driven**: each iter the agent asks "where is evidence
weakest?" — fill gaps in axis ranges, refine around current best, or add a new axis —
not just "what's near current best". This avoids local optima trap.

**Train-stage contract assumed**: same code SHA + same dataset + same split. Variation
is in `runtime_params` only (lr / bs / warmup / etc.). That contract is not paperwork —
it is what makes the trials **one series** instead of a pile of unrelated numbers, and it
is also the precondition this skill cannot check for itself.

## What this skill is not: `/explore`

**`/train-tune` answers 「这个模型怎么配」. `/explore` answers 「这个模型该是什么」** —
structure, components, and network selection, down to whether this is the right family of
model at all. Same layer, different unit and different precondition: `/explore`'s unit is a
**proposal** (a hypothesis, a pre-registered criterion, a guardrail, a kill condition), and
it runs while the architecture is still in question, which is exactly when this skill must
not.

‼️ **The test is not "parameters vs code."** Ask: *after this change, are the earlier runs
still answers to the same question?* Yes → here. No → `/explore`, because the question
changed and the criterion and noise floor have to be re-established first. And whether a
knob is exposed on the command line decides nothing — `--num-layers` and `--width` are
flags that change what the network *is*; `--lr` and `--batch-size` are flags that do not.
A capacity sweep driven entirely by existing flags is `/explore`'s work in this skill's
clothes.

**Running a tune session on an unsettled architecture is the expensive mistake**, and it is
silent: the config comes out fine, and then dies with the component it was tuned around
without anything in the record saying so. Order is `/explore` first, `/train-tune` after.

**`/explore` searches parameters too**, and that is not a boundary violation — one of its
three shapes calls *this* skill: a **scoped** tune inside one arm, so a ported component is
judged at a fair operating point rather than at the paper's. When invoked that way, the
result belongs to that card, not to the model — it does not become the project's config —
and the control arm gets the same budget. The full three shapes and the dividing test:
`references/skill-graph.md` -> "`/train-tune` vs `/explore`".

## Re-entry behavior

When invoked again, route by `--session <id>` arg (or auto-detect latest):

| State | Action |
|---|---|
| no session arg + no recent open session | Start new session |
| recent session in `running` status | Continue that session's loop |
| session in `done` / `converged` / `budget_exhausted` | Re-render report, do not relaunch |
| explicit `--session <id>` of a closed session | Same as above |

## On entry

Follow `references/skill-graph.md` -> "Workflow State Protocol". Stage = `training`. Upstream:
- `/train-init` done (`stages/training/config.json -> entry_command` non-empty)
- At least one prior `/train-run` completed (so we have a baseline configuration to fork from). If none, suggest user run a baseline first.
- **The architecture is settled.** Nothing declares this, but one thing can contradict it: if `stages/exploration/graph.json` exists, read it —

  ```bash
  python <mlclaw_root>/scripts/explore/graph.py status --project <PROJECT>
  ```

  Any card in `draft` / `blocked` / `ready` / `running` / `filled` means the model is still in question. **This is a warning, not a gate** — the user asked for a tune and may well want one anyway. Name the open cards, say in one line that a config tuned around a component under an open card is **provisional**, note it in the session's `state.json` and in `chain.md`'s recipe, and carry on. What is not acceptable is running the session as if nothing were open: the config comes out looking final, and it dies silently with whatever the open card removes.

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

## Step 1b: Where the trials run

Skip this entirely when `max_concurrent == 1` and the local machine can hold the job —
one box needs no fleet, and `/train-run` already reaches it. Read
`references/fleet.md` before doing anything here; it is the authority for
everything below and states what each rule costs when broken.

`scripts/shared/pool.py` holds the machines; it is provider-blind, so an owned
4090 and a rented H100 arrive as the same kind of slot.

```bash
P=scripts/shared/pool.py
python $P plan  --slots 4 --gpu-count 1 --gpu-memory-gb 40 --hours 8 [--allow-preemptible]
python $P open  --session {SESSION_DIR} --slots 4 --gpu-count 1 --gpu-memory-gb 40 \
                --hours 8 --confirmed-usd-per-hr <the figure the user agreed to>
```

**`plan` first, always, and show its output.** It fills owned hardware before anything
that bills, and it states the fleet's cost as **one number for the whole search** —
`N slots × $/hr × estimated hours` — which is the only moment anybody sees it. Four
separate per-box confirmations is how a sweep gets approved at four times the figure
anyone was holding in their head. `open` refuses outright when a billing pool has no
confirmed number, so this is not a step that can be skipped by accident.

Three things in `plan`'s output change what you tell the user, and none is a detail:

| Field | What it means | Say it |
|---|---|---|
| `price_confidence: claim` | the price came from a hand-maintained table, not a billing API | quote the total as an estimate off a note, never as a fact |
| `cost_is_complete: false` | some machine types have no price at all | the total is a **lower bound** |
| `plan_trustworthy: false` | capacity was read incompletely — a region or account did not answer | the plan may be short for no real reason; re-run before concluding "no capacity" |

**`--allow-preemptible` is usually right for a search and wrong for a final run.** Trials
are short, independent and checkpointed, so losing one costs its elapsed minutes;
interruptible capacity is much cheaper and — the part that surprises — frequently has
stock at an hour when on-demand has none, so refusing it is often the difference between
a search that runs and one that does not. It carries **one obligation**: checkpoints must
come back on the monitor cadence, not at finalize. A preemption that takes the box's disk
with it costs the whole trial, and the economics that justified the choice are gone.

`short_by > 0` is not a failure. Run the search at lower concurrency and say so; a
4-slot plan that filled 3 is a working search.

## Step 2: Read Project Guidance (Optional)

Look for `stages/training/research_goals.md`. If present, parse:

| Section | Effect |
|---|---|
| `fixed:` | Hard constraints — agent never modifies these `runtime_params` keys |
| `avoid:` | Soft constraints — agent should not propose these unless prior data refutes the avoidance reason |
| `search_priors:` | Per-axis range / scale priors (e.g., `lr: {scale: log, range: [1e-5, 5e-3]}`) — agent stays within these unless user override |
| `mode:` | `full` (default, full-epoch trials) or `screen_then_refine` (short trials first). This is the **search strategy** — distinct from `run.json -> mode` (`debug` / `screen` / `production`), which records one run's scale. |

If absent, agent uses domain-default priors (lr log-uniform 1e-5..1e-2, etc.) and no
hard constraints. Print a one-line note: "no research_goals.md, using domain defaults".

**Searchable axes are bounded by `param_injection`, not by this file.** Before choosing any axis, read `config.json -> param_injection.items`. A param marked `overridable: false` cannot be changed from outside the code, so it is **implicitly fixed** no matter what `research_goals.md` says or the user asks for — this constraint outranks user config. A user asking to "tune seed" when `seed` is hardcoded at `train.py:12` gets told to edit that line, not handed N trials.

**`screen_then_refine` crosses scales — carry direction, not magnitude.** Launch screening trials with `run.json -> mode: "screen"` (and their real `scope`); full trials get `production`. The comparability filter in Step 3 then keeps the two populations apart, which is correct: a 5-epoch metric and a 100-epoch metric are different quantities that share a name.

What legitimately crosses the boundary is **which region is worth trying**. "Screening says the useful lr region is near 1e-4, refine there" is sound. "Screening's best was 0.71, so this full trial at 0.68 is a regression" is not — that compares across scales. Never put a screen number and a production number in one ranking, one curve, or one best-so-far claim.

Params **absent** from `param_injection` are equally off-limits until `/train-init` classifies them. This is the exact failure this guards against: the sweep launches, every trial returns the same number, and the session concludes "this hyperparameter doesn't matter" — a wrong result that looks like a finding. If an axis you want isn't classified, stop and run `/train-init` Step 2b on it rather than assuming a flag works.

## Step 3: Read Prior Comparable Runs

Filter `stages/training/runs/*/run.json` by **train contract**:

```python
comparable = [
  r for r in all_runs
  if r.code.origin_commit  == query.code.origin_commit
  and r.cfg.data.dataset_id == query.cfg.data.dataset_id
  and r.cfg.data.split_seed == query.cfg.data.split_seed
  and r.mode  == query.mode          # debug / screen / production never mix
  and r.scope == query.scope         # same epochs + data scale
]
```

Do not hand-write this filter. The condition above is four clauses long and
forgetting the fourth is invisible — type all four flags, every time:

```bash
python <mlclaw_root>/scripts/shared/list_runs.py <project_root> --stage training --mode production --commit <sha> --no-session
```

Read `comparable` in the result before ranking anything. False means the matched
runs span more than one `scope` (or none recorded one), so their metrics are not
a series — `distinct_scopes` says which groups you actually have.

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
     - choose runtime_params overrides (within fixed / avoid / priors,
       and only over axes with param_injection.overridable = true)
     - write hypothesis text starting with [<tag>]
  4. LAUNCH
     - if a fleet is open: pool.py heartbeat --session <dir>     ← FIRST, before launching
     - for each trial this iter:
         - if a fleet is open: pool.py acquire --session <dir> --run <run_id>
                               → slot + reach; pass reach to /train-run as the target host
         - invoke /train-run as sub-skill with:
             - fork_of = <base run_id>
             - hypothesis = "<text>" (will be written to new run.json)
             - runtime_params overrides
         - new run inherits lineage.session = <self_session_id>
     - if max_concurrent > 1: launch up to that many in parallel (sync_batch)
  5. WAIT
     - block until all launched trials in this iter complete
     - if a fleet is open: pool.py status --session <dir> --probe
       → a slot that no longer answers took its trial down with it
     - for each finished trial:
         - pool.py release --session <dir> --slot <slot> --outcome ok|preempted|crashed
         - outcome `ok`        → read the trial's outcome as evidence, as below
           outcome `preempted` → NOT EVIDENCE. Re-queue the same hypothesis; do not
                                 record it as refuted (see Hard Rule 4)
     - read each trial's outcome (agent fills run.json.outcome based on metric vs hypothesis)
  6. UPDATE state.json
     - increment iteration
     - update best_run, best_metric if any trial beat them
  7. goto 1
```

**The heartbeat is step 4's first line and not an optional flourish.** A fleet's TTLs are
sized in hours and a search runs overnight; the loop is the only thing that knows the
search is still alive, so a missed heartbeat kills every trial in flight. It is one call
for the whole pool precisely so it cannot be half-forgotten
(`references/fleet.md` "Whose dead-man switch, and what renews it").

**A trial ending does not release its machine.** `pool.py release` returns the *slot*, not
the lease — the next trial wants that box, already provisioned and already staged. The
lease goes away at Step 6, once, for the whole fleet.

### Decision tags

Every hypothesis begins with one bracketed tag chosen from:

| Tag | When |
|---|---|
| `[baseline]` | iter 0 only — establishing reference. Use the project's known good config or first reasonable default. |
| `[fill_grid]` | An axis has tested values with gaps in between. Fill gap to identify shape. |
| `[refine_best]` | Best is plateau / unstable. Densify around best with smaller deltas. |
| `[add_axis]` | Current axes are well-explored; introduce a new axis to vary. Confirm the candidate is `overridable: true` in `param_injection` before committing trials to it — this is where an unsearchable axis is most likely to slip in. |
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

## Step 6: Close the fleet, then render

**Release before rendering, and release on every exit path** — `converged`,
`budget_exhausted`, `no_signal`, a crash, or the user stopping the search. A report is
worth minutes; a held fleet bills until something destroys it.

```bash
python <mlclaw_root>/scripts/shared/pool.py close --session {SESSION_DIR}
```

`close` verifies each teardown and **exits non-zero with the leases left open** when one
does not confirm as gone. That is deliberate: a lease row closed over a surviving box is
how a machine becomes invisible. On that exit, say so plainly and run `lease.py status`
— do not proceed to the report as though the search ended cleanly.

If `pool.json` is missing (the session that opened the fleet died and its directory is
gone), `close` cannot help and `lease.py reap` is the backstop — it works from the
provider side with no local state at all.

Then auto-invoke `/train-tune-report --session <session_id>` as sub-skill. It renders
`<session_dir>/chain.md` from runs + outcomes.

Print to stdout: report path + headline ("Best: trial_X val_acc=0.972; 18 trials, 22h"),
and the fleet's accrued cost if there was one.

## Hard Rules

These are non-negotiable behaviors `/train-tune` must enforce, regardless of agent's
own preferences:

1. **Read all comparable runs (including refuted)** every iter. Don't shortcut by
   only looking at "best surroundings" — refuted directions are valuable signal.
2. **Single-axis priority**. When multiple axes have low coverage, prefer to fully
   explore one before opening another. Multi-axis simultaneous variation only when
   user explicitly requests interaction study, or as `[refine_best]` micro-step.
3. **Hypothesis must include confidence**. End hypothesis with `(confidence: low|medium|high based on N comparable trials)`. This forces honest uncertainty estimates.
4. **An infrastructure outcome is never evidence about a hypothesis.** A trial ended by
   preemption, a TTL expiry, a reaped box or a host that vanished is re-queued and re-run
   — never recorded as a refuted hypothesis, and never counted toward coverage.

   This is the one rule here that breaks **silently**, which is why it is a rule rather
   than a note. A trial cut off at epoch 3 produces a truncated curve and a bad final
   metric; land that in the session as an ordinary result and the search concludes the
   *configuration* was bad, steers away from a region of the space for a reason that has
   nothing to do with the model, and nothing downstream can ever recover the mistake —
   the poisoned belief is indistinguishable from a real one. `pool.py release --outcome
   preempted` is what keeps them apart, and its `trial_counts_as_evidence: false` is the
   field to read. `scope` on the run records what it actually completed, so the existing
   comparability rules refuse it too (`references/run-mechanics.md` "Record
   integrity").

(Single-seed-best detection lives in `/train-tune-report`'s Open Questions section — it's a reporting concern that fires regardless of session status, not a stop-condition for the search loop. The agent can still proactively launch `[verify]` re-runs as a `refine_best` decision if budget allows, but it's no longer mandatory before stopping.)

## Failure Modes

| Symptom | Diagnosis | Action |
|---|---|---|
| First trial crashes (e.g., OOM at chosen lr) | Hypothesis exceeded hardware | Reduce batch_size / try smaller config; add to `avoid` list mentally for this session |
| Many trials in a row refuted | Search direction wrong | Switch decision tag (`fill_grid` → `add_axis`); agent should explicitly note "no progress on X axis, switching to Y" |
| Trial hangs > expected_duration × 2 | Probably hung / data issue | Cancel, mark trial as failed with note; continue with reduced sample of comparable runs |
| Wall-time approaching budget | Pre-emptive stop | Don't launch new trials whose ETA exceeds remaining budget; render partial report |
| Trial dies with no crash signature; its host stops answering | Slot preempted or reaped | `pool.py status --probe` to confirm, `release --outcome preempted`, re-queue the same hypothesis. **Not a refutation** — Hard Rule 4 |
| Every slot goes unreachable at once | The fleet's TTL expired — a heartbeat was missed | Nothing is recoverable in flight. `pool.py close`, re-open, and put `heartbeat` back as the first line of the loop |
| `open` fills fewer slots than asked | Capacity, not a bug | Run at lower concurrency and say so. Check `plan.plan_trustworthy` first — an incompletely-read capacity table understates what is there |

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
