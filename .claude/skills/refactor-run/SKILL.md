---
name: refactor-run
description: Execute one refactoring round — make changes, verify benchmark, commit or revert
---

# /refactor-run — Run Refactoring Round

Execute one round of refactoring on the code in `stages/refactor/code/`. Each invocation = one round. Multiple rounds across multiple conversations until the codebase is fully cleaned up.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Record the answer, then ask the next. Never list multiple questions at once.

## Workflow State

On entry: push `{ "skill": "refactor-run", "step": "locate_project" }` to `history.json` stack.
Update step as you progress. On completion: pop from stack, append `completed` to history.

## Dependency Check

On entry, check upstream requirements per the Skill Dependency Graph in CLAUDE.md:
- **refactor-init done**: `{PROJECT}/stages/refactor/plan.json` must exist with non-empty `modules`. If not, offer to run `/refactor-init` first.
- **resources.json for credentials**: checked lazily when benchmark needs non-local resources.

## Locate Project

Show recent projects and let user pick (same as other skills).

Once `project.json` is found, set `PROJECT = {project.root}`:
- Read `{PROJECT}/stages/refactor/plan.json`
- Read `{PROJECT}/stages/refactor/config.json`
- Read `{PROJECT}/stages/refactor/output.json` (for benchmark targets)

## Resume Check

Check if there is an unfinished round:

1. Scan `{PROJECT}/stages/refactor/runs/` for any `run.json` with `status: "running"` → status check on benchmark (see Step 4).
2. Scan for `status: "pending"` with partially completed steps → resume from last completed step.
3. If no unfinished round → proceed to new round.

## Step 1: Plan Round

Update workflow step to `plan_round`.

Determine the current round number from `plan.json → rounds` (length + 1).

Show the current state:
```
Refactoring progress:
  Completed rounds: <N>
  Modules remaining: <core: N, support: N, dead: N pending>

Next priority items:
  1. [dead] experiments/ablation/ — unused ablation study (42 files)
  2. [dead] tools/visualize.py — standalone visualization script
  3. [support] utils/distributed.py — multi-GPU wrapper, not needed for single-GPU eval
```

Ask user: "Proceed with these, or pick different targets?"

Record the round plan:
```json
{
  "round": 3,
  "targets": ["experiments/ablation/", "tools/visualize.py", "utils/distributed.py"],
  "strategy": "delete dead code, simplify distributed wrapper"
}
```

Append to `plan.json → rounds`.

### Cascade detection (after every round)

After each completed round, re-scan the codebase for new simplification opportunities that were unlocked by the changes:

- **Dead branches**: `if/else` where one branch references deleted code → now always takes the other branch → flatten
- **Orphaned imports**: imports of deleted modules in files that weren't directly modified this round
- **Collapsed wrappers**: functions that now just call one other function → inline
- **Single-implementation abstractions**: an ABC/interface with only one remaining implementation → remove the abstraction
- **Unused arguments**: function params that were only used by deleted code paths → remove

These newly discovered opportunities are added to `plan.json → modules` with the note `"discovered_after_round": N` and inserted into the next round's targets. This keeps the plan evolving — dead code deletion cascades into support simplification naturally.

### Duplicate code re-scan

Re-run duplicate detection (same method as `/refactor-init` Step 3d) on the current codebase. Previous deletions may have exposed new duplicates or made existing ones more obvious. Add findings to `plan.json → analysis.duplicate_groups`.

### Scope per round

Keep each round focused and verifiable:
- **Dead code rounds**: delete multiple dead files/dirs at once (low risk, batch OK)
- **Support rounds**: simplify one module at a time (medium risk)
- **Core rounds**: change one file at a time (high risk, minimal batching)

## Step 2: Refactor

Update workflow step to `refactor`.

Work in `{PROJECT}/stages/refactor/code/`. Make the planned changes:

### For dead code (priority 1):
1. Delete the targeted files/directories
2. Remove imports of deleted modules from remaining files
3. Fix any broken references (e.g., config entries pointing to deleted code)
4. Ensure no circular dependency breaks

### For support code (priority 2):
1. Identify what core code actually uses from this module
2. Inline small utilities into their callers, or keep a minimal version
3. Remove unused functions/classes
4. Simplify complex abstractions (e.g., replace a 200-line config system with 20 lines of argparse)

