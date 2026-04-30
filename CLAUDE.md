workspace_root: ~/agent_space/mlclaw/projects
# Where this user's MLClaw projects live. Edited by /project-init when the user picks a different path. `~/` expands to the OS home dir at use time (Windows: substitute `%USERPROFILE%\agent_space\mlclaw\projects`).

# MLClaw

You are managing an ML lifecycle tool. Your role is to be the ML engineer's interface to their entire pipeline — from code to production.

## What this project does

MLClaw replaces MLflow/W&B/TensorBoard with conversation-driven ML lifecycle management. Users bring their ML code (from GitHub, colleagues, papers), and you:

1. **Analyze their code** — understand what it needs, what it produces, how to run it
2. **Configure everything via dialogue** — no SDK integration, no code changes, no YAML editing
3. **Execute across environments** — local machine or remote servers, debug or production
4. **Track everything automatically** — code version, environment, metrics, lineage DAG
5. **Maintain state across sessions** — user closes laptop, comes back next day, you pick up where they left off

## Key design principles

- **Zero code invasion**: never ask users to add logging/tracking calls to their code. Extract everything from outside (stdout parsing, file scanning, env capture).
- **One question at a time**: never dump multiple questions on the user. Ask one, record answer, ask next.
- **Scripts are fallback-safe**: if any Python script fails, do the same work manually with Bash/Read/Write tools. Never let a script bug block the workflow.
- **JSON configs are the source of truth**: fixed keys, you fill values. Templates in `lifecycle/`, filled instances in user projects.
- **Confirm before saving**: always show what you're about to write, wait for user confirmation. Never auto-overwrite existing values.

## Status

**Implemented**: inference (init + run), evaluation (init + run + report), refactor (init + run), training (init + run + tune + tune-report), project init, resources.

**Next**: data stage, deployment stage, exploration stage (architecture search).

## Skills & Dependencies

| Skill | What it does |
|-------|-------------|
| `/project-init` | Create a new project: directory structure, project.json, git init |
| `/infer-init` | Analyze inference code → fill 4 JSON configs (config, artifacts, input, output) |
| `/infer-run` | Run inference: check sources → debug mode → production mode (local/remote) → collect results |
| `/eval-init` | Analyze evaluation code → fill 4 JSON configs (config with dataset info, artifacts, input with ground truth, output with baseline) |
| `/eval-run` | Run evaluation: check sources + GT → debug mode → production mode → collect metrics → baseline comparison |
| `/eval-report` | Generate self-contained HTML report from a completed eval run (metrics, baseline diff, per-class, bad cases) |
| `/train-init` | Analyze training code → fill 4 JSON configs (config with resources, artifacts, input with ground truth, output with streaming-metric schema + checkpoint selection + done signal) |
| `/train-run` | Run training: validate resources → resolve sources → background launch → monitor stream (heartbeat, last_step, latest_metrics) → detect done/crash → finalize (best ckpt + retention) |
| `/train-tune` | Adaptive HPO loop: agent observes prior runs → identifies coverage gaps → hypothesizes next config → launches trials via /train-run → iterates until budget or convergence. Auto-invokes /train-tune-report at close. |
| `/train-tune-report` | Render a tune session as markdown chain.md: headline, best-so-far curve, coverage map, decision timeline (with [fill_grid|refine_best|add_axis|verify] tags), confirmed/refuted distillation, recipe. |
| `/data-check` | Validate data quality: file integrity, code compatibility, annotation consistency, statistics, cross-dataset comparison |
| `/data-report` | Generate HTML data quality report with distribution charts, outlier gallery, cross-dataset drift |
| `/refactor-init` | Clone research repo, analyze codebase, classify modules (core/support/dead), extract paper benchmark targets |
| `/refactor-run` | Execute one refactoring round: make changes → run benchmark → compare with paper → commit or revert |
| `/refactor-report` | Generate refactoring audit report: round changelog, rollback points, verification results, reproduction instructions |
| `/resources` | Discover local credentials, models, data. Auto-populate workspace-level resources.json |

### Node hierarchy

