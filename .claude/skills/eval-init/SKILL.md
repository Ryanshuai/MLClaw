---
name: eval-init
description: Analyze evaluation code and fill the 4 config JSONs for the evaluation stage
---

# /eval-init — Evaluation Stage Initialization

Analyze evaluation code and fill the 4 JSON config files (schema only) in the project's `stages/evaluation/` directory.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Record the answer, then ask the next. Never list multiple questions at once.

## Workflow State

On entry: push `{ "skill": "eval-init", "step": "locate_project" }` to `history.json` stack.
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
- Verify `stages.evaluation.enabled` is true
- Resolve the effective code directory (see CLAUDE.md "Code Source Resolution"):
  - If `code_source.source == "local"` and `code_source.path` is set → use that path directly
  - If `code_source.source == "github"` → ensure cloned to `code_path`, use `code_path`
  - If `code_source.source == "server"` → code is on remote server, read via SSH
  - If `code_source.source` is null → use `code_path` (relative to project root)
- Check that the resolved code directory has code files. If empty, tell the user to add code first.

## Special case: evaluation code mixed with training code

Evaluation code is often bundled inside training repositories (e.g., `train.py --evaluate`, `eval.py` alongside `train.py`, or evaluation as a mode/flag within the main script). When analyzing code:

1. **Identify the evaluation entry point**: look for:
   - Standalone eval scripts: `eval.py`, `evaluate.py`, `test.py`, `validate.py`, `benchmark.py`
   - Eval flags/modes in training scripts: `--eval`, `--evaluate`, `--test`, `--val-only`, `--mode eval`
   - Eval functions: `def evaluate(`, `def validate(`, `def test(`
   - Conditional blocks: `if args.eval:`, `if mode == "eval":`

2. **Extract only the evaluation path**: even if the code has training logic, focus on:
   - What arguments/config keys control evaluation mode
   - What data the evaluation path reads (test set, val set, annotations)
   - What metrics it computes and reports
   - What output files it writes

3. **Set entry_command accordingly**: e.g., `python train.py --evaluate --resume checkpoint.pth` or `python eval.py --config configs/eval.yaml`

## Output: 4 JSON files in `{project.root}/stages/evaluation/`

| File | What it captures |
|------|-----------------|
| `config.json` | Entry command, config format, framework, dataset info, managed params |
| `artifacts.json` | Static inputs — what the code needs (items only, no paths) |
| `input.json` | Dynamic inputs — what data + ground truth the code needs (items only) |
| `output.json` | What the code produces — primarily metrics |

These files are **schema only** — they define WHAT is needed, not WHERE to get it. Concrete paths are provided at run time via assets (see `/eval-run`).

### Item schema

Each item in `artifacts.json → items`, `input.json → items`, `input.json → ground_truth → items`, `output.json → items`:

```json
{
  "type": "",
  "format": "",
  "description": "",
  "resource": ""
}
```

- `type`: one of `video|image|text|tabular|json|binary|model|checkpoint|config|log`
- `format`: file extension (e.g., `.onnx`, `.mp4`, `.json`)
- `description`: short text
- `resource`: key in `resources.json` indicating where this item typically comes from (e.g., `"server_172_31_60_66"`, `"aws"`, or `""` for local/unknown)

### Variable reference syntax `${}`

Use `${}` to reference items across config files. Resolved at runtime by `/eval-run` using assets.

| Prefix | Resolves to |
|--------|------------|
| `${project.xxx}` | project.json field (e.g., `${project.root}`, `${project.name}`) |
| `${resources.xxx.yyy}` | resources.json field (e.g., `${resources.aws.region}`) |
| `${artifact.xxx}` | resolved from artifact asset at runtime |
| `${input.xxx}` | resolved from input asset at runtime |
| `${output.xxx}` | stages/evaluation/runs/run_NNN/outputs/ (resolved at runtime) |

## Step 1: Analyze Code

Update workflow step to `analyze_code`.

Read all code files under the code directory (*.py, *.sh, *.yaml, *.yml, *.json, *.toml).

