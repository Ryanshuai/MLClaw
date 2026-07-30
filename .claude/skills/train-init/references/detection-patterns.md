# Code Detection Patterns

Reference for recognizing training-related patterns in user code. Read this when analyzing code in Step 1 and Step 4.

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