```
project
  └── stage (evaluation, inference, training, ...)
       └── skill (init, run, report — operations on a stage)
            └── execution (run_20260317_... — one instantiation of a skill execution)
                 └── steps (resolve_assets, create_run, execute, collect_results)
```

- **Stage** is a lifecycle phase. **Skill** is an operation on that stage. **Execution** is one instantiation of running a skill.
- `/project-init` is project-level, not tied to a stage. `/resources` is workspace-level — `resources.json` lives at the workspace root and is shared across all projects.
- **Run skill** executions are fully tracked: `runs/run_20260317_.../` with run.json, steps, outputs, logs.
- **Init skill** executions are currently not tracked separately — completion is defined by output files (4 JSON configs). Can be added later.
- **Report skill** executions are stored as output files within a run's `outputs/` directory.
- On disk, `runs/` directory = executions of the run skill. The naming is kept for simplicity.

### Skill Dependency Graph

Every skill knows its position in this graph. Two types of edges:

- **requires** (↑ upstream): must be done before this skill can run. If missing, pause and prompt user to do the upstream skill first.
- **suggests** (↓ downstream): after completing, offer the user the next logical step.

```
Skill              Requires (check on entry)              Suggests (offer on exit)
─────────────────  ─────────────────────────────────────  ──────────────────────────────
/project-init      (none — root)                          /resources, /infer-init, /eval-init, /train-init, /refactor-init
/resources         (none — workspace-level)                (return to caller, or suggest /infer-init, /eval-init, /train-init)
/infer-init        project.json exists, code available    /infer-run
/eval-init         project.json exists, code available    /eval-run
/train-init        project.json exists, code available    /train-run
/train-run         train-init done (config non-empty),    /eval-run, /train-tune
                   resources.json for credentials
/train-tune        train-init done, ≥1 prior train-run    /train-tune-report (auto at close)
                   completed
/train-tune-report tune session exists with ≥1 run        (done)
/refactor-init     project.json exists                    /refactor-run
/infer-run         infer-init done (config non-empty),    (done)
                   resources.json for credentials
/eval-run          eval-init done (config non-empty),     /eval-report
                   resources.json for credentials
/refactor-run      refactor-init done (plan.json exists), /refactor-run (next round),
                   resources.json for credentials       /refactor-report (when complete)
/refactor-report   refactor-run completed (run.json)    (done)
/eval-report       eval-run completed (run.json exists)   (done)
/data-check        project.json exists, data path         /data-report, then /{stage}-run if clean
/data-report       data-check completed (report.json)     (done)
```

#### How skills use this graph

**On entry** — check `requires` column. If a requirement is not met, offer to run the upstream skill. If user agrees, invoke it as a sub-skill (see Workflow State Protocol below). If user declines, stop.

**On exit** — check `suggests` column. Offer the next skill. If user accepts, invoke it as a sub-skill.

**`/resources`** is a utility skill — called on-demand by any run skill when credentials are missing. Can also be invoked standalone.

#### Requirement checks

| Requirement | How to check |
|-------------|-------------|
| project.json exists | file exists at `{PROJECT}/project.json` |
| code available | `code_source` is configured AND code directory has files |
| infer-init done | `{PROJECT}/stages/inference/config.json → entry_command` is non-empty |
| eval-init done | `{PROJECT}/stages/evaluation/config.json → entry_command` is non-empty |
| train-init done | `{PROJECT}/stages/training/config.json → entry_command` is non-empty |
| ≥1 prior train-run completed | `{PROJECT}/stages/training/runs/*/run.json` with `status: "completed"` exists |
| tune session exists with ≥1 run | `{PROJECT}/stages/training/tune_sessions/*/state.json` exists AND ≥1 run with `lineage.session = <id>` |
| resources.json for credentials | checked lazily — `{WORKSPACE}/resources.json`, only when a source needs non-local credentials |
| eval-run completed | `{PROJECT}/stages/evaluation/runs/*/run.json` with `status: "completed"` exists |
| refactor-init done | `{PROJECT}/stages/refactor/plan.json` exists with non-empty `modules` |
| refactor-run completed | `{PROJECT}/stages/refactor/runs/*/run.json` with `status: "completed"` exists |
| env_manager available | `{WORKSPACE}/resources.json → local.env_manager.tool` is non-empty |

