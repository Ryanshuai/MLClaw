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

**One question at a time** — training has many knobs (lr, bs, epochs, seed, optimizer, scheduler, mixed precision, save policy …). Asking them all at once overwhelms; ask one, record, ask next.

**Workflow state, dependency checks, locate project, variable references** — follow CLAUDE.md (Workflow State Protocol, Skill Dependency Graph, Variable Reference Syntax). Stage = `training`, upstream = `/train-init` (check `config.json -> entry_command` non-empty).

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

## Resource Validation

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

Follow CLAUDE.md "Run Skill Internal Dependencies" — that section owns the cross-skill rules. The shared parts in plain words:

1. **Resolve Assets** (step `check_sources`) — **pick from `candidates`; don't ask for paths.** `/train-init` Step 1c already located the options for every item in `artifacts.json` and `input.json` (plus `ground_truth` for both splits). Present each item's `match: "ok"` candidates and let the user choose; when there's exactly one, default to it and just confirm. Verify the chosen path still resolves — candidates are machine-specific and go stale after an rsync to a different host, so if they all dangle, re-run `/train-init` Step 1c here rather than falling back to interrogating the user path by path. Record the choice in the run's `sources.json`.

   The chosen `location` changes what happens next: `local` → use directly; `s3` / `downloadable` → fetch before launch and record where it landed; **`server:<key>` → this is the signal to run on that machine** instead of copying the data to this one. Raise that as an option rather than silently starting a 19GB transfer.

   When extending a prior training run (fork + ckpt-as-init), auto-resolve the base run's last ckpt from `parents[-1] -> {RUN_DIR}/<ckpt_path>` and confirm with user — don't re-prompt for it.

2. **Create Run** (step `create_run`) — create run dir, init `run.json`, env snapshot (pip freeze + GPU + CUDA), dependency check. **Code snapshot via `code_snapshot.py`** — see CLAUDE.md "Code snapshot (Step 2 detail)".

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

   A `blocks` entry should never reach here (init stops on those), so if you find one, treat it as a bug in the init record and stop. Then confirm with user. **`cwd` + `output_dir` rules** — see CLAUDE.md "Launch contract (Step 3 detail)". Train-specific overrides: production mode runs in background (see "Execution Modes" below).

### Execution Modes

**Debug mode** (default for first run on a new config):
- Override `epochs=1` (or `max_steps=200` if step-based) and `batch_size` ÷ 4 if needed for fast iteration. **Check `param_injection` first** — if `epochs` is `overridable: false`, debug mode cannot shorten the run, and launching it would silently start a full-length job under the label "quick debug". Say so and offer: (a) edit the code line, (b) run debug at full length anyway, (c) cancel.
- Run synchronously, stream stdout. Watch for crash signatures (see references/crash-signatures.md).
- On failure: diagnose, propose fix, ask "Apply and re-run debug?". On success: ask "production / inspect / cancel?"
- Debug mode runs in foreground because it's short. Production mode never does.
- **Set `run.json -> mode: "debug"` and `scope`** (epochs / steps actually reached, actual batch size — read from the log, not from what you passed). A debug run's val metric describes a 1-epoch model and must never be compared against, or ranked alongside, a full run's — see CLAUDE.md "Metric comparability".

**Production mode** — ask local or server. In both cases set `run.json -> mode: "production"` and `scope` (full epochs, full data) at launch, then correct `scope` at finalize from what the log actually reports:
- **Local**: launch in background (`run_in_background`), redirect stdout to `{RUN_DIR}/logs/stdout.log`. Set `run.json -> pid`, `started_at`, `status: "running"`. Return immediately.
- **Remote**: SCP run dir to server, launch via tmux session. Record session name in `run.json -> server` and `pid: tmux:<session>`. Return immediately.

For path mapping (so jsonl on the server is readable from local), see CLAUDE.md "Path Mapping". For multi-GPU launchers, the entry_command from `config.json` already encodes them — pass through.

## Step 4: Monitor (training-specific)

Invoked when the skill re-enters with `status: "running"`. **Never block here** — read state, report, return.

### 4a. Update streaming state in run.json

Read the jsonl at `{RUN_DIR}/<output.json -> metrics.log_path>`:

