---
name: eval-run
description: >
  Use this skill whenever the user wants to execute a model evaluation run — testing a trained model
  on a dataset to measure metrics like mAP, accuracy, IoU, precision, recall, or per-class AP. Trigger
  for: launching eval runs (debug or production mode), running evaluation on a remote server, checking
  status of a running/crashed eval job, collecting results when eval finishes, forking a previous eval
  run with changed parameters (threshold, NMS, confidence, dataset split), and comparing metrics against
  a baseline. Also trigger for Chinese requests like "跑评估", "测一下", "跑一下eval", "对比baseline".
  This is the execution skill — not for initial config setup (use eval-init) or HTML report generation
  (use eval-report).
---

# /eval-run — Run Evaluation

Execute an evaluation run: resolve sources (including ground truth), run the eval, collect metrics, compare against baseline.

**One question at a time** — asking multiple questions at once is overwhelming. Ask one, record, ask next. **And only what only they know** — a value you can read is not a question, and a value nobody has is recorded absent rather than asked for: CLAUDE.md "Decide what evidence can decide".

**Workflow state, dependency checks, locate project, variable references** — follow `references/skill-graph.md` (state protocol + the requires/suggests table) and `references/layout.md` (Variable Reference Syntax). Stage = `evaluation`, upstream = `/eval-init` (check `config.json -> entry_command` non-empty).

## Fork Check

Ask: "Base on a previous run? (run ID, or skip for fresh run)"

If forking: load the base run's `config_snapshot.json`, `sources.json`, and `lineage.parents` as starting point. Set `lineage.fork_of`. User changes only what they want. Sources from the base run are reused — skip Step 1 unless user wants to change them. If user changes the model artifact, update `lineage.parents` accordingly.

If skip: fresh run, `fork_of = null`.

## Steps 1-3: Shared Run Mechanics

Follow `references/run-mechanics.md` "Run Skill Internal Dependencies" for the shared step flow:

1. **Resolve Assets** (step `resolve_assets`) — fill concrete paths in `artifacts.json`, `input.json` sources, AND `input.json -> ground_truth -> sources`. Ground truth sources are what makes eval different from inference.

   **`/eval-init` Step 1b now fills `candidates`, so choose from them rather than asking for paths** — every `match` value routes somewhere and none may be silently filtered: `references/run-mechanics.md` → "Asset resolution (Step 1 detail)". Two of them are specific to this stage and both are refusals, not questions: a checkpoint cited as `run:training/<run_id>` must belong to a `mode: "production"` run, and a data candidate whose `samples` differs from `config.json -> dataset.num_samples` is measuring something else — that is `mismatch`, and running it produces a real number that is comparable to no baseline. Only fall back to asking path by path when there is no `candidates` block at all (an `input.json` written before Step 1b existed).

   For server matching, connectivity tests, and credential flows, see run-mechanics "Run Skill Internal Dependencies" Step 1. Scripts in `scripts/shared/` (test_connection.py, etc). If any script fails, do the same work manually with Bash.
2. **Create Run** (step `create_run`) — create run dir, initialize run.json, code snapshot, env snapshot, dependency check. Scripts: `create_run.py`, `capture_env.py`, `check_deps.py` (all in `scripts/shared/`). For code source resolution and environment resolution, see CLAUDE.md conventions.
3. **Build & Execute** (step `execute`) — resolve `${}` references, then build the command **per-param from `config.json -> param_injection.items`** (`references/run-mechanics.md` "Launch contract (Step 3 detail)" rule 3), not by guessing from `config_format`. A `runtime_params` key with no entry, or one marked `overridable: false`, is an error — stop and ask. For eval this is the difference between a real threshold sweep and five runs that silently share one threshold. Set `run.json -> mode` and `scope` before launching. Save `config_snapshot.json` and `sources.json` (including GT sources), confirm with user.

### Execution Modes

**Debug mode** (default for first run):
- Limit data — evaluation needs more samples than inference for meaningful metrics. Defaults: images 20, video 30s, text/tabular 50 rows. Use code's own limiting args if available (--num_samples, --max_det, --limit, --subset) — but treat the limiting arg as a param subject to `param_injection`: if the code ignores it, you get a full-scale run mislabeled as debug, or worse, a metric computed over an unknown subset.
- **Record `scope` from what actually happened, not from what you asked for.** After the run, read the real processed count out of stdout / the result file ("Evaluated 20 images", `len(results)`) and write that into `run.json -> scope` (e.g. `{"samples": 20}`), with `mode: "debug"`. If the actual count doesn't match the limit you passed, the limiting arg didn't take effect — say so, fix `param_injection`, and don't let the metric be compared against anything.
- For `single_file` pairing (COCO-style), warn that truncating annotation files is complex — prefer the code's own limiting args. If none exist, suggest adding one.
- Run synchronously, stream output.
- On failure: diagnose, propose fix, ask "Apply and re-run?". On success: show debug metrics with caveat ("on N samples — expect different on full dataset"), ask "production / retry / inspect?"

**Production mode.** ‼️ **First the audit gate**, before asking local or server:

```
python <mlclaw_root>/scripts/data-audit/audit_gate.py check \
    --project {PROJECT} --stage evaluation --mode production
```