### Run Skill Internal Dependencies

Run skills (infer-run, eval-run, train-run) share the same internal dependency chain. Each step depends on the previous step completing, and some steps have external dependencies that trigger other skills. Train-run adds a `monitor` step between `execute` and `collect_results` to handle long-running streaming state (heartbeat, last_step, latest_metrics).

```
Locate Project
     │
     │  [external: init done? ── no ──→ offer /{stage}-init]
     ↓
Fork check: "Base on a previous run?"
     │  - no  → proceed normally (fresh run)
     │  - yes → load base run's config_snapshot.json + sources.json + lineage.parents
     │          set fork_of = base run ID
     │          user modifies only what they want to change
     │          skip Step 1 if sources unchanged
     ↓
Step 1: Resolve Assets                        depends on: init (items defined),
     │  - fill concrete paths in                         resources (credentials)
     │    artifacts.json/input.json sources
     │  - ask user for each path (one at a time)
     │  [external: credentials fail? ── invoke /resources ── resume]
     │  - validate paths exist, test connectivity
     ↓
Step 2: Create Run                            depends on: assets resolved
     │  - create run dir + run.json
     │  - code snapshot — see "Code snapshot (Step 2 detail)" below
     │  - env snapshot (packages from lifecycle/run.json template)
     │  - dependency check (required vs installed)
     │  - snapshot resolved assets → sources.json
     │  - if fork_of is set: compute lineage.variation_summary
     │    (diff runtime_params vs base run; null otherwise)
     │  - if user provided hypothesis: store in run.json -> hypothesis
     ↓
Step 3: Build & Execute                       depends on: run created
     │  - resolve ${} references (from assets)
     │  - build command (argparse/yaml/hydra/hybrid)
     │  - cwd + output_dir — see "Launch contract (Step 3 detail)" below
     │  - debug mode (sync, limited data) or production mode (background/remote)
     │  [if fail: diagnose → fix → retry loop]
     ↓
Step 4: Collect Results                       depends on: execution finished
     │  - finalize run.json (status, duration, metrics) — this is the
     │    only writeback; there is no separate index file to maintain
     │  - extract metrics (from stdout/files)
     │  eval-run extras: per-class metrics, baseline comparison, offer baseline update
     │
     │  [external: eval-run only ── offer /eval-report]
     ↓
  Done (pop from stack)
```

**Internal step recording**: each run skill writes step completion to `run.json → steps`. Each step has `status` (`null` / `completed` / `skipped` / `failed`) and `at` (timestamp). Used for debugging, reproducibility, and resume — on resume, the skill reads `run.json → steps` to skip completed steps.

Steps correspond to headings in the skill's SKILL.md: `##` headings are major steps, `###` headings are sub-steps. All are recorded. Different stages may have different steps (e.g., training-run may add a `monitor` step).

`steps.ad_hoc` is an array for unplanned actions that don't match any predefined step — e.g., fixing a file permission, patching a config typo, installing a missing package. Each entry: `{ "name": "...", "description": "...", "after_step": "...", "status": "...", "at": "..." }`. If the same ad_hoc action shows up across multiple runs, it's a signal to promote it into a formal step in the SKILL.md.

#### Code snapshot (Step 2 detail)

Every run skill captures the exact code state at run-time so a completed run is self-contained for reproduction.

- **`code_dir`** is resolved by the unified rule from "Code Source Resolution":
  ```
  code_dir = stages/<stage>/code/_source if exists else stages/<stage>/code
  ```
