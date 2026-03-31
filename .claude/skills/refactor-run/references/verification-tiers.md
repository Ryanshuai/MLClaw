# Verification Tiers — Detailed Reference

Multi-tier verification for refactoring rounds. Both phases use the same Tier 0-4 structure; the content of each tier differs by phase.

```
Tier   Phase 1 (inference/eval)              Phase 2 (training)
-----  -------------------------------------  ------------------------------------------
  0    Static analysis (zero cost)            Static analysis (zero cost)
  1    Smoke: import + --help (seconds)       Smoke: training starts, no crash (seconds)
  2    Module snapshot: eval mode (sec~min)   Module snapshot: loss_fn, optimizer step,
                                                lr schedule, augmentation (sec~min)
  3    Mini benchmark: 100 samples (minutes)  Mini train: N steps -> eval (minutes~hour)
  4    Full benchmark: full dataset (hours)   Full train -> eval (days, final only)
```

Current phase comes from `plan.json -> phases`. Phase 2 cannot start until Phase 1's `benchmark_verified` is true.

All verification commands run in the project env: `{run_in_env} <command>`.

---

## Tier 0: Static Analysis

Step key: `verify_static`.

Analyze whether changes can possibly affect benchmark output. Build or update the codebase dependency graph (imports, function calls, class inheritance).

### Dead code deletion

Walk all `.py` files in `code/` (excluding deleted ones). Search for `import <deleted_module>`, `from <deleted_module> import`, and string references to deleted names.

- **Zero references found** -> provably safe. Mark `"static_pass"` and **skip Tiers 1-3 entirely**.
- **References found** -> those references are bugs to fix first, then proceed to Tier 1.

### Support/core changes

Check blast radius: which other modules import or call the changed code?

- Purely subtractive (removed unused functions, no signature changes) and no external callers -> `static_pass`.
- Signatures changed, return types changed, or callers exist -> proceed to Tier 1+.

Record in `steps.verify_static`: `result` (`static_pass` | `needs_runtime`), `reason`, `references_found` array.

If `static_pass` -> skip to Commit or Revert. No runtime verification needed.

---

## Tier 1: Smoke Test

Step key: `verify_smoke`.

### Both phases

1. **Build check** (if `plan.json -> env.build_steps` exists): re-run the build command (`pip install -e .` or `python setup.py build_ext --inplace`). Mandatory if refactoring touched `setup.py`, `csrc/`, or any file in `env.custom_ops`. Otherwise skip.
2. **Import check**: `python -c "import <main_module>"` from `code/`. Catches broken imports from deleted code.
3. **Dependency delta** (periodic, not every round): compare `requirements.txt` with actually used imports. After deleting dead code, some packages may no longer be needed. If confirmed unused, remove from `requirements.txt` and uninstall:
   ```bash
   {run_in_env} pip uninstall <package> -y
   ```

### Phase 1 additional

4. **Dry run**: eval entry with `--help` or minimal config to check startup.

### Phase 2 additional

4. **Dry run**: training entry with `--help` or minimal config.
5. **1-step training**: run 1 training step with minimal data. Catches broken loss, optimizer, data loader. Cheap (seconds).

If any fails -> fix or revert.

---

## Tier 2: Module Verification

Step key: `verify_module`.

Only test modules that were **actually modified in this round** and have `testable: true` in `plan.json`. Previously verified modules are not re-tested unless their dependencies changed.

Tier 2 has two sub-tiers. Try 2a first; if it passes, skip 2b.

### Tier 2a: Computation Graph Comparison (zero cost, no data needed)

For `nn.Module` subclasses (model, backbone, head, decoder, loss, etc.), compare the computation graph structure between original and refactored versions using `torch.fx`:

```python
import torch.fx

original_graph = torch.fx.symbolic_trace(original_module).graph
refactored_graph = torch.fx.symbolic_trace(refactored_module).graph

original_ops = [(n.op, n.target, n.args) for n in original_graph.nodes]
refactored_ops = [(n.op, n.target, n.args) for n in refactored_graph.nodes]
```

