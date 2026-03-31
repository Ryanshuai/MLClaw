---
name: eval-report
description: "Use this skill to generate a self-contained HTML evaluation report from a completed eval run. Includes metrics, baseline comparison, per-class breakdown, training context from upstream DAG, and embedded charts. Triggers for: '\u751F\u6210\u8BC4\u4F0B\u62A5\u544A', 'make eval report', 'create HTML report from eval results', 'share eval results with team'. Not for running evaluation (use eval-run)."
---

# /eval-report — Generate Evaluation Report

Generate a self-contained HTML report from a completed evaluation run. Embeds all data inline (no external dependencies except optional Plotly CDN). Report content is dynamically assembled from whatever data exists — no hardcoded fields.

Ask one question at a time — multiple questions at once overwhelms users.

Follow the Workflow State Protocol from CLAUDE.md: push on entry, update step as you progress, pop on completion.

**Requires**: at least one `{PROJECT}/stages/evaluation/runs/*/run.json` with `status: "completed"`. If none, offer `/eval-run`.

## Step 1: Locate Run

**From /eval-run**: use current `{PROJECT}` and `{RUN_DIR}`, skip to Step 2.

**Standalone**: locate project per CLAUDE.md conventions (show recent projects, let user pick). Then list recent evaluation runs from `{PROJECT}/runs_index.json` (most recent first, up to 10). Ask which run to report on. Read `{RUN_DIR}/run.json` — warn if not completed.

## Step 2: Gather Report Data

Walk the DAG upward from the current eval run and collect everything that exists.

### Current run data

Read from the eval run:
- `run.json` — all fields (metrics, per_class, env, lineage, code, duration, etc.)
- `config_snapshot.json` — resolved config at run time
- `sources.json` — artifacts and inputs used
- Stage's `config.json -> dataset`, `output.json -> metrics.baseline` (compute deltas if set)
- `outputs/` — list files, note images for embedding
- `logs/stdout.log` — scan for summary tables

### Walk DAG upstream

1. Read `run.json -> lineage.parents` (cross-stage references like `training/run_YYYYMMDD_HHmmss`)
2. For each parent, read its `run.json`, recurse on its parents
3. If no lineage, fall back to artifact matching: check if any run's outputs match this run's model artifacts
4. From each upstream run, collect: metrics, env, code, dataset, sources, notable outputs

### Project-level data

- `project.json` — name, metadata
- `{WORKSPACE}/resources.json` — server info for env context

### Build data bag

Assemble all collected data organized by run (target_run + upstream_runs array + project metadata). The data bag does not decide report layout — that happens in Step 3 based on what exists.

## Step 3: Report Scope

**Show what was collected** — summarize data by source (metrics, per-class count, baseline deltas, images, env, upstream training info, etc.).

**User additions**: ask "Anything to add that isn't in the data? (e.g., release notes, known issues)" — keep asking until done/skip.

**Sections**: auto-generate section list from data bag (training section if upstream has metrics, per-class if available, baseline comparison if deltas exist, charts if images found). Show as checklist, all checked by default. User can uncheck or reorder.

**Format**: ask for report title (suggest from project + run info), then ask for optional reference report to match format.

## Step 4: Generate Report

Write a Python script on the fly based on the actual data bag and selected sections.

**Rules**:
- Plotly for interactive charts (CDN). Offline: matplotlib + base64 PNG.
- All data embedded inline (JSON, base64 images, inline CSS/JS).
- Output: `{RUN_DIR}/outputs/report_{run_id}.html`
- Metrics with deltas: green = improvement, red = regression (direction-aware per metric).
- Per-class: sortable table + horizontal bar chart.
- Only render sections that have data.
- Style: clean/professional, sortable tables, collapsible verbose sections, print-friendly CSS.

**Reference report**: if provided, read it, match its layout and style, note what couldn't be mapped.

**Fallback**: if Python script fails, generate HTML directly with Write tool (simpler, pure HTML tables).

## Step 5: Deliver

1. Show report path: `{RUN_DIR}/outputs/report_{run_id}.html`
2. Open in browser (if local)
3. Ask "Anything to adjust?" — modify and regenerate if yes, done if no
