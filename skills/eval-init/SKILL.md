---
name: eval-init
description: >
  Use this skill to analyze evaluation code and configure the evaluation stage. Triggers when the user
  wants to set up evaluation for a model — analyzing what an eval script needs (model weights, data,
  ground truth), what metrics it computes, and filling the 4 JSON config files. Use for: "analyze eval
  code", "set up evaluation", "configure eval stage", "what does this eval script need", "分析评估代码",
  "配置评估", "初始化eval". Not for running evaluation (use eval-run) or generating reports (use eval-report).
---

# /eval-init — Evaluation Stage Initialization

Analyze evaluation code and fill 4 JSON config files (schema only) in `stages/evaluation/`. The goal is to understand what the code needs and produces, so `/eval-run` can execute it correctly later.

## How this skill works

The user brings evaluation code (standalone `eval.py` or eval mode inside a training script). You read it, figure out what it needs (model weights, test data, ground truth annotations) and what it produces (metrics like mAP, accuracy), then capture that understanding in 4 structured JSON files.

These files are **schema only** — they define WHAT is needed, not WHERE to get it. Concrete paths come later during `/eval-run`. However, if the user volunteers source locations during init, record them.

## Interaction approach

Ask one question at a time. Users find it overwhelming when asked to fill multiple fields simultaneously — each question should feel like a natural follow-up to the previous answer. Record the answer, then ask the next. **And only what only they know** — a value you can read is not a question, and a value nobody has is recorded absent rather than asked for: CLAUDE.md "Decide what evidence can decide".

## On entry

Follow `references/skill-graph.md` -> "Workflow State Protocol": push to stack, check dependencies (project.json exists, code available), locate project, resolve code directory. These are all documented in CLAUDE.md — follow them, don't duplicate them here.

## Output: 4 JSON files

| File | What it captures |
|------|-----------------|
| `config.json` | Entry command, config format, framework, dataset info, managed params |
| `artifacts.json` | Static inputs — model weights, evaluator configs + **`candidates`** (where each can actually be obtained) |
| `input.json` | Dynamic inputs — test data + ground truth annotations + **`candidates`** |
| `output.json` | What the code produces — metrics definitions and watch list |

For item/source schemas and type classification rules, read `references/schemas.md`.

**`candidates` here, not "schema only".** The rest of these files describe what the code needs; `candidates` records what is actually available and how well each option fits. Evaluation earns the exception for the same reason training does — a fixed val set and a fixed checkpoint are settled once, not per run — and for one reason training does not have, which is Step 1b's whole subject: in eval, **a candidate that holds fewer samples is not a smaller copy of the same thing, it is a different measurement.**

## Step 0: Locate the checkpoint and the data

**The data half is not this skill's job.** Invoke **`/discover`** as a sub-skill (utility pattern, like `/resources`) and read `discovery/leads.json`. Do not grow a sweep here: `/train-init` has one, this skill would have made two, and two implementations of "where is the data" is how they start disagreeing. A lead also outlives an init — access arrives weeks after a handover, and the leads file carries the unresolved ones forward where these four JSON files cannot.

**Ask `/discover` to `introspect` the checkpoint before anything else.** `discover.py introspect --checkpoint <the .pt>` reads what the training run recorded inside its own weights, and one field there is the thing this stage most needs and can least otherwise get: **`train_args.data` names the val split** — the only record of *which units* the checkpoint's metrics were measured on. It also yields the parent checkpoint, the run's own numbers, its params, and whether a commit was ever stamped. No host, no credential, no framework.

This is the highest-yield search in eval specifically, and the ordering is not a preference: **a checkpoint you already have is a stronger source than a wiki page and a cheaper one than a server you have no key for.** For an inherited model it is usually the *only* source that names a val set at all, and without one, Step 1b has nothing to judge a candidate against — every option becomes `mismatch` for lack of a `num_samples` to compare to. Read `/discover` `references/searches.md` → "checkpoint" for what it refuses and why.

Two things it hands you that land directly in the files below: the val-split path becomes an `input.json` candidate (`unreachable` until somebody says which machine that path was on), and `train_metrics` becomes `output.json → baseline` carrying `confidence: claimed` **and no scope** — which is exactly the gap the val split closes.

