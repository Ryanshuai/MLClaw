---
name: eval-report
description: Generate a self-contained HTML evaluation report from a completed eval run
---

# /eval-report — Generate Evaluation Report

Generate a self-contained HTML report from a completed evaluation run. The report embeds all data inline (no external dependencies except optional CDN for Plotly) and can be shared as a standalone file.

The report content is **not fixed** — it is dynamically assembled from whatever data exists in the current run and its upstream DAG. No hardcoded fields or sections.

## Interaction Rules — MUST FOLLOW

**Ask only ONE question at a time.** Record the answer, then ask the next. Never list multiple questions at once.

## Workflow State

On entry: push `{ "skill": "eval-report", "step": "locate_run" }` to `history.json` stack.
Update step as you progress. On completion: pop from stack, append `completed` to history.

## Dependency Check

On entry, check upstream requirements per the Skill Dependency Graph in CLAUDE.md:
- **eval-run completed**: at least one `{PROJECT}/stages/evaluation/runs/*/run.json` with `status: "completed"` must exist. If not, tell user no completed evaluation runs found and offer to run `/eval-run` first.

## Step 1: Locate Run

Update workflow step to `locate_run`.

**If invoked from /eval-run** (run context already known):
- Use the current `{PROJECT}` and `{RUN_DIR}` from the calling skill. Skip to Step 2.

**If invoked standalone**:
1. Show recent projects (top 5 from history.json). Ask user which one.
2. Once project is found, list recent evaluation runs from `{PROJECT}/runs_index.json` (most recent first, up to 10).
   ```
   Recent evaluation runs:
   1. run_20260317_091500  mAP=0.485  "COCO val v2"    completed
   2. run_20260316_153024  mAP=0.473  "COCO val v1"    completed
   ```
3. Ask user which run to report on. Can also accept a run ID directly.
4. Set `RUN_DIR = {PROJECT}/stages/evaluation/runs/{run_id}`.
5. Read `{RUN_DIR}/run.json` — must have `status: "completed"`. If not, warn and ask to confirm.

## Step 2: Gather Report Data

Update workflow step to `gather_data`.

Report data is collected dynamically — no predefined field list. Walk the DAG upward from the current eval run and collect everything that exists.

### 2a. Current run data

Read everything available from the eval run itself:

- `{RUN_DIR}/run.json` — all fields (run_id, stage, status, duration, created_at, server, env, metrics, lineage, code, etc.)
- `{RUN_DIR}/run.json → metrics` — whatever metrics exist (keys and values are not predefined)
- `{RUN_DIR}/run.json → metrics.per_class` — if present, whatever classes and metrics are there
- `{RUN_DIR}/config_snapshot.json` — resolved config at run time
- `{RUN_DIR}/sources.json` — what artifacts and inputs were used
- `{PROJECT}/stages/evaluation/config.json → dataset` — if filled
- `{PROJECT}/stages/evaluation/output.json → metrics.baseline` — if set, load baseline data and compute deltas
- `{RUN_DIR}/outputs/` — list all files with sizes. Note images (`.png`, `.jpg`, `.svg`) for embedding.
- `{RUN_DIR}/logs/stdout.log` — scan for summary tables or final output

### 2b. Walk the DAG upstream

From the current eval run, trace the lineage DAG to find all upstream runs across any stage.

**How to walk:**

1. Read `{RUN_DIR}/run.json → lineage.parents`. Each entry is a cross-stage reference like `{stage}/run_{YYYYMMDD}_{HHmmss}`.
2. For each parent, read `{PROJECT}/stages/{stage}/runs/{run_id}/run.json`.
3. Recursively check each parent's `lineage.parents` — keep walking until no more parents.
4. **If lineage.parents is empty**, fall back to artifact matching:
   - Read `{RUN_DIR}/sources.json` → find artifact entries (model, checkpoint)
   - Scan `{PROJECT}/runs_index.json` — check if any run's outputs match those artifact paths
   - Scan `{PROJECT}/stages/*/runs/*/run.json` if index lookup fails

**From each upstream run, collect the same way — read whatever exists:**
- `run.json` — all fields (metrics, env, code, duration, etc.)
- Stage's `config.json → dataset` — if filled
- `sources.json` — what that run consumed
- `outputs/` — notable output files

### 2c. Collect project-level data

- `{PROJECT}/project.json` — project name, description, any metadata
- `{WORKSPACE}/resources.json` — server info referenced by runs (for env context)

### 2d. Build a data bag

