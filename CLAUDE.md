workspace_root: D:\agent_space\mlclaw\projects

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

## Current stage coverage

**Inference** and **evaluation** are fully implemented (init + run + report). Training, data, deployment stages follow the same pattern — when needed, create `{stage}-init` and `{stage}-run` skills modeled on inference/evaluation.

## Future direction

- **SQLite database** for real-time metrics streaming and fast queries (when JSON files become a bottleneck)
- **Training stage** with step-level metric monitoring via log tailing
- **Run comparison** skill for side-by-side metrics/params/env diff

---

## Skills

| Skill | What it does |
|-------|-------------|
| `/project-init` | Create a new project: directory structure, project.json, git init, resources template |
| `/infer-init` | Analyze inference code → fill 4 JSON configs (config, artifacts, input, output) |
| `/infer-run` | Run inference: check sources → debug mode → production mode (local/remote) → collect results |
| `/eval-init` | Analyze evaluation code → fill 4 JSON configs (config with dataset info, artifacts, input with ground truth, output with baseline) |
| `/eval-run` | Run evaluation: check sources + GT → debug mode → production mode → collect metrics → baseline comparison |
| `/eval-report` | Generate self-contained HTML report from a completed eval run (metrics, baseline diff, per-class, bad cases) |
| `/resources` | Discover local credentials, models, data. Auto-populate resources.json |

## Skill Dependency Graph

Every skill knows its position in this graph. Two types of edges:

- **requires** (↑ upstream): must be done before this skill can run. If missing, pause and prompt user to do the upstream skill first.
- **suggests** (↓ downstream): after completing, offer the user the next logical step.

```
Skill              Requires (check on entry)              Suggests (offer on exit)
─────────────────  ─────────────────────────────────────  ──────────────────────────────
/project-init      (none — root)                          /resources, /infer-init, /eval-init
/resources         project.json exists                    (return to caller, or suggest /infer-init, /eval-init)
/infer-init        project.json exists, code available    /infer-run
/eval-init         project.json exists, code available    /eval-run
/infer-run         infer-init done (config non-empty),    (done)
                   resources.json for credentials
/eval-run          eval-init done (config non-empty),     /eval-report
                   resources.json for credentials
/eval-report       eval-run completed (run.json exists)   (done)
```

### How skills use this graph

**On entry** — each skill checks its `requires` column:
1. Check each requirement in order
2. If a requirement is not met (file missing, config empty, etc.), tell the user what's missing and offer to run the upstream skill
3. If user agrees → pause self (push to stack with status `paused`), invoke the upstream skill
4. When upstream skill completes → pop it, resume self
5. If user declines → stop, don't proceed with broken dependencies

**On exit** — each skill checks its `suggests` column:
1. After successful completion, offer the next logical skill: "Next step: /xxx — want to proceed?"
2. If user says yes → invoke it (same pause/push/pop protocol)
3. If user says no → done

**`/resources` is a utility skill** — it doesn't have a fixed position in the pipeline. It gets called on-demand by any run skill when credentials are missing. It can also be invoked standalone.

### Requirement checks

| Requirement | How to check |
|-------------|-------------|
| project.json exists | file exists at `{PROJECT}/project.json` |
| code available | `code_source` is configured AND code directory has files |
| infer-init done | `{PROJECT}/stages/inference/config.json → entry_command` is non-empty |
| eval-init done | `{PROJECT}/stages/evaluation/config.json → entry_command` is non-empty |
| resources.json for credentials | checked lazily — only when a source needs non-local credentials |
| eval-run completed | `{PROJECT}/stages/evaluation/runs/*/run.json` with `status: "completed"` exists |

### Run Skill Internal Dependencies

Run skills (infer-run, eval-run) share the same internal dependency chain. Each step depends on the previous step completing, and some steps have external dependencies that trigger other skills.

```
Locate Project
     │
     │  [external: init done? ── no ──→ offer /{stage}-init]
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
     │  - code snapshot (origin_commit, project_commit)
     │  - env snapshot (packages from lifecycle/run.json template)
     │  - dependency check (required vs installed)
     │  - snapshot resolved assets → sources.json
     ↓
Step 3: Build & Execute                       depends on: run created
     │  - resolve ${} references (from assets)
     │  - build command (argparse/yaml/hydra/hybrid)
     │  - debug mode (sync, limited data) or production mode (background/remote)
     │  [if fail: diagnose → fix → retry loop]
     ↓
Step 4: Collect Results                       depends on: execution finished
     │  - finalize run.json (status, duration)
     │  - extract metrics (from stdout/files)
     │  - update runs_index.json
     │  eval-run extras: per-class metrics, baseline comparison, offer baseline update
     │
     │  [external: eval-run only ── offer /eval-report]
     ↓
  Done (pop from stack)
```

**Dependency chain**: resources → assets → run → execute → collect