Exit 1 is the answer. Route **by state, never generically**: `fatal` → whatever the audit's own suggestion said (a `/data-label` rework · a `/data-curate` conversion · `/data-freeze` for a corrected snapshot — never fix a label in place) · `never_audited` / `unverifiable` → run `/data-audit`, or just the missing layer · `stale` → re-audit the snapshot this run cites · `unresolved` → this run reads a path rather than a frozen snapshot, so no audit can have covered it. `--waive <id>` launches and stamps it into `run.json`.

Then ask local or server:
- **Local**: run in background (`run_in_background`), log to `{RUN_DIR}/logs/`. Return immediately so the user can continue working. They can check back with `/eval-run` again, or `/loop 5m /eval-run` for auto-polling.
- **Remote**: resolve server from resources.json, use `python_path` from server entry. SCP config + run.sh to server, launch in tmux. Return immediately.

For local/remote execution details and path mapping, see `references/run-mechanics.md` "Run Skill Internal Dependencies" Step 3 and "Path Mapping".

### Status Check

When `/eval-run` is invoked and a run has `status: "running"`:
- **Local**: check PID alive. If alive, show log tail + "still running". If dead, proceed to collect results.
- **Remote**: check tmux session. If alive, show log tail. If done, SCP logs back, proceed to collect results.
- Offer "Cancel this run?" (kill PID or tmux session, set status to `cancelled`).

Never block waiting for a long-running command. Return immediately so the user can continue working — they check back by invoking `/eval-run` again.

## Step 4: Collect Results (eval-specific)

After execution finishes:

1. **Finalize run** — update `run.json` status, finished_at, duration_s. Script: `finalize_run.py`. Fallback: manual update.

2. **Check outputs** — look in `{RUN_DIR}/output/` for expected files, search code dir for misplaced outputs.

3. **Collect metrics** — extract from stdout/result files into `run.json -> metrics`. Script: `extract_metrics.py`. Fallback: parse logs manually.

4. **Per-class metrics** — if `output.json -> metrics.per_class` is `true`:
   - Look for per-class metrics in result files (JSON with per-class keys, CSV with class column, stdout tables)
   - Store in `run.json -> metrics.per_class` as `{ "class_name": { "metric": value } }`
   - In summary, show top-3 best and worst performing classes

5. **Baseline comparison** — if `output.json -> metrics.baseline` is set:
   - **Check comparability before computing any delta.** The baseline run and this run must agree on three things: `run.json -> mode`, equivalent `run.json -> scope`, and `config.json -> dataset` (name + split). If any differs, report **not comparable** and name the differing dimension — do not print a delta or a percentage next to it. A mAP difference between a 20-image debug run and a 5000-image production run is arithmetic performed on unrelated quantities; formatting it as `+2.5%` is what turns a mistake into a decision. A baseline with `mode: null` (pre-dating this field) is also not comparable — ask the user to confirm what scale it was run at.
   - Script: `scripts/eval-run/compare_baseline.py {RUN_DIR}/run.json <baseline>`. Baseline can be a run ID (resolved to that run's run.json) or inline JSON. Fallback: manually load both metric sets and compute deltas.
   - Show delta table with improvements highlighted and regressions flagged:
     ```
     Metrics (vs baseline evaluation/run_20260316_153024):
       mAP:       0.485  (+0.012, +2.5%)
       AP50:      0.673  (+0.008, +1.2%)
       mAP_small: 0.289  (-0.003, -1.0%)  <- regression
     ```

6. **Alias** — ask user for optional alias/description; write into `run.json -> alias` / `description`. No separate index file to update — `run.json` files are the source of truth, queried on demand via `shared/list_runs.py` (see `references/run-mechanics.md` "Listing runs (no separate index)").

7. **Offer baseline update** — "Set this run as the new baseline?" **Only offer this for `mode: "production"` runs at full `scope`.** A debug run must never become the baseline: every future comparison would silently inherit the error, and the person reading those diffs months later has no way to see why the numbers look off. If the current run is debug, skip this step and say why. If yes, update `output.json -> metrics.baseline` to this run's ID.

8. **Show summary**:
   ```
   Run: run_20260317_091500 | Status: completed | Duration: 12m 34s
   Dataset: COCO val2017 (5000 images)
   Metrics:
     mAP: 0.485
     AP50: 0.673
     AP75: 0.521
   Per-class: best [person 0.72, car 0.68, dog 0.65] worst [toothbrush 0.12, hair_dryer 0.15, parking_meter 0.18]
   Outputs: results.json (45KB), confusion_matrix.png (120KB)
   ```

9. **Downstream suggestion** — offer `/eval-report` (per `references/skill-graph.md` -> "Skill Dependency Graph"). If user accepts, invoke as sub-skill following Workflow State Protocol.

10. Pop from workflow stack, append `completed` to history.

## Quick Mode

When user provides paths inline (e.g., "evaluate model.pt on COCO val with annotations instances_val2017.json"):
1. Match paths to declared items by type/extension (including ground truth)
2. Fill sources in artifacts.json / input.json / ground_truth
3. Skip source-filling dialogue, proceed directly to execution