- `last_heartbeat` ← jsonl file mtime (ISO timestamp). This is implicit-heartbeat: if jsonl is being written, training is alive. Avoids requiring train code to write heartbeat itself (zero code invasion).
- `last_step` ← step from the most recent record matching `record_types` with a `step` field.
- `latest_metrics` ← key fields from the most recent epoch-level record (e.g., last `val_epoch`'s primary_metric and watch_epoch fields).

Write all three to `run.json` atomically.

### 4b. Health classification

| Signal | Status |
|---|---|
| Process alive + jsonl mtime within last 2× expected_interval | **healthy** |
| Process alive + jsonl mtime stale > 2× expected_interval | **likely hung** (dataloader, deadlock, GPU hang) |
| Process dead + last record matches `done_signal` | **completed** → go to Step 5 |
| Process dead + last record does NOT match `done_signal` | **crashed** → go to Step 4c |
| Process dead + node SIGTERM signature in stdout | **preempted** → suggest fork + load last.pt as init on re-invoke |

`expected_interval` is derived from `record_types[step_type].frequency` (e.g., "every 50 steps" + observed step throughput). If unknown, default 5 minutes.

### 4c. Crash diagnosis

For details on signature → fix mapping, read `references/crash-signatures.md`. High-level patterns:

| Signature in stdout | Diagnosis | Suggested fix |
|---|---|---|
| `OutOfMemoryError`, `CUDA out of memory` | OOM | `batch_size÷2`, or add `gradient_accumulation_steps` |
| `loss=nan`, `Loss is nan` | NaN explosion | lower `lr`, add `grad_clip`, check data |
| `Killed`, `Bus error`, exit code 137 | OS kill (likely OOM at host level) | reduce model size or workers |
| Stack trace ending in dataloader | data corruption / missing file | verify sources |
| No clear signature | Unknown | dump last 50 stdout lines for user |

Show diagnosis, offer "Apply suggested fix and retry as fork?" If user accepts: create new run with `fork_of = self`, apply fix, launch.

### 4c'. ETA report

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

## Step 5: Finalize

After done_signal matched (or user manually marks completed):

### 5a. Pick best checkpoint

Read `output.json -> checkpoints`:

1. Scan jsonl for all epoch-level records.
2. Rank by `selection.best_by` in `selection.direction`.
3. Resolve the corresponding checkpoint file via `path_pattern` + epoch/step substitution.
4. If the file exists: record path in `run.json -> outputs.best_checkpoint`. If multiple matches (e.g., script saved every epoch), the highest-ranking one wins.
5. If the script already saved a `best.pt` (independent best-tracking inside training), prefer it and verify the metric matches.

### 5b. Apply retention policy

Per `output.json -> checkpoints.retention`:

| Policy | Action |
|---|---|
| `keep_all` | Do nothing. |
| `keep_last_n` | Delete all checkpoints except the `n` most recent (by epoch/step). Default `n=3` if not specified. Always keep the chosen best. |
| `keep_best_only` | Delete all except the chosen best. |
| `keep_best_and_last` | Keep best + last. Delete the rest. |

Confirm with user before any deletion. Show what will be kept and what removed.

### 5c. Finalize run.json

- `status: "completed"`, `finished_at`, `duration_s`
- `metrics`: terminal snapshot (final epoch's full record + best epoch's primary_metric)
- `last_heartbeat`: final jsonl mtime
- Reset `last_step` / `latest_metrics`? No — keep them for retrospective. They're now historical, not live.

### 5d. Summary

- No separate index file to update — `run.json` is the source of truth, queried on demand via `jq` (see CLAUDE.md "Listing runs (no separate index)" for canonical patterns).
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

Offer `/eval-run` (per Skill Dependency Graph) — pre-fill the new ckpt as the eval input artifact. If user accepts, invoke as sub-skill per Workflow State Protocol.

For sweeps and continued chains: also surface "Fork to try a variant?" or "Continue training (more epochs)?" offers, depending on training trajectory. (`/train-compare`, when available, will be offered for comparing multiple completed runs.)

Pop from workflow stack, append `completed` to history.

## Quick Mode

When user provides paths inline (e.g., "train on /data/imagenet with config configs/r50.yaml, batch 256, 100 epochs"):
1. Match paths to declared items by type/extension (data, GT, pretrained backbone)
2. Fill sources in `artifacts.json` / `input.json`
3. Apply inline param overrides to `runtime_params` — but check `param_injection.items` first. If the user names a param marked `overridable: false` (or one absent from `param_injection`), say so instead of accepting it silently: "`seed` is hardcoded at `train.py:12`; changing it needs a code edit, not a flag." Accepting it would produce a run whose recorded config lies about what actually trained.
4. Skip source-filling dialogue, proceed to Resource Validation
