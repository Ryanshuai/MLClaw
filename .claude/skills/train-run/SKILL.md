---
name: train-run
description: >
  Use this skill whenever the user wants to execute a training run — launching, monitoring, or finalizing
  a model training job. Trigger for: starting a training run (debug or production mode), checking status
  of an in-progress training job, diagnosing a crashed run, finalizing a completed run (picking best
  checkpoint, applying retention), forking a previous training with changed hyperparameters, or
  continuing/resuming training from a prior checkpoint. Also trigger for Chinese requests like
  "跑训练", "开训", "继续训", "训练崩了看一下", "训练完了". This is the execution skill — not for initial
  schema setup (use train-init) or comparing runs (use train-compare, when available).
---

# /train-run — Run Training

Execute a training run: validate resources, resolve sources, launch in background, monitor stream, detect termination, finalize. Training is long-running by nature — this skill never blocks.

**One question at a time** — training has many knobs (lr, bs, epochs, seed, optimizer, scheduler, mixed precision, save policy …). Asking them all at once overwhelms; ask one, record, ask next. **And only what only they know** — a value you can read is not a question, and a value nobody has is recorded absent rather than asked for: CLAUDE.md "Decide what evidence can decide".

**Workflow state, dependency checks, locate project, variable references** — follow `lifecycle/references/skill-graph.md` (state protocol + the requires/suggests table) and `lifecycle/references/layout.md` (Variable Reference Syntax). Stage = `training`, upstream = `/train-init` (check `config.json -> entry_command` non-empty).

**Re-entry behavior** — when this skill is invoked again on an existing run, do NOT re-launch. Read `run.json -> status` and route:

| Status | Action |
|---|---|
| `running` | Status check (tail jsonl, update heartbeat / last_step / latest_metrics, report ETA) |
| `completed` | Show final summary, offer `/eval-run` |
| `failed` | Show diagnosis, offer fix + retry |
| `cancelled` | Show last state, offer fresh launch |
| `preempted` | Offer to continue (fork self, load last ckpt as init via runtime_params; parents += [self]) |

## Fork Check

Ask: "Base on a previous run? (run ID, or skip for fresh run)"

If forking: load the base run's `config_snapshot.json`, `sources.json`, and `lineage.parents` as starting point. Set `lineage.fork_of`. User changes only what they want. Sources reused — skip Step 1 unless user wants to change them. If user changes the model artifact (different pretrained backbone), update `lineage.parents` accordingly.

If skip: fresh run, `fork_of = null`.

**Continuing training / preempt recovery / fine-tuning** is a common case but does NOT need a separate lineage field. Express it as fork + ckpt-as-init:

