---
name: refactor-run
description: >
  Execute one refactoring round — make changes, run verification, commit or revert. Each invocation
  is one round; multiple rounds across conversations until the codebase is clean. Trigger for:
  running a refactoring round, verifying benchmark after changes, reverting a failed round, resuming
  an interrupted round, checking refactoring progress, promoting refactored code to other stages.
  Also trigger for: "跑重构", "重构一轮", "跑一下refactor", "验证benchmark", "回退这轮".
  This is the execution skill — not for initial analysis (use refactor-init) or audit reports
  (use refactor-report).
---

# /refactor-run — Run Refactoring Round

Execute one round of refactoring on `stages/refactor/code/`. Each invocation = one round.

One question at a time. Push/pop `history.json` stack per Workflow State Protocol in CLAUDE.md.

**Requires**: `plan.json` exists with non-empty `modules` (otherwise offer `/refactor-init`). Credentials checked lazily.

**Locate project**: show recent projects, let user pick. Read `plan.json`, `config.json`, `output.json`.

## Resume Check

1. Scan `{PROJECT}/stages/refactor/runs/` for `run.json` with `status: "running"` -> status check on benchmark (see Step 4).
2. Scan for `status: "pending"` with partially completed steps -> resume from last completed step.
3. No unfinished round -> proceed to new round.

## Step 1: Plan Round

Step key: `plan_round`.

Determine round number from `plan.json -> rounds` (length + 1). Show current state:
```
Refactoring progress:
  Completed rounds: <N>
  Modules remaining: <core: N, support: N, dead: N pending>

Next priority items:
  1. [dead] experiments/ablation/ — unused ablation study (42 files)
  2. [support] utils/distributed.py — multi-GPU wrapper
```

Ask: "Proceed with these, or pick different targets?" Record the round plan, append to `plan.json -> rounds`.

### Cascade detection (after every round)

Re-scan the codebase for simplification opportunities unlocked by changes:
- **Dead branches**: `if/else` where one branch references deleted code -> flatten
- **Orphaned imports**: imports of deleted modules in files not directly modified
- **Collapsed wrappers**: functions that now just call one other function -> inline
- **Single-implementation abstractions**: ABC with one remaining impl -> remove abstraction
- **Unused arguments**: params only used by deleted code paths -> remove

Add discoveries to `plan.json -> modules` with `"discovered_after_round": N`.

### Duplicate code re-scan

Re-run duplicate detection (same method as `/refactor-init` Step 3d) on the current codebase. Add findings to `plan.json -> analysis.duplicate_groups`.

### Scope per round

- **Dead code**: batch multiple files/dirs (low risk)
- **Support code**: one module at a time (medium risk)
- **Core code**: one file at a time (high risk, minimal batching)

## Step 2: Refactor

Step key: `refactor`. Work in `{PROJECT}/stages/refactor/code/`.

### Dead code (priority 1)
1. Delete targeted files/directories
2. Remove imports of deleted modules from remaining files
3. Fix broken references (config entries pointing to deleted code)
4. Check no circular dependency breaks

### Support code (priority 2)
1. Identify what core code actually uses from this module
2. Inline small utilities or keep minimal version
3. Remove unused functions/classes
4. Simplify complex abstractions (e.g., 200-line config system -> 20 lines of argparse)

### Core code (priority 3)
1. Remove dead branches, disabled features
2. Simplify data flow, remove unnecessary abstractions
3. Clean up naming, remove cryptic variable names
4. Parameterize hardcoded paths, magic numbers
5. **Model architecture files are off-limits by default.** Model definition code (architecture, forward pass, layers, numerical ops) risks checkpoint compatibility and benchmark regression. If you believe a model file needs changes, stop and ask the user first — explain what and why.

### Auto-cleanup pass (every round, after manual changes)

Run on all modified files without asking — these are mechanical, zero-risk transformations:

1. **Remove unused imports**: `autoflake --in-place --remove-all-unused-imports --remove-unused-variables <changed_files>`
2. **Sort imports**: `isort --profile black <changed_files>`
3. **Format**: `ruff format <changed_files>` (fallback: `black`)
4. **Lint + auto-fix**: `ruff check --fix <changed_files>`

Check tool availability first; skip missing tools silently. Only run on files touched this round.