- **Helper**: `python <mlclaw_root>/lifecycle/scripts/shared/code_snapshot.py <code_dir> <RUN_DIR>` — outputs a JSON dict; merge it into `run.json -> code`.
- **Git working tree** (typical): records `repo` (origin URL), `branch`, `origin_commit` (SHA). If the tree is dirty, writes `<RUN_DIR>/code_dirty.patch` (output of `git diff HEAD`) and fills `dirty_patch_path` + `dirty_files_count`. Reproduction contract: `git checkout <origin_commit> && git apply <run_dir>/code_dirty.patch`.
- **Non-git directory** (rare, one-off scripts): falls back to a full copy into `<RUN_DIR>/code_snapshot/` (excludes `__pycache__`, `.venv`, `*.pt`, `*.pth`); records `snapshot_dir` instead.

The same call applies to all run skills (`/train-run`, `/eval-run`, `/infer-run`, `/refactor-run`) — no per-skill variant.

#### Launch contract (Step 3 detail)

Two rules, uniform across all run skills:

1. **`cwd = <code_dir>`** (the same path used in Step 2). For module-style entry commands like `python -m pkg.train` this is mandatory — package imports won't resolve from anywhere else.
2. **`output_dir` (or framework equivalent) must be overridden to an absolute path under `<RUN_DIR>/output/`**. The default config's relative `output_dir` would land artifacts back in the user's code repo, where MLClaw can't manage them and the next run would overwrite them. Override syntax depends on the framework:
   - omegaconf / hydra: `--set output_dir=<abs>` or `+output_dir=<abs>`
   - argparse: `--output-dir <abs>` / `--output_dir <abs>`
   - HF Trainer: `--output_dir <abs>`
   - accelerate: `--output_dir <abs>`
   - env var: `OUTPUT_DIR=<abs>`
   `/train-init` (and per-stage init skills) record which form a given codebase uses; `/{stage}-run` consumes that and substitutes.

Stage-specific extras (production mode launching, monitoring, ETA computation, finalize hooks) go in each run skill's SKILL.md — these two rules do not.

#### Listing runs (no separate index)

There is no `runs_index.json` cache. The source of truth is the `run.json` files themselves; "list all runs" / "find comparable runs" is a `jq` query over the run tree, run on demand. This avoids cache drift after rsync, manual run deletion, schema evolution, and concurrent updates — none of which need any code to handle when there's no cache.

Canonical patterns:

```bash
# All completed runs in a stage, with key fields
jq -s '
  map(select(.status == "completed") | {
    run_id, alias, status, duration_s,
    primary_metric: .metrics.best.primary_metric_value,
    path: ("stages/" + .stage + "/runs/" + .run_id),
    session: .lineage.session
  })
' stages/<stage>/runs/run_*/run.json

# Runs comparable for /train-tune (same code SHA + dataset split)
jq -s --arg sha "$SHA" '
  map(select(.code.origin_commit == $sha and .lineage.session == null))
' stages/training/runs/run_*/run.json

# Most recent N runs (by created_at) for menus
jq -s 'sort_by(.created_at) | reverse | .[0:10]' stages/<stage>/runs/run_*/run.json
```

If a single `run.json` is malformed, jq errors on that file; agents should `find ... -exec jq ...` per-file with `2>/dev/null` when scanning unattended. At realistic scale (≤ 10k runs per project) the scan completes in well under a second.

Each run tracks two types of relationships in `run.json → lineage`:

```
lineage:
  parents:             ["training/run_20260315_120000"]   ← I consume their output artifact
  fork_of:             "evaluation/run_20260317_091500"   ← I copied their config to start
  variation_summary:   "lr: 1e-4 → 2e-4; warmup: 0 → 0.03"  ← auto-derived diff vs fork_of
  session:             "20260428_120000_lr_search"        ← optional, set by /train-tune
```

- **`parents`** (cross-stage, hard dependency): this run consumes artifacts produced by those runs (e.g., eval consumes train ckpt). Base's artifact must exist for this run to be reproducible. Drawn as solid arrow across stage columns.
- **`fork_of`** (same-stage, metadata only): this run started from that run's config, with modifications. **No I/O dependency** — fork is reproducible even if base is deleted. Drawn as dashed arrow within the same stage column.
- **`variation_summary`** (auto-derived, optional): short human-readable diff of `runtime_params` vs the `fork_of` base, e.g., `"lr: 1e-4 → 2e-4; warmup_ratio: 0 → 0.03"`. Filled by the run skill at create time. Null when `fork_of` is null. Saves `/train-compare` and DAG renderers from re-diffing snapshots.
- **`session`** (optional): when this run is part of a `/train-tune` HPO session, this field holds the session ID (matches `tune_sessions/<id>/` directory). Null for ad-hoc runs. `/train-tune-report` filters runs by this field to render a single session's chain.md without scanning all project runs.