- **Resources** (`resources.json`): credentials, server configs — managed by `/resources`
- **Assets** (`artifacts.json/input.json → sources`): concrete paths to models, data, GT — resources provide the credentials to access them
- **Run**: snapshots the resolved assets + env, then executes

External triggers (marked `[external]`) follow the Workflow State Protocol (below).

### Workflow State Protocol

The dependency graph is persisted across sessions via `history.json`. Each skill is **atomic** — either completed or not. No partial internal state is tracked across conversations.

**Completion is defined by outputs, not by history.** Skills check upstream dependencies by examining whether the expected output artifacts exist (see Requirement Checks table), not by reading history.json for completion records. A skill that ran but didn't produce its outputs is treated as not completed.

Every skill MUST:
1. **On entry**: push to `stack` with `project` field set to the current PROJECT path, append to `history` with status `started`
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

Skills resolve the effective code directory as:
1. If `code_source.source == "local"` and `code_source.path` is set → use `code_source.path` directly
2. If `code_source.source == "github"` → clone to `code_path` if not already cloned, then use `code_path`
3. If `code_source.source == "server"` → scp to `code_path`, then use `code_path`
4. If `code_source.source` is null → use `code_path` (relative to project root)

`code_path` always remains as the local working directory (for overrides, local modifications). When `source == "local"`, the external path is the primary read location.

### Path Mapping (Cross-Machine Execution)

When executing code on a remote server, local MLClaw paths must be mapped to remote paths. Each compute resource (server or local) in `resources.json` has:

- `mlclaw_root`: the MLClaw workspace root on that machine (e.g., `/home/ubuntu/agent_space/mlclaw`)
- `python_path`: the Python executable path on that machine (e.g., `/home/ubuntu/miniconda3/envs/ml/bin/python`)

Path mapping rule:
```
local:  {local mlclaw_root}/projects/detection/stages/evaluation/...
remote: {server mlclaw_root}/projects/detection/stages/evaluation/...
```

The project-relative path stays the same; only the root prefix changes. Run skills sync only necessary files to the remote `mlclaw_root` before execution.

## File Layout

### MLClaw repo (d:\10_projects\MLClaw) — the tool

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
    resources/SKILL.md              ← /resources
  settings.json
lifecycle/
  project.json                      ← project config template
  resources.json                    ← access credentials and resource definitions template
  history.json                      ← workflow state template
  run.json                          ← run record template
  runs_index.json                   ← runs index template
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
      update_index.py               ← append run summary to runs_index.json
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
```

### User project (e.g., D:\agent_space\mlclaw\projects\detection)

```
project.json                        ← project config (git tracked)
resources.json                      ← access credentials and resources (NEVER commit)
history.json                        ← workflow state + history
runs_index.json                     ← all runs quick lookup (alias, metrics, tags)
.gitignore
stages/
  inference/
    code/                           ← user's inference code (git tracked)
    artifacts.json                  ← filled by /infer-init
    config.json                     ← filled by /infer-init
    input.json                      ← filled by /infer-init
    output.json                     ← filled by /infer-init
    artifacts/                      ← actual artifact files (gitignored)
    data/                           ← actual input data (gitignored)
    runs/
      run_{YYYYMMDD}_{HHmmss}/
        run.json                    ← run record (code, env, metrics, lineage)
        sources.json                ← snapshot of sources used
        config_snapshot.json        ← frozen config
        outputs/                    ← output files
        logs/                       ← stdout/stderr logs
  evaluation/
    code/                           ← user's evaluation code (git tracked)
    artifacts.json                  ← filled by /eval-init
    config.json                     ← filled by /eval-init (includes dataset info)
    input.json                      ← filled by /eval-init (includes ground truth)
    output.json                     ← filled by /eval-init (includes baseline)
    artifacts/                      ← actual artifact files (gitignored)
    data/                           ← actual input data + ground truth (gitignored)
    runs/
      run_{YYYYMMDD}_{HHmmss}/
        run.json                    ← run record (code, env, metrics, lineage)
        sources.json                ← snapshot of sources used (incl. GT)
        config_snapshot.json        ← frozen config
        outputs/                    ← output files (metrics, confusion matrices)
        logs/                       ← stdout/stderr logs
```

## Variable Reference Syntax `${}`

Used across all config files. Resolved at runtime by `/{stage}-run`.

| Reference | Source |
|-----------|--------|
| `${project.name}` | project.json → name |
| `${project.root}` | project.json → root |
| `${resources.aws.region}` | resources.json → aws → region |
| `${resources.servers.xxx.host}` | resources.json → servers → xxx → host |
| `${artifact.xxx}` | stages/{stage}/artifacts.json → sources → xxx → path |
| `${input.xxx}` | stages/{stage}/input.json → sources → xxx → path |
| `${output.xxx}` | stages/{stage}/runs/run_NNN/outputs/ (resolved at runtime) |