What this skill *does* look for itself, because `/discover` has no reason to:

| Source | What to look for | How |
|---|---|---|
| **This project's own training runs** | checkpoints this eval exists to measure | `list_runs.py --stage training --mode production`, then `output/` per run |
| **This project's data line** | a frozen val snapshot; batches still out | `phase.py phase --project {PROJECT}` — records only, no network |
| **Registry** | released weights | `resources.json -> registry` |

**The first row is the one that matters, and it is unique to evaluation.** A checkpoint from `stages/training/runs/<run_id>/output/` is not a file somebody put on a disk — it is the output of a run whose `run.json` records the data it trained on, its env, and its own metrics. Cite it as `run:training/<run_id>` and the eval becomes an edge in the lineage graph rather than a number about an anonymous `.pt`. **Only `mode: "production"` runs qualify** — the same rule Step 4's baseline already enforces, for the same reason.

## Step 1: Analyze Code

**First decide whether there is code to analyze.** There are two shapes and this step is written for the first:

| Shape | What you inherited | Where the eval logic is |
|---|---|---|
| **a repo** | somebody's project | files under the code directory — read them |
| **a framework** | a built artifact plus a package (`.deb` → `.pt`, a released checkpoint, a handed-over model) | inside an installed library; the entry point is its CLI |

For the second, `code_source.source` is **`framework`** and `code_source.path` holds the pinned spec (`ultralytics==8.4.40`). `layout.md` → "Code Source Resolution" has the mode; what matters here is that **the questions below still all have answers, they are just read from different places**:

| Determine | From a repo | From a framework |
|---|---|---|
| `entry_command` | the eval entry point | the CLI verb plus its args (`yolo val model=… data=… imgsz=…`) |
| `framework` + version | `requirements.txt` | `code_source.path`, resolved **in the environment the run will use** — `importlib.metadata.version` there, not here |
| `dataset` info, classes | dataset code and config | the **checkpoint**, via `/discover introspect` — `train_args.data`, plus the class list from `metadata.yaml` or the checkpoint's `names` |
| preprocessing | `dataset.py` | the framework's documented defaults **for that version**, plus whatever the artifact records (`imgsz`, `conf`, letterbox mode) |
| metrics | the printing code | the CLI's own output format for that version |

Three things to get right on the framework branch, because each is a way for the record to lie:

- **Do not offer `git init`.** There is no tree; site-packages is not the user's to initialize. The call is `code_snapshot.py … --framework <pkg>==<version>`, and it refuses an unpinned spec — a package name is not a reproduction contract.
- **Every default is now load-bearing.** A framework CLI has on the order of a hundred arguments and a version pin says nothing about which the invocation relied on. So `config.json → param_injection` carries weight it does not carry for a repo, where the code records its own defaults. Fill it from the *resolved* args, not the ones you typed.
- **A framework default is `evidence`, not a guess — but only with its version.** Write `ultralytics 8.4.40 default` and never a bare `framework default`: these move between minor releases, and an unversioned citation is a value nobody can check. Where a default cannot be established, leave the field empty — `input.json → preprocessing`'s own rule: "Leave a field empty rather than filling a framework default: a blank gets asked about, a wrong constant gets used."
- **Install the pinned version and read it.** On a repo you read the code; on a framework the code is the *installed* package, so if it is not installed there is nothing to read and every value above is documentation rather than evidence — a `claim` wearing an `evidence` field. This is not a reason to stop and it is not a question to put to the user: install it, then cite `site-packages/<pkg>/…:<line>` like any other source. Put it in the **run environment, never in MLClaw's own** — MLClaw does not run anybody's model, so a workload's framework in `pixi.toml` here would break the stdlib-only default env for nothing. It belongs where the run will execute (`resources.json → servers.<key>` / `local.env_manager`), pinned to the spec in `code_source.path`. Until that read has happened, say which values are documented and unverified rather than letting them pass as read.

Then continue below; everything from Step 1b on is the same for both shapes.

Read all code files (*.py, *.sh, *.yaml, *.yml, *.json, *.toml) under the code directory.

