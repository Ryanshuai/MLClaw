---
name: refactor-init
description: "Use this skill to set up a research code repository for refactoring. Clones the repo, analyzes codebase structure, classifies modules (core/support/dead), extracts paper benchmark targets, and creates a verification plan. Triggers for: '\u91CD\u6784\u4EE3\u7801', 'clean up research repo', 'analyze codebase', 'set up refactoring', 'prepare for code cleanup'. Not for executing refactoring rounds (use refactor-run)."
---

# /refactor-init — Refactor Stage Initialization

Clone a research code repository, analyze its structure, classify modules, extract paper benchmark targets, and configure the verification pipeline.

Ask one question at a time — multiple questions at once overwhelms users.

Follow the Workflow State Protocol from CLAUDE.md: push on entry, update step as you progress, pop on completion.

**Requires**: `project.json` exists (offer `/project-init` if not).

## Locate Project

Locate project per CLAUDE.md conventions. Show recent projects, let user pick. Verify `stages.refactor.enabled` is true (ask to enable if not). Set `PROJECT = {project.root}`.

## Step 1: Get Repository

Ask user for GitHub repository URL.

1. Clone into `{PROJECT}/stages/refactor/original/`. Ask for branch if needed.
2. Record in `plan.json -> repo`: url, branch, commit.
3. Copy to `{PROJECT}/stages/refactor/code/` (working copy; `original/` stays untouched as reference).
4. Init git in `code/`, commit: `"refactor: initial copy from original"`.
5. Update `project.json -> stages.refactor.code_source` (source, path, branch, commit).

## Step 2: Get Paper Info

Ask for paper reference (title, arXiv URL, PDF, or "check the README").

Record in `plan.json -> paper`: title, url.

Ask: "What are the benchmark results from the paper that we should match?" Guide user to provide dataset, model variant, and metrics. These become acceptance criteria — after refactoring, code must reproduce within tolerance (e.g., +/-0.5%).

Also set `output.json -> metrics.baseline` to paper numbers:
```json
"baseline": { "source": "paper", "metrics": { "mAP": 48.5, "AP50": 67.3 } }
```

## Step 3: Environment Detection

Detect environment requirements — do NOT install or modify, just understand what's needed.

### 3.1 Dependency files

Scan for: `requirements.txt`, `environment.yml`, `setup.py`, `pyproject.toml`, `Makefile`, `CMakeLists.txt`, `Dockerfile`. Record in `plan.json -> env.dependency_files`.

### 3.2 Custom build steps

Detect compilation needs: CUDA extensions (`CUDAExtension` in setup.py), custom operators (`*.cu`, `*.cpp` with `torch::`), build scripts. Record in `plan.json -> env.build_steps`.

CUDA extensions are fragile — if refactoring touches `csrc/` or modifies extension lists, they must be recompiled and re-tested. Flag these files as `core` with `status: "keep"` because changing them risks breaking the build and benchmark.

### 3.3 Git submodules

Check for `.gitmodules`. If present, initialize submodules in both `original/` and `code/`.

### 3.4 Create verification environment

Create an isolated env so the refactored code's dependencies are tested cleanly:

1. Resolve env manager from `{WORKSPACE}/resources.json -> local.env_manager.tool` (run `/resources` if empty).
2. Determine Python version from repo's config files.
3. Create env: `{env_manager} create -n refactor_{project_name} python=<version> -y` (or `uv venv`).
4. Install dependencies from repo's files (environment.yml, requirements.txt, setup.py — ask user if multiple).
5. Build CUDA extensions if detected.
6. Record: `plan.json -> env.env_name`, `env.python_path`, `env.created_from`.
7. Snapshot: `pip freeze > env_snapshot.txt`.
8. Sanity: `python -c "import <main_module>"`.

## Step 4: Analyze Codebase

Read all code files in `original/`. See `references/analysis-patterns.md` for detailed patterns on each sub-step below.

### 4a. Entry points
Find all runnable scripts. Record in `plan.json -> analysis.entry_points`.

### 4b. Core pipelines
Trace both inference and training pipelines. Record `core_pipeline` with `inference`, `training`, and `shared` lists.

### 4c. Module classification
Classify every file/directory as core/support/dead. Model architecture files default to `core` + `status: "keep"`. Record in `plan.json -> modules`.

### 4d. Duplicate code detection
Flag functions with >80% similarity across files. Record `duplicate_groups`.

### 4e. Testable boundaries
Identify module boundaries for snapshot comparison (backbone, head, postprocessing, etc.). Record `test_entry` per module.

### 4f. Stats
Count total files and lines. Record in `plan.json -> analysis`.

### 4g. Benchmark config
Fill 4 JSON configs (config.json, artifacts.json, input.json, output.json) for benchmark verification, same schema as `/eval-init`. Templates in `lifecycle/refactor/`.

## Step 5: Present Plan

Show summary: repository, paper, benchmark target, codebase stats, classification counts (core/support/dead with key files), estimated refactoring rounds.

**Stop and wait for user confirmation.** User may reclassify modules or adjust priorities.

## Step 6: Present Config Files

Show each JSON one at a time (config.json -> artifacts.json -> input.json -> output.json). Each requires explicit user confirmation before proceeding.

## Step 7: Validate & Save

Validate: entry script exists, items have valid types, `${}` references resolve, ground truth consistency.

Save: `plan.json` + 4 config JSONs to `{PROJECT}/stages/refactor/`. Create `runs/` directory.

**Downstream**: offer `/refactor-run` to start the first refactoring round.
