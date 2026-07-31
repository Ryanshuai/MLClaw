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

### `provenance.json`

A sidecar next to the four config files. Records where values came from and which ones aren't settled. `/train-run` never reads it — it exists so a later reader can tell a read fact from a guess.

```json
{
  "source_mode": "inherited",
  "sources_checked": {
    "company_docs": { "status": "needs_auth", "needs": "Atlassian connector authorization",
                      "checked_at": "2026-07-30" },
    "s3":           { "status": "reachable", "what_found": "s3://bucket/datasets/coco2017/",
                      "checked_at": "2026-07-30" },
    "wandb":        { "status": "absent", "what_found": "no wandb/mlflow call in the code" }
  },
  "evidence": {
    "config.entry_command": "README.md:12",
    "output.metrics.done_signal": "train.py:210",
    "input.pairing": "dataset.py:44"
  },
  "unresolved": [
    { "key": "output.metrics.primary_metric", "status": "guessed",
      "why": "code logs 5 metrics with none marked primary; inferred val_mAP from the best-ckpt save condition at train.py:196" },
    { "key": "config.resources.gpu_memory_gb", "status": "blocking",
      "why": "no way to determine from code; needs one debug run or the user's knowledge" },
    { "key": "output.metrics.done_signal", "status": "absent",
      "why": "script just ends; no terminal record, no marker, no flag file" }
  ],
  "notes": []
}
```

`source_mode`: `authored` | `explored` | `inherited` — sets how much was excavated versus asked.

`sources_checked` — one entry per source the Step 0 sweep touched, keyed by source name. Entry: `{status, what_found?, needs?, checked_at?}`. `status`: `reachable` | `needs_auth` | `absent` | `skipped`. This block is **load-bearing, not a log**: Step 1c reads the local/S3/server findings instead of re-scanning, Steps 1d and 4d reuse the tracking connection instead of opening a second one, and Step 9 renders it into `recipe.md` verbatim so a handover states its own known unknowns. It is also the single place reachability is recorded — `output.json -> metrics.tracking` deliberately omits a reachability field so the two can't drift.

`status` enum — the distinction that matters is whether a (possibly wrong) value is sitting in the config:

| Value | Config holds | Action |
|---|---|---|
| `blocking` | nothing | ask now; the flow stops |
| `guessed` | an inferred value | **must be confirmed at Step 7 review** |
| `absent` | nothing, correctly | a conclusion — stop looking |
| `unverified` | a value from a README / the author's word | plausible, unchecked |

**`evidence` covers only the values with nowhere else to live.** `param_injection`, `preprocessing`, `hazards`, and `artifacts` `origin` all carry `evidence` inline already — don't duplicate. What's left is `entry_command`, `done_signal`, `primary_metric`, `pairing`, `checkpoints.path_pattern`, `resources.gpu_count`: precisely the values most often gotten wrong in unfamiliar code.

**`guessed` refines the "never guess a value" rule.** Leaving a field empty is right when empty is survivable — an empty field gets asked about, a wrong value gets used. But some fields can't be empty and still produce a runnable config (`primary_metric` drives checkpoint selection). For those, infer a value *and* record it as `guessed`, so Step 7 highlights it instead of presenting it as read from code.

### `config.json -> hazards`

Landmines that can't be expressed as a parameter. (A landmine that *is* a parameter belongs in `param_injection` with `overridable: false` — the distinction is exactly "can I change this by passing something".)

```json
[
  { "kind": "absolute_path", "impact": "blocks",
    "what": "data_root hardcoded to /home/prev_owner/data/coco",
    "evidence": "configs/base.yaml:7",
    "fix": "point at ${input.train_data}, or expose it as a CLI flag" },

  { "kind": "data_leakage", "impact": "degrades",
    "what": "val split drawn with random.sample() and no seed — overlaps train across runs",
    "evidence": "dataset.py:118",
    "fix": "fix the split seed, or materialize the split to a file" },

  { "kind": "world_size", "impact": "risks",
    "what": "asserts world_size == 8; lr is scaled by it",
    "evidence": "train.py:52, sched.py:20",
    "fix": "single-GPU needs lr adjusted and the assert relaxed" }
]
```

`impact` — this is the field that matters, it decides *when* someone has to act:

| Value | Meaning | When enforced |
|---|---|---|
| `blocks` | won't run until fixed | surfaced **before** the user fills the remaining files |
| `degrades` | runs fine, produces plausible-but-wrong results | warned at init and echoed before launch |
| `risks` | breaks only under a condition (wrong GPU count, offline machine) | echoed before launch, when the condition is knowable |

`kind`: `absolute_path` | `dir_structure` | `dependency_version` | `world_size` | `nondeterminism` | `network_required` | `platform` | `data_leakage` | `other`.

`degrades` is the class this whole schema exists for. `blocks` announces itself the first time you run; `degrades` never does — a `data_leakage` entry means every metric this code produces is inflated, with nothing anywhere to indicate it.

### `config.json -> env_snapshot`

What the original author had installed — not what the code declares it needs (`required_packages`).

```json
{
  "source": "poetry.lock",
  "python": "3.10.12",
  "key_packages": { "torch": "2.1.0+cu118", "torchvision": "0.16.0", "numpy": "1.24.3",
                    "transformers": "4.35.0" },
  "cuda": "11.8",
  "evidence": "poetry.lock:12-48",
  "full_snapshot_path": "stages/training/env_original.txt"
}
```

`source` enum, ordered by how much it proves:

