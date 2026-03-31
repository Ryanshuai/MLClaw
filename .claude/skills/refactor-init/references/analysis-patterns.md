# Codebase Analysis Patterns for Refactor-Init

Reference document for Step 4 (Analyze Codebase) of `/refactor-init`. Contains detailed patterns for module classification, pipeline analysis, duplicate detection, and testable boundary identification.

## Entry Point Detection

Find all runnable scripts:
- `if __name__ == "__main__"` blocks
- Scripts referenced in README (training, evaluation, inference commands)
- Shell scripts (*.sh), Makefile targets
- `setup.py` / `pyproject.toml` console_scripts

Record as: `{ "file": "...", "purpose": "train|eval|infer|data|util|unknown" }`.

## Core Pipeline Analysis

Research repos typically contain both inference and training code sharing modules. Analyze **both** pipelines:

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

Record in `plan.json -> analysis.core_pipeline` with `inference`, `training`, and `shared` lists. The `shared` list is critical — these modules are on both pipelines. Refactoring shared modules is verified via inference first (cheap), then confirmed in training context.

## Module Classification

For every file/directory, assign:
- **`core`**: on the critical path for benchmark reproduction. Must keep.
- **`support`**: used by core but can be simplified (utilities, logging, visualization, config systems).
- **`dead`**: not needed for benchmark reproduction (other experiments, unused model variants, demo apps, notebooks).

**Model architecture files are off-limits by default.** Files defining model architecture (layer definitions, forward pass, numerical operations) get `core` with `status: "keep"` — not `"pending"`. Modifying model architecture breaks checkpoint compatibility and risks benchmark regression. If a model file looks genuinely messy, note it in the plan but do not schedule it — flag to user: "Model file X could be cleaned up, but this risks checkpoint/benchmark compatibility. Want to include it?"

Record in `plan.json -> modules`:
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

## Refactoring Order — Inference First, Then Training

This ordering is a hard constraint because:
1. Inference is cheaper to verify (one forward pass per sample)
2. Shared modules (model, data) are verified in Phase 1
3. Phase 2 only touches training-specific code, verified by Phase 1's trusted eval

**Phase 1: Inference/Eval** (verified by paper benchmark)
- Refactor all inference-path and shared modules
- Verification: Tier 0-4 as described in `/refactor-run`
- Exit criteria: full benchmark matches paper numbers
- Output: a clean, verified eval pipeline that becomes the measurement tool for Phase 2

**Phase 2: Training** (verified using the Phase 1 eval pipeline)
- Refactor training-only modules (loss, optimizer, training loop, augmentation, etc.)
- Verification: original trains N steps -> checkpoint -> eval with clean pipeline -> baseline vs refactored trains N steps -> checkpoint -> eval with clean pipeline -> compare

Priority assignment within each phase:
- Phase 1, priority 1: dead code deletion (safe)
- Phase 1, priority 2: support simplification (moderate risk)
- Phase 1, priority 3: core cleanup (verify with benchmark)
- Phase 2, priority 4: training support (logging, config, checkpoint management)
- Phase 2, priority 5: training core (loss, optimizer, loop)
- Phase 2, priority 6: training infrastructure (distributed, mixed precision)

## Duplicate Code Detection

Scan for copy-pasted code across files. Research codebases frequently have identical functions in multiple places (e.g., `load_checkpoint` in both train.py and eval.py).

For each pair of `.py` files, compare function bodies. Flag functions with >80% line similarity. Also look for identical code blocks >10 lines.

Record in `plan.json -> analysis.duplicate_groups`:
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

These become priority 2 (support) refactoring targets — consolidate into one canonical copy.

## Testable Boundaries (Module-Level Verification)

Full benchmarks are expensive (hours of GPU). To verify correctness cheaply, identify module boundaries for snapshot comparison.

For each `core` and `support` module marked `testable`:
1. Identify the class/function boundary (e.g., `RTDETRDecoder`, `build_backbone()`)
2. Determine input (tensor shapes, config params) and output
3. Record in `plan.json -> modules -> {path} -> test_entry`

**Good boundaries** (clear input->output, deterministic):
- Model submodules: backbone, neck, head, decoder
- Data transforms / preprocessing pipelines
- Postprocessing: NMS, decoding, metric computation
- Utility functions with tensor input/output

**Bad boundaries** (skip):
- Entire training loops (too coarse, non-deterministic)
- I/O-heavy code (file loading, network calls)
- Code with unseeded random state
