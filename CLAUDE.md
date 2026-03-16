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

Only **inference** is fully implemented (init + run). Training, evaluation, data, deployment stages follow the same pattern — when needed, create `{stage}-init` and `{stage}-run` skills modeled on inference.

## Future direction

- **SQLite database** for real-time metrics streaming and fast queries (when JSON files become a bottleneck)
- **Training stage** with step-level metric monitoring via log tailing
- **Run comparison** skill for side-by-side metrics/params/env diff
- **Visualization** via generated charts or local web UI

---

## Skills

| Skill | What it does |
|-------|-------------|
| `/project-init` | Create a new project: directory structure, project.json, git init, secrets template |
| `/infer-init` | Analyze inference code → fill 4 JSON configs (config, artifacts, input, output) |
| `/infer-run` | Run inference: check sources → debug mode → production mode (local/remote) → collect results |
| `/resources` | Discover local credentials, models, data. Auto-populate secrets.json |

## On Conversation Start

Check the most recently used project (by `history.json → updated_at`):

### 1. Check for running tasks

Scan `stages/*/runs/*/run.json` in the current project. If any has `"status": "running"`:
- Tell user: "There is a running task: {project}/{stage}/run_{NNN} on {server or local}. Check status?"
- If yes → check if still running (local: PID alive? remote: tmux session alive?) → report progress or collect results
- If no → continue

### 2. Check workflow stack

Read `history.json` in the current project.

- If `stack` is non-empty → there is unfinished work. Tell the user:
  "Last session was in the middle of {skill} at step {step}. Resume or start fresh?"
  - Resume → continue from where stack says
  - Start fresh → clear stack (but keep history), proceed with new request
- If `stack` is empty → no pending work, proceed normally.

## Workflow State Protocol

Every skill MUST:
1. **On entry**: push to `stack` with `project` field set to the current PROJECT path, append to `history` with status `started`
2. **On calling sub-skill**: update own status to `paused` in history, push sub-skill to stack (inherit `project` from parent)
3. **On sub-skill return**: pop sub-skill from stack, append `resumed` to history, continue
4. **On completion**: pop self from stack, append `completed` to history
5. **On error/interruption**: leave stack as-is (so next conversation can detect and resume)

Write `history.json` after every state change.

## Script Integration

Skills use Python scripts from `lifecycle/scripts/<skill>/` for mechanical tasks. Each skill's scripts are in a matching subdirectory, invoked via `python lifecycle/scripts/<skill>/<name>.py <args>`.

**Fallback rule**: If a script fails (non-zero exit, import error, file not found), the skill MUST NOT stop. Instead:
1. Log the error silently
2. Perform the same operation manually (Claude does it inline using Bash/Read/Write tools)
3. Continue the flow as if the script had succeeded

Scripts are an optimization, not a dependency.

## File Layout

### MLClaw repo (d:\10_projects\MLClaw) — the tool

```
CLAUDE.md                           ← this file
.claude/
  skills/
    project-init/SKILL.md           ← /project-init
    infer-init/SKILL.md             ← /infer-init
    infer-run/SKILL.md              ← /infer-run
    resources/SKILL.md              ← /resources
  settings.json
lifecycle/
  project.json                      ← project config template
  secrets.json                      ← credentials template
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
```

### User project (e.g., D:\agent_space\mlclaw\projects\detection)

```
project.json                        ← project config (git tracked)
secrets.json                        ← credentials (NEVER commit)
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
```

## Variable Reference Syntax `${}`

Used across all config files. Resolved at runtime by `/{stage}-run`.

| Reference | Source |
|-----------|--------|
| `${project.name}` | project.json → name |
| `${project.root}` | project.json → root |
| `${secrets.aws.region}` | secrets.json → aws → region |
| `${secrets.servers.xxx.host}` | secrets.json → servers → xxx → host |
| `${artifact.xxx}` | stages/{stage}/artifacts.json → sources → xxx → path |
| `${input.xxx}` | stages/{stage}/input.json → sources → xxx → path |
| `${output.xxx}` | stages/{stage}/runs/run_NNN/outputs/ (resolved at runtime) |