1. Fork the prior run (sets `fork_of = prior_run`, copies config)
2. Set `runtime_params.resume_from` (or your code's equivalent) to point at `prior_run/last.pt` so weights load on launch. Confirm the key exists in `param_injection` with `overridable: true` — a resume flag the code ignores means training silently restarts from scratch, which looks identical to a successful resume until the loss curve gives it away hours later.
3. Append `prior_run` to `lineage.parents` (since you now consume its ckpt — hard dependency)

The reasoning ("why continue") goes in `description` / `hypothesis`, or in `decisions.jsonl` if running under `/train-tune`.

## Resource Validation (step `resource_validation`)

Before launching, check that the host has enough hardware to run the configured training:

1. Read `config.json -> resources` (gpu_count, gpu_memory_gb, distributed).
2. Probe local environment: `nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader`.
3. Compare:
   - **gpu_count**: count free GPUs (memory.free > 80% of memory.total). Fail if fewer than required.
   - **gpu_memory_gb**: each free GPU's `memory.total` must be ≥ required.
   - **distributed**: if `ddp` / `fsdp` / `deepspeed_*`, verify launch command matches the configured launcher (`torchrun`, `accelerate launch`, `deepspeed`).
4. If insufficient: surface the gap to the user, ask if they want to (a) wait, (b) launch on a remote server with more resources, or (c) override (mark the run as "underprovisioned" — useful for debug at small scale).

For remote servers, query the server's `nvidia-smi` over SSH first; resolve via `resources.json -> servers`.

## Steps 1-3: Shared Run Mechanics

Follow `lifecycle/references/run-mechanics.md` "Run Skill Internal Dependencies" — that section owns the cross-skill rules. The shared parts in plain words:

1. **Resolve Assets** (step `resolve_assets`) — **pick from `candidates`; don't ask for paths.** `/train-init` Step 1c already located the options for every item in `artifacts.json` and `input.json` (plus `ground_truth` for both splits). Full rules: `lifecycle/references/run-mechanics.md` → "Asset resolution (Step 1 detail)" — read them there, not from a summary here. **Every `match` value routes somewhere and filtering to `ok` is a bug**: a `pending` candidate has a party and a due date, an `unreachable` one has a missing credential, and reporting either as "no options" conflates *not here* with *could not look*. Record the choice in the run's `sources.json`.

   The chosen `location` changes what happens next: `local` → use directly; `s3` / `downloadable` → fetch before launch and record where it landed; **`server:<key>` → this is the signal to run on that machine** instead of copying the data to this one. Raise that as an option rather than silently starting a 19GB transfer.

   When extending a prior training run (fork + ckpt-as-init), auto-resolve the base run's last ckpt from `parents[-1] -> {RUN_DIR}/<ckpt_path>` and confirm with user — don't re-prompt for it.

2. **Create Run** (step `create_run`) — **use the scripts; do not create the run dir by hand.**

   ```bash
   python <mlclaw_root>/lifecycle/scripts/shared/create_run.py <stage_dir> <mlclaw_root>/lifecycle/run.json
   python <mlclaw_root>/lifecycle/scripts/shared/code_snapshot.py <code_dir> <RUN_DIR>   # merge into run.json -> code
   python <mlclaw_root>/lifecycle/scripts/shared/capture_env.py <RUN_DIR>
   python <mlclaw_root>/lifecycle/scripts/shared/check_deps.py  <code_dir> <RUN_DIR>
   ```

   `create_run.py` is not a convenience here. `run_id` has one-second resolution, and a `/train-tune` session with `max_concurrent > 1` launches trials in the same second — hand-rolled `mkdir -p` lets two runs share a directory and the second `run.json` write destroys the first run's record. The script allocates a free id and says so. It also writes `created_at` as UTC-with-offset, which is what makes `list_runs.py`'s ordering correct across machines.

   **Code snapshot** — see `lifecycle/references/run-mechanics.md` "Code snapshot (Step 2 detail)". Read `reproducible` in its output before continuing; false means the snapshot cannot rebuild this tree.

   **Train-specific: diff the captured env against `config.json -> env_snapshot`** (what the original author had) and report key-package mismatches before launching:

   ```
   env differs from the original:  torch 2.1.0 → 2.4.1   timm 0.9.12 → 1.0.3
   timm's default model configs changed between those versions.
   ```

   Don't block on it — just say it. This is the first thing to check when the author's numbers won't reproduce: same code, same data, different `torch` or `timm` → different results, and without this line the code takes the blame for an environment difference. When `env_snapshot.source` is `"none"`, say that instead: there's no record to compare against, so a reproduction gap can't be attributed either way.

3. **Build & Launch** (step `execute`) — resolve `${}` references, then build the command **per-param from `config.json -> param_injection.items`**, not by guessing from `config_format`: `via: cli` → append its `flag`, `via: yaml` → write its `key` into the config copy, `via: env` → export it. A `runtime_params` key with no `param_injection` entry is an error, not a "just try `--key value`" — stop and ask, because a silently-ignored flag produces a run whose recorded config doesn't match what trained. Params marked `overridable: false` must never be passed (they can't take effect); if the user wants one changed, tell them which `path:line` to edit. Then save `config_snapshot.json` and `sources.json`.

   **Echo `config.json -> hazards` before asking for confirmation** — one line per `degrades` and `risks` entry. This is the only moment anyone reads them; left sitting in JSON they may as well not exist. Judge `risks` against *this* launch, since the condition is finally knowable: a `world_size` hazard matters now that the GPU count is decided, `network_required` now that you know whether this host has egress.

   ```
   ⚠ risks    · code asserts world_size == 8, launching with 1 — lr is scaled by world size (train.py:52)
   ⚠ degrades · val split unseeded — metrics drift between runs (dataset.py:118)
   ```

   A `blocks` entry should never reach here (init stops on those), so if you find one, treat it as a bug in the init record and stop. Then confirm with user. **`cwd` + `output_dir` rules** — see `lifecycle/references/run-mechanics.md` "Launch contract (Step 3 detail)". Train-specific overrides: production mode runs in background (see "Execution Modes" below).