After cleanup, also scan for:
- Unused function/class definitions left behind (tools won't catch these — grep for unreferenced `def`/`class`)
- Empty files (all code removed -> delete)
- Empty `__init__.py` that only re-exported deleted modules -> clean up

### Record and commit

Record in `steps.refactor`: `files_changed`, `files_deleted`, `files_added`, `summary`, `auto_cleanup`.

Commit: `git add -A && git commit -m "refactor round <N>: <summary>"` in `code/`.

## Step 3: Verify

Verification uses a cost-efficient hierarchy — only escalate when the current tier passes. Early tiers are cheap enough for every round; expensive tiers are reserved for milestones.

| Tier | What | Cost | When to run |
|------|------|------|-------------|
| 0 | Static analysis: check if any surviving code references changed/deleted code | zero | every round |
| 1 | Smoke test: import check, --help, 1-step train (Phase 2) | seconds | every round (if Tier 0 needs runtime) |
| 2 | Module verification: graph comparison (2a), tensor snapshots (2b), perf check | sec~min | rounds touching testable modules |
| 3 | Mini end-to-end: 100 samples eval, or N-step train -> eval | minutes | core code rounds |
| 4 | Full benchmark: full dataset eval, or full train -> eval | hours~days | milestones only |

**Tier 0 can short-circuit everything**: if static analysis proves the deletion is safe (zero surviving references), skip Tiers 1-3 entirely and go straight to commit.

When a tier requires actually launching the user's code (Tier 1+ smoke / mini / full benchmark), follow CLAUDE.md "Run Skill Internal Dependencies" Step 2 (`code_snapshot.py` for SHA + dirty patch) and Step 3 (`cwd` = unified code_dir, `output_dir` = absolute under `<RUN_DIR>/output/`). Refactor-run is the exception in that the code_dir is `stages/refactor/code/` directly (no `_source` symlink — refactor edits in place); the cwd / output_dir / snapshot rules still apply.

Read `references/verification-tiers.md` for detailed tier descriptions, Phase 1 vs Phase 2 variants, graph comparison methods, tensor snapshot procedures, tolerance values, and JSON recording schemas.

## Step 4: Compare Results

Step key: `compare`. Only applies when Tier 3 or Tier 4 was run; skip if only Tier 0-2.

### Phase 1: compare against paper targets

Extract metrics (same as `/eval-run` Step 4). Compare against `plan.json -> paper.benchmarks`:
```
Milestone benchmark (after priority 1 — dead code removal):
  mAP:  48.3  (paper: 48.5, delta: -0.2, -0.4%)  within tolerance
  AP50: 67.1  (paper: 67.3, delta: -0.2, -0.3%)  within tolerance
  Verdict: PASS
```
Default tolerance: +/-0.5% relative or +/-0.3 absolute (whichever is more lenient). User can override.

### Phase 2: compare against training baseline

Same comparison as Tier 3 Phase 2 (train N steps -> eval -> compare against baseline), applied to full benchmark when Tier 4 runs. Training tolerance: +/-1% relative (CUDA non-determinism in backward pass).

Record in `steps.compare`: `pass`, `phase`, `deltas`; Phase 2 adds `training_steps`, `baseline_run_id`. Also write metrics to `run.json -> metrics`.

## Step 5: Commit or Revert

Step key: `commit_or_revert`.

**PASS**: tell user "Round N passed. Changes committed." The git commit from Step 2 stands. Update `plan.json -> modules` (set targeted modules to `"refactored"` or `"deleted"`), update `plan.json -> rounds[-1]` with `"pass"`. Record `action = "commit"`, `commit_hash`.

**FAIL**: show which metrics regressed and by how much. Ask: "Revert this round, or investigate?"
- **Revert**: `git revert HEAD` in `code/`, update round to `"failed"`, record `action = "revert"`.
- **Investigate**: user debugs, makes fixes, re-runs benchmark (loop to Step 3). Each retry tracked as `ad_hoc` step.

**PARTIAL** (some metrics pass, some fail): show breakdown. Ask: "Accept with regressions, revert, or investigate?"

## Step 6: Finalize Round

Step key: `finalize`.

1. Finalize `run.json` (status, duration, metrics). No separate index file — `run.json` is the source of truth (see CLAUDE.md "Listing runs (no separate index)" for the jq query pattern).
2. Pop from `history.json` stack, append `completed`.

Show progress:
```
Refactoring progress after round <N>:
  Original:  <total_files> files, <total_lines> lines
  Current:   <current_files> files, <current_lines> lines (<pct>% reduction)
  Rounds:    <completed>/<planned> completed
  Status:    <N dead removed, M support simplified, K core cleaned>
```

### What comes next

**Phase 1 modules remain** -> offer next `/refactor-run` round.

**Phase 1 complete, Phase 2 not started** -> phase transition. Announce Phase 1 completion, note the eval pipeline is now verified and trusted. Ask user how many training baseline steps to run (recommend 100 for quick, 1000 for confidence).

**Phase 2 modules remain** -> offer next `/refactor-run` round.

**All modules done** -> declare refactoring complete, then **promote**:

Ask user which stages should use the refactored code. For each selected stage, update `project.json -> stages.{stage}.code_source`:
```json
{
  "source": "local",
  "path": "{PROJECT}/stages/refactor/code",
  "branch": null,
  "commit": null
}
```
This makes `code/` the code source for that stage (soft link, no copy). If user hasn't run init for those stages yet, suggest it. Offer `/refactor-report`.
