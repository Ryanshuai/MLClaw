---
name: infer-init
description: >
  Use this skill to analyze inference code and configure the inference stage. Triggers when the user
  wants to set up inference for a model — analyzing what a prediction script needs (model weights,
  input data), what outputs and performance metrics it produces, and filling the 4 JSON config files.
  Use for: "analyze inference code", "set up inference", "configure inference stage", "what does this
  script need to run", "分析推理代码", "配置推理", "初始化infer", "设置推理流程". Not for running inference
  (use infer-run) or evaluation with ground truth (use eval-init).
---

# /infer-init — Inference Stage Initialization

Analyze inference code and fill 4 JSON config files (schema only) in `stages/inference/`. The goal is to understand what the code needs and produces, so `/infer-run` can execute it correctly later.

## How this skill works

The user brings inference code (a standalone `predict.py`, detection pipeline, or inference mode inside a larger codebase). You read it, figure out what it needs (model weights, input data) and what it produces (predictions, detections, visualizations, performance metrics like FPS), then capture that understanding in 4 structured JSON files.

These files are **schema only** — they define WHAT is needed, not WHERE to get it. Concrete paths come later during `/infer-run`. However, if the user volunteers source locations during init, record them.

## Interaction approach

Ask one question at a time. Users find it overwhelming when asked to fill multiple fields simultaneously — each question should feel like a natural follow-up to the previous answer. Record the answer, then ask the next.

## On entry

Follow `lifecycle/references/skill-graph.md` -> "Workflow State Protocol": push to stack, check dependencies (project.json exists, code available), locate project, resolve code directory. These are all documented in CLAUDE.md — follow them, don't duplicate them here.

## Output: 4 JSON files

| File | What it captures |
|------|-----------------|
| `config.json` | Entry command, config format, framework, managed params |
| `artifacts.json` | Static inputs — model weights, decoders, label maps (items only, no paths) |
| `input.json` | Dynamic inputs — images, video, text fed per run (items only) |
| `output.json` | What the code produces — result files, visualizations, metrics definitions |

For item/source schemas and type classification rules, read `references/schemas.md`.

## On finding the data

**This skill has no `candidates` block, and that is deliberate.** `/train-init` and `/eval-init` both
settle their data once because a training set and a val set are fixed assets; inference inputs
genuinely change every run, so locating them at init would pin something that is meant to move.
Concrete paths come from `/infer-run` Step 1.

What that does *not* mean is that you are on your own when nobody knows where anything is. If the
user cannot say what data exists — an inherited project, a wiki page nobody trusts — invoke
**`/data-discover`** as a sub-skill and let `discovery/leads.json` hold the answer. It is a utility,
callable from anywhere, and its leads outlive this init. Do not grow a source sweep here; there is
one, and two would start disagreeing.

## Step 1: Analyze Code

Read all code files (*.py, *.sh, *.yaml, *.yml, *.json, *.toml) under the code directory.

For pattern recognition guidance (entry points, input/output detection, metrics extraction), read `references/detection-patterns.md`.

Determine:
- **entry_command**: how to run inference (e.g., `python detect.py --source data/ --weights model.pt`)
- **config_format**: argparse, yaml, omegaconf, json, hydra, or combination (e.g., "argparse+yaml")
- **config_path**: where the config file lives relative to code/
- **framework**: pytorch, onnxruntime, tensorrt, custom, etc.
- **Artifacts** -> model weights, decoders, label maps, static configs
- **Inputs** -> images, video, text, or directories of data
- **Outputs** -> prediction files, annotated images, JSON results, visualizations
- **Metrics** -> performance values the code reports (FPS, latency, throughput), with extraction patterns
- **Required packages**: run `python lifecycle/scripts/infer-init/scan_requirements.py <code_dir>`. If it fails, check requirements.txt manually.

For metrics: after identifying them, ask the user which ones to track across runs. Their selection goes into `output.json -> metrics.watch`. Inference metrics are typically performance-oriented (FPS, latency, throughput) rather than accuracy metrics, which belong in the evaluation stage.

## Step 2: Discover Real Config

Look for actual config files in the code directory:
1. Check config_path from analysis
2. Scan `configs/`, `config/`, `conf/` directories
3. Prefer files named "infer", "inference", "predict", "detect", "default", "baseline", "base", "main"
4. Pick the largest YAML file as fallback

If found, load all discovered parameters.

## Step 3: Select Managed Parameters

Config files often have dozens of parameters. Instead of dumping them all, use progressive disclosure — this respects the user's attention and helps them focus on what matters:

**3a. Show category summary:**
```
Config: configs/default.yaml (47 parameters)

Categories:
  Data paths:      5 params (input dirs, output dirs)
  Model:           3 params (weights path, architecture, num_classes)
  Runtime:         8 params (batch_size, num_workers, device, ...)
  Preprocessing:  12 params (resize, normalize, augmentation, ...)
  Postprocessing:  6 params (NMS threshold, confidence, ...)
  Other:          13 params

Which categories do you want to see?
```

**3b. Expand requested categories** with current values, let user pick which MLClaw should manage per run.

**3c. Verify each pick is actually overridable.** A value in a config file is only the *declared* value — code may shadow it. For each param the user selected, trace it to the line that consumes it and look for: a literal at the use site, a post-parse assignment (`args.batch_size = 1`), or a value recomputed from another.

For inference the casualty is the **performance number**: if `batch_size`, `precision`, or `device` doesn't actually take effect, the FPS / latency you record belongs to a different configuration than the one written in the run record. Benchmarks then get compared across runs that were never actually different.

Cheap checks: `grep -rn "batch_size\|half\|fp16\|device" --include=*.py <code_dir>` then read the use site; `python infer.py --help` confirms which flags actually exist.

**3d. Record**: selected params -> `config.json -> runtime_params` (**effective values** — what the code runs with, not what the config declares) with `${artifact.xxx}` / `${input.xxx}` references where applicable. Each key also gets a `config.json -> param_injection.items` entry recording `via` / flag-or-key / `overridable` / `evidence` (`path:line`), per `lifecycle/references/run-mechanics.md` "Launch contract (Step 3 detail)" rule 3. Params found `overridable: false` must **not** go into `runtime_params` — keep them in `param_injection` with a note and tell the user which line to edit. Unselected params stay in original config files untouched.

## Step 4: Present Each File for Review

Show each JSON file one at a time in order: config.json -> artifacts.json -> input.json -> output.json.

For each file: show proposed content, then wait for the user to confirm before moving on. The reason for presenting one at a time is that each file builds on the previous — the user can catch issues early before they cascade. If the user says "skip", accept remaining files as-is.

## Step 5: Validate

Run `python lifecycle/scripts/infer-init/validate_refs.py <stage_dir>`. If the script fails, check manually:

- Entry script file exists in code/
- All items have a valid `type` field
- `${artifact.xxx}` / `${input.xxx}` / `${output.xxx}` references match actual keys
- config_path file exists (if specified)
- config_format is valid

Don't save if there are broken references — the user needs to fix those first, otherwise `/infer-run` will fail downstream.

## Step 6: Save

Write all 4 JSON files to `{project.root}/stages/inference/`. Update workflow state per `lifecycle/references/skill-graph.md`. Offer `/infer-run` as next step.