### Execution Modes

**Debug mode** (default for first run on a new config):
- Override `epochs=1` (or `max_steps=200` if step-based) and `batch_size` ÷ 4 if needed for fast iteration. **Check `param_injection` first** — if `epochs` is `overridable: false`, debug mode cannot shorten the run, and launching it would silently start a full-length job under the label "quick debug". Say so and offer: (a) edit the code line, (b) run debug at full length anyway, (c) cancel.
- Run synchronously, stream stdout. Watch for crash signatures (see references/crash-signatures.md).
- On failure: diagnose, propose fix, ask "Apply and re-run debug?". On success: ask "production / inspect / cancel?"
- Debug mode runs in foreground because it's short. Production mode never does.
- **Set `run.json -> mode: "debug"` and `scope`** (epochs / steps actually reached, actual batch size — read from the log, not from what you passed). A debug run's val metric describes a 1-epoch model and must never be compared against, or ranked alongside, a full run's — see `lifecycle/references/run-mechanics.md` "Metric comparability".

**Production mode** — ask local or server. In both cases set `run.json -> mode: "production"` and `scope` (full epochs, full data) at launch, then correct `scope` at finalize from what the log actually reports:
- **Local**: launch in background (`run_in_background`) with `PYTHONUNBUFFERED=1` in the environment, redirect stdout to `{RUN_DIR}/logs/stdout.log`. Set `run.json -> pid`, `started_at`, `status: "running"`. Return immediately.
- **Remote**: SCP run dir to server, launch via tmux session with `PYTHONUNBUFFERED=1` set **inside** the session command (`tmux new-session -d ... 'PYTHONUNBUFFERED=1 <cmd> > logs/stdout.log 2>&1'`) — tmux does not inherit the local shell's env. Record session name in `run.json -> server` and `pid: tmux:<session>`. Return immediately.

`PYTHONUNBUFFERED=1` is not cosmetic: it is what makes `logs/stdout.log` advance in real time, and that file is the reliable leg of the Step 4b liveness check. MLClaw controls the launch environment, so this costs the training code nothing — zero code invasion holds.

For path mapping (so jsonl on the server is readable from local), see `lifecycle/references/run-mechanics.md` "Path Mapping (Cross-Machine Execution)". For multi-GPU launchers, the entry_command from `config.json` already encodes them — pass through.

## Step 4: Monitor (step `monitor`, training-specific)

Invoked when the skill re-enters with `status: "running"`. **Never block here** — read state, report, return.

Re-entrant, so `steps.monitor.status` stays null while the job runs; set it `completed` only when the run terminates. Infer/eval have no such step and leave it `skipped`.

### 4a. Update streaming state in run.json (sub-step `stream_state`)

Two different files, for two different jobs — see `lifecycle/references/run-mechanics.md` "Metric stream" for the vocabulary:

- **Probe the source** — `{RUN_DIR}/<output.json -> metrics.log_path>` and `{RUN_DIR}/logs/stdout.log`. Liveness must read what the training process itself touches. Probing the derived stream instead makes "training stalled" and "our ingest failed" look identical, and the second one would be reported as the first.
- **Read records from the stream** — resolve it through `stream_path()` (explicit path → `{RUN_DIR}/stream.jsonl` → source fallback) rather than opening `log_path` directly, so this step reads the same records `select_checkpoint` will later rank.

Re-derive the stream first, then read it:

```bash
{run_in_env} python <mlclaw_root>/lifecycle/scripts/train-run/ingest.py \
    <stage>/output.json --run-dir <RUN_DIR>
```