For pattern recognition guidance (entry points, dataset loading, ground truth formats, metric extraction), read `references/detection-patterns.md`.

Determine:
- **entry_command**: how to run evaluation. Eval code is often bundled inside training repos (e.g., `train.py --evaluate`). When this happens, extract only the evaluation path — what args control eval mode, what data it reads, what metrics it computes.
- **config_format**: argparse, yaml, omegaconf, json, hydra, or combination (e.g., "argparse+omegaconf")
- **config_path**: where the config file lives relative to code/
- **framework**: pytorch, onnxruntime, tensorrt, custom, etc.
- **Dataset info** → fill `config.json → dataset` (name, split, num_samples, classes)
- **Artifacts** → model weights, evaluator configs, label maps
- **Inputs** → test/val images, videos, text
- **Ground truth** → annotation/label data, with pairing mode
- **Outputs** → result files, visualizations
- **Metrics** → numerical values the code reports, with extraction patterns
- **Per-sample records** → the one-record-per-sample file, if the code writes one → `output.json → per_sample`. See Step 1c
- **Required packages**: run `python <mlclaw_root>/scripts/infer-init/scan_requirements.py <code_dir>`. If it fails, check requirements.txt manually.

For metrics: after identifying them, ask the user which ones to track across runs. Their selection goes into `output.json → metrics.watch`. If the code produces per-class breakdowns, set `output.json → metrics.per_class` to `true` (confirm with user).

## Step 1b: Candidates, and the match judgment

Fill `input.json -> candidates` and `artifacts.json -> candidates` — the list `/eval-run` picks from. **The schema is `/train-init` `references/schemas.md` → `candidates`; read it there.** Two deltas are documented in this skill's `references/schemas.md`, and nothing else is repeated — a second copy of that table is a copy that drifts, which is the whole reason the sweep moved to `/discover` in the first place.

Translate leads mechanically, then judge:

| lead status | `match` |
|---|---|
| `verified` | `ok` — *unless* one of the two gates below fires |
| `gone` | `absent` — looked, not there. A conclusion |
| `unreachable`, or never probed | **`unreachable`** — carry the `evidence` across, never drop the candidate |

**An unreachable source produces a candidate, not silence.** A bucket you have no key for must still appear, or `input.json` reads as proof the data does not exist. Let `/eval-run` refuse rather than guess.

### The two gates that are specific to evaluation

**1. Sample count is part of the metric, not a property of the copy.** Record `samples` on every candidate and compare it against `config.json -> dataset.num_samples`. A count that differs is **`mismatch`**, not `ok`, however healthy the files look.

This is the gate training does not need. Train on a subset and the damage shows up in the metrics; *evaluate* on a subset and the metric is simply a different number wearing the same name — mAP on 500 images against a paper's 5000-image baseline yields a delta made of sampling noise, and nothing anywhere errors. Step 4 already refuses an unqualified baseline for exactly this reason; this is the same rule one file earlier, where it can still be prevented instead of caveated.

`dataset:<id>@<snapshot>` is therefore worth more here than in training: it pins the count against a dated census, so "the val set" stops being whatever is in a directory today.

**2. Ground truth has to pair, or the candidate is not a candidate.** Images present with annotations missing is `mismatch`, not `ok`. Check the winning candidate against `pairing` before writing `ok` — a val directory that satisfies `items` and fails `ground_truth.items` produces a run that starts, loads, and reports nothing.

Before writing a `dataset:` candidate as `ok`, gate it:

```bash
python <mlclaw_root>/scripts/data/phase.py gate --project {PROJECT} --dataset <id> --to consume
```

Exit 1 is the answer, not a broken script (CLAUDE.md → "Script Integration", the fallback-rule exception). Record the blocker verbatim in `notes`, mark the candidate `mismatch`, and route to `/data-freeze`.

**Always list the code-declared path**, even absent — it is the layout reference and the yardstick for every other candidate.

If nothing reaches `ok`, stop before Step 4 rather than presenting a complete-looking config, and say which kind of no it is: `absent`/`mismatch` → `/data-collect` or a conversion; `pending` → name the party and the due date and **stop**, don't fall through to asking for a path; `unreachable` → `/resources`.

## Step 1c: Per-sample records — the file nobody used to ask about