- **Graphs structurally identical** -> mathematically equivalent for all inputs. Mark `graph_pass`, **skip Tier 2b**.
- **Graphs differ** -> may be intentional (simplified logic) or a bug. Show the diff, proceed to Tier 2b.

**Fallback** when `torch.fx` tracing fails (dynamic control flow, unsupported ops):
- Try `torch.export` (more robust for modern PyTorch)
- Try ONNX export: `torch.onnx.export()` both versions, compare graph structure
- If all tracing fails -> skip 2a, go directly to 2b

**State_dict key compatibility check**: compare `original_module.state_dict().keys()` vs `refactored_module.state_dict().keys()`. If keys differ, warn that pretrained weights won't load directly. If intentional (renamed layers), record the key mapping for checkpoint migration.

Record in `steps.verify_module.graph_check`: `method` (`torch.fx` | `torch.export` | `onnx` | `skipped`), `result` (`graph_pass` | `graph_diff` | `trace_failed`), `state_dict_compatible`, `key_diff`.

### Tier 2b: Tensor Snapshot Comparison (seconds~minutes, needs data)

Only needed when Tier 2a shows a diff or fails to trace.

**Phase 1**: snapshot in eval mode (forward pass only).

**Phase 2**: snapshot training-specific modules with fixed seed + deterministic mode:
- **Loss function**: fixed pred + target tensors -> compare loss value (exact match)
- **Optimizer step**: fixed gradients -> compare parameter updates (exact match)
- **LR scheduler**: compare lr values for first 100 steps (pure math, exact match)
- **Data augmentation**: fixed seed + fixed image -> compare augmented output
- **Checkpoint round-trip**: save state_dict -> load -> compare all values
- **Gradient check**: 1 forward+backward with fixed input -> compare per-layer gradients (tolerance: 1e-5 for CUDA non-determinism)

**Snapshot capture & comparison**:

1. **Capture** (first time only): generate a bespoke script per module that runs the **original** code with a fixed input (random tensor with correct shape, `torch.manual_seed(42)`), saves input+output to `{PROJECT}/stages/refactor/snapshots/<module_name>.pkl`. Run from `original/` directory.

2. **Compare**: load snapshot, run **refactored** module with same input, compare outputs. Tolerance: `1e-5` for float tensors (user can override).

Scripts are auto-generated based on analyzing each module's interface — not a generic framework, because research code structures vary wildly.

**Results display**:
```
Module verification:
  backbone (ResNet50):     PASS (max diff: 2.3e-7)
  neck (FPN):              PASS (max diff: 1.1e-6)
  decoder (RTDETRDecoder): PASS (max diff: 8.4e-7)
```

If any module fails: show diff magnitude and which output keys diverged, ask "Fix and retry, or revert round?"

Record in `steps.verify_module`: `modules_tested` dict with `pass` boolean and `max_diff` per module.

### Performance Regression Check (piggybacks on Tier 2b)

When running Tier 2b, also measure speed and memory — negligible extra cost since the module is already being run.

**How**: wrap the module call with profiling (warmup 3 runs, then measure 10 runs average). Record `torch.cuda.max_memory_allocated()` for peak GPU memory.

Run the same profiling on original (cached in snapshots) and refactored. Compare:
```
Performance check:
  backbone:   12.3ms / 245MB -> 12.1ms / 245MB
  decoder:     8.7ms / 180MB ->  8.9ms / 180MB
  full model: 45.2ms / 1.2GB -> 44.8ms / 1.2GB
```

**Thresholds**: warn if >10% slower or >10% more memory. Fail if >25%. This is a **warning, not a blocker** — minor performance trade for readability is acceptable, but large regressions indicate accidental changes.

Record in `steps.verify_module.perf`: per-module `original_ms`, `refactored_ms`, `delta_pct`, `original_mem_mb`, `refactored_mem_mb`, `mem_delta_pct`, plus `warnings` array.