Determine:
- **entry_command**: how to run evaluation (e.g., `python eval.py --data val --weights model.pt`). See "Special case" above for mixed training/eval code.
- **config_format**: one or combination of: argparse, yaml, omegaconf, json, hydra (e.g., "argparse+omegaconf")
- **config_path**: where the config file lives relative to code/
- **framework**: e.g., pytorch, onnxruntime, tensorrt, custom
- **Dataset info**: fill `config.json → dataset` — see Dataset Detection below
- **Artifacts**: what static files the code needs (model weights, evaluator configs, etc.)
- **Inputs**: what dynamic data the code takes (test/val images, videos, text)
- **Ground truth**: what annotation/label data the code needs — see Ground Truth Detection below
- **Outputs**: what the code produces (result files, visualizations)
- **Metrics**: what numerical values the code reports — see Metrics Analysis below
- **Required packages**: run `python lifecycle/scripts/infer-init/scan_requirements.py <code_dir>`. Store result in `config.json → required_packages`. **Fallback**: if script fails, manually check for requirements.txt and parse it.

### Dataset detection

Look for dataset loading patterns to fill `config.json → dataset`:

- **name**: infer from code — `CocoDetection`, `ImageFolder`, `load_dataset("imagenet")`, path patterns like `coco/val2017`, `VOC2012/test`. Ask user to confirm.
- **split**: look for `--split`, `val`, `test`, `val2017`, `testdev` in args or paths
- **num_samples**: look for dataset length prints, `len(dataset)`, or known benchmark sizes
- **classes**: look for class lists, label maps, `num_classes` args. Can be empty if not applicable.

### Ground truth detection

Look for annotation/label data loading to fill `input.json → ground_truth`:

- **Common patterns**:
  - COCO-style: `COCO(annotation_file)`, `pycocotools`, `--ann-file`, `instances_val2017.json`
  - YOLO-style: label `.txt` files in parallel directory structure
  - VOC-style: XML annotation files
  - Generic: `--gt`, `--ground-truth`, `--annotations`, `--labels`, `--target`
  - Embedded: HDF5 datasets, TFRecord with labels, CSV with target column

- **Pairing mode** — set `input.json → pairing`:
  - `"single_file"` — one annotation file covers all inputs (COCO JSON, CSV manifest)
  - `"directory"` — per-input annotation files in parallel directory (YOLO .txt, VOC .xml)
  - `"embedded"` — GT embedded in input files (HDF5, TFRecord)
  - `"index"` — separate index/manifest maps inputs to GT entries

### Type classification rules

**Artifacts** (static, per model version):
- model weights: .onnx, .pt, .pth, .engine, .safetensors, .trt, .tflite → type `model`
- checkpoints: .ckpt → type `checkpoint`
- static configs: .yaml, .yml, .toml, .ini → type `config`
- evaluator tools, label maps, decoders → type `json` or `binary`

**Inputs** (dynamic, per run):
- video: .mp4, .avi, .mov, .mkv → type `video`
- images: .jpg, .png, .bmp, .tiff → type `image`
- text: .txt, .jsonl → type `text`
- tabular: .csv, .tsv, .parquet → type `tabular`

**Ground truth**:
- COCO annotations: .json → type `json`
- YOLO labels: .txt → type `text`
- VOC annotations: .xml → type `text`
- CSV labels: .csv → type `tabular`
- Embedded: .hdf5, .tfrecord → type `binary`

### Metrics analysis

Scan code for evaluation metrics. Look for:

- **Evaluation-specific patterns**: `COCOeval`, `evaluate()`, `compute_metrics()`, `classification_report`, `confusion_matrix`, `MeanAveragePrecision`, `MultiClassMetrics`
- **Common metric names**: accuracy, precision, recall, f1, f1_score, mAP, AP, AP50, AP75, mAP_small, mAP_medium, mAP_large, BLEU, ROUGE, FID, IS, PSNR, SSIM, WER, CER, IoU, mIoU, dice, AUC, top1, top5, perplexity, loss
- **Per-class output**: `per_category_ap`, `class_results`, loops printing per-class metrics, `ap_per_class`, `map_per_class`
- **Print/logging patterns**: `print(f"mAP: {mAP}")`, `logger.info(f"accuracy: {acc}")`, `results["mAP"]`
- **PL logger patterns**: `self.log_dict(metrics, ...)`, `self.logger.add_figure(...)` — metrics logged to TensorBoard/MLflow
- **Result file writes**: `json.dump(results, ...)`, `to_csv()`, `save_results()`, `pickle.dump(...)`