Fill `output.json -> per_sample`. **Aggregate metrics say the model got worse; only per-sample records say where**, and until this block exists `/eval-triage` has nothing to rank — it refuses rather than inventing a pile.

Most eval code already writes one and nothing in MLClaw ever asked, so it goes unrecorded: a `--save-json` results file, a per-image detections dump, a predictions csv, a `results.pkl`. Look for a write inside the per-batch loop, an accumulating list dumped after it, or a `--save-*` flag. Record `evidence` as `path:line` — the same standard as `param_injection`, and for the same reason: until it points at a line, this block is a claim about somebody else's code.

Three fields carry the weight, and each has a failure that looks like success:

- **`score.direction`** — REQUIRED, never inferred from the field name. Rank the wrong end and the pile is the model's *best* predictions, reviewed as if they were its worst. Nothing errors and the review reads normally.
- **`unit_key` + `resolves_to`** — a finding has to be addressable by whoever will act on it. If the eval writes `image_id: 12345` while the dataset's units are `site_a/20260731/frame_0012`, the finding names something no manifest can look up and no annotator can be sent. Work out the mapping now, while the code is open in front of you; `resolves_to: null` is honest and makes `/eval-triage route` refuse, which is the correct outcome of not knowing.
- **`fields`** — what a reviewer needs to judge *without re-running anything*. Prediction, ground truth, class, and any rendered overlay the code already writes. This is what decides whether triage is a look at 40 images or 40 re-runs.

`per_sample: null` is a legitimate answer — say so plainly and tell the user what it costs (no bad-case triage until the eval code writes one), rather than half-filling the block.

## Step 2: Discover Real Config

Look for actual config files in the code directory:
1. Check config_path from analysis
2. Scan `configs/`, `config/`, `conf/` directories
3. Prefer files named "eval", "evaluate", "test", "val", "default", "baseline", "base", "main"
4. Pick the largest YAML file as fallback

If found, load all discovered parameters.

## Step 3: Select Managed Parameters

Config files often have dozens of parameters. Instead of dumping them all, use progressive disclosure — this respects the user's attention and helps them focus on what matters:

**3a. Show category summary:**
```
Config: configs/eval.yaml (35 parameters)

Categories:
  Data paths:      4 params (image root, annotation path, output dir, ...)
  Model:           3 params (weights, architecture, num_classes)
  Eval settings:   6 params (batch_size, device, confidence_threshold, ...)
  Dataset/loader:  8 params (split, num_workers, transforms, ...)
  Other:          14 params

Which categories do you want to see?
```

**3b. Expand requested categories** with current values, let user pick which MLClaw should manage per run.

**3c. Verify each pick is actually overridable.** A value in a config file is only the *declared* value — code may shadow it. For each param the user selected, trace it to the line that consumes it and look for: a literal at the use site (`nms(boxes, iou=0.5)` ignoring `cfg.nms_iou`), a post-parse assignment (`args.conf = 0.25`), or a value recomputed from another.

This matters more in eval than anywhere else, because **eval params shape the metric directly**. A `--conf-threshold` the code ignores turns a threshold sweep into five identical numbers — and the natural reading of five identical numbers is "the metric is insensitive to threshold", a wrong conclusion that looks exactly like a finding. Nothing errors; the runs all complete.

Cheap checks: `grep -rn "conf_thres\|confidence\|nms" --include=*.py <code_dir>` then read the use site; `python eval.py --help` confirms which flags actually exist.

**3d. Record**: selected params → `config.json → runtime_params` (**effective values** — what the code runs with, not what the yaml declares) with `${artifact.xxx}` / `${input.xxx}` references. Each key also gets a `config.json → param_injection.items` entry recording `via` / flag-or-key / `overridable` / `evidence` (`path:line`), per `references/run-mechanics.md` "Launch contract (Step 3 detail)" rule 3. Params found `overridable: false` must **not** go into `runtime_params` — keep them in `param_injection` with a note and tell the user which line to edit to change them. Unselected params stay in original config files untouched.

## Step 4: Present Each File for Review

Show each JSON file one at a time in order: config.json → artifacts.json → input.json → output.json.