Pull-based and idempotent for the stream, safe to call on every check, and it cannot affect the training process. On a remote run it goes over ssh on the training host, whose env has the packages the source needs.

Exit codes decide what you do next, and the two failure modes are not interchangeable:

| Exit | Meaning | What the skill does |
|---|---|---|
| 0 | stream written (a `warn` still exits 0) | continue; surface any finding |
| 1 | ingest **refused** — bad `group_by`, no adapter for this source, or the source path is our own render target | stop and report. Do **not** read the source by hand: that is the check being overridden |
| 2 | ingest broke — missing package, unreadable file | fall back to reading the source directly, and say in the report that the stream is stale |

Ingest also renders `{RUN_DIR}/tb/` by default. When `meta.tb.rendered` is true, give the user the command with the run's own path filled in — local `tensorboard --logdir {RUN_DIR}`, or for a remote run TB on the training host plus `ssh -N -L 6006:localhost:6006 {alias}`. Don't manage that process; it's theirs to close. When `rendered` is false, `meta.tb.why` says whether that is because the code already writes tfevents (point them at `output/` instead) or because no writer package exists.

- `liveness_probe` ← `{observed_at, jsonl_mtime, jsonl_size, stdout_mtime}` as observed right now, all four off the **source**.
- `last_heartbeat` ← `observed_at`, but **only if at least one of those three counters advanced past the stored `liveness_probe`**. If none did, leave the previous `last_heartbeat` in place — the widening gap is exactly what 4b reads as a hang.
- `last_step` ← step from the most recent record matching `record_types` with a `step` field.
- `latest_metrics` ← key fields from the most recent epoch-level record (e.g., last `val_epoch`'s primary_metric and watch_epoch fields).

Take both from ingest's own `report.tail` when it is present — it is the last record, already parsed. Re-opening the stream to find it costs a full parse of a file that reaches tens of MB on a long run, and on a remote run a second ssh round trip for data that already crossed once.

Write all four to `run.json` atomically. Liveness needs a *previous* observation to compare against, which is why `liveness_probe` is persisted rather than recomputed; on the first check of a run there is nothing to diff, so store the probe, set `last_heartbeat = started_at`, and classify healthy.

Three counters, not one, because **jsonl mtime alone is a false-alarm generator**: Python's buffered writer flushes roughly every 8 KB while a jsonl record is ~200 bytes, so mtime can lag many minutes behind a perfectly healthy process.

| Counter | Advances when | Stale on a *healthy* run when |
|---|---|---|
| `jsonl_mtime` | records get flushed to disk | writer is still buffering (up to ~8 KB of records) |
| `jsonl_size` | the file grows, including before mtime moves | logging interval is long ("every N steps") |
| `stdout_mtime` | the process prints anything | code is silent between epochs |

`stdout_mtime` is the reliable leg: MLClaw itself owns the redirect to `logs/stdout.log` and sets `PYTHONUNBUFFERED=1` at launch, so it advances with zero cooperation from the training code.

### 4b. Health classification (sub-step `health_check`)

| Signal | Status |
|---|---|
| Process alive + **any** counter advanced (`last_heartbeat` within 2× expected_interval) | **healthy** |
| Process alive + **all three** counters stale > 2× expected_interval | **likely hung** (dataloader, deadlock, GPU hang) |
| Process dead + last record matches `done_signal` | **completed** → go to Step 5 |
| Process dead + last record does NOT match `done_signal` | **crashed** → go to Step 4c |
| Process dead + node SIGTERM signature in stdout | **preempted** → suggest fork + load last.pt as init on re-invoke |

All three, not any one — a single stale counter is the normal case per the 4a table, and "likely hung" is a claim the user acts on by killing a run that may be six hours in. `expected_interval` is derived from `record_types[step_type].frequency` (e.g., "every 50 steps" + observed step throughput). If unknown, default 5 minutes.

### 4c. Crash diagnosis (sub-step `crash_diagnosis`)

For details on signature → fix mapping, read `references/crash-signatures.md`. High-level patterns:

| Signature in stdout | Diagnosis | Suggested fix |
|---|---|---|
| `OutOfMemoryError`, `CUDA out of memory` | OOM | `batch_size÷2`, or add `gradient_accumulation_steps` |
| `loss=nan`, `Loss is nan` | NaN explosion | lower `lr`, add `grad_clip`, check data |
| `Killed`, `Bus error`, exit code 137 | OS kill (likely OOM at host level) | reduce model size or workers |
| Stack trace ending in dataloader | data corruption / missing file | verify sources |
| No clear signature | Unknown | dump last 50 stdout lines for user |

Show diagnosis, offer "Apply suggested fix and retry as fork?" If user accepts: create new run with `fork_of = self`, apply fix, launch.

### 4d. ETA report (no tracked sub-step — output, not state)

Format:
```
Run: training/run_20260427_180000  status: running (healthy)
Step:  4711 / 7800   epoch  60/100   throughput  47 step/s
Latest val_epoch (epoch 59):
  val_loss        0.234
  val_acc         0.967    ← primary_metric (max), best so far at epoch 59
ETA:   ~ 11 minutes (based on throughput × remaining_steps)
last_heartbeat: 12s ago
```

## Step 5: Finalize (step `collect_results`)

After done_signal matched (or user manually marks completed). **Run these three in order — each one's output is the next one's input, and running them out of order or skipping one means ranking a checkpoint against a schema nobody checked.**

### 5a'. Reconcile the metric schema (sub-step `reconcile_metrics`)

```bash
python <mlclaw_root>/lifecycle/scripts/train-run/reconcile_metrics.py \
    <stage>/output.json --run-dir <RUN_DIR>
```

Checks the schema `/train-init` wrote against what the stream actually emitted: does `primary_metric` exist in the stream, on which record types, on the training or the held-out split, and does its declared `direction` contradict its own name. **A `fail` verdict stops finalization** — do not select a checkpoint or record a metric from a stream the schema does not describe. Fix `output.json` (or re-run `/train-init` Step 3) and re-reconcile.

The verdict this catches most often: `primary_metric` names a field the code never emits, and the near-miss list shows what it emits instead (`val_loss` declared, `loss` and `train_loss` present). Second most often: a training-split metric driving checkpoint selection.

### 5a. Pick best checkpoint (sub-step `select_checkpoint`)

```bash
python <mlclaw_root>/lifecycle/scripts/train-run/select_checkpoint.py \
    <stage>/output.json --run-dir <RUN_DIR> --output-dir <RUN_DIR>/output
```

Returns the chosen file **plus the ranking with the values as they literally appear in the jsonl, and the raw record behind the winner**. Show the ranking to the user, not just the path — a path alone carries no evidence that the sort was right.

Record `chosen.path` in `run.json -> outputs.best_checkpoint`. Cases it surfaces that need a decision:

| Finding | What it means |
|---|---|
| `best_record_skipped` | the peak epoch was never saved (`save_every=N`). Falling through to the next-best is fine — but record **that** checkpoint's value, not the stream's peak. Recording the peak next to a lower-scoring artifact is a fake metric. |
| `script_saved_best_differs` | the training script tracked "best" itself and disagrees with this ranking. Two rankings disagreeing means one uses a different metric or direction. Resolve before trusting either. |
| `best_by_differs_from_primary` | the checkpoint chosen and the number on the leaderboard describe different things. |
| `checkpoints_without_a_metric` | files on disk with no record in the stream — retention will refuse to delete these. |

### 5b. Apply retention policy (sub-step `retention`)

`output.json -> checkpoints.retention`: `keep_all` | `keep_last_n` (default `n=3`) | `keep_best_only` | `keep_best_and_last`. The chosen best survives every policy.

**This is the only irreversible operation in MLClaw, so it is two commands, not one.** "Confirm with the user" is not by itself a safeguard — a user shown a list of filenames has no way to check whether the sort behind it was right.

```bash
# 1. plan — deletes nothing, ever. Prints each file with the metric value that decided its fate.
python <mlclaw_root>/lifecycle/scripts/train-run/retention.py plan \
    <stage>/output.json --run-dir <RUN_DIR> --output-dir <RUN_DIR>/output \
    --plan <RUN_DIR>/retention_plan.json

# 2. show the table to the user, then apply with the token from the plan file
python <mlclaw_root>/lifecycle/scripts/train-run/retention.py apply \
    --plan <RUN_DIR>/retention_plan.json --confirm <confirm_token>
```

`plan` refuses outright — no plan, no token, nothing deleted — when the ranking has a `fail` finding, when the best is not in the keep set, when the keep set would be empty, when everything would be deleted, or when **a file scheduled for deletion has no metric in the stream**. That last one is the rule worth remembering: never delete what you cannot rank.

`apply` re-stats every file against the plan's digest and aborts on any drift (a file changed, vanished, or a new checkpoint appeared). It deletes nothing partially — drift blocks the whole operation, and the fix is to re-plan, not to force through.

### 5c. Finalize run.json (sub-step `finalize`)

```bash
python <mlclaw_root>/lifecycle/scripts/shared/finalize_run.py <RUN_DIR>/run.json completed
```

Sets `status` / `finished_at` / `duration_s`. Read the `warnings` it returns rather than only the duration — it reports when `started_at` carried no timezone (so the duration rests on an assumption), when the subtraction could not be done at all, and when the result is negative from clock skew between the launch host and this one. A null `duration_s` with no explanation is indistinguishable from a run that never started.

Then fill the rest by hand:

- `metrics`: terminal snapshot (final epoch's full record + best epoch's primary_metric). Write the `best` block in the shape `list_runs.py` and `/train-tune` read: `metrics.best = {primary_metric, primary_metric_value, epoch}`.
- **The recorded metric must be the chosen checkpoint's value.** If 5a reported `best_record_skipped`, use `record_this_value` from that finding, not the stream's peak.
- `last_heartbeat`: the last probe at which the job was still advancing (per 4a) — **not** `finished_at`. Leave `liveness_probe` holding its final observation. Together they distinguish "finished at 04:12" from "stopped moving at 02:40 and was reaped at 04:12".
- Reset `last_step` / `latest_metrics`? No — keep them for retrospective. They're now historical, not live.

### 5d. Summary

- No separate index file to update — `run.json` is the source of truth, queried on demand via `shared/list_runs.py` (see `lifecycle/references/run-mechanics.md` "Listing runs (no separate index)" for canonical patterns).
- Ask user for optional alias / description, write into `run.json -> alias` / `description`.
- Show summary:
  ```
  Run: training/run_20260427_180000  status: completed  duration 4h 12m
  Total epochs: 100   total steps: 7800   throughput: 31 step/s avg
  Best ckpt:    runs/.../epoch_87/  (val_acc=0.974 at epoch 87)
  Retention applied: keep_best_and_last → 12 ckpts kept, 88 removed
  Outputs: train_log.jsonl (4.2 MB), best.pt (450 MB), last.pt (450 MB)
  Lineage: parents=[], fork_of=null
  ```

### 5e. Downstream suggestion

Offer `/eval-run` (per `lifecycle/references/skill-graph.md` -> "Skill Dependency Graph") — pre-fill the new ckpt as the eval input artifact. If user accepts, invoke as sub-skill per Workflow State Protocol.

For sweeps and continued chains: also surface "Fork to try a variant?" or "Continue training (more epochs)?" offers, depending on training trajectory. (`/train-compare`, when available, will be offered for comparing multiple completed runs.)

Pop from workflow stack, append `completed` to history.

## Quick Mode

When user provides paths inline (e.g., "train on /data/imagenet with config configs/r50.yaml, batch 256, 100 epochs"):
1. Match paths to declared items by type/extension (data, GT, pretrained backbone)
2. Fill sources in `artifacts.json` / `input.json`
3. Apply inline param overrides to `runtime_params` — but check `param_injection.items` first. If the user names a param marked `overridable: false` (or one absent from `param_injection`), say so instead of accepting it silently: "`seed` is hardcoded at `train.py:12`; changing it needs a code edit, not a flag." Accepting it would produce a run whose recorded config lies about what actually trained.
4. Skip source-filling dialogue, proceed to Resource Validation
