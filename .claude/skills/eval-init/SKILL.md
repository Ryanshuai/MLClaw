---
name: eval-init
description: >
  Use this skill to analyze evaluation code and configure the evaluation stage. Triggers when the user
  wants to set up evaluation for a model — analyzing what an eval script needs (model weights, data,
  ground truth), what metrics it computes, and filling the 4 JSON config files. Use for: "analyze eval
  code", "set up evaluation", "configure eval stage", "what does this eval script need", "分析评估代码",
  "配置评估", "初始化eval". Not for running evaluation (use eval-run) or generating reports (use eval-report).
---

# /eval-init — Evaluation Stage Initialization

Analyze evaluation code and fill 4 JSON config files (schema only) in `stages/evaluation/`. The goal is to understand what the code needs and produces, so `/eval-run` can execute it correctly later.

## How this skill works

The user brings evaluation code (standalone `eval.py` or eval mode inside a training script). You read it, figure out what it needs (model weights, test data, ground truth annotations) and what it produces (metrics like mAP, accuracy), then capture that understanding in 4 structured JSON files.

These files are **schema only** — they define WHAT is needed, not WHERE to get it. Concrete paths come later during `/eval-run`. However, if the user volunteers source locations during init, record them.

## Interaction approach

Ask one question at a time. Users find it overwhelming when asked to fill multiple fields simultaneously — each question should feel like a natural follow-up to the previous answer. Record the answer, then ask the next.

## On entry

Follow the standard Workflow State Protocol from CLAUDE.md: push to stack, check dependencies (project.json exists, code available), locate project, resolve code directory. These are all documented in CLAUDE.md — follow them, don't duplicate them here.

## Output: 4 JSON files

| File | What it captures |
|------|-----------------|
| `config.json` | Entry command, config format, framework, dataset info, managed params |
| `artifacts.json` | Static inputs — model weights, evaluator configs (items only, no paths) |
| `input.json` | Dynamic inputs — test data + ground truth annotations (items only) |
| `output.json` | What the code produces — metrics definitions and watch list |

For item/source schemas and type classification rules, read `references/schemas.md`.

## Step 1: Analyze Code

Read all code files (*.py, *.sh, *.yaml, *.yml, *.json, *.toml) under the code directory.

For pattern recognition guidance (entry points, dataset loading, ground truth formats, metric extraction), read `references/detection-patterns.md`.

Determine:
- **entry_command**: how to run evaluation. Eval code is often bundled inside training repos (e.g., `train.py --evaluate`). When this happens, extract only the evaluation path — what args control eval mode, what data it reads, what metrics it computes.
- **config_format**: argparse, yaml, omegaconf, json, hydra, or combination (e.g., "argparse+omegaconf")
- **config_path**: where the config file lives relative to code/
- **framework**: pytorch, onnxruntime, tensorrt, custom, etc.
- **Dataset info** → fill `config.json → dataset` (name, split, num_samples, classes)
- **Artifacts** → model weights, evaluator configs, label maps
- **Inputs** → test/val images, videos, text
- **Ground truth** → annotation/label data, with pairing mode
- **Outputs** → result files, visualizations
- **Metrics** → numerical values the code reports, with extraction patterns
- **Required packages**: run `python lifecycle/scripts/infer-init/scan_requirements.py <code_dir>`. If it fails, check requirements.txt manually.

For metrics: after identifying them, ask the user which ones to track across runs. Their selection goes into `output.json → metrics.watch`. If the code produces per-class breakdowns, set `output.json → metrics.per_class` to `true` (confirm with user).

## Step 2: Discover Real Config

Look for actual config files in the code directory:
1. Check config_path from analysis
2. Scan `configs/`, `config/`, `conf/` directories
3. Prefer files named "eval", "evaluate", "test", "val", "default", "baseline", "base", "main"
4. Pick the largest YAML file as fallback

If found, load all discovered parameters.

## Step 3: Select Managed Parameters

Config files often have dozens of parameters. Instead of dumping them all, use progressive disclosure — this respects the user's attention and helps them focus on what matters:

**3a. Show category summary:**
```
Config: configs/eval.yaml (35 parameters)

Categories:
  Data paths:      4 params (image root, annotation path, output dir, ...)
  Model:           3 params (weights, architecture, num_classes)
  Eval settings:   6 params (batch_size, device, confidence_threshold, ...)
  Dataset/loader:  8 params (split, num_workers, transforms, ...)
  Other:          14 params

Which categories do you want to see?
```

