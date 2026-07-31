# Layout, paths, and reference syntax

Loaded on demand. Where things live on disk, how a stage's code directory is
resolved, how paths survive a cross-machine rsync, and how `${}` references
resolve. Needed by init skills and by anything that has to find a file; not
needed to decide what to do next, which is why it is not in CLAUDE.md.

## Workspace and tool-repo location

**Workspace** — directory holding all of this user's MLClaw projects + their shared `resources.json`. One value, lives in `workspace_root:` at the top of this file. `/project-init` rewrites that line if the user picks a different path. No registry, no priority chain — when there's actually more than one workspace on the same machine, that's the time to add structure, not before. CLI override: `/project-init --workspace <path>`.

**MLClaw repo** — auto-detected and cached in `~/.mlclaw/state.json`:
```
mlclaw_root  = $(python <repo>/lifecycle/scripts/shared/workspaces.py tool)
```
Self-bootstraps from `__file__` on first call, so skills don't need the user to pass the MLClaw path each time. Override with `workspaces.py register-tool <path>` if you have multiple clones.

**Path portability**: `init_project.py` rewrites any `$HOME`-relative path in `project.json` to `~/`-prefixed form (`root`, `workspace`, every `stages.<>.code_source.path`). Always `os.path.expanduser` before using these paths.

## Code Source Resolution

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

## File Layout

### MLClaw repo (`<wherever the MLClaw tool repo is cloned>`, e.g. `~/code/MLClaw`) — the tool

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
    train-init/SKILL.md             ← /train-init
    train-run/SKILL.md              ← /train-run
    train-tune/SKILL.md             ← /train-tune
    train-tune-report/SKILL.md      ← /train-tune-report
    refactor-init/SKILL.md          ← /refactor-init
    refactor-run/SKILL.md           ← /refactor-run
    refactor-report/SKILL.md        ← /refactor-report
    resources/SKILL.md              ← /resources
    lease/SKILL.md                  ← /lease
  settings.json
contracts/                          ← executable form of this file's contracts; stdlib only
  helpers.py                        ← temp dirs, real git repos, script loading by path
  contract_code_snapshot.py         ← the reproduction contract
  contract_metric_extraction.py     ← extraction failure must not look like absence
  contract_run_record.py            ← timestamps, run_id uniqueness, step-key correspondence
  contract_training_stream.py       ← metric schema, ckpt ranking, retention aborts
  contract_docs.py                  ← this file, README, and .claude/skills/ must agree
.github/workflows/ci.yml            ← contracts + compileall + JSON parse + no-credentials gate
lifecycle/
  references/
    run-mechanics.md                ← run step chain, contracts, record integrity (read on demand)
    layout.md                       ← this file (read on demand)
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
    train-run/
      _stream.py                    ← shared jsonl reader; infers the record-type key
      reconcile_metrics.py          ← declared metric schema vs what the stream emits
      select_checkpoint.py          ← rank ckpts, show the jsonl values behind the pick
      retention.py                  ← plan / apply; the only irreversible operation
    lease/
      lease.py                      ← acquire / renew / release a rented host
      provider_ssh.py               ← SSH-reachable provider backend
    resources/
      parse_ssh_config.py           ← parse ~/.ssh/config into server entries
    shared/
      code_snapshot.py              ← SHA + dirty patch incl. untracked; reproduction contract
      list_runs.py                  ← THE run query; mode filter cannot be omitted
      relink_sources.py             ← repair local-source symlinks after a cross-host rsync
      build_dag.py                  ← build lineage DAG from all runs
      tag_lineage.py                ← tag a run + propagate up the DAG
      workspaces.py                 ← locate the MLClaw tool repo
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

### Workspace root (`{workspace_root}`, e.g. `~/agent_space/mlclaw/projects`)

```
resources.json                      ← access credentials and resources, shared across all projects (NEVER commit)
detection/                          ← one project
another_project/                    ← another project
```

### User project (`{workspace_root}/{project_name}`, e.g. `~/agent_space/mlclaw/projects/detection`)

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
    provenance.json                 ← sidecar: source_mode, sources_checked, evidence, unresolved (training only so far)
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

## Variable Reference Syntax `${}`

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