Assemble all collected data into an internal structure organized by run, not by predefined fields:

```
data_bag = {
  "project": { ...project.json fields... },
  "target_run": {
    "run_id": "...",
    "stage": "evaluation",
    "run_json": { ...everything from run.json... },
    "config_snapshot": { ... },
    "sources": { ... },
    "dataset": { ... },
    "output_files": [ { "name": "...", "size": ..., "is_image": true }, ... ],
    "baseline": { "ref": "...", "metrics": {...}, "deltas": {...} }  // or null
  },
  "upstream_runs": [
    {
      "run_id": "...",
      "stage": "training",
      "run_json": { ... },
      "config_snapshot": { ... },
      "sources": { ... },
      "dataset": { ... },
      "output_files": [ ... ]
    },
    ...  // more upstream runs if DAG has depth
  ]
}
```

The data bag is a flat collection — it does NOT decide what goes into the report. That happens in Step 3 based on what data actually exists.

## Step 3: Report Scope

Update workflow step to `report_scope`.

### 3a. Show what was collected

Present the user a summary of all data found, organized by source:

```
Collected data:

  evaluation/run_20260317_091500 (this run):
    metrics: mAP=0.485, AP50=0.673, AP75=0.521
    per_class: 15 classes
    baseline: vs run_20260316_153024 (deltas computed)
    images: confusion_matrix.png, pose_diff.png
    env: PyTorch 2.1, CUDA 12.1, A100
    duration: 12m 34s

  training/run_20260315_120000 (upstream):
    metrics: epoch=197, loss_val=1.9474, loss_train=2.8793
    dataset: production/2025_0709
    code: github.com/xxx/perlis (commit abc1234)
    env: PyTorch 2.1, 8x A100

  project: detection
```

### 3b. User additions (one at a time)

Ask: "Anything to add that isn't in the data? (e.g., release notes, known issues, context — or skip)"

If user provides something, record it. Keep asking "Anything else?" until user says done/skip.

### 3c. Sections

Based on the data bag, auto-generate a section list. Each section corresponds to a cluster of related data that actually exists. For example:

- If upstream training run has metrics → propose a "Training" section
- If eval run has per_class → propose a "Per-class Breakdown" section
- If images found in outputs → propose a "Charts" section
- If baseline deltas computed → propose a "Baseline Comparison" section
- If user provided free text → propose corresponding section

Show the auto-generated list with checkboxes (all checked by default). User can uncheck or reorder.

### 3d. Format

Ask ONE question:
- **Report title** — suggest based on project name + run info. User can customize.

Then ask:
- **Reference report** — "Match an existing report's format? (file path or URL, or skip)"

## Step 4: Generate Report

Update workflow step to `generate_report`.

Write a Python script on the fly and execute it. The script is generated fresh each time based on the actual data bag and selected sections — there is no fixed template.

### Generation rules

1. **Use Plotly** for interactive charts (CDN link). If user needs offline, use matplotlib + base64 PNG.
2. **Embed all data** inline in the HTML — JSON data, base64 images, inline CSS/JS.
3. **Output location**: `{RUN_DIR}/outputs/report_{run_id}.html`
4. **Images from outputs/**: read each image file, base64-encode, embed as `<img>` tags.
5. **Metrics with deltas**: color-code green (improvement) / red (regression). Direction depends on metric — higher is better for mAP/accuracy, lower is better for loss/error.
6. **Per-class data**: render as a sortable table + horizontal bar chart.
7. **Sections are driven by data** — only render sections that have data. Never render empty sections.

### Style guidelines

- Clean, professional — suitable for stakeholders
- Sortable tables (simple inline JS)
- Collapsible sections for verbose data (config, env, full logs)
- Print-friendly (CSS media query)
- Light theme by default

### If user provided a reference report

1. Read the reference file (HTML/PDF/Word/Confluence page)
2. Understand its structure, visual style, section ordering
3. Generate the new report with the same layout, populated with data from the data bag
4. Tell user what was matched and what couldn't be mapped

### Fallback

If Python script fails:
- Generate the HTML directly using the Write tool (simpler, no charts, pure HTML tables)

## Step 5: Deliver

Update workflow step to `deliver`.

1. Show the report path:
   ```
   Report generated: {RUN_DIR}/outputs/report_{run_id}.html
   ```

2. Open in browser automatically (if local execution).

3. Ask: "Anything to adjust?"
   - If yes → modify and regenerate
   - If no → done

4. Pop self from `history.json` stack, append `completed` to history.