| Value | Evidence quality |
|---|---|
| `tracking_backend` | strongest — an actual `pip freeze` captured at training time (mlflow `conda.yaml`, wandb `requirements.txt`) |
| `poetry.lock` / `uv.lock` / `Pipfile.lock` / `conda_yaml` | strong — resolved, exact versions |
| `dockerfile` | strong if the base image is tagged, weak if `:latest` |
| `requirements_pinned` | moderate — `==` pins, but transitive deps unresolved |
| `requirements_unpinned` | weak — `torch>=1.10` proves almost nothing |
| `readme` | weak — "tested with torch 1.13" is a claim, not a snapshot |
| `none` | no record exists. A conclusion, not a gap. |

`key_packages` is deliberately not a full freeze. Record the ones where a version bump silently changes numerics or defaults — deep-learning framework, CUDA, numpy (2.0 was breaking), and model-definition libs (`transformers`, `timm`, `mmcv`). Put the complete list in a file and reference it via `full_snapshot_path`.

`/train-run` diffs this against the run's own captured env (`run.json -> env`) and warns on key-package mismatches. Same code, same data, different torch → different numbers; without this field the natural suspect is the code.

### `input.json` / `artifacts.json` -> `candidates`

The middle layer between "what the code needs" (`items`) and "what this run used" (`sources`). Machine-specific: re-scan after moving hosts.

```json
{
  "items": {
    "train_data": [
      { "location": "code_default", "path": "/data/coco2017", "match": "absent",
        "notes": "original author's path; layout reference: images/ + annotations/instances_train2017.json",
        "evidence": "configs/base.yaml:7" },
      { "location": "local", "path": "/home/shuai/data/coco", "match": "ok",
        "notes": "118k imgs, annotations consistent with pairing=coco_json",
        "host": "this", "scanned_at": "2026-07-30", "samples": 118287 },
      { "location": "server:server_4090", "path": "/data/coco2017", "match": "ok",
        "notes": "on the 4090 box — prefer training there over a 19GB transfer" },
      { "location": "s3", "path": "s3://bucket/datasets/coco2017/", "match": "ok",
        "notes": "~19GB, download or stream", "needs": "aws credentials" },
      { "location": "downloadable", "path": "https://cocodataset.org/#download",
        "match": "ok", "notes": "fallback when no local copy exists" }
    ]
  }
}
```

`location`: `code_default` | `local` | `s3` | `server:<key>` | `downloadable` | `registry` (weights only).
`match`: `ok` | `mismatch` | `absent`.

`location` drives `/train-run` behavior, it isn't decoration: use directly / fetch first / **run on that host instead**. The `code_default` entry is listed even when `absent` — it's the layout reference and the yardstick for the others.

### `artifacts.json -> items.<name>.origin`

For items inherited from someone else — a handed-over checkpoint, a paper's released weights. Sits alongside the normal item fields (`type` / `format` / `description` / `resource`).

```json
"author_best_ckpt": {
  "type": "checkpoint", "format": ".pth",
  "description": "handover ckpt from the previous owner",
  "origin": {
    "why": "paper Table 2 row 3 config, plus warmup — kept because it beat the no-warmup run",
    "metrics": { "mAP": 48.5, "AP50": 67.3 },
    "scope": { "dataset": "COCO val2017", "split": "val", "samples": 5000 },
    "confidence": "claimed",
    "config": { "lr": 3e-4, "epochs": 100, "batch_size": 32 },
    "env": { "torch": "2.1.0", "timm": "0.9.12" },
    "source": "wandb:someone/detection-v2/run_abc123"
  }
}
```

`confidence` enum:

| Value | Means |
|---|---|
| `verified` | you re-ran eval with this ckpt and got these numbers |
| `claimed` | the author's / wandb's recorded number, not re-checked |
| `asked` | told verbally, no artifact backs it |

**`scope` and `confidence` are both required.** Without scope, the number can't be compared to anything (see `lifecycle/references/run-mechanics.md` "Metric comparability"). Without confidence, a `claimed` number gets treated as ground truth — and inherited unverified metrics are the most damaging fake metrics there are, because everything downstream is calibrated against them. Authors misremember, hand over the wrong file, or forget TTA was enabled.

**Promote `claimed` → `verified` by re-running eval with this ckpt.** That single run validates the preprocessing chain, the data, the metric definition, and the environment simultaneously — it's the best acceptance test a handover has. Record the verifying run's ID in `source` when you do.

`why` is the decision trail in its obtainable form. Don't try to reconstruct a full search history; record why each surviving checkpoint exists. That's what the previous owner can actually still tell you.

### `input.json -> preprocessing`

What happens to an input before the model sees it. Read out of code, so every block carries `evidence`.

```json
{
  "normalization":   { "mean": [0.485,0.456,0.406], "std": [0.229,0.224,0.225],
                       "source": "imagenet", "evidence": "dataset.py:31" },
  "input_layout":    { "size": [640,640], "resize_mode": "letterbox",
                       "channel_order": "RGB", "evidence": "dataset.py:44" },
  "label_transform": { "index_base": 0, "class_mapping": "coco80",
                       "background_class": false, "evidence": "dataset.py:66" },
  "augmentation":    [ { "op": "RandomHFlip", "p": 0.5 } ],
  "pipeline_evidence": "dataset.py:28-91"
}
```

`normalization.source`: `imagenet` | `computed` | `generic` | `custom`.
`resize_mode`: `letterbox` | `stretch` | `center_crop` | `random_crop` | `pad`.

**Cross-stage contract**: `normalization`, `input_layout`, and `label_transform` must be identical in training and in eval/infer. `augmentation` is train-only — non-empty in an eval/infer stage means a bug, not a variation. Leave a field empty rather than filling a framework default: a blank gets asked about, a wrong constant gets used.

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
