---
name: train-init
description: >
  Use this skill to analyze training code and configure the training stage — including taking over
  someone else's code, where it sweeps every reachable source (repo, git history, company docs, S3,
  W&B, compute) and records which values were read from code versus guessed. Triggers when the user
  wants to set up training for a model: what the script needs (data, labels, pretrained weights),
  what it produces (checkpoints, logs, streaming metrics), how it signals completion, which params
  actually take effect, and what landmines the code carries. Produces 4 JSON configs plus a
  provenance sidecar and a recipe.md handover document. Also covers **setting up a fine-tune** —
  configuring a repo whose training starts from a pretrained or foreign base model, where the base,
  the frozen/trainable surface (LoRA rank, `target_modules`, `freeze_backbone`) and whether the code
  actually honors them are all part of what Step 1 has to establish. Use for: "analyze training
  code", "set up training", "configure training stage", "what does this training script log", "take
  over this repo", "接手训练代码", "分析训练代码", "配置训练", "初始化train", "配一下微调",
  "微调要怎么设". Not for running training (use train-run) or evaluation (use eval-init).
---

# /train-init — Training Stage Initialization

Analyze training code and fill 4 JSON config files, a `provenance.json` sidecar, and a `recipe.md` handover document in `stages/training/`. The goal is to understand what the code needs, what it streams during a long run, what it produces, and **how much of that you actually know versus inferred** — so `/train-run` can launch, monitor, and select checkpoints correctly, and so a person can tell months later which numbers to trust.

## How this skill works

The user brings training code (`train.py`, `pretrain.py`, `accelerate launch ...`, or a training mode inside a larger codebase). You read it, figure out:

- **Inputs**: train/val data, labels, optional pretrained weights
- **Streaming output**: how metrics are emitted during the run (jsonl / stdout regex / wandb / tb), and at what granularity (per step / per epoch)
- **Terminal output**: which checkpoints get saved, how to pick the "best" one, what signals "training is done"

…and capture all of that in 4 structured JSON files.

These files are **mostly schema** — WHAT the code needs and produces. Two deliberate exceptions, both specific to training:

- **`candidates`** (in `input.json` / `artifacts.json`) — init records which of the located data and weights actually fit, and how well. Training data is a project-level asset that rarely changes, so settling it here is what makes "init is done" a real state rather than one that collapses the first time you try to launch. It also locks the dataset for `/train-tune`, whose whole comparability premise is that every trial saw the same data. `/infer-init` keeps the original rule, since inference inputs genuinely do change every run — it calls `/discover` per run instead, if at all.

  **`/eval-init` has the same case for candidates and reaches them differently**: the locating half is `/discover`, which it calls rather than growing a second copy of the sweep. What is genuinely eval-specific is the `match` judgment against its own `items` + ground-truth `pairing`.
- **`preprocessing`** (in `input.json`) — the transform chain, read out of the code. It's a cross-stage contract, not a per-run choice.

