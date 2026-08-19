# recipe.md template (full worked example)

The canonical layout Step 9 should produce. All seven sections required; render an empty one with "—" rather than omitting it, because a missing section reads as "nothing to report" when it may mean "never checked".

**This example is in English because this file instructs an agent. The document you produce follows the user's language** (see SKILL.md → Interaction approach). Structure, section order, and the confidence markers carry over unchanged; prose gets translated.

Three rules the example demonstrates throughout:

1. **Confidence is inline, never flattened.** "read at `train.py:44`", "inferred", "the author said so" are three different claims and must not share one voice.
2. **"Not checked" is never rendered as "does not exist."** An unauthorized connector is a known unknown and belongs in the handover.
3. **The launch command is concrete.** Real paths from the chosen candidates, no `${}` placeholders — someone should be able to copy the block and run it.

---

```markdown
# Training readiness: detection-v2

**Can training start?** Yes — with two caveats that affect whether the results mean anything. Read §5 before trusting any metric.

- Data, weights, and launch command all resolved.
- **`degrades` ×2**: val split is unseeded (metrics drift between runs); `timm` is 3 minor versions ahead of the original.
- 3 open questions, none blocking.

## 1. What this trains

| | |
|---|---|
| Model | RT-DETR-R50, 80 classes |
| Dataset | COCO-format, 118k train / 5k val |
| Primary metric | `val_mAP` (max) — ⚠ **guessed**, see §6 |
| Inherited baseline | mAP 48.5 / AP50 67.3 — **`claimed`**, never reproduced. Not a target until verified (§6). |
| Provenance | inherited from the previous owner; no README in the repo |

## 2. Launch command

Runs on `server_4090`, where the data already lives.

```bash
ssh server_4090
cd ~/agent_space/mlclaw/projects/detection-v2/stages/training/code
mamba run -n mlclaw_detection_v2 \
  torchrun --nproc_per_node 1 tools/train.py \
    --config configs/rtdetr_r50.yaml \
    --data-root /data/coco2017 \
    --lr 1e-4 \
    --batch-size 8 \
    --output-dir <RUN_DIR>/output
```

`--lr 1e-4` is the **effective** value — the yaml says `3e-4` and `optim.py:44` overrides it.
`--seed` is deliberately absent: hardcoded at `train.py:12`, so passing it does nothing.

## 3. Sources swept

| Source | Status | Found |
|---|---|---|
| Code + git history | reachable | 47 commits; 3 messages carry intent (§6) |
| Local disk | reachable | no COCO copy on this machine |
| Local tracking leftovers | absent | no `wandb/` or `mlruns/` in the repo |
| GitHub | reachable | private repo; no issues, no releases |
| **Company docs** | **needs_auth** | **not checked** — Atlassian connector unauthorized. Jira probably holds why the target is 48.5. |
| S3 | skipped | user declined |
| W&B / MLflow | absent | no tracking call anywhere in the code |
| Compute | reachable | server_4090: 1×4090 24GB — code assumes 8 GPUs (§5) |

Company docs and S3 above are **unknowns, not absences**.

## 4. Data and weights

| Item | Chosen | Why |
|---|---|---|
| train/val data | `server:server_4090` → `/data/coco2017` | 19GB already there; copying it here would be pointless |
| pretrained backbone | `downloadable` → torchvision `ResNet50_Weights.IMAGENET1K_V1` | code default `pretrained/r50.pth` exists nowhere reachable |
| inherited ckpt | `local` → `~/handover/rtdetr_best.pth` | previous owner's best; metrics unverified (§1) |

The code-declared path `/data/coco2017` was `absent` locally but **matched on the 4090** — that's why training goes there rather than here.

## 5. Hazards

**`degrades` — runs fine, produces wrong or unstable numbers:**

| What | Where | Effect |
|---|---|---|
| val split via `random.sample()`, unseeded | `dataset.py:118` | different val set every run; two runs aren't comparable, and train/val overlap accumulates across runs |
| `timm` 0.9.12 → 1.0.3 | `poetry.lock:31` vs current env | default model configs changed between these versions; the architecture may not be the one that produced 48.5 |

**`risks` — conditional, and the condition holds here:**

| What | Where | Effect |
|---|---|---|
| `assert world_size == 8`; `lr` scaled by world size | `train.py:52`, `sched.py:20` | you have 1 GPU. Assert relaxed and `lr` passed pre-scaled — but this differs from how 48.5 was produced |

**Not a hazard, not a knob either:** `seed` is hardcoded at `train.py:12`. Changing it needs a code edit.

## 6. Open questions

**Blocking: none.** Training can start.

**Guessed — a value is in the config and may be wrong:**

- `primary_metric = val_mAP`. The code logs `mAP`, `AP50`, `AP75`, `mAP_s`, `mAP_l`, none marked primary; inferred from the best-checkpoint save condition at `train.py:196`. **Confirm this is what checkpoints should be selected on.**

**Unverified — plausible, unchecked:**

- The inherited ckpt's mAP 48.5. Re-running eval with `~/handover/rtdetr_best.pth` would validate the preprocessing chain, the data, the metric definition, and the environment in a single run. Until then it's a rumor, not a baseline.

**Absent — confirmed, stop looking:**

- No done signal. The script ends silently: no terminal record, no stdout marker, no flag file. `/train-run` falls back to "process exited and the last `val_epoch` reached `epochs - 1`".

**From git history — the author's own words, written at the time:**

- `a3f81c2` "revert mosaic aug, hurts mAP 2pt" — mosaic was tried and rejected
- `7d4e9b1` "hardcode num_classes=80 for now" — the *for now* never arrived
- `1f2a8e0` "fix lr scaling for 8gpu" — confirms the world-size coupling in §5

## 7. Environment

| | Original | Here | |
|---|---|---|---|
| python | 3.10.12 | 3.10.14 | ok |
| torch | 2.1.0+cu118 | 2.1.0+cu118 | ok |
| **timm** | **0.9.12** | **1.0.3** | ⚠ §5 |
| numpy | 1.24.3 | 1.26.4 | ok |
| CUDA | 11.8 | 11.8 | ok |

Original source: `poetry.lock` — resolved and exact, so this comparison is trustworthy.
Full freeze: `stages/training/env_original.txt`.

**Preprocessing** — must match any eval/infer stage of this model:
ImageNet normalization constants (`dataset.py:31`) · letterbox to 640×640, RGB (`dataset.py:44`) · labels 0-indexed, coco80, no background class (`dataset.py:66`)
```

---

## Why each section is where it is

- **Verdict first.** The one thing a reader wants is "can I start". Everything else is detail they may never need.
- **Launch command second, not last.** It's the most-used part of the document on every later visit.
- **Sources before hazards.** What you couldn't check bounds how much the hazard list is worth — an unread Jira may hold a hazard nobody recorded.
- **Environment last.** Needed only when reproduction fails, which is not most visits.

## Failure modes to avoid

| Wrong | Right |
|---|---|
| "Company docs: none found" | "Company docs: not checked — connector unauthorized" |
| "Baseline: mAP 48.5" | "Baseline: mAP 48.5 (`claimed`, unreproduced)" |
| "primary_metric: val_mAP" | "primary_metric: val_mAP ⚠ guessed — inferred from `train.py:196`" |
| `--data-root ${input.train_data}` | `--data-root /data/coco2017` |
| Hazards sorted by severity of sound | `degrades` first — those are the ones that corrupt results silently |
