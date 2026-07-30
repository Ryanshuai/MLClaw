# Config Schemas for Training Stage

## Inherited from eval-init

Item, source, and ground_truth schemas are identical to eval-init. See [`../../eval-init/references/schemas.md`](../../eval-init/references/schemas.md) for:

- `items` schema (`type`, `format`, `description`, `resource`)
- `sources` schema (`source`, `path`, `credentials`, `origin`)
- `ground_truth` substructure (same as eval — train and val splits both have paired labels)
- Type classification rules (model / checkpoint / config / image / video / etc.)

## Training-only deltas

### `config.json -> resources`

```json
{
  "gpu_count": 1,
  "gpu_memory_gb": null,
  "expected_duration_h": null,
  "distributed": ""
}
```

- `gpu_count`: minimum GPUs needed (1 for single-GPU, 8 for typical multi-GPU pretraining)
- `gpu_memory_gb`: per-GPU memory required, peak. Used by `/train-run` to validate hardware match.
- `expected_duration_h`: rough wall-clock estimate (used for monitoring ETA + alerting on hung runs)
- `distributed`: `single_gpu` | `ddp` | `fsdp` | `deepspeed_zero1` | `deepspeed_zero2` | `deepspeed_zero3` | `tensor_parallel` | `""`

### `config.json -> param_injection`

One entry per `runtime_params` key. Answers two questions the rest of the lifecycle depends on: **how does a value actually get into the code**, and **does an external override survive**.

```json
{
  "items": {
    "lr":         { "via": "cli",  "flag": "--lr",  "overridable": true,
                    "evidence": "train.py:33" },
    "batch_size": { "via": "yaml", "key": "data.bs", "overridable": true,
                    "evidence": "configs/base.yaml:14" },
    "seed":       { "via": "hardcoded", "overridable": false,
                    "evidence": "train.py:12",
                    "note": "torch.manual_seed(42) after arg parse — changing --seed has no effect" },
    "warmup_steps": { "via": "derived", "derived_from": ["epochs", "gpu_count"],
                    "overridable": false, "evidence": "sched.py:20",
                    "note": "recomputed from epochs × steps_per_epoch; direct override is discarded" }
  }
}
```

`via` enum:

| Value | Meaning | typical `overridable` |
|---|---|---|
| `cli` | argparse / click flag | `true` — also record `flag` |
| `yaml` | key in the config file | `true` — also record `key` (dotted path) |
| `env` | environment variable | `true` — also record `env` |
| `hardcoded` | literal in code, assigned **after** config load | `false` |
| `derived` | computed from other params | `false` — also record `derived_from` |

**The effective-value rule.** `runtime_params` holds what the code actually runs with, not what a config file claims. When a yaml value is later overwritten in code, record the code's value and note the shadowing in `param_injection`. Getting this backwards is worse than leaving the param out: `/train-tune` would sweep a knob that isn't connected, produce N identical runs, and conclude the hyperparameter doesn't matter.

**`overridable: false` params should not be in `runtime_params` at all** — they are not knobs. Keep them in `param_injection` as documentation of *why* they can't be tuned, and surface them as risks (the user has to edit code to change them). Step 8 validates this.

### `output.json -> checkpoints`

```json
{
  "path_pattern": "<output_dir>/checkpoint-{step}.pt",
  "selection": {
    "best_by": "val_acc",
    "direction": "max"
  },
  "retention": "keep_best_and_last"
}
```

- `path_pattern`: glob with `{step}` / `{epoch}` placeholders. `/train-run` resolves at runtime.
- `selection.best_by`: metric name; should match `metrics.primary_metric`
- `selection.direction`: `max` | `min`
- `retention`: `keep_all` | `keep_last_n` | `keep_best_only` | `keep_best_and_last`

### `output.json -> metrics`

```json
{
  "log_format": "jsonl",
  "log_path": "train_log.jsonl",
  "stdout_extractor": null,
  "record_types": {
    "<type_name>": {
      "fields": ["<field1>", "<field2>"],
      "frequency": "<description or 'is_terminal: true'>"
    }
  },
  "definitions": {
    "<metric_name>": {"unit": "", "description": ""}
  },
  "watch_step":  ["loss", "lr"],
  "watch_epoch": ["val_loss", "val_acc"],
  "primary_metric": "val_acc",
  "direction": "max",
  "done_signal": {
    "type": "record",
    "record_type": "done"
  }
}
```

`log_format` enum:

| Value | Meaning | extractor needed? |
|---|---|---|
| `jsonl` | Code writes JSON-lines to a file (preferred) | No |
| `jsonl_stdout` | Code prints JSON-lines to stdout | No (just tail stdout) |
| `wandb` | Code uses `wandb.log` | `/train-run` reads via wandb API or tails `wandb/*/files/output.log` |
| `tensorboard` | Code uses `SummaryWriter` | `/train-run` parses event files via `EventAccumulator` |
| `stdout_regex` | Plain text prints; need pattern extraction | Yes — `stdout_extractor` field has the regex |

`done_signal` shapes:

```json
{"type": "record", "record_type": "done"}                     // jsonl record marker
{"type": "exit", "expect_record_at_epoch": "max_epochs - 1"}  // process exit + last record matches
{"type": "stdout_substring", "value": "Training complete"}    // text marker
{"type": "file_exists", "path": "<output_dir>/.done"}         // flag file
```

## Why the deltas

Training is **stream-emitting** and **selection-aware**. Infer/eval finish in one shot and report a final number; training runs for hours, emits metrics continuously, saves multiple checkpoints, and one of them must be picked as canonical. The extra fields capture exactly that behavior so `/train-run` can monitor health and finalize correctly.