Which candidate a given run actually used stays per-run (the run's `sources.json` snapshot).

## Interaction approach

Ask one question at a time. Training has more knobs than infer/eval (lr, bs, epochs, optimizer, scheduler, mixed precision, checkpoint policy …) — overwhelming the user with all of them at once is the failure mode. Ask one, record, then the next. **And only what only they know** — a value you can read is not a question, and a value nobody has is recorded absent rather than asked for: CLAUDE.md "Decide what evidence can decide".

**Language.** This file, the schemas, and every example in them are English — they're instructions for an agent. Everything a *human* reads follows the user's language: the questions you ask, free text you author (`notes`, `unresolved.why`, `hazards.what`, `origin.why`), and `recipe.md`. A Chinese-speaking user should not be handed an English handover document. Enumerated values, keys, and `path:line` evidence stay as-is in every language.

## On entry

Follow `lifecycle/references/skill-graph.md` -> "Workflow State Protocol": push to stack, check dependencies (project.json exists, code available), locate project, resolve code directory.

**Settle `provenance.json -> source_mode` first** — it decides whether the steps below ask or excavate. Infer a default and confirm in one line rather than asking cold:

| Signal | Likely mode |
|---|---|
| `project.json -> stages.training.code_source.source` is `github`, or `git log` authors aren't the user | `inherited` |
| an `/explore-*` output exists for this project | `explored` |
| local source, git authored by the user | `authored` |

`authored` → the user knows the answers, so ask instead of inferring. `inherited` → dig (code, git history, tracking backend) and record evidence, because nobody can confirm it for you. `explored` → read the upstream output first; most of it is already structured.

**You may have been called from the data line.** Both `/data-freeze` and `/data-check` suggest this skill, so the user may arrive having *just* frozen a snapshot — in which case that snapshot is the training input, and re-deriving it from directory listings in Step 1c throws away the one thing they came here having done. Look at `{PROJECT}/datasets/` before asking anyone where the data is; Step 0 has the row that does it.

## Output: 4 JSON files + provenance sidecar + recipe

| File | What it captures |
|------|-----------------|
| `config.json` | Entry command, config format, framework, **resource requirements**, managed params + **`env_snapshot`** (what the original author had installed) |
| `artifacts.json` | Static inputs — pretrained backbone, tokenizer, base ckpt for fine-tuning + **`candidates`** (where each can actually be obtained) |
| `input.json` | Dynamic inputs — train + val data with labels + **`candidates`** + **`preprocessing`** (the transform chain, a cross-stage contract) |
| `output.json` | Checkpoints, **log stream format**, **streaming metrics schema**, **primary metric + done signal** + **`metrics.tracking`** (where the author's run history lives) |
| `provenance.json` | Sidecar — `source_mode`, `sources_checked` (the Step 0 sweep), `evidence` for values with no inline evidence field, and `unresolved` (what's guessed / blocking / absent). Not read by `/train-run`; it's how a later reader tells a read fact from a guess. |
| `recipe.md` | Human-readable output of Step 9 — readiness verdict, the concrete launch command, what was and wasn't checked, hazards, open questions. The handover document. |

For item/source schemas and type classification, read `references/schemas.md`. The schemas are mostly inherited from eval-init — only the deltas are documented.

## Step 0: Sweep the Sources

Before analyzing anything, find out **what you can see** — the way a person taking over a project starts by checking which systems they have access to. Organize by source, not by element: each backend gets contacted once here, and every credential gap surfaces together rather than ambushing you at Step 4. Results → `provenance.json -> sources_checked`.

**The data half of this sweep is not this skill's.** Finding out what data exists is `/discover`, and it is invoked here as a sub-skill (utility pattern, like `/resources`). Two reasons it does not live here: `/eval-init` and `/infer-init` need exactly the same excavation and would otherwise each grow their own copy; and a lead is *longer-lived than an init* — access arrives weeks later, and `discovery/leads.json` is what carries the unresolved ones forward, where a `provenance.json` written once does not. This sweep keeps the model-side sources: weights, params, tracking backend, compute, hazards.

| Source | What to look for | How | Default? |
|---|---|---|---|
| **Code + git history** | the repo, `git log` (commit messages carry intent: `revert aug, hurts mAP 2pt`) | local | yes |
| **This project's own data line** | frozen snapshots this training could cite; batches still out with somebody | `phase.py phase --project {PROJECT}` + `handoff.py status --project {PROJECT} --open-only` — **records only, no network** | yes |
| **Local disk** | data, weights | `resources.json -> local.base_paths` | yes |
| **Local tracking leftovers** | `wandb/`, `mlruns/`, `lightning_logs/` — past runs cached on disk, **no credentials needed** | `ls` in the repo | yes |
| **GitHub** | repo reachable? issues / PRs / releases carrying context or weights | `gh` CLI | yes, cheap |
| **Company docs** | the project's real context — target metrics, annotation spec, who did it before, which bucket + which account | MCP connector (Confluence / Notion / M365 / …) | **ask** — needs authorization |
| **S3 / object store** | does the bucket even exist; what's in it | `aws s3 ls` | **ask** — slow, needs credentials |
| **W&B / MLflow** | project reachable? how many past runs? | API | **ask** — needs credentials |
| **Compute** | how many machines, how many GPUs, free right now? | `resources.json -> servers.*` + `nvidia-smi` | **ask** — hosts may be down |

Do the `yes` rows unprompted, then show what you found and ask **once** for the rest as a single question — not four separate ones.

**Detect capabilities; don't assume them.** ToolSearch for the tool (`confluence` / `jira` / `notion` / `sharepoint` / `wiki`) — found means usable now. **"Connected" is not the same as "callable here":** MCP tools register at session start, so a server authorized afterwards shows connected while its tools stay absent, and the fix is a session restart, not reconfiguration. `references/detection-patterns.md` → "Source reachability" has the full decision table.

**This sweep is read-only.** Document connectors usually grant write scopes next to the read ones — an authorized Atlassian connector typically carries `write:page:confluence`, `write:comment:confluence`, and `write:jira-work`. Never touch them here. Creating a page or commenting on a ticket in order to answer a question about your own project is modifying someone else's system as a side effect, and it's visible to their whole team. Writing to a company system happens only when the user asks for it in those words (e.g. "publish the recipe to Confluence"), never as part of init.

Search scope also needs restraint: query for this project by name / repo / dataset, not a broad crawl of every space. You're looking for the few pages and tickets about *this* model, and a wide sweep both wastes time and pulls in material that has nothing to do with the task.

Record each source's `status` honestly — the difference decides whether an agent should ever look again:

- `reachable` — connected, content extracted
- `needs_auth` — exists but credentials or authorization are missing. **A todo, not a dead end.** Name what's missing in `needs` and say where to fix it; never let this decay into `absent`, because "couldn't check" and "doesn't exist" are different facts and only the second one lets you stop wondering.
- `absent` — confirmed not to exist (no wandb call anywhere in the code; the bucket 404s; no `datasets/` in the project, so nothing here was ever censused). **A conclusion — stop searching.**
- `skipped` — the user declined.

Later steps consume this block instead of redoing the work: Step 1c reads the local/S3/server findings rather than re-scanning, and Steps 1d and 4d reuse the tracking connection rather than opening a second one.

## Step 1: Analyze Code

Everything read out of the code happens here, in five sub-steps: **1a** the mechanics of running it, **1b** the preprocessing chain, **1c** where the data and weights actually are, **1d** the original environment, **1e** the landmines. They share one input (the code) and one working mode (read, then record with `path:line` evidence), which is why they're one step rather than five.

### 1a. Entry point, framework, requirements

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
  - **If there is a base ckpt, this stage is a fine-tune, and that changes what `output.json` has to carry.** `/train-run` will measure that base on this run's data before launching, so work out *how* now, while the eval path is in front of you, and record it as `output.json -> baseline_measurement`: the command or API call, and the settings dict it must use. `null` is a legitimate answer with a reason — what is not legitimate is leaving it unasked, because at launch time the question surfaces as "skip it, we can measure later", and later never comes. Rationale and the two refusals it feeds: `lifecycle/references/run-mechanics.md` → "Baseline measurement (fine-tune only)".
  - Fill the settings from what the **base checkpoint itself records**, not from the library's defaults — most frameworks bury evaluation-shaping flags (mask overlap, NMS iou, letterboxing, max detections) in defaults that differ from what the weights were trained under, and a measurement taken at the default produces a plausible number that is not comparable to anything published for those weights.
- **Inputs** → train images/text + train labels + val images/text + val labels
- **Ground truth pairing** → directory parallel / coco json / hf datasets / yolo txt
- **Outputs** → checkpoints (with naming pattern), log files
- **Required packages**: run `python lifecycle/scripts/infer-init/scan_requirements.py <code_dir>`. If it fails, check requirements.txt manually.

### 1b. Extract the preprocessing chain

Read the `Dataset` / transform definitions and record what happens to an input before the model sees it → `input.json -> preprocessing`. See `references/detection-patterns.md` → "Preprocessing chain detection" for where to look.

Split the findings by who must agree on them:

| Block | Rule |
|---|---|
| `normalization` (mean / std / source) | **Must match eval + infer exactly.** Also flag it when `source` is `imagenet` but `items` shows non-natural images (X-ray, satellite, spectrograms) — the constants likely need recomputing for this data. |
| `input_layout` (size / resize_mode / channel_order) | **Must match eval + infer exactly.** `channel_order` is worth stating explicitly even when it looks obvious — cv2 reads BGR, PIL reads RGB, and getting it backwards costs a few points with no error anywhere. |
| `label_transform` (index_base / class_mapping / background_class) | **Must match.** The usual traps: 0- vs 1-indexed classes, COCO's 91-category ids vs 80 contiguous ones, whether a background class occupies index 0. |
| `augmentation` | **Train-only.** Its presence in an eval or infer stage is a bug, not a variation. |

Every block records `evidence` (`path:line`) — none of these values are declared anywhere, they're read out of code, so the next person needs to know where you got them.

If a value can't be determined, leave it empty rather than filling in the framework default. A guessed mean/std that happens to be wrong is worse than a blank, because a blank gets asked about and a wrong number gets used.

### 1c. Locate data and weights

Fill `input.json -> candidates` and `artifacts.json -> candidates` — the list `/train-run` will pick from.

**The data candidates are `/discover`'s output — read `discovery/leads.json`, don't re-excavate.** A `verified` lead becomes a candidate with `match: "ok"`; a `gone` one becomes `match: "absent"`; a lead that was never probed or came back `unreachable` becomes **`match: "unreachable"`**, which is the value that must not be skipped.

**An unreachable source produces a candidate, not silence.** Dropping it yields an `input.json` in which that data does not appear at all, and every later reader concludes it does not exist — the claim is real, only the check is missing. Record it with its evidence and let `/train-run` refuse rather than guess.

**Weight reachability was already settled in Step 0 — don't re-ask and don't reconnect.** Read `provenance.json -> sources_checked`. Two candidate sources are specific to this sub-step, because neither Step 0 nor `/discover` goes looking for them:

| Source | Where from |
|---|---|
| `code_default` | config defaults, code constants, README example invocations |
| `downloadable` | inferred from the dataset / model name in `items` — COCO, ImageNet, an HF hub id, a torchvision weights enum, the paper's release URL |

**Two more locations come out of Step 0's data-line row, and the first of them outranks every path in the list.**

`dataset:<id>@<snapshot>` **is a citation, not a path** — a frozen membership set rather than a directory as it is today, so **prefer it whenever it exists**. `path` stays empty and the entry carries a `resolve` block; `census.py resolve` turns that into openable paths **per run, into the run dir**, never into this config, because a resolved path embeds one machine's root and `input.json` outlives that machine. Fill `resolve.layers` from what the `Dataset` actually opens (1a/1b) and `resolve.at` from where training will run. The consuming run cites the snapshot in `lineage.parents`, which is the edge that makes data and models one graph. Full schema: `references/schemas.md` → `candidates`.

Before writing one down as `match: "ok"`, gate it:

```bash
python lifecycle/scripts/data/phase.py gate --project {PROJECT} --dataset <id> --to consume
```

Exit 1 is the answer, not a broken script — pass it through. It catches two things invisible from the filesystem: `snapshot_stale` (frozen from a census predating accepted inflow — reads as current and is not) and `census_incomplete` (every count under it is a lower bound). Record such a snapshot as `mismatch` with the blocker verbatim in `notes` and route to `/data-freeze`. **Never `--acknowledge` here**: it would stamp a stale citation into a config permanently, and that is not this skill's call. `/train-run` gates again at launch; this pass exists so a broken citation never becomes an `ok`.

`handoff:<handoff_id>` **is the one location nothing you can run will resolve** — every other resolves by doing something, this one by somebody else finishing. Hence **`pending` is not `absent`**: "the labels aren't here" and "the labels are with vendor-a, spec v3, due 2026-08-14" are different facts, and only the second tells the reader to wait instead of going hunting.

**Always list the code-declared path, even when it doesn't exist here** (`match: "absent"`). It's the original author's layout — the reference for where your data should go and the yardstick for judging every other candidate. Absent is a conclusion, not a gap.

What this sub-step contributes is the **match judgment**: compare each candidate against `items` + `pairing` and say *why* in `notes`. That comparison is the part that required reading the code, and it's what actually saves the user time:

```
1. dataset:coco@0728                 —                ok        118k units frozen 2026-07-28 from census_20260728, 0 unverified;
                                                                resolve --at nas --layer images,annotations
2. code_default                      /data/coco2017   absent    original author's path; layout reference: images/ + annotations/instances_train2017.json
3. local                             ~/data/coco      ok        same bytes as (1) — but a directory listing, not a pinned set
4. server:4090                       /data/coco2017   ok        sits on the 4090 box — prefer training there over pulling 19GB back
5. handoff:handoff_20260731_025626   —                pending   5000 extra imgs out with vendor-a, spec v3, due 2026-08-14
6. local                             ~/my_labels      mismatch  YOLO txt, code wants COCO json — needs conversion
```

Entry 3 is why the citation ranks first: it may well be the same files, but a directory can be written to between init and launch and a frozen set cannot.

`location` is not a label — it decides how `/train-run` proceeds: use directly, download first, resolve a snapshot into the run dir, wait for somebody else, **or run on that machine instead**. Surface that last case explicitly; data living on the GPU box is a reason to train there.

If no candidate has `match: "ok"`, stop before Step 7 rather than presenting a complete-looking config — and say *which* kind of no it is, because they don't route to the same place:

| Best you have | What it means | What you do |
|---|---|---|
| `absent` / `mismatch` only | the data isn't here and nothing is bringing it | training can't start, and that's the finding. `/data-collect` if it sits on a machine you can reach; conversion if `mismatch` |
| `pending` | it's with a named party, due on a date | **name the party and the date; do not go looking for a path.** `handoff.py status --project {PROJECT} --open-only`, report, stop. Falling through to "so where is your data?" is how a half-labeled directory gets picked instead |

**Inherited checkpoints get an `origin` block.** When a weight file came from someone else (handover, paper release) rather than being a standard pretrained backbone, record what's known about it in `artifacts.json -> items.<name>.origin` — see `references/schemas.md` → "artifacts.json -> items.<name>.origin". Fill what you can automatically by querying the backend Step 0 reached for this checkpoint's own run (`config` and final `metrics` come for free); Step 4d later does the same at project scope for the whole history. Then ask the user only for what's missing — above all `why` (what experiment this was, why it was kept).

Two fields decide whether the rest is usable: `scope` (a bare "mAP 48.5" is comparable to nothing) and `confidence` (`claimed` until you've re-run eval yourself). Default to `claimed` — never record an author's number as `verified` on their word.

Then offer the acceptance test — **re-run eval with this ckpt and see whether the number reproduces** (`references/schemas.md` → `origin` explains what that one run proves). Mention it; don't force it.

### 1d. Capture the original environment

Fill `config.json -> env_snapshot` — what the author actually had installed, as opposed to what the code declares (`required_packages`, filled in Step 1). See `references/detection-patterns.md` → "Original environment detection" for where to look and how much each source proves.

If Step 0 reached a tracking backend or found local `wandb/` / `mlruns/` leftovers, the captured `pip freeze` is already in hand — use it rather than reconnecting.

Prefer resolved sources over declared ones: a lock file or a tracking backend's captured `pip freeze` is evidence; `torch>=1.10` in `requirements.txt` is not. Record `source` honestly — a weak source correctly labeled is useful, a weak source presented as a snapshot is misleading.

Record only the packages whose version silently changes behavior (framework, CUDA, numpy, model-definition libs like `transformers` / `timm` / `mmcv`), not a whole freeze. Dump the full list to a file and point `full_snapshot_path` at it.

If nothing exists, set `source: "none"`. That's a conclusion — stop looking, and tell the user that reproducing the original numbers may be impossible to verify.

`/train-run` diffs this against each run's captured env and reports key-package mismatches — see `references/schemas.md` → `env_snapshot` for why that diff matters.

### 1e. Scan for hazards

Fill `config.json -> hazards` — landmines that **can't** be expressed as a parameter. (Anything you *can* change by passing a value belongs in `param_injection` with `overridable: false`; that's the dividing line.) See `references/detection-patterns.md` → "Hazard scanning" for the greps and how to read each hit.

`hazards` is collected **throughout** this skill — every step appends what it trips over (Step 1b noticing ImageNet constants on non-natural images, Step 1c finding a dead code-declared path, Step 2b finding a shadowed param that can't be reached). This step is the dedicated sweep for the kinds no other step would touch: network dependency, platform assumptions, missing seed, train/val leakage, hardcoded GPU counts.

Set `impact` deliberately — it's the field that determines when someone has to act:

| `impact` | Means | What you do |
|---|---|---|
| `blocks` | won't run until fixed | **report immediately, before the user fills anything else in** |
| `degrades` | runs and produces plausible-but-wrong results | warn now; `/train-run` echoes it again before launch |
| `risks` | breaks only under a condition (wrong GPU count, offline host) | record; the condition is only knowable at launch |

**`blocks` short-circuits the rest of this skill.** If the code-declared data path is dead and no candidate matched, say so and stop — don't walk someone through Steps 2-6 filling out four JSON files for code that can't start. An init that completes while hiding a blocker is worse than one that stops early.

**`degrades` is why this field exists.** A `blocks` hazard announces itself the first time you run. A `degrades` hazard never does: unseeded train/val splits inflate every metric this code will ever produce, a stale `timm` pin silently swaps the model's default config, and nothing anywhere reports either. These are the same class of failure as the fake metrics guarded elsewhere in this lifecycle — found by reading, or not at all.

Two hazards get discovered by comparison rather than grep, so do them here:

- **`dependency_version`** — diff `env_snapshot.key_packages` (Step 1d) against what's installed now.
- **`dir_structure`** — check what the `Dataset` takes for granted (subdirectory names, filename conventions) against the winning `candidates` entry from Step 1c.

## Step 2: Discover Real Config

**2a. Find the config.** Look for actual config files in the code directory:
1. Check `config_path` from analysis
2. Scan `configs/`, `config/`, `conf/`, `recipes/` directories
3. Prefer files named "train", "training", "pretrain", "finetune", "default", "baseline", "base", "main"
4. Pick the largest YAML / JSON config as fallback

If found, load all discovered parameters. **These are declared values, not effective ones** — 2b resolves the difference.

**2b. Trace declared → effective.** A config file value is only what the code *claims* to use. For each parameter worth managing, trace it from declaration to the line that consumes it, and check whether anything overwrites it on the way. Read `references/detection-patterns.md` → "Param shadowing detection" for the patterns (post-parse literals, recompute-from-other-params, world-size scaling, framework defaults, config-layer merges) and the grep-then-read procedure.

For each param record four things:

| | |
|---|---|
| **effective value** | what the code actually runs with — this is what goes in `runtime_params` |
| **how it gets in** | `cli` (+flag) / `yaml` (+key) / `env` / `hardcoded` / `derived` (+derived_from) |
| **overridable** | does an externally supplied value survive to the read site? |
| **evidence** | `path:line` of the read site, plus the shadowing site when they differ |

A yaml saying `lr: 3e-4` is worthless if `optim.py:44` passes a literal `1e-4`. Record `1e-4`, mark it `hardcoded` / `overridable: false`, and note the shadowing. Getting this backwards silently breaks `/train-tune` — it would sweep a disconnected knob, produce N identical runs, and conclude the hyperparameter doesn't matter.

When verification is cheap, verify instead of tracing: `python train.py --help` for argparse, or a one-time resolved-config print for omegaconf/hydra.

## Step 3: Select Managed Parameters

Training configs typically have many parameters. Use progressive disclosure (same as infer-init).

Common training parameters worth offering as managed:
- **Optimization**: `learning_rate`, `weight_decay`, `optimizer`, `lr_scheduler`, `warmup_ratio`, `warmup_steps`
- **Batch**: `batch_size`, `gradient_accumulation_steps`, `eval_batch_size`
- **Duration**: `epochs`, `max_steps`, `eval_every`, `save_every`
- **Reproducibility**: `seed`
- **Precision / hardware**: `mixed_precision`, `bf16`, `fp16`, `compile`
- **Output**: `output_dir`, `run_name`

**Overridability gate.** Only offer a param as managed if Step 2b found it `overridable: true`. A param that can't be changed from outside is not a knob — offering it invites the user to "set" a value that silently does nothing.

For each `overridable: false` param found, still record it in `param_injection.items` (documenting *why* it can't be tuned) and surface it to the user as a risk: "changing this requires editing `<path:line>`". Don't put it in `runtime_params`.

Selected params → `config.json -> runtime_params` (**effective values from Step 2b**) with `${artifact.xxx}` / `${input.xxx}` references where applicable. Every key also gets a `config.json -> param_injection.items` entry recording `via` / flag-or-key / `overridable` / `evidence` — see `references/schemas.md` → "config.json -> param_injection". `/train-run` reads this to build the launch command per-param instead of guessing from `config_format`; `/train-tune` reads it to know which axes are actually searchable.

## Step 4: Identify Log Format and Streaming Schema

This step has no equivalent in infer-init / eval-init. Training emits metrics continuously during a long run; the gate stage / monitor needs to know exactly how.

**4a. Detect log format.** Check the code for one of:

| Pattern | log_format | readable today? |
|---|---|---|
| `f.write(json.dumps({...}))` to a `.jsonl` file | `jsonl` | yes |
| `print(json.dumps({...}))` only | `jsonl_stdout` | yes |
| Plain `print(f"epoch {e} loss {l}")` | `stdout_regex` (build extractor in 4b) | yes |
| `SummaryWriter().add_scalar(...)` | `tensorboard` | adapter written but **never executed** — needs `tensorboard` in the env running `ingest.py` |
| `wandb.log({...})` | `wandb` | **no — no adapter** |
| Multiple of the above | prefer a readable one; record the others as fallback | — |

Record in `output.json -> metrics.log_format` and `metrics.log_path`. Both name
**the source** — what the code writes. What `/train-run` reads is the normalized
stream; see `lifecycle/references/run-mechanics.md` → "Metric stream".

**Record what you found, then say what it costs.** `references/schemas.md` →
`output.json -> metrics` owns the per-row detail (what "never executed" means for
tensorboard, why `wandb` still gets recorded, what to offer instead); read it there
rather than from a second copy here, which is how this table came to promise a
reader that did not exist.

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

**4d. Locate the tracking backend.** Steps 4a-4c answered "how does MLClaw read *this* run". This one answers "where is the author's *past* history, and how do I pull it" → `output.json -> metrics.tracking`. See `references/detection-patterns.md` → "Tracking backend detection".

**Reachability was already settled in Step 0 — reuse it, don't reconnect.** `provenance.json -> sources_checked` holds whether this backend answered and what credentials were missing; that's the single record of it. What this sub-step adds is the **locator and the history pull**.

The code holds the locator: `wandb.init(entity=..., project=...)`, `mlflow.set_tracking_uri(...)`, `SummaryWriter(log_dir=...)`; env vars (`WANDB_PROJECT`, `MLFLOW_TRACKING_URI`) hold it too. Record it even when Step 0 couldn't connect — a locator you can't reach yet is a todo, not a dead end. Only "no tracking call anywhere in the code" justifies `backend: "none"`, which is a **conclusion**: stop searching.

When you pull, **include the failed runs** — they're the only surviving record of what the author already ruled out, and a `nan` at lr=1e-3 saves you from rediscovering it. **Bound the pull**: most recent 200 plus anything an inherited checkpoint's `origin.source` names, and ask before going further; HPO projects hold thousands of runs and each one becomes a `runs/` entry. Import per the plan in CLAUDE.md (with `mode` recorded) rather than leaving them in a foreign format.

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

**Mark every `guessed` value as you present it.** A value inferred from surrounding logic looks identical to one read off a line of code, and the user can only catch the wrong ones if you distinguish them:

```
primary_metric:  val_mAP   ⚠ guessed — 5 metrics logged, none marked primary;
                            inferred from the best-ckpt save condition (train.py:196)
done_signal:     (absent)  — script just ends; no record, marker, or flag file
```

Then present `unresolved` as its own short list, `blocking` entries first. Keep it short — anything that doesn't stop the run from launching belongs in `notes`, not in the questions you put in front of the user. Write `provenance.json` last, after the four files are confirmed, so `unresolved` reflects what the user actually settled during review.

## Step 8: Validate

Confirm the schema is internally consistent:

- `metrics.primary_metric` must appear in at least one `record_types.<type>.fields`
- `metrics.watch_epoch` items must appear in `record_types` (any type whose `is_terminal` is false)
- `checkpoints.selection.best_by` typically equals `metrics.primary_metric` (warn if different)
- `done_signal` must reference an actual record type or stdout pattern
- every `runtime_params` key has a matching `param_injection.items` entry — a managed param with unknown injection can't be launched correctly
- no `param_injection.items` entry with `overridable: false` appears in `runtime_params` (**hard failure**, not a warning — this is the case that silently corrupts `/train-tune` conclusions)
- every `param_injection.items` entry has non-empty `evidence`; `via: derived` entries also have `derived_from`
- every `hazards` entry has non-empty `evidence` and an `impact`; every `blocks` entry has already been surfaced to the user (**hard failure** — a completed init that conceals a blocker is worse than an incomplete one)
- every `dataset:` candidate marked `ok` has an empty `path`, a `resolve` block with all four keys non-empty, and passed `gate --to consume`; every `pending` candidate names a `handoff_id` that exists under `{PROJECT}/handoffs/` and is still open (**hard failure** on the `pending` half — a candidate pointing at a handoff that already closed is either an `ok` nobody promoted or a fiction, and both send `/train-run` to wait for something that already came back)
- `provenance.json`: `source_mode` is set; every `unresolved` entry has a `key` resolving to a real path in one of the four files, an enumerated `status`, and a non-empty `why`; no `status: "guessed"` entry remains unreviewed after Step 7

If any check fails, surface it to the user and ask to fix or override.

## Step 9: Write recipe.md

Render `stages/training/recipe.md` — one self-contained document answering "how do I train this model, and is everything in place?" The JSON files are what machines read; this is what a person reads, including you in three months.

**Read `references/recipe-template.md` for the full worked example** — it shows all seven sections filled in, plus the failure modes to avoid. Sections, in this order:

1. **Readiness verdict, one screen.** Can training start right now — yes / no / yes-with-caveats. If no, the blocking items and nothing else. Put this first; everything below is detail.
2. **What this trains** — model, dataset, task, target metric. Include the inherited baseline from `artifacts.items[].origin` when there is one, **with its `confidence`** (a `claimed` 48.5 is not a target, it's a rumor).
3. **The launch command**, concretely — actual paths from the chosen candidates, not `${}` placeholders. Someone should be able to copy this line.
4. **What was checked and what wasn't** — the `sources_checked` table verbatim. `needs_auth` rows matter here: they're the known unknowns, and they belong in a handover.
5. **Hazards** — `degrades` first (those silently corrupt results), then `blocks`, then `risks`.
6. **Open questions** — `unresolved`, `blocking` first, with the `guessed` values called out as guesses.
7. **Environment** — the original's `env_snapshot` next to what's installed now, differences highlighted.

Two rules for writing it: **state confidence inline** (read from `train.py:44` / inferred / the author said so — never flatten those into the same voice), and **never present an unchecked source as an absent one**. "Company docs: not checked, connector unauthorized" and "Company docs: none exist" are different facts, and conflating them is how a handover loses information.

The point of this file is that it makes you not be the previous owner. Everything painful about inheriting this code — no record of why, unverified numbers, data of unknown provenance — is what the next person inherits from you unless this document exists.

## Done — handoff to /train-run

Once the four config files, `provenance.json`, and `recipe.md` are saved, the training stage is initialized. From here, `/train-run` consumes the schemas to launch, monitor, and finalize each training run. Each run also fills its own `lineage` block (`parents` / `fork_of` / `variation_summary`) per CLAUDE.md.
