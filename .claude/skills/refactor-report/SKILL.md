---
name: refactor-report
description: "Use this skill to generate a refactoring audit report documenting round-by-round changes, rollback points, verification results, and reproduction instructions. Triggers for: '\u91CD\u6784\u62A5\u544A', 'refactoring summary', 'audit report', 'show refactoring progress'. Requires at least one refactor-run round."
---

# /refactor-report — Refactoring Audit Report

Generate a self-contained HTML report documenting the entire refactoring process. The report's core purpose is **reproducibility and rollback** — when someone needs to intervene, they can understand what happened, roll back to any point, and reproduce any state.

Ask one question at a time — multiple questions at once overwhelms users.

Follow the Workflow State Protocol from CLAUDE.md: push on entry, update step as you progress, pop on completion.

**Requires**: at least one `{PROJECT}/stages/refactor/runs/*/run.json`. If none, offer `/refactor-run`.

## Locate Project

Locate project per CLAUDE.md conventions. Show recent projects, let user pick.

## Gather Data

Read all data sources:
1. `plan.json` — module classification, paper targets, phases
2. `runs/*/run.json` — every round's record
3. `code/` — current state (file count, line count)
4. `git -C code/ log --oneline` — commit history (rollback points)

## Report Structure

Output: `{PROJECT}/stages/refactor/refactor_report.html` (self-contained HTML).

### 1. Overview

Repository, paper, benchmark target, current status (phase/round), codebase size reduction (original vs current files/lines).

### 2. Round-by-Round Changelog

For each round: git commit hash (rollback point), target modules, changes (files modified/deleted/added with line deltas), auto-cleanup results, verification tier reached + results, pass/fail outcome with failure reasons.

### 3. Rollback Guide

Table of all rollback points with git commit hashes and `git checkout` commands. Include instructions for rolling back and returning to HEAD.

### 4. Verification Summary

Aggregate across all rounds: how many rounds reached each verification tier, performance regression status, failed round count with reasons.

### 5. Module Status Map

Current classification of every module: deleted (which round), refactored (which round, line delta), keep (reason), pending (which phase).

### 6. Reproduction Instructions

Step-by-step: clone original repo at exact commit, apply refactoring (copy code/ or replay rounds), verify with benchmark command + expected metrics, environment details.

## Generate Report

Python script producing self-contained HTML with: inline CSS, collapsible round details, color-coded status (green=pass, red=fail, gray=skipped), copy-to-clipboard on git commands.

**Fallback**: generate HTML directly with Write tool if script fails.
