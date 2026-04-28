---
name: train-init
description: >
  Use this skill to analyze training code and configure the training stage. Triggers when the user
  wants to set up training for a model — analyzing what a training script needs (data, labels,
  pretrained weights), what it produces (checkpoints, logs, streaming metrics), how it signals
  completion, and filling the 4 JSON config files. Use for: "analyze training code", "set up training",
  "configure training stage", "what does this training script log", "分析训练代码", "配置训练",
  "初始化train". Not for running training (use train-run) or evaluation (use eval-init).
---

# /train-init — Training Stage Initialization

Analyze training code and fill 4 JSON config files (schema only) in `stages/training/`. The goal is to understand what the code needs, what it streams during a long run, and what it produces — so `/train-run` can launch, monitor, and select checkpoints correctly later.

## How this skill works

The user brings training code (`train.py`, `pretrain.py`, `accelerate launch ...`, or a training mode inside a larger codebase). You read it, figure out:

- **Inputs**: train/val data, labels, optional pretrained weights
- **Streaming output**: how metrics are emitted during the run (jsonl / stdout regex / wandb / tb), and at what granularity (per step / per epoch)
- **Terminal output**: which checkpoints get saved, how to pick the "best" one, what signals "training is done"

…and capture all of that in 4 structured JSON files.

These files are **schema only** — they define WHAT is needed and produced, not WHERE to get it. Concrete paths come later during `/train-run`.

## Interaction approach

Ask one question at a time. Training has more knobs than infer/eval (lr, bs, epochs, optimizer, scheduler, mixed precision, checkpoint policy …) — overwhelming the user with all of them at once is the failure mode. Ask one, record, then the next.

## On entry

Follow the standard Workflow State Protocol from CLAUDE.md: push to stack, check dependencies (project.json exists, code available), locate project, resolve code directory.

## Output: 4 JSON files

| File | What it captures |
|------|-----------------|
| `config.json` | Entry command, config format, framework, **resource requirements**, managed params |
| `artifacts.json` | Static inputs — pretrained backbone, tokenizer, base ckpt for fine-tuning |
| `input.json` | Dynamic inputs — train + val data with labels (same shape as eval) |
| `output.json` | Checkpoints, **log stream format**, **streaming metrics schema**, **primary metric + done signal** |

For item/source schemas and type classification, read `references/schemas.md`. The schemas are mostly inherited from eval-init — only the deltas are documented.

## Step 1: Analyze Code

Read all code files (*.py, *.sh, *.yaml, *.yml, *.json, *.toml) under the code directory.

For training-specific pattern recognition (entry points, distributed launchers, log writers, ckpt savers, done signals), read `references/detection-patterns.md`.

Determine:
- **entry_command**: how to launch training. May be a wrapper (`bash train.sh`), distributed launcher (`torchrun --nproc_per_node 8 train.py`, `accelerate launch ...`, `deepspeed train.py`), or plain `python train.py`. Capture the full launch as the user runs it.
- **config_format**: argparse, yaml, omegaconf, hydra, or combination
- **config_path**: where the config file lives relative to code/
- **framework**: pytorch, deepspeed, accelerate, lightning, hf-trainer, etc.
- **distributed**: `single_gpu`, `ddp`, `fsdp`, `deepspeed_zero{1,2,3}`, `tensor_parallel`, or `""` if single-process
- **Resources** → fill `config.json -> resources` (gpu_count, gpu_memory_gb, expected_duration_h)
- **Artifacts** → pretrained backbone, tokenizer, base ckpt (for fine-tune / training extension)
- **Inputs** → train images/text + train labels + val images/text + val labels
- **Ground truth pairing** → directory parallel / coco json / hf datasets / yolo txt
- **Outputs** → checkpoints (with naming pattern), log files
- **Required packages**: run `python lifecycle/scripts/infer-init/scan_requirements.py <code_dir>`. If it fails, check requirements.txt manually.

## Step 2: Discover Real Config

Look for actual config files in the code directory:
1. Check `config_path` from analysis
2. Scan `configs/`, `config/`, `conf/`, `recipes/` directories
3. Prefer files named "train", "training", "pretrain", "finetune", "default", "baseline", "base", "main"
4. Pick the largest YAML / JSON config as fallback

If found, load all discovered parameters.

## Step 3: Select Managed Parameters

Training configs typically have many parameters. Use progressive disclosure (same as infer-init).

Common training parameters worth offering as managed:
- **Optimization**: `learning_rate`, `weight_decay`, `optimizer`, `lr_scheduler`, `warmup_ratio`, `warmup_steps`
- **Batch**: `batch_size`, `gradient_accumulation_steps`, `eval_batch_size`
- **Duration**: `epochs`, `max_steps`, `eval_every`, `save_every`
- **Reproducibility**: `seed`
- **Precision / hardware**: `mixed_precision`, `bf16`, `fp16`, `compile`
- **Output**: `output_dir`, `run_name`

