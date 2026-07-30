# Code Detection Patterns

Reference for recognizing training-related patterns in user code. Read this when sweeping sources in Step 0 and when analyzing code in Steps 1 and 4.

## Source reachability (for Step 0)

Whether a source is usable, in order of checking:

| Check | Verdict |
|---|---|
| ToolSearch finds the tool | usable now |
| Not found, but `claude mcp list` shows `✔ Connected` | **restart the session.** MCP tools register at startup, so a server authorized mid-session stays invisible. That's the entire fix — no reconfiguration. |
| `claude mcp list` shows `! Needs authentication` | `needs_auth`. `claude.ai *` servers are authorized in claude.ai connector settings; others via `claude mcp` or `/mcp` in an interactive session. OAuth cannot be completed from a non-interactive session — say where to do it and move on. |
| Absent from `claude mcp list` entirely | Claude Code has no such source. Some claude.ai connectors are web-only — GitHub Integration is one. Substitute (`gh` CLI, or an API token plus WebFetch) instead of waiting on authorization. |

An authorized document connector usually carries write scopes next to the read ones (`write:page:confluence`, `write:jira-work`). The sweep never uses them — see the read-only rule in Step 0.

## Training entry-point patterns

- Standalone training scripts: `train.py`, `pretrain.py`, `finetune.py`, `main.py`, `run_training.py`
- Training mode in dual-purpose script: `--train`, `--mode train`, `if args.do_train:`
- Wrapper scripts: `train.sh`, `run.sh`, `bash scripts/pretrain.sh`
- Distributed launchers wrapping it:
  - `torchrun --nproc_per_node N script.py` (PyTorch native)
  - `accelerate launch script.py` (HuggingFace accelerate)
  - `deepspeed --num_gpus N script.py` (DeepSpeed)
  - `python -m torch.distributed.launch ...` (legacy)
- Frameworks with built-in CLI:
  - `python -m transformers.trainer ...`
  - `lightning fit --config ...`
  - `mlflow run ...`

When entry is wrapped, capture the **outermost** invocation as `entry_command` (what the user actually types).

## Distributed setup detection

To fill `config.json -> resources.distributed`:

| Pattern | distributed |
|---|---|
| `torchrun --nproc_per_node N` | `ddp` |
| `accelerate launch` + `accelerate config` showing `multi_gpu` | `ddp` |
| `accelerate config` showing `fsdp` | `fsdp` |
| `deepspeed --num_gpus N` + zero stage 1 config | `deepspeed_zero1` |
| `deepspeed` + zero stage 2 / 3 config | `deepspeed_zero2` / `deepspeed_zero3` |
| `tensor_parallel_size > 1` (Megatron, vLLM) | `tensor_parallel` |
| Plain `python script.py` | `single_gpu` |

Cross-check by reading `accelerate config` output (`~/.cache/huggingface/accelerate/default_config.yaml`) or the deepspeed JSON config.

## GPU resource detection

- `gpu_count`: from `--nproc_per_node`, `--num_gpus`, `accelerate config -> num_processes`, or hard-coded
- `gpu_memory_gb`: read user-supplied estimate; or compute from model param count + batch_size × seq_len × dtype_bytes (rough). Don't auto-fill; ask user.

## Preprocessing chain detection (for Step 1b)

Where the chain lives, by framework:

| Framework | Look at |
|---|---|
| plain PyTorch | `Dataset.__getitem__`, `transforms.Compose([...])` at construction, `collate_fn` |
| torchvision | `transforms` / `v2` pipelines; `weights.transforms()` when using pretrained presets |
| albumentations | `A.Compose([...])`, note `A.Normalize(mean, std)` and `ToTensorV2` position |
| HuggingFace | `AutoImageProcessor` / `AutoTokenizer` config (`preprocessor_config.json` carries mean/std/size) |
| mmdetection / mmcv | `train_pipeline` / `test_pipeline` lists in the config; `img_norm_cfg` |
| Lightning | `LightningDataModule.setup` / `train_dataloader` |

**`channel_order` is decided by the reader, not the transform.** Trace it:

```python
img = cv2.imread(path)                      # BGR
img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)  # → RGB (look for this line; its absence is the finding)
img = Image.open(path).convert("RGB")       # RGB
```

A pipeline whose `Normalize` uses ImageNet constants while the reader hands over BGR is a real (and very common) bug in research code — record what the code does, and flag the discrepancy rather than quietly "correcting" it.

**`label_transform`** — look for a class-name list (its length tells you the count), an id-remapping dict (`coco91_to_coco80`), `num_classes` vs actual label range, and whether index 0 is background. Cross-check `num_classes` against `len(classes)`: an off-by-one between them is the background class, declared or not.