### For core code (priority 3):
1. Remove dead branches (unused if/else paths, disabled features)
2. Simplify data flow (remove unnecessary abstractions between data and model)
3. Clean up naming, remove cryptic variable names
4. Remove hardcoded paths, magic numbers — parameterize if needed
5. **Model architecture files are off-limits by default.** Do not modify model definition code (architecture, forward pass, layer definitions, numerical operations) unless the user explicitly requests it. If during refactoring you believe a model file needs changes, **stop and ask the user first** — explain what you want to change and why. Model changes break checkpoint compatibility and risk benchmark regression.

### Auto-cleanup pass (every round, after manual changes)

Run automated cleanup tools on all modified files. These are mechanical, zero-risk transformations that dramatically improve readability. Run them **every round** without asking — they're always beneficial.

**1. Remove unused imports** — `autoflake`:
```bash
autoflake --in-place --remove-all-unused-imports --remove-unused-variables <changed_files>
```
Dead code deletion always leaves orphaned imports. This catches them all.

**2. Sort imports** — `isort`:
```bash
isort --profile black <changed_files>
```

**3. Format code** — `ruff format` (preferred) or `black`:
```bash
ruff format <changed_files>
# fallback: black <changed_files>
```

**4. Lint and auto-fix** — `ruff check`:
```bash
ruff check --fix <changed_files>
```
Catches: unused variables, f-string issues, simplifiable comparisons, redundant `pass` statements, etc.

**Tool availability**: check which tools are installed before running. If a tool is missing, skip it silently — don't install anything, don't block the workflow. The cleanup is best-effort.

**Scope**: only run on files touched in this round (from `files_changed` + `files_added`), not the entire codebase. This keeps it fast and focused.