Selected params → `config.json -> runtime_params` with `${artifact.xxx}` / `${input.xxx}` references where applicable.

## Step 4: Identify Log Format and Streaming Schema

This step has no equivalent in infer-init / eval-init. Training emits metrics continuously during a long run; the gate stage / monitor needs to know exactly how.

**4a. Detect log format.** Check the code for one of:

| Pattern | log_format |
|---|---|
| `f.write(json.dumps({...}))` to a `.jsonl` file | `jsonl` |
| `print(json.dumps({...}))` only | `jsonl_stdout` |
| `wandb.log({...})` | `wandb` |
| `SummaryWriter().add_scalar(...)` | `tensorboard` |
| Plain `print(f"epoch {e} loss {l}")` | `stdout_regex` (build extractor in 4b) |
| Multiple of the above | pick the most structured one; record others as fallback |

Record in `output.json -> metrics.log_format` and `metrics.log_path`.

**4b. If `stdout_regex`**: ask user to run training for 1 epoch / 50 steps and capture stdout. Build a regex extractor that parses each metric line into a record. Store as `metrics.stdout_extractor`. `/train-run` will pipe stdout through this at runtime.

**4c. Discover record types.** For jsonl-style logs, identify the distinct `type` values emitted (e.g., `train_step`, `val_epoch`, `ckpt_saved`, `done`). For each type, list the fields. Fill `output.json -> metrics.record_types`.

Example:
```json
"record_types": {
  "train_step": {"fields": ["step", "epoch", "loss", "lr"], "frequency": "every N steps"},
  "val_epoch": {"fields": ["epoch", "step", "val_loss", "val_acc"], "frequency": "every M epochs"},
  "done":      {"fields": ["best_val_acc"], "is_terminal": true}
}
```

## Step 5: Identify Primary Metric and Done Signal

**5a. Primary metric.** Ask: "Which metric drives 'best checkpoint' selection? Direction (max/min)?"

Common answers: `val_acc` / max, `val_loss` / min, `mAP` / max, `bleu` / max, `cer` / min.

Record in `output.json -> metrics.primary_metric` + `direction`.

**5b. Watch lists.** From the discovered metric fields, ask user which to track:
- `watch_step`: high-frequency metrics (loss, lr, throughput) — one entry per step record
- `watch_epoch`: low-frequency metrics (val_*, primary_metric) — one entry per epoch record

Both go into `output.json -> metrics.watch_step` and `watch_epoch`.

**5c. Done signal.** How does the script signal completion? Options:

| Signal | Example |
|---|---|
| Explicit jsonl record | `{"type": "done", ...}` last line |
| Process exit + last `val_epoch.epoch == max_epoch - 1` | most clean training scripts |
| Stdout marker | "Training complete", "Saved final model" |
| File presence | `<run>/.done` flag, `final.pt` exists |

Record in `output.json -> metrics.done_signal` as a structured matcher (preferred) or stdout substring (fallback).

## Step 6: Identify Checkpoint Pattern

**6a. Path pattern.** Where does the script save? Examples: `<output_dir>/best.pt`, `<output_dir>/checkpoint-{step}.pt`, `<output_dir>/epoch_{epoch}.pt`. Record as glob in `output.json -> checkpoints.path_pattern`.

**6b. Selection.** Which checkpoint is the canonical "best" for downstream stages? Default: `best_by=primary_metric, direction=max`. Confirm with user.

**6c. Retention.** Default `keep_all`. Other options: `keep_last_n`, `keep_best_only`, `keep_best_and_last`. Confirm.

## Step 7: Present Each File for Review

Show each JSON file one at a time in order: `config.json` → `artifacts.json` → `input.json` → `output.json`. For each: show proposed content, wait for confirmation, then move on. If user says "skip", accept remaining files as-is.

`output.json` has the most novelty (record_types, primary_metric, done_signal) — present its `metrics` block last and walk through each subkey if user wants detail.

## Step 8: Validate

Confirm the schema is internally consistent:

- `metrics.primary_metric` must appear in at least one `record_types.<type>.fields`
- `metrics.watch_epoch` items must appear in `record_types` (any type whose `is_terminal` is false)
- `checkpoints.selection.best_by` typically equals `metrics.primary_metric` (warn if different)
- `done_signal` must reference an actual record type or stdout pattern

If any check fails, surface it to the user and ask to fix or override.

## Done — handoff to /train-run

Once all 4 files are saved, the training stage is initialized. From here, `/train-run` consumes these schemas to launch, monitor, and finalize each training run. Each run also fills its own `lineage` block (`parents` / `fork_of` / `variation_summary`) per CLAUDE.md.