**`normalization.source`** — `[0.485, 0.456, 0.406] / [0.229, 0.224, 0.225]` is ImageNet; `[0.5,0.5,0.5]/[0.5,0.5,0.5]` is a generic default; anything else was probably computed from a specific dataset — ask which, because reusing it on different data is a silent quality loss.

## Data and weight discovery (for Step 1c)

**1. Code-declared path** — grep the config and code for path-shaped values:

```bash
grep -rnE '(data_root|dataset_dir|img_dir|ann_file|train_path|root)\s*[:=]' \
     --include=*.py --include=*.yaml --include=*.json <code_dir>
grep -rnE '(pretrained|weights|ckpt|checkpoint|resume)\s*[:=].*\.(pth|pt|ckpt|safetensors|bin)' \
     --include=*.py --include=*.yaml <code_dir>
```

Also check README / scripts for example invocations — they often carry the real paths the author used even when the config has placeholders.

**2. Local disk** — for each `resources.json -> local.base_paths` entry, look for structures matching `pairing`:

```bash
find <base> -maxdepth 3 -type d \( -name 'annotations' -o -name 'labels' -o -name 'images' \)
find <base> -maxdepth 3 -name 'instances_*.json' -o -name '*.coco.json'
```

**3. Downloadable** — decide from the dataset / model name in `items` whether a canonical public source exists (COCO, ImageNet, HF hub id, torchvision weights enum, the paper's release URL). Judging this costs nothing; only the download is slow.

**4. S3** (ask first) — `aws s3 ls s3://<bucket>/ --recursive --max-items 200`, narrowed by a prefix guess from the dataset name. Never run an unbounded recursive listing on a large bucket.

**5. Remote servers** (ask first) — `ssh <host> "ls -d <likely_roots>"` before any `find`; a bare `find /` on a fileserver is unacceptable. Reuse the code-declared path as the first guess — the author's path often still exists on the machine they trained on.

Judging `match`:

| Verdict | Means |
|---|---|
| `ok` | structure matches `items` + `pairing`; usable as-is |
| `mismatch` | data exists but the shape is wrong (YOLO txt where code wants COCO json; missing `annotations/`) — say what conversion is needed |
| `absent` | path doesn't resolve here. Still list it when it's the code-declared one. |

Record sample counts when cheap (`ls images | wc -l`, `jq '.images | length' ann.json`) — that number is what lets the user distinguish the full set from a subset, and it later becomes the run's `scope`.

## Hazard scanning (for Step 1e)

Each grep below maps to one `kind`. Read the hit before recording it — a match is a candidate, not a finding.

```bash
# absolute_path — someone else's filesystem baked in
grep -rnE '["'"'"']((/home|/data|/mnt|/scratch|/workspace)/|[A-Z]:\\)' \
     --include=*.py --include=*.yaml --include=*.json --include=*.sh <code_dir>

# network_required — needs to reach the internet at runtime (fatal on an offline cluster)
grep -rnE 'urlopen|requests\.(get|post)|wget |curl |from_pretrained\(|hub\.load|torch\.hub|snapshot_download' \
     --include=*.py <code_dir>

# nondeterminism — absence is the finding here
grep -rnE 'manual_seed|random\.seed|np\.random\.seed|seed_everything|cudnn\.(benchmark|deterministic)' \
     --include=*.py <code_dir>

# world_size — hard assumptions about GPU count
grep -rnE 'world_size\s*[=!]=|nproc_per_node|gpu_ids|CUDA_VISIBLE_DEVICES|\.cuda\([0-9]' \
     --include=*.py --include=*.sh <code_dir>

# platform — shells out, or assumes a specific OS / filesystem
grep -rnE 'os\.system|subprocess\.(run|call|Popen)|/dev/shm|nvidia-smi|apt-get' \
     --include=*.py <code_dir>
```

**`nondeterminism` is the inverted one**: no `manual_seed` anywhere means two identical runs give different numbers, which makes every A/B comparison and every `/train-tune` conclusion noisy. `cudnn.benchmark = True` is the same story in milder form. Record the *absence*.

**`dependency_version`** — cross-reference `env_snapshot.key_packages` (Step 1d) against what's installed now. A `timm` or `transformers` major-version gap is a `degrades` hazard: same model name, different default config, no error.

**`dir_structure`** — read the `Dataset` for layout it takes for granted: hardcoded subdirectory names (`images/`, `labels/`), a filename convention (`{id:06d}.jpg`), an expectation that ann files sit as siblings. Check it against the winning `candidates` entry from Step 1c.

**`data_leakage`** — grep won't settle this; read the split logic:

```bash
grep -rnE 'train_test_split|random_split|\.sample\(|shuffle\(|\[:split|val_ratio|holdout' \
     --include=*.py <code_dir>
```

Three questions: is the split seeded (unseeded → different val set every run, and eventually train/val overlap across runs)? is it materialized to a file, or recomputed each time? are normalization statistics computed over the whole set before splitting (that leaks val into train)? Any "yes-but" here is `impact: degrades` — the metrics come out inflated and nothing indicates it.

## Original environment detection (for Step 1d)

Search in descending order of what the source actually proves — stop at the first strong one:

```bash
# strongest: a pip-freeze captured at training time by the tracking tool
find <code_dir> \( -name 'conda.yaml' -o -name 'requirements.txt' \) \
     \( -path '*wandb*' -o -path '*mlruns*' -o -path '*artifacts*' \)

# strong: resolved lock files
ls poetry.lock uv.lock Pipfile.lock environment.yml conda-lock.yml 2>/dev/null

# strong-ish: pinned base image (weak if the tag is :latest)
grep -E '^FROM' Dockerfile 2>/dev/null

# moderate: == pins
grep -E '==' requirements.txt 2>/dev/null

# weak: prose claims
grep -inE 'tested with|torch |cuda |python 3\.' README* 2>/dev/null
```

Which packages to record in `key_packages` — the ones where a version bump changes results **without erroring**:

| Package | What drifts silently |
|---|---|
| `torch` / `tensorflow` / `jax` | attention backend selection, default dtypes, reduction order → small numeric differences that compound over training |
| CUDA / cuDNN | kernel selection, non-determinism characteristics |
| `numpy` | 2.0 changed behavior and removed aliases; some breaks are loud, some aren't |
| `timm` / `transformers` / `mmcv` / `ultralytics` | **default model configs change between versions** — same model name, different architecture details or pretrained weights. This is the worst class: nothing errors, the model just isn't the one the paper used. |

Loud breaks (import errors, removed APIs) don't need this field — you'd find them on the first run. This field exists for the silent ones.

## Tracking backend detection (for Step 4d)

```bash
grep -rnE 'wandb\.init|WANDB_(PROJECT|ENTITY)|mlflow\.(set_tracking_uri|set_experiment)|SummaryWriter\(|Task\.init|neptune\.init|comet_ml' \
     --include=*.py --include=*.sh --include=*.yaml <code_dir>
```

**Check local leftovers first — they're free and need no credentials:**

```bash
ls -d wandb/ mlruns/ lightning_logs/ runs/ outputs/ 2>/dev/null
```

| Leftover | What's in it |
|---|---|
| `wandb/run-*/files/` | `config.yaml` (that run's full config) + `wandb-summary.json` (final metrics) — a complete offline record per run |
| `mlruns/` | mlflow's local store; params/metrics/tags readable straight off disk |
| `lightning_logs/version_*/` | `hparams.yaml` + tfevents |

If the author trained on this machine, the entire history may already be sitting in the repo. Check before asking for an API key.

`locator` shape per backend:

| backend | locator | how to pull |
|---|---|---|
| `wandb` | `{entity, project, sweep_id?}` | `wandb.Api().runs("<entity>/<project>")` |
| `mlflow` | `{tracking_uri, experiment_name}` | `mlflow.search_runs(experiment_names=[...])` |
| `tensorboard` | `{log_dir}` | `EventAccumulator` per run dir |
| `clearml` | `{project_name, task_name}` | `Task.get_tasks(project_name=...)` |

An entity/project hardcoded in the code is the locator even when you can't reach it — record it with `reachable: false` and `needs`. That's a todo. Only `backend: "none"` (no tracking call exists anywhere) is a conclusion.

## Param shadowing detection (for Step 2 and Step 3)

A config file value is only the *declared* value. What matters is the value at the moment the code uses it. Trace each candidate param from declaration to use, and look for these shadowing patterns:

```python
# 1. Post-parse literal — the flag exists but is dead
args = parser.parse_args()
args.lr = 1e-4                       # --lr is silently discarded

# 2. Recompute from other params
cfg.warmup_steps = int(0.03 * cfg.epochs * steps_per_epoch)   # override discarded

# 3. Scaling by world size — the value you pass is not the value used
lr = cfg.lr * dist.get_world_size()  # single-GPU vs 8-GPU differ 8×

# 4. Constructor literal ignoring config
optimizer = Adam(model.parameters(), lr=3e-4)   # cfg.lr never read

# 5. Framework default winning
TrainingArguments(output_dir=out)     # learning_rate defaults to 5e-5, cfg.lr unused

# 6. Later config layer winning
cfg = OmegaConf.merge(base_cfg, exp_cfg)   # whichever is merged last wins
```

Cheap first pass — for each param name, list every assignment site and compare against the read site:

```bash
grep -rn "\blr\b\|learning_rate" --include=*.py --include=*.yaml <code_dir>
```

If a param is assigned more than once, the **last write before the read** is the effective value. Read the surrounding lines rather than guessing from the grep hit alone.

Then classify:

| What the trace shows | `via` | `overridable` |
|---|---|---|
| Flag / yaml key read once, used directly | `cli` / `yaml` | `true` |
| Flag exists but overwritten after parse (pattern 1, 4, 5) | `hardcoded` | `false` |
| Computed from other params (pattern 2, 3) | `derived` | `false` |
| Read from env var | `env` | `true` |
| Multiple config layers merged (pattern 6) | `yaml` | `true` — record the *winning* layer as `key` |

**Verify instead of assume when it's cheap.** For an argparse entry point, `python train.py --help` confirms which flags exist. For omegaconf/hydra, printing the resolved config at startup settles the whole question at once — if the code doesn't already do that, it's a one-line addition the user can run once in debug mode. A confirmed override beats a traced one.

Pattern 3 (world-size scaling) deserves a note even when `overridable` is `true` for the base value: the effective lr depends on `resources.gpu_count`, so a run reproduced on a different GPU count silently trains differently. Record it in `note` and flag it as a risk.

## Log writer detection (for Step 4a)

| Code pattern | log_format | Notes |
|---|---|---|
| `f.write(json.dumps({...}) + "\n")` to a `.jsonl` path | `jsonl` | Preferred. Capture log_path. |
| `with open("train_log.jsonl", "a") as f: f.write(...)` | `jsonl` | Same |
| `print(json.dumps({...}))` only | `jsonl_stdout` | Tail stdout |
| `import wandb; wandb.log({...})` | `wandb` | Capture project / run name conventions |
| `from torch.utils.tensorboard import SummaryWriter; w.add_scalar(...)` | `tensorboard` | Capture log_dir pattern |
| `pytorch_lightning.loggers.{TensorBoardLogger,WandbLogger,CSVLogger}` | follow corresponding | Lightning auto-saves under `lightning_logs/` |
| `transformers.Trainer` default | `wandb` if `report_to="wandb"`, else `tensorboard`, else CSV | HF Trainer auto-logs |
| Plain `print(f"...")` only | `stdout_regex` | Build extractor in 4b |

When multiple coexist (e.g., HF Trainer logs to both wandb and CSV), pick the **most structured** format MLClaw can read directly — usually jsonl > wandb-export > tensorboard > stdout regex.

## Record-type detection (for Step 4c)

When `log_format = jsonl` or `jsonl_stdout`, scan the writer for distinct record shapes. Common patterns:

```python
# Per-step training metric (high frequency)
log({"type": "train_step", "step": step, "loss": ..., "lr": ...})

# Per-epoch validation metric (low frequency)
log({"type": "val_epoch", "epoch": epoch, "val_loss": ..., "val_acc": ...})

# Checkpoint save event
log({"type": "ckpt_saved", "path": ckpt_path, "epoch": epoch})

# Terminal record
log({"type": "done", "best_val_acc": ...})
```

Or without explicit `type`, but distinguishable by which fields are present (loss vs val_loss). Prefer `type`-tagged code.

## Checkpoint pattern detection (for Step 6a)

Look for save calls and reconstruct the filename pattern:

| Save call | Pattern |
|---|---|
| `torch.save(state, f"{out}/checkpoint-{step}.pt")` | `<output_dir>/checkpoint-{step}.pt` |
| `torch.save(state, f"{out}/best.pt")` if val improved | `<output_dir>/best.pt` |
| `accelerator.save_state(f"{out}/epoch_{epoch}")` | `<output_dir>/epoch_{epoch}/` (directory) |
| `Trainer.save_model(f"{out}/checkpoint-{step}")` (HF) | `<output_dir>/checkpoint-{step}/` |
| `trainer.save_checkpoint(f"{out}/last.ckpt")` (Lightning) | `<output_dir>/last.ckpt` |

Also capture the **selection logic** — does the script save "best" by tracking a metric? Look for code like:

```python
if val_acc > best_acc:
    best_acc = val_acc
    torch.save(state, "best.pt")
```

That confirms the selection metric (`val_acc`, direction `max`) for Step 6b.

## Done-signal detection (for Step 5c)

Look for:

| Pattern in code | done_signal shape |
|---|---|
| Final `log({"type": "done", ...})` after epoch loop | `{type: "record", record_type: "done"}` |
| `if epoch == cfg.epochs - 1:` then save final ckpt and exit | `{type: "exit", expect_record_at_epoch: "max_epochs - 1"}` |
| `print("Training complete")` at end | `{type: "stdout_substring", value: "Training complete"}` |
| `Path(f"{out}/.done").touch()` | `{type: "file_exists", path: "<output_dir>/.done"}` |

If none of the above is present (script just ends silently), recommend the user add a `{"type": "done"}` jsonl record. This is a one-line change with high downstream value (`/train-run` won't have to guess when training finished).