**3b. Expand requested categories** with current values, let user pick which MLClaw should manage per run.

**3c. Verify each pick is actually overridable.** A value in a config file is only the *declared* value — code may shadow it. For each param the user selected, trace it to the line that consumes it and look for: a literal at the use site (`nms(boxes, iou=0.5)` ignoring `cfg.nms_iou`), a post-parse assignment (`args.conf = 0.25`), or a value recomputed from another.

This matters more in eval than anywhere else, because **eval params shape the metric directly**. A `--conf-threshold` the code ignores turns a threshold sweep into five identical numbers — and the natural reading of five identical numbers is "the metric is insensitive to threshold", a wrong conclusion that looks exactly like a finding. Nothing errors; the runs all complete.

Cheap checks: `grep -rn "conf_thres\|confidence\|nms" --include=*.py <code_dir>` then read the use site; `python eval.py --help` confirms which flags actually exist.

**3d. Record**: selected params → `config.json → runtime_params` (**effective values** — what the code runs with, not what the yaml declares) with `${artifact.xxx}` / `${input.xxx}` references. Each key also gets a `config.json → param_injection.items` entry recording `via` / flag-or-key / `overridable` / `evidence` (`path:line`), per `lifecycle/references/run-mechanics.md` "Launch contract (Step 3 detail)" rule 3. Params found `overridable: false` must **not** go into `runtime_params` — keep them in `param_injection` with a note and tell the user which line to edit to change them. Unselected params stay in original config files untouched.

## Step 4: Present Each File for Review

Show each JSON file one at a time in order: config.json → artifacts.json → input.json → output.json.

For each file: show proposed content, then wait for the user to confirm before moving on. The reason for presenting one at a time is that each file builds on the previous — the user can catch issues early before they cascade. If the user says "skip", accept remaining files as-is.

After output.json is confirmed, ask about baseline:
"Set a baseline for comparison? (1) a previous run ID, (2) external numbers like paper results, (3) skip"

Whichever is chosen, **record the scale the baseline describes** — this is the source of every later comparison, so an unqualified number here propagates into every diff downstream.

- For (1), accept only a run with `run.json -> mode: "production"`. A debug run as baseline silently poisons every future comparison, and whoever reads those diffs months later has no way to see why the numbers look wrong. Store the run's `scope` next to the baseline so `/eval-run` can check comparability without re-reading it.
- For (2), record what the external numbers were measured on (e.g. `"COCO val2017, all 5000 images"`). Paper numbers are full-test-set numbers; a truncated run compared against them yields a delta made of sampling noise.

## Step 5: Validate

Two scripts, one job each — they do not overlap, so run both:

```
python lifecycle/scripts/infer-init/validate_refs.py <stage_dir>          # every ${} reference, all four files
python lifecycle/scripts/eval-init/validate_ground_truth.py <stage_dir>   # GT items/sources, dataset cross-check, preprocessing contract
```

`validate_refs.py` is the **only** reference validator. It is the one that knows all four files and folds `ground_truth.items` into the `input` namespace, so `${input.gt_ann}` resolves. Don't add a second reference check beside it and don't hand-verify references when it reports clean — two validators answering one question is how this step used to block saves on correct configs.

Exit codes, per CLAUDE.md "Script Integration": `1` means the script worked and found errors — fix them, don't redo the check by hand. `2` means the script broke; then check manually:

- Entry script file exists in code/
- All items have a valid `type` field
- `${artifact.xxx}` / `${input.xxx}` / `${output.xxx}` references match actual keys — remembering that `${input.xxx}` may name either a plain input or a `ground_truth.items` entry
- If `ground_truth.items` is non-empty, `pairing` must be set
- config_path file exists (if specified)
- dataset.name is filled (warn if empty — it helps when comparing runs later)

Don't save if there are broken references — the user needs to fix those first, otherwise `/eval-run` will fail downstream.

## Step 6: Save

Write all 4 JSON files to `{project.root}/stages/evaluation/`. Create `stages/evaluation/assets/` if needed. Update workflow state per CLAUDE.md protocol. Offer `/eval-run` as next step.