Skip for: dead code rounds, CPU-only runs (timing unstable), Phase 2 training-specific modules (overhead is negligible).

---

## Tier 3: Mini End-to-End

Step key: `verify_mini`.

### Phase 1 -- Mini eval benchmark (minutes)

Run the eval pipeline end-to-end on a tiny data subset (~100 samples). Catches integration issues that module snapshots miss (data loading, output format, metric computation).

**When to run**: every round that touches core code, or after support changes to data pipeline / metrics. Skip for dead code rounds.

**How**:
1. Use the same entry command from `config.json`
2. Limit data: `--num_samples 100`, `--limit_test_batches 0.02`, or whichever limiting arg the code supports
3. Run synchronously (minutes)
4. Extract metrics, compare against paper targets

**Tolerance**: +/-2% relative or +/-1.0 absolute (small sample = natural variance).

### Phase 2 -- Mini training -> eval (minutes~hour)

Run refactored training code for N steps, then evaluate the checkpoint with the verified Phase 1 eval pipeline.

**When to run**: every round that touches training core (loss, optimizer, training loop). Skip for training support (logging, config management).

**How**:
1. Run refactored training for N steps (default: 100, user-configurable)
2. Save checkpoint
3. Run verified eval pipeline on the checkpoint
4. Compare eval metrics against the training baseline (recorded at Phase 2 start)

**Training baseline** (captured once at Phase 2 start):
- Run **original** training code for the same N steps, same config, same seed
- Eval the checkpoint -> record as `plan.json -> phases.phase_2.training_baseline`

**Tolerance**: +/-1% relative (CUDA non-determinism in backward pass).

Record in `steps.verify_mini`: `phase`, `num_samples`, `metrics`, `pass`. Phase 2 adds `training_steps`, `baseline_metrics`.

If mini fails badly (>5% regression) -> likely a real bug. Investigate before proceeding.

---

## Tier 4: Full End-to-End (milestone only)

Step key: `benchmark`.

### Phase 1 -- Full eval benchmark (hours)

Run the complete eval benchmark on the full dataset. Only at milestones:
- After all dead code is removed (end of priority 1)
- After all support code is simplified (end of priority 2)
- After final inference core cleanup (end of priority 3)
- Or when user explicitly requests it

Ask user: "Module tests passed. Run full benchmark now, or save it for later?"

### Phase 2 -- Full training -> eval (days)

Run the complete training schedule, then evaluate. Only at:
- End of Phase 2 (all training modules refactored)
- Or when user explicitly requests it

This is the most expensive verification — days of GPU time. Should only run once, as the final confirmation that the refactored training code reproduces the paper's training results.

Ask user: "Training module tests passed. Run full training? This will take <estimated time>."

### Execution details

If running, follow `/eval-run` Steps 1-3 (resolve sources, create run, build & execute), adapted for the refactor stage:
- Run template: `lifecycle/refactor/refactor_run.json` (set `round` to current round number)
- Run directory: `{PROJECT}/stages/refactor/runs/run_{YYYYMMDD}_{HHmmss}/`
- Sources: reuse from previous round if unchanged
- First benchmark: debug mode. Subsequent: ask user.
- Phase 2: run training command instead of eval command, then eval the checkpoint with Phase 1 pipeline.

Never block waiting for a long-running benchmark. Return immediately and let user check back.

---

## Comparison Tolerances Summary

| Context | Tolerance |
|---------|-----------|
| Tier 2b tensor snapshots | 1e-5 for float tensors (user-overridable) |
| Tier 2 performance | warn >10%, fail >25% |
| Tier 3 mini benchmark | +/-2% relative or +/-1.0 absolute |
| Tier 3 mini training | +/-1% relative |
| Tier 4 full benchmark | +/-0.5% relative or +/-0.3 absolute |
| Tier 4 full training | +/-1% relative |