**After cleanup, also scan for**:
- Unused function/class definitions left behind by dead code deletion (tools won't catch these — Claude should grep for unreferenced `def`/`class` in modified files)
- Empty files (all code removed → delete the file)
- Empty `__init__.py` that only re-exported deleted modules → clean up

### After all changes:
Record what was done in the run's `steps.refactor`:
- `files_changed`: list of modified files
- `files_deleted`: list of removed files
- `files_added`: list of new files (rare — only for extracted/split modules)
- `summary`: one-line description of changes
- `auto_cleanup`: which tools ran and what they changed (e.g., `"autoflake: removed 23 unused imports in 8 files"`)

Commit changes in `{PROJECT}/stages/refactor/code/`:
```
git add -A && git commit -m "refactor round <N>: <summary>"
```

## Step 3: Verify — Multi-Tier Verification

Verification uses a cost-efficient hierarchy. Only escalate to the next tier when the current tier passes. Early tiers are cheap enough to run every round; expensive tiers are reserved for milestones.

Both phases use the same Tier 0→1→2→3→4 structure, but the content of each tier differs:

```
Tier   Phase 1 (inference/eval)              Phase 2 (training)
─────  ─────────────────────────────────────  ──────────────────────────────────────────
  0    Static analysis (zero cost)            Static analysis (zero cost)
  1    Smoke: import + --help (seconds)       Smoke: training starts, no crash (seconds)
  2    Module snapshot: eval mode (sec~min)   Module snapshot: loss_fn, optimizer step,
                                                lr schedule, augmentation (sec~min)
  3    Mini benchmark: 100 samples (minutes)  Mini train: N steps → eval (minutes~hour)
  4    Full benchmark: full dataset (hours)   Full train → eval (days, final only)
```

The current phase is determined from `plan.json → phases`. Phase 2 cannot start until Phase 1's `benchmark_verified` is true.

**All verification commands run in the dedicated mamba env** created by `/refactor-init`:
```bash
mamba run -n {plan.json → env.env_name} <command>
```

### Tier 0: Static Analysis

Update workflow step to `verify_static`.

Before any runtime check, analyze whether the changes can possibly affect benchmark output. Build or update the dependency graph of the codebase (imports, function calls, class inheritance).

**For dead code deletion**: check if any surviving file imports or references the deleted code.
- Walk all `.py` files in `code/` (excluding deleted ones)
- Search for `import <deleted_module>`, `from <deleted_module> import`, and string references to deleted names
- If **zero references found** → the deletion is provably safe. Mark verification as `"static_pass"` and **skip Tier 1–3 entirely**.
- If references found → those references are bugs to fix first, then proceed to Tier 1.

**For support/core changes**: check the blast radius.
- Which other modules import or call the changed code?
- If the change is purely subtractive (removed unused functions, no signature changes) and no external caller uses the removed parts → `static_pass`.
- If signatures changed, return types changed, or callers exist → must proceed to Tier 1+.

Record in `run.json → steps.verify_static`:
```json
{
  "status": "completed",
  "at": "...",
  "result": "static_pass|needs_runtime",
  "reason": "no surviving code references deleted modules",
  "references_found": []
}
```

If `result == "static_pass"` → skip to Step 5 (Commit or Revert). No runtime verification needed.

### Tier 1: Smoke Test

Update workflow step to `verify_smoke`.

**Both phases**:
1. **Build check** (if `plan.json → env.build_steps` exists): re-run the build command (`pip install -e .` or `python setup.py build_ext --inplace`). If refactoring touched `setup.py`, `csrc/`, or any file listed in `env.custom_ops`, this is mandatory. Otherwise skip.
2. **Import check**: `python -c "import <main_module>"` from code/. Catches broken imports from deleted code.
3. **Dependency delta** (periodic, not every round): compare `requirements.txt` with actually used imports. After deleting dead code, some packages may no longer be needed. If confirmed unused, remove from `requirements.txt` and uninstall from the mamba env:
   ```bash
   mamba run -n {env_name} pip uninstall <package> -y
   ```
   This keeps the env minimal and tracks the real dependency footprint of the refactored code.

**Phase 1 additional**:
4. **Dry run**: eval entry with `--help` or minimal config to check startup.

**Phase 2 additional**:
4. **Dry run**: training entry with `--help` or minimal config.
5. **1-step training**: run 1 training step with minimal data. Catches broken loss, optimizer, data loader. Cheap (seconds).

If any fails → fix or revert.

### Tier 2: Module Verification

Update workflow step to `verify_module`.

For each module changed in this round that has `testable: true` in `plan.json`.
Only test modules that were **actually modified in this round** — previously verified modules are not re-tested unless their dependencies changed.

Tier 2 has two sub-tiers. Try 2a first; if it passes, skip 2b.

#### Tier 2a: Computation Graph Comparison (zero cost, no data needed)

For `nn.Module` subclasses (model, backbone, head, decoder, loss, etc.), compare the computation graph structure between original and refactored versions using `torch.fx`:

```python
import torch.fx

# Trace both versions
original_graph = torch.fx.symbolic_trace(original_module).graph
refactored_graph = torch.fx.symbolic_trace(refactored_module).graph

# Compare: same ops, same topology, same shape propagation
original_ops = [(n.op, n.target, n.args) for n in original_graph.nodes]
refactored_ops = [(n.op, n.target, n.args) for n in refactored_graph.nodes]
```

**If graphs are structurally identical** → mathematically equivalent for all inputs. Mark as `graph_pass` and **skip Tier 2b** (no need for tensor comparison).

**If graphs differ** → the difference may be intentional (simplified logic) or a bug. Show the diff and proceed to Tier 2b for numerical verification.

**Fallback options** when `torch.fx` tracing fails (dynamic control flow, unsupported ops):
- Try `torch.export` (more robust for modern PyTorch)
- Try ONNX export: `torch.onnx.export()` both versions, compare ONNX graph structure
- If all tracing fails → skip 2a, go directly to 2b

**Also check state_dict key compatibility**:
```python
original_keys = set(original_module.state_dict().keys())
refactored_keys = set(refactored_module.state_dict().keys())
added = refactored_keys - original_keys
removed = original_keys - refactored_keys
```
If keys differ → **warning**: pretrained weights won't load directly. Show the diff. If intentional (renamed layers), record the key mapping for checkpoint migration.

Record in `run.json → steps.verify_module.graph_check`:
```json
{
  "method": "torch.fx|torch.export|onnx|skipped",
  "result": "graph_pass|graph_diff|trace_failed",
  "state_dict_compatible": true,
  "key_diff": { "added": [], "removed": [], "mapping": {} }
}
```

#### Tier 2b: Tensor Snapshot Comparison (seconds~minutes, needs data)

Only needed when Tier 2a graph comparison shows a diff or fails to trace.

**Phase 1**: snapshot in eval mode (forward pass only). See below for details.

**Phase 2**: snapshot training-specific modules with fixed seed + deterministic mode:
- **Loss function**: fixed pred + target tensors → compare loss value (exact match)
- **Optimizer step**: fixed gradients → compare parameter updates (exact match)
- **LR scheduler**: compare lr values for first 100 steps (pure math, exact match)
- **Data augmentation**: fixed seed + fixed image → compare augmented output
- **Checkpoint round-trip**: save state_dict → load → compare all values
- **Gradient check**: 1 forward+backward with fixed input → compare per-layer gradients (tolerance: 1e-5 for CUDA non-determinism)

These are all fast (seconds each) because they test isolated components, not the full training loop.

**Snapshot capture & comparison**:

1. **Capture** (first time only): generate a bespoke script per module that runs the **original** code with a fixed input (random tensor with correct shape, `torch.manual_seed(42)`), saves input+output to `{PROJECT}/stages/refactor/snapshots/<module_name>.pkl`. Run from `original/` directory.

2. **Compare**: load snapshot, run **refactored** module with same input, compare outputs. Tolerance: `1e-5` for float tensors (user can override).

Scripts are auto-generated by Claude based on analyzing each module's interface — not a generic framework, because research code structures vary wildly.

**Results**

```
Module verification:
  backbone (ResNet50):     PASS (max diff: 2.3e-7)
  neck (FPN):              PASS (max diff: 1.1e-6)
  decoder (RTDETRDecoder): PASS (max diff: 8.4e-7)
```

If any module fails:
- Show the diff magnitude and which output keys diverged
- Ask: "Fix and retry, or revert round?"

Record results in `run.json → steps.verify_module`:
```json
{
  "status": "completed",
  "at": "...",
  "modules_tested": {
    "backbone": { "pass": true, "max_diff": 2.3e-7 },
    "decoder":  { "pass": true, "max_diff": 8.4e-7 }
  }
}
```

#### Performance Regression Check (piggybacks on Tier 2b)

When running Tier 2b tensor snapshots, also measure speed and memory — negligible extra cost since the module is already being run.

**How**: wrap the module call with profiling (warmup 3 runs, then measure 10 runs average). Record `torch.cuda.max_memory_allocated()` for peak GPU memory.

Run the same profiling on original (cached in snapshots) and refactored. Compare:

```
Performance check:
  backbone:   12.3ms / 245MB → 12.1ms / 245MB  ✓
  decoder:     8.7ms / 180MB →  8.9ms / 180MB  ✓
  full model: 45.2ms / 1.2GB → 44.8ms / 1.2GB  ✓
```

**Thresholds**: warn if >10% slower or >10% more memory. Fail if >25%. This is a **warning, not a blocker** — minor performance trade for readability is acceptable, but large regressions indicate accidental changes.

Record in `run.json → steps.verify_module.perf`:
```json
{
  "modules": {
    "backbone": {
      "original_ms": 12.3, "refactored_ms": 12.1, "delta_pct": -1.6,
      "original_mem_mb": 245, "refactored_mem_mb": 245, "mem_delta_pct": 0
    }
  },
  "warnings": []
}
```

Skip for: dead code rounds, CPU-only runs (timing unstable), Phase 2 training-specific modules (loss/optimizer/scheduler overhead is negligible).

### Tier 3: Mini End-to-End

Update workflow step to `verify_mini`.

**Phase 1 — Mini eval benchmark** (minutes):

Run the eval pipeline end-to-end on a tiny data subset (~100 samples). Catches integration issues that module snapshots miss (data loading, output format, metric computation).

**When to run**: every round that touches core code, or after support changes to data pipeline / metrics. Skip for dead code rounds.

**How**:
1. Use the same entry command from `config.json`
2. Limit data: `--num_samples 100`, `--limit_test_batches 0.02`, or whichever limiting arg the code supports
3. Run synchronously (minutes)
4. Extract metrics, compare against paper targets

**Tolerance**: ±2% relative or ±1.0 absolute (small sample = natural variance).

```
Mini eval benchmark (100 samples):
  mAP:  47.8  (paper: 48.5, delta: -0.7, -1.4%)  ✓ within mini tolerance
  AP50: 66.5  (paper: 67.3, delta: -0.2, -0.3%)  ✓ within mini tolerance
  Verdict: PASS
```

**Phase 2 — Mini training → eval** (minutes~hour):

Run refactored training code for N steps, then evaluate the checkpoint with the verified Phase 1 eval pipeline.

**When to run**: every round that touches training core (loss, optimizer, training loop). Skip for training support (logging, config management).

**How**:
1. Run refactored training for N steps (default: 100, user-configurable)
2. Save checkpoint
3. Run verified eval pipeline on the checkpoint
4. Compare eval metrics against the training baseline (recorded at Phase 2 start)

**Training baseline** (captured once at Phase 2 start):
- Run **original** training code for the same N steps, same config, same seed
- Eval the checkpoint → record as `plan.json → phases.phase_2.training_baseline`

**Tolerance**: ±1% relative (training has inherent variance even with fixed seeds due to CUDA non-determinism in backward pass).

```
Mini training verification (100 steps):
  Original  (100 steps) → eval: mAP=12.3, loss=2.41
  Refactored (100 steps) → eval: mAP=12.1, loss=2.43
  Verdict: PASS (within training variance)
```

Record in `run.json → steps.verify_mini`:
```json
{
  "status": "completed",
  "at": "...",
  "phase": 1,
  "num_samples": 100,
  "metrics": { "mAP": 47.8, "AP50": 66.5 },
  "pass": true
}
```

Phase 2 adds: `"training_steps": 100, "baseline_metrics": { ... }`

If mini fails badly (>5% regression) → likely a real bug. Investigate before proceeding.

### Tier 4: Full End-to-End (milestone only)

Update workflow step to `benchmark`.

**Phase 1 — Full eval benchmark** (hours):

Run the complete eval benchmark on the full dataset. Only at milestones:
- After all dead code is removed (end of priority 1)
- After all support code is simplified (end of priority 2)
- After final inference core cleanup (end of priority 3)
- Or when user explicitly requests it

Ask user: "Module tests passed. Run full benchmark now, or save it for later?"

**Phase 2 — Full training → eval** (days):

Run the complete training schedule, then evaluate. Only at:
- End of Phase 2 (all training modules refactored)
- Or when user explicitly requests it

This is the most expensive verification — days of GPU time. Should only run once, as the final confirmation that the refactored training code reproduces the paper's training results.

Ask user: "Training module tests passed. Run full training? This will take <estimated time>."

If running, follow `/eval-run` Steps 1–3 (resolve sources, create run, build & execute), adapted for the refactor stage:
- Run template: `lifecycle/refactor/refactor_run.json` (set `round` to current round number)
- Run directory: `{PROJECT}/stages/refactor/runs/run_{YYYYMMDD}_{HHmmss}/`
- Sources: reuse from previous round if unchanged
- First benchmark: debug mode. Subsequent: ask user.
- Phase 2: run training command instead of eval command, then eval the checkpoint with Phase 1 pipeline.

**CRITICAL: Never block waiting for a long-running benchmark. Return immediately and let user check back.**

## Step 4: Compare Results

Update workflow step to `compare`.

### Phase 1 (inference/eval rounds): compare against paper

Only applies when Tier 3 mini benchmark or Tier 4 full benchmark was run. Skip if only Tier 0–2.

After benchmark completes, extract metrics (same as `/eval-run` Step 4).

Compare against paper targets from `plan.json → paper.benchmarks`:

```
Milestone benchmark (after priority 1 — dead code removal):
  Dataset: COCO val2017 (5000 images)

  Metrics vs paper targets:
    mAP:  48.3  (paper: 48.5, delta: -0.2, -0.4%)  ✓ within tolerance
    AP50: 67.1  (paper: 67.3, delta: -0.2, -0.3%)  ✓ within tolerance

  Verdict: PASS (all metrics within ±0.5% of paper)
```

**Tolerance**: default ±0.5% relative or ±0.3 absolute (whichever is more lenient). User can override.

### Phase 2 (training rounds): train → eval with verified pipeline

Same comparison logic as Tier 3 Phase 2 (train N steps → eval with verified pipeline → compare against training baseline), but applied to the full benchmark result when Tier 4 is run.

**Training tolerance**: ±1% relative (inherent CUDA non-determinism in backward pass).

**Progressive N**: 100 steps for most rounds, full training only at the very end.

Record in `run.json → steps.compare`:
- `pass`, `phase`, `deltas`, and for Phase 2: `training_steps`, `baseline_run_id`

Also write extracted metrics to `run.json → metrics`.

## Step 5: Commit or Revert

Update workflow step to `commit_or_revert`.

### If PASS:
1. Tell user: "Round <N> passed. Changes committed."
2. The git commit from Step 2 stands.
3. Update `plan.json → modules` — set targeted modules' status to `"refactored"` or `"deleted"`.
4. Update `plan.json → rounds[-1]` with result: `"pass"`.
5. Record in `run.json → steps.commit_or_revert`: `action = "commit"`, `commit_hash = <hash>`.

### If FAIL:
1. Show which metrics regressed and by how much.
2. Ask user: "Revert this round, or investigate?"
   - **Revert**: `git revert HEAD` in `code/`, update round status to `"failed"`, record `action = "revert"`.
   - **Investigate**: user debugs, makes targeted fixes, then re-run benchmark (loop back to Step 4). Each retry is tracked as an ad_hoc step.

### If PARTIAL (some metrics pass, some fail):
1. Show breakdown.
2. Ask user: "Accept with regressions, revert, or investigate?"

## Step 6: Finalize Round

Update workflow step to `finalize`.

1. Finalize `run.json`: status, duration, metrics.
2. Update `runs_index.json`.
3. Pop self from `history.json` stack, append `completed` to history.

Show progress summary:
```
Refactoring progress after round <N>:
  Original:  <total_files> files, <total_lines> lines
  Current:   <current_files> files, <current_lines> lines (<pct>% reduction)
  Rounds:    <completed>/<planned> completed
  Status:    <N dead removed, M support simplified, K core cleaned>

Remaining:
  [support] utils/logger.py — complex logging framework
  [core] models/backbone.py — cleanup naming
```

4. Check what comes next:

**If Phase 1 modules remain** → offer next `/refactor-run` round.

**If Phase 1 complete, Phase 2 not started** → phase transition:
   ```
   Phase 1 (inference/eval) complete!
     Eval benchmark: mAP=48.4 (paper: 48.5) ✓
     Eval pipeline is now verified and trusted.

   Ready for Phase 2 (training code).
     Training modules remaining: <N> (loss, optimizer, training loop, ...)

   Before starting Phase 2, we need a training baseline:
     Run original training code for N steps → eval → record baseline metrics.
     How many steps? (recommend: 100 for quick check, 1000 for confidence)
   ```

**If Phase 2 modules remain** → offer next `/refactor-run` round.

**If all modules done** → declare refactoring complete, then **promote**:

   ```
   Refactoring complete!
     Before: 142 files, 18,500 lines
     After:   38 files,  4,200 lines (77% reduction)

     Inference benchmark: mAP=48.4 (paper: 48.5) ✓
     Training verification: mAP=32.0 (baseline: 32.1, 1000 steps) ✓

   The code in stages/refactor/code/ is the clean version.
   ```

   **Promote** — link the refactored code to other stages so `/eval-run`, `/infer-run`, etc. can use it directly.

   Ask user: "Which stages should use the refactored code?"
   - Show available stages from `project.json → stages` (evaluation, inference, training, ...)
   - For each selected stage, update `project.json → stages.{stage}.code_source`:
     ```json
     {
       "source": "local",
       "path": "{PROJECT}/stages/refactor/code",
       "branch": null,
       "commit": null
     }
     ```
   - This makes the refactored `code/` directory the code source for that stage — a soft link, no copy.
   - The stage's existing `code_path` (`stages/{stage}/code/`) is still available for stage-specific overrides.

   If user hasn't run `/eval-init` or `/infer-init` yet, suggest running them on the refactored code.

   Offer `/refactor-report` to generate the audit report.