For each metric found, create an entry in `output.json → metrics.definitions`:
```json
{
  "type": "float|int",
  "source": "stdout|file|tensorboard",
  "pattern": "regex to extract from stdout (if source=stdout)",
  "path": "relative path to result file (if source=file)",
  "key": "JSON key in result file (if source=file)"
}
```

Note: `source: "tensorboard"` is for metrics logged via PyTorch Lightning `self.log_dict()`. These appear in TensorBoard event files and also in PL's stdout summary table.

Then ask user: "Found these metrics: mAP, AP50, AP75, accuracy, f1. Which ones do you want to track across runs?"

User's selection goes into `output.json → metrics.watch` as a list.

**Per-class metrics**: if the code produces per-class breakdowns, set `output.json → metrics.per_class` to `true`. Ask user to confirm.

## Step 2: Discover Real Config

Update workflow step to `discover_config`.

Look for the actual config file in the code directory:
1. Check config_path from analysis
2. Scan `configs/`, `config/`, `conf/` directories
3. Prefer files named "eval", "evaluate", "test", "val", "default", "baseline", "base", "main"
4. Pick the largest YAML file as fallback

If found, load all discovered parameters.

## Step 3: Select Managed Parameters

Update workflow step to `select_params`.

Config files can have dozens of parameters. Instead of dumping them all, use a progressive disclosure flow:

**3a. Show category summary first:**
```
Config: configs/eval.yaml (35 parameters)

Categories:
  Data paths:      4 params (image root, annotation path, output dir, ...)
  Model:           3 params (weights, architecture, num_classes)
  Eval settings:   6 params (batch_size, device, confidence_threshold, ...)
  Dataset/loader:  8 params (split, num_workers, transforms, ...)
  Other:          14 params

Which categories do you want to see? (or "all")
```

**3b. Expand requested categories:**
Show only the categories the user asked for, with current values:
```
Model:
  □ model.pretrained_weight = s3://...fp32.pth
  □ model.architecture = rtdetr_r50
  □ model.num_classes = 80

Eval settings:
  □ trainer.devices = [0]
  □ trainer.limit_test_batches = 1.0
  □ dataloader.test.batch_size = 32
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

After output.json is confirmed, ask one more question:
"Set a baseline for comparison? Options: (1) a previous run ID, (2) external numbers (e.g., paper results), (3) skip"
- If run ID → set `output.json → metrics.baseline` to that run ID string
- If external numbers → ask for each watched metric's baseline value, set as inline object
- If skip → leave as null

## Step 5: Validate

Update workflow step to `validate`.

### Checks to run

Run `python lifecycle/scripts/infer-init/validate_refs.py <stage_dir>` where stage_dir is the project's `stages/evaluation/` directory.

**Fallback**: if script fails, manually check these:
- Entry script file exists in code/
- All items have a `type` field with valid value
- Every `${artifact.xxx}` in runtime_params has a matching key in artifacts.json → items
- Every `${input.xxx}` has a matching key in input.json → items
- Every `${output.xxx}` has a matching key in output.json → items

Run `python lifecycle/scripts/eval-init/validate_ground_truth.py <stage_dir>` for GT consistency.

**Fallback**: if script fails, manually check:
- If `ground_truth.items` is non-empty, `pairing` must be set
- If `pairing` is `single_file`, GT should have one main item
- GT items should be consistent with pairing mode

Additional checks (always done by Claude, not scripted):
- config_path file exists in code/ (if specified)
- config_format is valid
- dataset.name is filled (warn if empty: "Consider filling dataset name for run comparison")
- Items that reference a resource: warn if that resource key doesn't exist in resources.json
- Semantic sanity: does the config make sense for the code?

**CRITICAL: Do not allow saving if there are broken references. User must fix before proceeding.**

Report errors/warnings to user. Let them fix or accept warnings.

## Step 6: Save

Update workflow step to `save`.

1. Write all 4 JSON files to `{project.root}/stages/evaluation/`
2. Create `{project.root}/stages/evaluation/assets/` directory if it doesn't exist
3. Pop self from `history.json` stack, append `completed` to history
4. **Downstream suggestion** (per Skill Dependency Graph in CLAUDE.md): offer `/eval-run`.