For each file: show proposed content, then wait for the user to confirm before moving on. The reason for presenting one at a time is that each file builds on the previous — the user can catch issues early before they cascade. If the user says "skip", accept remaining files as-is.

After output.json is confirmed, ask about baseline:
"Set a baseline for comparison? (1) a previous run ID, (2) external numbers like paper results, (3) skip"

Whichever is chosen, **record the scale the baseline describes** — this is the source of every later comparison, so an unqualified number here propagates into every diff downstream.

- For (1), accept only a run with `run.json -> mode: "production"`. A debug run as baseline silently poisons every future comparison, and whoever reads those diffs months later has no way to see why the numbers look wrong. Store the run's `scope` next to the baseline so `/eval-run` can check comparability without re-reading it.
- For (2), record what the external numbers were measured on (e.g. `"COCO val2017, all 5000 images"`). Paper numbers are full-test-set numbers; a truncated run compared against them yields a delta made of sampling noise.

## Step 5: Validate

Two scripts, one job each — they do not overlap, so run both:

```
python <mlclaw_root>/scripts/infer-init/validate_refs.py <stage_dir>          # every ${} reference, all four files
python <mlclaw_root>/scripts/eval-init/validate_ground_truth.py <stage_dir>   # GT items/sources, dataset cross-check, preprocessing contract
```

`validate_refs.py` is the **only** reference validator. It is the one that knows all four files and folds `ground_truth.items` into the `input` namespace, so `${input.gt_ann}` resolves. Don't add a second reference check beside it and don't hand-verify references when it reports clean — two validators answering one question is how this step used to block saves on correct configs.

Exit codes, per CLAUDE.md "Script Integration": `1` means the script worked and found errors — fix them, don't redo the check by hand. `2` means the script broke; then check manually:

- Entry script file exists in code/
- All items have a valid `type` field
- `${artifact.xxx}` / `${input.xxx}` / `${output.xxx}` references match actual keys — remembering that `${input.xxx}` may name either a plain input or a `ground_truth.items` entry
- If `ground_truth.items` is non-empty, `pairing` must be set
- config_path file exists (if specified)
- dataset.name is filled (warn if empty — it helps when comparing runs later)

Candidate checks, added with Step 1b. The first is a **hard failure**, not a warning:

- **every candidate marked `ok` has a `samples` count equal to `config.json -> dataset.num_samples`.** A subset recorded as `ok` is the failure this whole stage's numbers rest on: the run completes, the metric is real, and it answers a different question than the baseline it will be diffed against. Mark it `mismatch` and say the count in `notes`.
- every `ok` candidate's ground truth pairs — `items` satisfied and `ground_truth.items` satisfied, per `pairing`
- every `run:` candidate names a run that exists under `stages/<stage>/runs/` with `mode: "production"` (**hard failure** — a debug checkpoint's number is comparable to nothing)
- every `dataset:` candidate marked `ok` has an empty `path`, a complete `resolve` block, and passed `gate --to consume`
- every `pending` candidate names a handoff under `{PROJECT}/handoffs/` that is **still open** (hard failure otherwise — it is either an `ok` nobody promoted or a fiction, and both send `/eval-run` to wait for something that already came back)
- at least one candidate per item is `ok`, or Step 1b already stopped and said which kind of no it was

Per-sample checks, added with Step 1c. All are warnings, not failures — an eval with no per-sample file is a working eval:

- if `per_sample.path` is set, `score.field` and `score.direction` are both non-null. A declared file with no stated bad end is worse than no file: `/eval-triage` would have to guess which way to sort, and guessing wrong yields a pile of the model's best predictions reviewed as its worst
- if `per_sample.path` is set, `unit_key` is non-null, and `resolves_to` is either set or explicitly recorded as unknown in `evidence`. Silence here becomes an unroutable finding weeks later
- `per_sample.evidence` names a real `path:line` in the code dir

Don't save if there are broken references — the user needs to fix those first, otherwise `/eval-run` will fail downstream.

## Step 6: Save

Write all 4 JSON files to `{project.root}/stages/evaluation/`. Create `stages/evaluation/assets/` if needed. Update workflow state per `references/skill-graph.md`. Offer `/eval-run` as next step.
