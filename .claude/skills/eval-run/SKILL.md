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

**One question at a time** — asking multiple questions at once is overwhelming. Ask one, record, ask next.

**Workflow state, dependency checks, locate project, variable references** — follow CLAUDE.md (Workflow State Protocol, Skill Dependency Graph, Variable Reference Syntax). Stage = `evaluation`, upstream = `/eval-init` (check `config.json -> entry_command` non-empty).

## Fork Check

Ask: "Base on a previous run? (run ID, or skip for fresh run)"

If forking: load the base run's `config_snapshot.json`, `sources.json`, and `lineage.parents` as starting point. Set `lineage.fork_of`. User changes only what they want. Sources from the base run are reused — skip Step 1 unless user wants to change them. If user changes the model artifact, update `lineage.parents` accordingly.

If skip: fresh run, `fork_of = null`.

## Steps 1-3: Shared Run Mechanics

Follow CLAUDE.md "Run Skill Internal Dependencies" for the shared step flow:

1. **Resolve Assets** (step `check_sources`) — fill concrete paths in `artifacts.json`, `input.json` sources, AND `input.json -> ground_truth -> sources`. Ground truth sources are what makes eval different from inference — ask for those after regular input sources. For server matching, connectivity tests, and credential flows, see CLAUDE.md "Run Skill Internal Dependencies" Step 1. Scripts in `lifecycle/scripts/infer-run/` (test_connection.py, etc). If any script fails, do the same work manually with Bash.
2. **Create Run** (step `create_run`) — create run dir, initialize run.json, code snapshot, env snapshot, dependency check. Scripts: `create_run.py`, `capture_env.py`, `check_deps.py` (all in `lifecycle/scripts/infer-run/`). For code source resolution and environment resolution, see CLAUDE.md conventions.
3. **Build & Execute** (step `execute`) — resolve `${}` references, build command per `config_format`, save `config_snapshot.json` and `sources.json` (including GT sources), confirm with user.

### Execution Modes

**Debug mode** (default for first run):
- Limit data — evaluation needs more samples than inference for meaningful metrics. Defaults: images 20, video 30s, text/tabular 50 rows. Use code's own limiting args if available (--num_samples, --max_det, --limit, --subset).
- For `single_file` pairing (COCO-style), warn that truncating annotation files is complex — prefer the code's own limiting args. If none exist, suggest adding one.
- Run synchronously, stream output.
- On failure: diagnose, propose fix, ask "Apply and re-run?". On success: show debug metrics with caveat ("on N samples — expect different on full dataset"), ask "production / retry / inspect?"

**Production mode** — ask local or server:
- **Local**: run in background (`run_in_background`), log to `{RUN_DIR}/logs/`. Return immediately so the user can continue working. They can check back with `/eval-run` again, or `/loop 5m /eval-run` for auto-polling.
- **Remote**: resolve server from resources.json, use `python_path` from server entry. SCP config + run.sh to server, launch in tmux. Return immediately.

For local/remote execution details and path mapping, see CLAUDE.md "Run Skill Internal Dependencies" Step 3 and "Path Mapping".

### Status Check

When `/eval-run` is invoked and a run has `status: "running"`:
- **Local**: check PID alive. If alive, show log tail + "still running". If dead, proceed to collect results.
- **Remote**: check tmux session. If alive, show log tail. If done, SCP logs back, proceed to collect results.
- Offer "Cancel this run?" (kill PID or tmux session, set status to `cancelled`).

Never block waiting for a long-running command. Return immediately so the user can continue working — they check back by invoking `/eval-run` again.

## Step 4: Collect Results (eval-specific)

After execution finishes:

1. **Finalize run** — update `run.json` status, finished_at, duration_s. Script: `finalize_run.py`. Fallback: manual update.

2. **Check outputs** — look in `{RUN_DIR}/outputs/` for expected files, search code dir for misplaced outputs.

3. **Collect metrics** — extract from stdout/result files into `run.json -> metrics`. Script: `extract_metrics.py`. Fallback: parse logs manually.

4. **Per-class metrics** — if `output.json -> metrics.per_class` is `true`:
   - Look for per-class metrics in result files (JSON with per-class keys, CSV with class column, stdout tables)
   - Store in `run.json -> metrics.per_class` as `{ "class_name": { "metric": value } }`
   - In summary, show top-3 best and worst performing classes

5. **Baseline comparison** — if `output.json -> metrics.baseline` is set:
   - Script: `lifecycle/scripts/eval-run/compare_baseline.py {RUN_DIR}/run.json <baseline>`. Baseline can be a run ID (resolved to that run's run.json) or inline JSON. Fallback: manually load both metric sets and compute deltas.
   - Show delta table with improvements highlighted and regressions flagged:
     ```
     Metrics (vs baseline evaluation/run_20260316_153024):
       mAP:       0.485  (+0.012, +2.5%)
       AP50:      0.673  (+0.008, +1.2%)
       mAP_small: 0.289  (-0.003, -1.0%)  <- regression
     ```

6. **Alias & index** — ask user for optional alias/description. Update `runs_index.json`. Script: `update_index.py`.

7. **Offer baseline update** — "Set this run as the new baseline?" If yes, update `output.json -> metrics.baseline` to this run's ID.

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

9. **Downstream suggestion** — offer `/eval-report` (per Skill Dependency Graph). If user accepts, invoke as sub-skill following Workflow State Protocol.

10. Pop from workflow stack, append `completed` to history.

## Quick Mode

When user provides paths inline (e.g., "evaluate model.pt on COCO val with annotations instances_val2017.json"):
1. Match paths to declared items by type/extension (including ground truth)
2. Fill sources in artifacts.json / input.json / ground_truth
3. Skip source-filling dialogue, proceed directly to execution
