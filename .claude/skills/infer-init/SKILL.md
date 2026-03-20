---
name: infer-init
description: Analyze inference code and fill the 4 config JSONs for the inference stage
---

# /infer-init — Inference Stage Initialization

Analyze inference code and fill the 4 JSON config files in the project's `stages/inference/` directory.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Record the answer, then ask the next. Never list multiple questions at once.

## Workflow State

On entry: push `{ "skill": "infer-init", "step": "locate_project" }` to `history.json` stack.
Update step as you progress through each step below. On completion: pop from stack, append `completed` to history.

## Dependency Check

On entry, check upstream requirements per the Skill Dependency Graph in CLAUDE.md:
- **project.json exists**: if no project is found and user cannot provide one, offer to run `/project-init` first (invoke as sub-skill following the Workflow State Protocol).
- **code available**: after locating the project, verify the stage's code directory has files. If empty and `code_source` is not configured, tell the user to add code first.

## Locate Project

Show recent projects and let user pick. Ask the user which one:

1. **Recent projects (top 5)** — scan `history.json` history across all projects in the workspace root (default `D:\agent_space\mlclaw\projects\`). List up to 5 most recently used projects with their paths. Show like:
   ```
   Recent projects:
   1. detection  (D:\agent_space\mlclaw\projects\detection)  — last used: 2026-03-16
   2. tracking   (D:\agent_space\mlclaw\projects\tracking)   — last used: 2026-03-14
   ```
   If no projects found, skip this option.
2. **Provide a path** — user gives a path to the project directory (must contain `project.json`).
3. **Other** — user has another way to locate it. Follow their instructions.

Once `project.json` is found:
- Verify `stages.inference.enabled` is true
- Resolve the effective code directory (see CLAUDE.md "Code Source Resolution"):
  - If `code_source.source == "local"` and `code_source.path` is set → use that path directly
  - If `code_source.source == "github"` → ensure cloned to `code_path`, use `code_path`
  - If `code_source.source` is null → use `code_path` (relative to project root)
- Check that the resolved code directory has code files. If empty, tell the user to add code first.

## Output: 4 JSON files in `{project.root}/stages/inference/`

| File | What it captures |
|------|-----------------|
| `config.json` | Entry command, config format, framework, params |
| `artifacts.json` | Static inputs — model weights, decoders, label maps |
| `input.json` | Dynamic inputs — videos, images, text per run |
| `output.json` | What the code produces |

These files were created as empty templates by `/project-init`. This skill fills in the values.

### Item schema

Each item in `artifacts.json → items`, `input.json → items`, `output.json → items`:

```json
{
  "type": "",
  "format": "",
  "description": ""
}
```

- `type`: one of `video|image|text|tabular|json|binary|model|checkpoint|config|log`
- `format`: file extension (e.g., `.onnx`, `.mp4`, `.json`)
- `description`: short text

### Source schema

Each entry in `artifacts.json → sources`, `input.json → sources`:

```json
{
  "source": "local",
  "path": "",
  "credentials": ""
}
```

- `source`: one of `local|s3|server|stage_output|registry`
- `path`: actual file/directory path (empty = not yet filled)
- `credentials`: key in `resources.json → servers` or `aws`, etc. Only needed when source is not `local`.

### Variable reference syntax `${}`

Use `${}` to reference values across config files. Resolved at runtime by `/infer-run`.

| Prefix | Resolves to |
|--------|------------|
| `${project.xxx}` | project.json field (e.g., `${project.root}`, `${project.name}`) |
| `${resources.xxx.yyy}` | resources.json field (e.g., `${resources.aws.region}`) |
| `${artifact.xxx}` | artifacts.json → sources → xxx → path |
| `${input.xxx}` | input.json → sources → xxx → path |
| `${output.xxx}` | stages/inference/runs/run_NNN/outputs/ (resolved at runtime by /infer-run) |

## Step 1: Analyze Code

Update workflow step to `analyze_code`.

Read all code files under the code directory (*.py, *.sh, *.yaml, *.yml, *.json, *.toml).

Determine:
- **entry_command**: how to run inference (e.g., `python main.py --config configs/default.yaml`)
- **config_format**: one or combination of: argparse, yaml, omegaconf, json, hydra (e.g., "argparse+omegaconf" if code uses both CLI args and YAML config)
- **config_path**: where the config file lives relative to code/
- **framework**: e.g., pytorch, onnxruntime, tensorrt, custom
- **Artifacts**: what static files the code needs (models, decoders, etc.)
- **Inputs**: what dynamic data the code takes per run
- **Outputs**: what the code produces
- **Metrics**: what numerical values the code reports (see Metrics Analysis below)
- **Required packages**: run `python lifecycle/scripts/infer-init/scan_requirements.py <code_dir>`. It scans requirements.txt, pyproject.toml, setup.py, conda yaml, and falls back to import analysis. Store result in `config.json → required_packages`. **Fallback**: if script fails, manually check for requirements.txt and parse it.

### Type classification rules

**Artifacts** (static, per model version):
- model weights: .onnx, .pt, .pth, .engine, .safetensors, .trt, .tflite → type `model`
- checkpoints: .ckpt → type `checkpoint`
- static configs: .yaml, .yml, .toml, .ini → type `config`
- lookup tables, decoders, label maps → type `json` or `binary`

**Inputs** (dynamic, per run):
- video: .mp4, .avi, .mov, .mkv → type `video`
- images: .jpg, .png, .bmp, .tiff → type `image`
- text: .txt, .jsonl → type `text`
- tabular: .csv, .tsv, .parquet → type `tabular`

### Metrics analysis

Scan code for metrics the program reports. Look for:
- `print()` / `logging` statements with numerical values (FPS, latency, accuracy, mAP, loss, etc.)
- JSON/CSV result files the code writes
- Return values from evaluation functions
- Common metric patterns: `f"FPS: {fps}"`, `results["mAP"]`, `logger.info(f"accuracy: {acc}")`

For each metric found, create an entry in `output.json → metrics.definitions`:
```json
{
  "type": "float|int",
  "source": "stdout|file",
  "pattern": "regex to extract from stdout (if source=stdout)",
  "path": "relative path to result file (if source=file)",
  "key": "JSON key in result file (if source=file)"
}
```

Then ask user: "Found these metrics: fps, mAP, total_detections, inference_time_ms. Which ones do you want to track across runs?"

User's selection goes into `output.json → metrics.watch` as a list. These are the metrics that will be collected after each run and stored in `run.json → metrics` for comparison.

## Step 2: Discover Real Config

Update workflow step to `discover_config`.

Look for the actual config file in the code directory:
1. Check config_path from analysis
2. Scan `configs/`, `config/`, `conf/` directories
3. Prefer files named "default", "baseline", "base", "main"
4. Pick the largest YAML file as fallback

If found, load all discovered parameters.

## Step 3: Select Managed Parameters

Update workflow step to `select_params`.

Config files can have dozens of parameters. Instead of dumping them all, use a progressive disclosure flow:

**3a. Show category summary first:**
```
Config: configs/default.yaml (47 parameters)

Categories:
  Data paths:   5 params (input dirs, output dirs)
  Model:        3 params (weights path, architecture, num_classes)
  Runtime:      8 params (batch_size, num_workers, device, ...)
  Preprocessing: 12 params (resize, normalize, augmentation, ...)
  Postprocessing: 6 params (NMS threshold, confidence, ...)
  Other:        13 params

Which categories do you want to see? (or "all")
```

**3b. Expand requested categories:**
Show only the categories the user asked for, with current values:
```
Model:
  □ model.weights = /path/to/model.pt
  □ model.architecture = rtdetr_r50
  □ model.num_classes = 80

Runtime:
  □ batch_size = 32
  □ num_workers = 4
  □ device = cuda:0
  ...

Which parameters should MLClaw manage per run?
```

**3c. Record selection:**
- Selected params go into `config.json → runtime_params` with `${artifact.xxx}` / `${input.xxx}` references where applicable
- Unselected params stay in original config files — not touched by MLClaw
- If user says "all" at any point → select all params in that category

## Step 4: Present Each File — MUST WAIT FOR EXPLICIT CONFIRMATION

Update workflow step to `review_{filename}` (e.g., `review_config`).

Show each JSON file's proposed content one at a time.

**Order**: config.json → artifacts.json → input.json → output.json

For each file:
1. Show the proposed JSON content
2. **STOP and wait for user response. Do NOT proceed to the next file until the user explicitly confirms.**
3. User confirms (e.g., "ok", "good", "next", "confirmed") → save and move to next file
4. User requests changes → revise, show again, and wait for confirmation again
5. "skip" → accept all remaining files as-is

**CRITICAL: Never auto-advance. Every file requires an explicit user confirmation before moving on.**

## Step 5: Validate

Update workflow step to `validate`.

### Checks to run

Run `python lifecycle/scripts/infer-init/validate_refs.py <stage_dir>` where stage_dir is the project's `stages/inference/` directory.

**Fallback**: if script fails, manually check these:
- Entry script file exists in code/
- All items have a `type` field with valid value
- Every `${artifact.xxx}` in runtime_params has a matching key in artifacts.json → items
- Every `${input.xxx}` has a matching key in input.json → items
- Every `${output.xxx}` has a matching key in output.json → items

Additional checks (always done by Claude, not scripted):
- config_path file exists in code/ (if specified)
- config_format is valid
- Semantic sanity: does the config make sense for the code?

**CRITICAL: Do not allow saving if there are broken references. User must fix before proceeding.**

Report errors/warnings to user. Let them fix or accept warnings.

## Step 6: Save

Update workflow step to `save`.

1. Write all 4 JSON files to `{project.root}/stages/inference/`
2. Pop self from `history.json` stack, append `completed` to history
3. **Downstream suggestion** (per Skill Dependency Graph in CLAUDE.md): offer `/infer-run`.