```
     training           evaluation
     train_run_1  ──→   eval_run_1
                  ──→   eval_run_2  (fork_of: eval_run_1, changed threshold)
                  ──→   eval_run_3  (fork_of: eval_run_1, changed dataset)

     train_run_2  ──→   eval_run_4  (fresh, not a fork)
```

**Fork behavior**: when `fork_of` is set, the run skill loads the base run's `config_snapshot.json`, `sources.json`, and `lineage.parents` as starting point. User only changes what they want. Assets that haven't changed are reused (Step 1 can be skipped). Fork inherits the base run's `lineage.parents` — if the user changes the model artifact (not just params), parents should be updated accordingly.

**Continuing training / preempt recovery / fine-tuning**: there is no separate lineage field for "I extend prior training". Express it as a `fork_of` (config copy) plus loading the base's checkpoint as initial weights via `runtime_params` — and add the base to `parents` since the new run consumes its ckpt. The reasoning ("why continue") lives in the run's `description` / `hypothesis` field, or in `decisions.jsonl` when running `/train-tune`.

### Optional narrative fields

Two top-level run.json fields exist purely to enrich human-readable reports. **Both are optional, default null, and tools must not require them.**

- **`hypothesis`** (set at run creation): a one-sentence expectation, e.g., `"Higher lr with warmup should reach lower val_loss faster."` Skills may prompt for it but should never block on it.
- **`outcome`** (set at run completion): a free-text retrospective, e.g., `"Refuted. val_loss 0.234 → 0.241 (+3%), convergence epoch 87 → 92."` Agents fill this when finalizing; users may also write it manually.

When both are present, `/train-compare` weaves them into the narrative ("hypothesis was X; outcome confirmed/refuted"). When absent, comparisons fall back to pure metric deltas — both should remain valid.

### Workflow State Protocol

The dependency graph is persisted across sessions via `history.json`.

**Two levels of state tracking**:
- **Inter-skill** (dependency graph): completion is defined by **output artifacts**, not by history records. Skills check upstream dependencies by examining whether the expected outputs exist (see Requirement Checks table). A skill that ran but didn't produce its outputs is treated as not completed.
- **Intra-skill** (execution steps): progress is tracked in `run.json → steps`. On resume, the stack entry points to the exact execution and step, and `run.json → steps` shows which steps completed — so the skill can skip finished steps and continue from where it stopped.

Stack entries follow the node hierarchy — they locate the exact position in the tree:

```json
{
  "skill": "eval-run",
  "stage": "evaluation",
  "execution": "run_20260317_091500",
  "step": "execute",
  "project": "~/agent_space/mlclaw/projects/detection"
}
```

