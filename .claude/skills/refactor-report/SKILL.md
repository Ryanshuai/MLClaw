---
name: refactor-report
description: Generate a refactoring audit report — round-by-round changelog, rollback points, verification results, reproduction instructions
---

# /refactor-report — Refactoring Audit Report

Generate a self-contained HTML report documenting the entire refactoring process. The report's core purpose is **reproducibility and rollback** — when a human needs to intervene, they can understand exactly what happened, roll back to any point, and reproduce any intermediate state.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.**

## Workflow State

On entry: push `{ "skill": "refactor-report", "step": "locate_project" }` to `history.json` stack.
On completion: pop from stack, append `completed` to history.

## Dependency Check

- **refactor-run completed**: at least one round must have been run. Check `{PROJECT}/stages/refactor/runs/` has at least one `run.json`.

## Locate Project

Same as other skills — recent projects list, let user pick.

## Gather Data

Read all data sources:
1. `{PROJECT}/stages/refactor/plan.json` — module classification, paper targets, phases
2. `{PROJECT}/stages/refactor/runs/*/run.json` — every round's record
3. `{PROJECT}/stages/refactor/code/` — current state (file count, line count)
4. `git -C {PROJECT}/stages/refactor/code/ log --oneline` — commit history (rollback points)

## Report Structure

Generate a single self-contained HTML file at `{PROJECT}/stages/refactor/refactor_report.html`.

### 1. Overview

```
Repository: <url> (<branch>)
Paper: <title>
Benchmark target: <metric>=<value>, ...

Status: Phase <1|2>, Round <N>/<total>
  Original:  142 files, 18,500 lines
  Current:    52 files,  6,200 lines (66% reduction)
```

### 2. Round-by-Round Changelog

For each round, in order:

```
Round 3 — 2026-03-20 14:30
  Git commit: a1b2c3d  ← rollback point
  Target: [dead] experiments/ablation/, tools/visualize.py
  Changes:
    Deleted: 42 files (experiments/ablation/)
    Deleted: 1 file (tools/visualize.py)
    Modified: 3 files (removed imports)
  Auto-cleanup: autoflake removed 12 unused imports in 3 files
  Verification:
    Tier 0: static_pass (no surviving references)
  Result: PASS — committed
```

```
Round 5 — 2026-03-21 10:15
  Git commit: d4e5f6g  ← rollback point
  Target: [support] utils/distributed.py
  Changes:
    Modified: utils/distributed.py (200 → 25 lines, inlined sync_bn)
    Modified: train.py (removed distributed imports)
  Auto-cleanup: ruff fixed 4 lint issues
  Verification:
    Tier 0: needs_runtime (callers exist)
    Tier 1: smoke PASS
    Tier 2a: graph_pass (computation graph identical)
    Tier 2 perf: backbone 12.3ms→12.1ms ✓
  Result: PASS — committed
```

```
Round 7 — 2026-03-22 09:00  ★ FAILED
  Git commit: (reverted)
  Target: [support] data/transforms.py
  Changes:
    Modified: data/transforms.py (simplified augmentation pipeline)
  Verification:
    Tier 0: needs_runtime
    Tier 1: smoke PASS
    Tier 2b: FAIL — augmentation output mismatch (max diff: 0.34)
  Result: FAIL — reverted to h7i8j9k
  Notes: augmentation order matters for normalization. Kept original.
```

**Key information per round**:
- **Git commit hash** — the exact rollback point. `git checkout <hash>` restores that state.
- **What changed** — files modified/deleted/added, with line count deltas
- **Auto-cleanup results** — what tools ran, what they fixed
- **Verification tier reached** — how far up the verification hierarchy we went
- **Result** — pass/fail/revert, with the reason for failures

### 3. Rollback Guide

A table of all rollback points:

```
State                          Git Commit   Command
─────────────────────────────  ──────────   ──────────────────────────────────
Original (unmodified)          abc1234      git checkout abc1234
After Round 1 (dead code)      def5678      git checkout def5678
After Round 2 (dead code)      ghi9012      git checkout ghi9012
After Round 3 (support)        jkl3456      git checkout jkl3456
  Round 4 FAILED (reverted)    —            —
After Round 5 (support)        mno7890      git checkout mno7890
Current state                  pqr1234      (HEAD)
```

Include instructions:
```
To roll back to any state:
  cd {PROJECT}/stages/refactor/code/
  git checkout <commit_hash>

To return to current state:
  git checkout main
```

### 4. Verification Summary

Aggregate verification results across all rounds:

```
Verification coverage:
  Tier 0 (static):     8/10 rounds  (6 static_pass, 2 needs_runtime)
  Tier 1 (smoke):      4/10 rounds
  Tier 2a (graph):     3/10 rounds  (3 graph_pass)
  Tier 2b (snapshot):  2/10 rounds  (2 pass)
  Tier 3 (mini):       2/10 rounds  (2 pass)
  Tier 4 (full):       1/10 rounds  (1 pass — mAP=48.4, paper=48.5)

Performance:
  No regressions detected.

Failed rounds: 1 (Round 7 — augmentation order, reverted)
```

### 5. Module Status Map

Current classification of every module:

```
Status     Module                          Round   Notes
─────────  ──────────────────────────────  ──────  ─────────────
deleted    experiments/ablation/ (42 files) R1
deleted    tools/visualize.py              R1
deleted    tools/demo.py                   R2
refactored utils/distributed.py            R5      200→25 lines
refactored utils/config.py                 R6      150→30 lines
keep       models/backbone.py              —       model architecture (off-limits)
keep       models/head.py                  —       model architecture (off-limits)
pending    utils/logger.py                 —       Phase 1 remaining
pending    train.py                        —       Phase 2
```

### 6. Reproduction Instructions

How to reproduce the final state from scratch:

```
1. Clone original repo:
   git clone <url> -b <branch>
   git checkout <commit>

2. Apply refactoring (option A — use our git history):
   Copy stages/refactor/code/ as the clean version.

3. Apply refactoring (option B — replay rounds):
   Follow the round-by-round changelog above.

4. Verify:
   <benchmark command>
   Expected: <metric>=<value> (paper: <paper_value>)

5. Environment:
   Python: <version>
   Key packages: torch==<v>, torchvision==<v>, ...
```

## Generate Report

Use Python to generate a self-contained HTML file with:
- Inline CSS (no external dependencies)
- Collapsible round details (click to expand)
- Color-coded status (green=pass, red=fail, gray=skipped)
- Copy-to-clipboard on git commands

Save to `{PROJECT}/stages/refactor/refactor_report.html`.

Pop self from `history.json` stack, append `completed` to history.
