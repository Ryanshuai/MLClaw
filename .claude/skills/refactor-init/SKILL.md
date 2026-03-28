---
name: refactor-init
description: Clone a research repo, analyze its codebase, create a refactoring plan with paper benchmark targets
---

# /refactor-init — Refactor Stage Initialization

Clone a research code repository from GitHub, analyze its structure, classify modules (core / support / dead), extract paper benchmark targets, and configure the verification pipeline.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Record the answer, then ask the next. Never list multiple questions at once.

## Workflow State

On entry: push `{ "skill": "refactor-init", "step": "locate_project" }` to `history.json` stack.
Update step as you progress through each step below. On completion: pop from stack, append `completed` to history.

## Dependency Check

On entry, check upstream requirements per the Skill Dependency Graph in CLAUDE.md:
- **project.json exists**: if no project is found, offer to run `/project-init` first.

## Locate Project

Show recent projects and let user pick. Ask the user which one:

1. **Recent projects (top 5)** — scan `history.json` history across all projects in the workspace root (default `D:\agent_space\mlclaw\projects\`). List up to 5 most recently used projects with their paths.
2. **Provide a path** — user gives a path to the project directory (must contain `project.json`).
3. **Other** — user has another way to locate it.

Once `project.json` is found:
- Verify `stages.refactor.enabled` is true. If not, set it to true (ask user first).
- Set `PROJECT = {project.root}`.

## Step 1: Get Repository

Update workflow step to `get_repo`.

Ask user for the GitHub repository URL.

1. Clone the repo into `{PROJECT}/stages/refactor/original/`:
   ```
   git clone <url> {PROJECT}/stages/refactor/original/
   ```
   If a specific branch is needed, ask user (or detect from paper/README).

2. Record in `plan.json → repo`: url, branch, commit (HEAD after clone).

3. Copy the entire repo to `{PROJECT}/stages/refactor/code/`:
   ```
   cp -r {PROJECT}/stages/refactor/original/ {PROJECT}/stages/refactor/code/
   ```
   This is the working copy where all refactoring happens. `original/` stays untouched as reference.

4. Initialize git in `code/` if not already a git repo. Create an initial commit tagging the unmodified state:
   ```
   cd {PROJECT}/stages/refactor/code/
   git add -A && git commit -m "refactor: initial copy from original"
   ```

5. Update `project.json → stages.refactor`:
   - `code_source.source` = `"github"`
   - `code_source.path` = the GitHub URL
   - `code_source.branch` = branch name
   - `code_source.commit` = HEAD commit hash

## Step 2: Get Paper Info

Update workflow step to `get_paper`.

Ask user for the paper reference. Accept any of:
- Paper title
- arXiv / conference URL
- PDF path
- "check the README" (then read `original/README.md` for paper links)

Extract and record in `plan.json → paper`:
- `title`: paper title
- `url`: link to paper

Then ask: "What are the benchmark results from the paper that we should match?"

Guide the user to provide benchmark targets. For each benchmark:
```json
"benchmarks": {
  "<benchmark_name>": {
    "dataset": "<dataset name>",
    "model_variant": "<which model config, if multiple>",
    "metrics": {
      "<metric_name>": <value>,
      ...
    }
  }
}
```

Example interaction:
```
Paper benchmarks to verify against?

For example:
  "COCO val2017, RT-DETR-R50: mAP=48.5, AP50=67.3"
  or point me to a results table in the README.
```

If multiple model variants exist, ask which one to target. Record the chosen variant.

These benchmark numbers become the acceptance criteria — after refactoring, the code must reproduce these numbers (within a small tolerance, e.g., ±0.5%).

Also set `output.json → metrics.baseline` to the paper numbers as an inline object:
```json
"baseline": {
  "source": "paper",
  "metrics": { "mAP": 48.5, "AP50": 67.3 }
}
```

## Step 3: Environment Detection

Update workflow step to `detect_env`.

Research repos have diverse environment requirements. Detect and record them — do NOT install or modify anything, just understand what's needed.

### 3.1 Dependency files

Scan for:
- `requirements.txt`, `requirements/*.txt` (pip)
- `environment.yml`, `environment.yaml` (conda)
- `setup.py`, `setup.cfg`, `pyproject.toml` (package install)
- `Makefile`, `CMakeLists.txt` (build systems)
- `Dockerfile` (containerized env)

Record found files in `plan.json → env.dependency_files`.

### 3.2 Custom build steps

Detect if the repo needs compilation:
- **CUDA extensions**: `setup.py` with `ext_modules`, `CUDAExtension`, `CppExtension`
- **Custom operators**: `*.cu`, `*.cpp` files in source tree with `torch::` or `AT_DISPATCH`
- **Build scripts**: `build.sh`, `install.sh`, `make` targets

If found, record in `plan.json → env.build_steps`:
```json
{
  "has_cuda_extensions": true,
  "build_command": "pip install -e . / python setup.py build_ext --inplace",
  "custom_ops": ["csrc/nms_cuda.cu", "csrc/roi_align.cpp"]
}
```

**CRITICAL**: CUDA extensions are fragile. If refactoring touches files in `csrc/` or modifies `setup.py`'s extension list, the extension must be recompiled and re-tested. Flag these files as `core` with `status: "keep"` by default.

### 3.3 Git submodules

Check for `.gitmodules`. If present, ensure submodules are initialized:
```bash
git -C {PROJECT}/stages/refactor/original/ submodule update --init --recursive
git -C {PROJECT}/stages/refactor/code/ submodule update --init --recursive
```

Record submodule paths in `plan.json → env.submodules`.

### 3.4 Create verification environment

Create an isolated environment dedicated to this refactoring project. This ensures:
- The refactored code's dependencies are tested cleanly, not polluted by other projects
- Dependency changes from dead code removal are tracked naturally
- Verification runs in a reproducible environment

**Steps**:

1. **Resolve env manager**: read `{WORKSPACE}/resources.json → local.env_manager.tool`. If empty, run `/resources` to detect it. The tool (mamba/conda/uv) determines the commands below.

2. **Determine Python version**: check repo's `setup.py`, `pyproject.toml`, `environment.yml`, or README for version requirements. Ask user if unclear.

3. **Create environment**:
   ```bash
   # mamba/conda:
   {env_manager} create -n refactor_{project_name} python=<version> -y
   # uv:
   uv venv {PROJECT}/stages/refactor/.venv --python <version>
   ```

4. **Install dependencies** from the repo's dependency files (in order of preference):
   ```bash
   # mamba/conda — if environment.yml exists:
   {env_manager} env update -n refactor_{project_name} -f environment.yml
   # mamba/conda — if requirements.txt:
   {env_manager} run -n refactor_{project_name} pip install -r requirements.txt
   # uv — if requirements.txt:
   uv pip install -r requirements.txt --python {PROJECT}/stages/refactor/.venv
   # Any — if setup.py / pyproject.toml:
   {run_in_env} pip install -e .
   ```
   If multiple dependency files exist, ask user which to use.

5. **Build CUDA extensions** (if detected in Step 3.2):
   ```bash
   {run_in_env} python setup.py build_ext --inplace
   ```
   If build fails, report the error and ask user to help resolve.

6. **Record environment**:
   - `plan.json → env.env_name`: env name or venv path
   - `plan.json → env.python_path`: path to the env's Python executable
   - `plan.json → env.created_from`: which dependency file was used
   - Run `{run_in_env} pip freeze > {PROJECT}/stages/refactor/env_snapshot.txt` as baseline

7. **Quick sanity**: `{run_in_env} python -c "import <main_module>"` to verify the install works.

`{run_in_env}` is a shorthand — resolves to `mamba run -n refactor_{name}` or `source .venv/bin/activate &&` depending on env manager.

## Step 4: Analyze Codebase

Update workflow step to `analyze_code`.

Read all code files in `original/` (*.py, *.sh, *.yaml, *.yml, *.json, *.toml, *.cfg). Build an understanding of:

### 3a. Entry points

Find all runnable scripts — look for:
- `if __name__ == "__main__"` blocks
- Scripts referenced in README (training, evaluation, inference commands)
- Shell scripts (*.sh)
- Makefile targets
- `setup.py` / `pyproject.toml` console_scripts

Record in `plan.json → analysis.entry_points` as list of `{ "file": "...", "purpose": "train|eval|infer|data|util|unknown" }`.

### 3b. Core pipelines — inference AND training

Research repos typically contain both inference/eval and training code that share modules (model definition, data loading, etc.). Analyze **both** pipelines:

**Inference/eval pipeline** — trace from eval entry point to metrics output:
- Model definition (architecture classes)
- Data loading (dataset classes, eval transforms)
- Forward pass / inference logic
- Postprocessing (NMS, decoding)
- Metric computation
- Config / argument parsing

**Training pipeline** — trace from training entry point to checkpoint save:
- All of the above (shared with inference), plus:
- Loss function definition
- Optimizer and LR scheduler setup
- Backward pass / gradient computation
- Training loop logic (epoch, step, logging)
- Checkpoint save/load
- Data augmentation (training-only transforms)
- Distributed training wrappers (if applicable)

Record in `plan.json → analysis`:
```json
{
  "core_pipeline": {
    "inference": ["model/backbone.py", "model/head.py", "eval.py", ...],
    "training": ["train.py", "loss.py", "optimizer.py", ...],
    "shared": ["model/backbone.py", "model/head.py", "data/dataset.py", ...]
  }
}
```

The `shared` list is critical — these modules are on both pipelines. Refactoring shared modules is verified via the inference pipeline first (cheap), then confirmed in training context.

### 3c. Module classification

**Model architecture files are off-limits by default.** Files defining model architecture (layer definitions, forward pass, numerical operations) are classified as `core` with `status: "keep"` — not `"pending"`. Do not plan to refactor them unless the user explicitly requests it. Modifying model architecture breaks checkpoint compatibility and risks benchmark regression. If during analysis you see model files that are genuinely messy, note it in the plan but do not schedule them for refactoring — flag to user: "Model file X could be cleaned up, but this risks checkpoint/benchmark compatibility. Want to include it?"

For every file/directory, assign one of:
- **`core`**: on the critical path for benchmark reproduction. Must keep.
- **`support`**: used by core but can be simplified (utilities, logging, visualization helpers, complex config systems).
- **`dead`**: not needed for benchmark reproduction (other experiments, unused model variants, demo apps, notebooks).

Record in `plan.json → modules`:
```json
{
  "path/to/file_or_dir": {
    "classification": "core|support|dead",
    "path_type": "inference|training|shared|dead",
    "reason": "why this classification",
    "phase": null,
    "priority": null,
    "status": "pending",
    "testable": true,
    "test_entry": "ClassName or function_name for snapshot testing"
  }
}
```

- `path_type`: which pipeline this module belongs to. `shared` = used by both inference and training.
- `testable`: whether this module can be independently tested with snapshot comparison
- `test_entry`: the class or function to hook for snapshot capture (e.g., `"RTDETRDecoder"`, `"build_backbone"`, `"postprocess"`)
- `phase` and `priority`: assigned in Step 3f (Refactoring Order)

### Refactoring order — inference first, then training

**This is a hard constraint, not a suggestion.** The refactoring must proceed in two phases:

**Phase 1: Inference/Eval** (verified by paper benchmark)
- Refactor all inference-path and shared modules
- Verification: Tier 0–4 as described in `/refactor-run`
- Exit criteria: full benchmark matches paper numbers
- **Output**: a clean, verified eval pipeline that becomes the measurement tool for Phase 2

**Phase 2: Training** (verified using the Phase 1 eval pipeline)
- Refactor training-only modules (loss, optimizer, training loop, augmentation, etc.)
- Verification: use the already-verified eval pipeline to measure training quality
  - Original code trains N steps → checkpoint → eval with clean pipeline → baseline
  - Refactored code trains N steps → checkpoint → eval with clean pipeline → compare
- No need for gradient comparison or loss curve matching — eval metrics are the ground truth

This ordering works because:
1. Inference is cheaper to verify (one forward pass per sample)
2. Shared modules (model, data) are verified in Phase 1
3. Phase 2 only touches training-specific code, verified by Phase 1's trusted eval

Priority assignment within each phase:
- Phase 1, priority 1: dead code deletion (safe)
- Phase 1, priority 2: support simplification (moderate risk)
- Phase 1, priority 3: core cleanup (verify with benchmark)
- Phase 2, priority 4: training support (logging, config, checkpoint management)
- Phase 2, priority 5: training core (loss, optimizer, loop)
- Phase 2, priority 6: training infrastructure (distributed, mixed precision)

### 3d. Duplicate code detection

Scan for copy-pasted code across files. Research codebases frequently have identical or near-identical functions in multiple places (e.g., `load_checkpoint` in both train.py and eval.py, same transform pipeline copy-pasted).

For each pair of `.py` files, compare function bodies. Flag functions with >80% line similarity. Also look for identical code blocks >10 lines.

Record in `plan.json → analysis.duplicate_groups`:
```json
[
  {
    "function": "load_checkpoint",
    "locations": ["train.py:45", "eval.py:32", "utils/io.py:78"],
    "similarity": 0.92,
    "action": "consolidate into utils/io.py"
  }
]
```

These become priority 2 (support) refactoring targets — consolidate into one canonical copy, replace others with imports.

### 3e. Identify testable boundaries (for module-level verification)

Full benchmark runs are expensive (hours of GPU time). To verify refactoring correctness cheaply, identify module boundaries where we can do snapshot comparison:

For each `core` and `support` module that is `testable`:
1. Identify the class/function that forms the boundary (e.g., `class RTDETRDecoder`, `def build_backbone()`)
2. Determine what input it takes (tensor shapes, config params) and what it returns
3. Record in `plan.json → modules → {path} → test_entry`

These boundaries will be used in `/refactor-run` to generate snapshot tests: run the original module with a fixed input, save the output, then verify the refactored module produces the same output.

**Good boundaries** (clear input→output, deterministic):
- Model submodules: backbone, neck, head, decoder
- Data transforms / preprocessing pipelines
- Postprocessing: NMS, decoding, metric computation
- Utility functions with tensor input/output

**Bad boundaries** (skip these):
- Entire training loops (too coarse, non-deterministic)
- I/O-heavy code (file loading, network calls)
- Code with random state that can't be seeded

### 3f. Stats

Count total files and lines. Record in `plan.json → analysis`.

### 3g. Benchmark config

Fill the 4 JSON config files exactly as `/eval-init` does (same schema), but for the benchmark verification task:

- **config.json**: entry command for benchmark, config format, framework, dataset info, required packages
- **artifacts.json**: model weights needed (items + sources if known)
- **input.json**: benchmark dataset + ground truth
- **output.json**: metrics definitions + watched metrics + paper baseline

Follow the same analysis patterns as `/eval-init` Step 1 (entry command detection, dataset detection, ground truth detection, metrics analysis). The templates are in `lifecycle/refactor/`.

## Step 5: Present Plan

Update workflow step to `review_plan`.

Show the refactoring plan summary:

```
Repository: <url> (<branch>, <commit_short>)
Paper: <title>
Benchmark target: <dataset> — <metric1>=<value1>, <metric2>=<value2>

Codebase: <total_files> files, <total_lines> lines

Classification:
  Core:    <N> files — <list key files>
  Support: <N> files — <list key dirs/files>
  Dead:    <N> files — <list key dirs/files>

Refactoring rounds (estimated):
  Round 1: Delete dead code (<N> files, ~<M> lines)
  Round 2: Simplify support modules
  Round 3: Clean core code
```

**STOP and wait for user confirmation.** User may reclassify modules or adjust priorities.

## Step 6: Present Config Files

Update workflow step to `review_config`.

Show each JSON file one at a time, same as `/eval-init` Step 4:
- config.json → artifacts.json → input.json → output.json

**Each file requires explicit user confirmation before proceeding to the next.**

## Step 7: Validate & Save

Update workflow step to `validate`.

Run the same validation as `/eval-init` Step 5:
- Entry script exists in code/
- All items have valid type
- `${}` references resolve
- Ground truth consistency

Then save all files:
- `plan.json` to `{PROJECT}/stages/refactor/`
- 4 config JSONs to `{PROJECT}/stages/refactor/`
- Create `{PROJECT}/stages/refactor/runs/` directory

Pop self from `history.json` stack, append `completed` to history.

**Downstream suggestion**: offer `/refactor-run` to start the first refactoring round.