- `skill` + `stage`: which skill on which stage
- `execution`: which specific execution instance (null for init skills that don't create executions yet)
- `step`: which step within the execution (matches a key in `run.json → steps`)

On resume, read the execution's `run.json → steps` to see exactly which steps completed and which didn't.

Every skill MUST:
1. **On entry**: push to `stack` with `project`, `skill`, `stage`, and `step` fields. For run skills, also set `execution` once the run dir is created. Append to `history` with status `started`
2. **On calling sub-skill** (upstream dependency or downstream suggestion): update own status to `paused` in history, push sub-skill to stack (inherit `project` from parent)
3. **On sub-skill return**: pop sub-skill from stack, append `resumed` to history, continue
4. **On completion**: pop self from stack, append `completed` to history
5. **On error/interruption**: leave stack as-is (so next conversation can detect and resume)

Write `history.json` after every state change.

### On Conversation Start

Restore the dependency chain state from the previous session:

1. **Check for running tasks**: scan `stages/*/runs/*/run.json` in the current project. If any has `"status": "running"`:
   - Tell user: "There is a running task: {project}/{stage}/run_{NNN} on {server or local}. Check status?"
   - If yes → check if still running (local: PID alive? remote: tmux session alive?) → report progress or collect results
   - If no → continue

2. **Check workflow stack**: read `history.json` in the current project.
   - If `stack` is non-empty → there is unfinished work. Tell the user:
     "Last session was in the middle of {skill} at step {step}. Resume or start fresh?"
     - Resume → continue from where stack says
     - Start fresh → clear stack (but keep history), proceed with new request
   - If `stack` is empty → no pending work, proceed normally.

---

## Conventions

### Script Integration

Skills use Python scripts from `lifecycle/scripts/<skill>/` for mechanical tasks. Each skill's scripts are in a matching subdirectory, invoked via `python lifecycle/scripts/<skill>/<name>.py <args>`.

**Fallback rule**: If a script fails (non-zero exit, import error, file not found), the skill MUST NOT stop. Instead:
1. Log the error silently
2. Perform the same operation manually (Claude does it inline using Bash/Read/Write tools)
3. Continue the flow as if the script had succeeded

Scripts are an optimization, not a dependency.

### Environment Resolution

All run skills (infer-run, eval-run, refactor-run, future train-run) need a Python environment to execute code.

**One project, one env** — prefer a single shared environment per project. Different stages in the same project usually share the same codebase (or refactored version of it), so their dependencies overlap. Maintaining separate envs per stage is unnecessary overhead.

Environment resolution:

1. **Check `project.json → env_name`** (project-level). If set, use it for all stages.

2. **If empty, look for an existing env**:
   - Refactor stage has a verified env (`plan.json → env.env_name`)? → promote it to project-level: set `project.json → env_name`, reuse for all stages.
   - No refactor env? → create a new one (see below).

3. **Create project env**: use the env manager from `{WORKSPACE}/resources.json → local.env_manager`:
   - Env name: `mlclaw_{project_name}` (e.g., `mlclaw_detection`)
   - Install from: `requirements.txt` or `setup.py` in the stage's code directory
   - Record in `project.json → env_name`
   - If a later stage needs extra packages, install them into the same env — don't create a new one.

4. **Stage-level override** (rare): if a stage truly has conflicting dependencies (e.g., inference needs TensorRT but training doesn't), set `project.json → stages.{stage}.env_name` to override. This is the exception, not the norm.

5. **Remote execution**: server's `python_path` in `resources.json → servers.{key}` takes precedence. Local env resolution doesn't apply.

**Env manager** is read from `{WORKSPACE}/resources.json → local.env_manager.tool` (mamba/conda/uv). If empty, invoke `/resources` to detect it.

Run skills use `{run_in_env}` as shorthand for the activation command:
- mamba/conda: `mamba run -n {env_name}` or `conda run -n {env_name}`
- uv/venv: `source {venv_path}/bin/activate &&` (Linux) or `{venv_path}/Scripts/activate &&` (Windows)

### Workspace and tool-repo location

**Workspace** — directory holding all of this user's MLClaw projects + their shared `resources.json`. One value, lives in `workspace_root:` at the top of this file. `/project-init` rewrites that line if the user picks a different path. No registry, no priority chain — when there's actually more than one workspace on the same machine, that's the time to add structure, not before. CLI override: `/project-init --workspace <path>`.

**MLClaw repo** — auto-detected and cached in `~/.mlclaw/state.json`:
```
mlclaw_root  = $(python <repo>/lifecycle/scripts/shared/workspaces.py tool)
```
Self-bootstraps from `__file__` on first call, so skills don't need the user to pass the MLClaw path each time. Override with `workspaces.py register-tool <path>` if you have multiple clones.

**Path portability**: `init_project.py` rewrites any `$HOME`-relative path in `project.json` to `~/`-prefixed form (`root`, `workspace`, every `stages.<>.code_source.path`). Always `os.path.expanduser` before using these paths.

### Code Source Resolution

Each stage in `project.json` has a `code_source` block:

```json
"code_source": {
  "source": "local|github|server",
  "path": "",
  "branch": null,
  "commit": null,
  "credentials": ""
}
```

| Source | Behavior |
|--------|----------|
| `local` | Read code directly from `path` (external directory, no copy). Acts as a soft link. |
| `github` | Clone repo from `path` (URL) into `code_path`. Track `branch` and `commit`. |
| `server` | SCP code from remote `path` into `code_path`. Uses `credentials` for SSH. |
| `null` | Code already lives in `code_path` (manually placed). |

**Unified code-dir rule** — every skill's cwd / read path is exactly one expression:

```
code_dir = stages/<stage>/code/_source if exists else stages/<stage>/code
```

`/project-init` puts the right thing under `stages/<stage>/code/` per source mode, so all downstream skills only see the unified path:

| Source | What `/project-init` does | Effective `code_dir` |
|---|---|---|
| `local` | Symlink `stages/<stage>/code/_source → expanduser(code_source.path)`. The user's external repo stays the source of truth — edits in the IDE are visible immediately. | `code/_source` (the symlink) |
| `github` | `git clone code_source.path` into `stages/<stage>/code/`, then remove `.git` so files are tracked under project git. | `code/` (no `_source`) |
| `server` | `scp` from remote `path` into `stages/<stage>/code/`. | `code/` (no `_source`) |
| `null` | Code was placed manually under `code/`. | `code/` (no `_source`) |

**Why a symlink (not a copy) for `local`**: ML users iterate in their own repo with their own IDE/git. Copying creates two trees and bidirectional sync friction; symlink keeps a single source of truth. The lockdown of "what code did this run actually use" is solved separately at run-time by `code_snapshot.py` (SHA + dirty patch — see Run Skill Internal Dependencies).

**rsync portability**: the symlink stores an *expanded* absolute path (filesystems don't expand `~` at read time), so it dangles after `rsync` to a new machine where `$HOME` is different. Recovery: `python lifecycle/scripts/shared/relink_sources.py [<project_root>]` reads the `~/`-portable `code_source.path` from `project.json` and recreates symlinks for all local-source stages on the current host. Idempotent.

`code_path` (project.json) is always `stages/<stage>/code` — keep it as the join target, don't reinterpret it per source.

### Path Mapping (Cross-Machine Execution)

When executing code on a remote server, local MLClaw paths must be mapped to remote paths. Each compute resource (server or local) in `{WORKSPACE}/resources.json` has:

- `mlclaw_root`: the MLClaw workspace root on that machine (e.g., `/home/ubuntu/agent_space/mlclaw`)
- `python_path`: the Python executable path on that machine (e.g., `/home/ubuntu/miniconda3/envs/ml/bin/python`)

Path mapping rule:
```
local:  {local mlclaw_root}/projects/detection/stages/evaluation/...
remote: {server mlclaw_root}/projects/detection/stages/evaluation/...
```

The project-relative path stays the same; only the root prefix changes. Run skills sync only necessary files to the remote `mlclaw_root` before execution.

### File Layout

#### MLClaw repo (`<wherever the MLClaw tool repo is cloned>`, e.g. `~/code/MLClaw`) — the tool

```
CLAUDE.md                           ← this file
.claude/
  skills/
    project-init/SKILL.md           ← /project-init
    infer-init/SKILL.md             ← /infer-init
    infer-run/SKILL.md              ← /infer-run
    eval-init/SKILL.md              ← /eval-init
    eval-run/SKILL.md               ← /eval-run
    eval-report/SKILL.md            ← /eval-report
    data-check/SKILL.md             ← /data-check
    data-report/SKILL.md            ← /data-report
    refactor-init/SKILL.md          ← /refactor-init
    refactor-run/SKILL.md           ← /refactor-run
    refactor-report/SKILL.md        ← /refactor-report
    resources/SKILL.md              ← /resources
  settings.json
lifecycle/
  project.json                      ← project config template
  resources.json                    ← access credentials and resource definitions template
  history.json                      ← workflow state template
  run.json                          ← run record template
  scripts/
    project-init/
      init_project.py               ← create project dirs, copy templates, git init
    infer-init/
      scan_requirements.py          ← extract dependencies from code
      validate_refs.py              ← validate ${} references across JSONs
    infer-run/
      create_run.py                 ← create run directory + initialize run.json
      capture_env.py                ← capture ML environment snapshot
      check_deps.py                 ← compare required vs installed packages
      test_connection.py            ← test SSH/S3 connectivity
      extract_metrics.py            ← extract metrics from stdout/result files
      finalize_run.py               ← update run.json with duration/status
    eval-init/
      validate_ground_truth.py      ← validate GT config consistency
    eval-run/
      compare_baseline.py           ← compare metrics against baseline
    resources/
      parse_ssh_config.py           ← parse ~/.ssh/config into server entries
    shared/
      build_dag.py                  ← build lineage DAG from all runs
      tag_lineage.py                ← tag a run + propagate up the DAG
  inference/                        ← inference stage JSON templates
    artifacts.json
    config.json
    input.json
    output.json
  evaluation/                       ← evaluation stage JSON templates
    artifacts.json
    config.json                     ← includes dataset block
    input.json                      ← includes ground_truth block
    output.json                     ← includes per_class, baseline
  refactor/                         ← refactor stage JSON templates
    plan.json                       ← repo analysis, module classification, paper benchmarks
    config.json                     ← benchmark entry command + params
    artifacts.json                  ← model weights for benchmark
    input.json                      ← benchmark data + ground truth
    output.json                     ← expected metrics from paper
    refactor_run.json               ← run record template (refactor-specific steps)
```

#### Workspace root (`{workspace_root}`, e.g. `~/agent_space/mlclaw/projects`)

```
resources.json                      ← access credentials and resources, shared across all projects (NEVER commit)
detection/                          ← one project
another_project/                    ← another project
```

#### User project (`{workspace_root}/{project_name}`, e.g. `~/agent_space/mlclaw/projects/detection`)

```
project.json                        ← project config (git tracked)
history.json                        ← workflow state + history
.gitignore
stages/
  {stage}/                          ← same structure for each stage (inference, evaluation, ...)
    code/                           ← user's code (git tracked)
    artifacts.json                  ← filled by /{stage}-init
    config.json                     ← filled by /{stage}-init
    input.json                      ← filled by /{stage}-init
    output.json                     ← filled by /{stage}-init
    artifacts/                      ← actual artifact files (gitignored)
    data/                           ← actual input data (gitignored)
    runs/
      run_{YYYYMMDD}_{HHmmss}/      ← one execution
        run.json                    ← run record (code, env, metrics, steps, lineage)
        sources.json                ← snapshot of sources used
        config_snapshot.json        ← frozen config
        outputs/                    ← output files
        logs/                       ← stdout/stderr logs
  refactor/                         ← refactor stage (special structure)
    original/                       ← GitHub clone, read-only reference
    code/                           ← refactored version (working copy, git tracked)
    plan.json                       ← refactoring plan + module classification
    config.json                     ← benchmark config
    artifacts.json                  ← benchmark artifacts
    input.json                      ← benchmark inputs + ground truth
    output.json                     ← benchmark expected metrics
    snapshots/                      ← module I/O snapshots from original code (for Tier 2 verification)
    runs/                           ← refactoring round executions
      run_{YYYYMMDD}_{HHmmss}/      ← one round
```

### Variable Reference Syntax `${}`

Used across all config files. Resolved at runtime by `/{stage}-run`.

| Reference | Source |
|-----------|--------|
| `${project.name}` | project.json → name |
| `${project.root}` | project.json → root |
| `${resources.aws.region}` | {WORKSPACE}/resources.json → aws → region |
| `${resources.servers.xxx.host}` | {WORKSPACE}/resources.json → servers → xxx → host |
| `${artifact.xxx}` | stages/{stage}/artifacts.json → sources → xxx → path |
| `${input.xxx}` | stages/{stage}/input.json → sources → xxx → path |
| `${output.xxx}` | stages/{stage}/runs/run_NNN/outputs/ (resolved at runtime) |
