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

## Never silently

Six rules that outrank convenience. They are here, in the always-loaded file, because the moment they matter most is when no skill is running — a user says "clean up the old checkpoints", "which run was best", or "标注回来了导进去吧", nothing loads, and an obliging agent does the wrong thing without anything raising. Mechanism and rationale: `lifecycle/references/run-mechanics.md -> "Record integrity"`.

- **Never delete a checkpoint outside `retention.py plan` → `apply`.** Showing the user a list of filenames is not confirmation — the list carries no evidence the ranking behind it was right. Never delete a file you cannot rank.
- **Never record a metric you did not read.** Extraction failure and "the run never produced it" are different facts and must not both become `null`.
- **Never compare metrics across different `mode` or non-equivalent `scope`.** Query through `lifecycle/scripts/shared/list_runs.py`; do not hand-write the filter.
- **Never pass a param the code ignores.** Check `config.json -> param_injection` first; a silently-dropped flag produces a run whose recorded config lies about what trained.
- **Never treat an untracked file as clean.** `git diff HEAD` cannot see it, and the run will reproduce different code.
- **Never accept a claimed return as a verified one.** "标好了" is a claim; completeness is `handoff.py receive` computing it against the frozen manifest. Copying the files in because someone said they were done records a partial batch as a whole one, and every downstream number inherits that.

## Reading order

This file is routing and constraint only. Detail is loaded when it applies, so that what stays here actually gets read.

| Before you… | Read |
|---|---|
| execute or finalize any run (`/train-run`, `/eval-run`, `/infer-run`, `/refactor-run`) | `lifecycle/references/run-mechanics.md` — step chain, launch contract, code snapshot, record integrity, checkpoint selection, retention, listing runs, env resolution, remote path mapping |
| find code/data on disk, resolve a `${}` reference, or create a project | `lifecycle/references/layout.md` — workspace and tool-repo location, code-source resolution, full file layout, `${}` syntax |
| change a script, a template, or a contract | `contracts/` and "Contracts" below |

## Status

**Implemented**: inference (init + run), evaluation (init + run + report), refactor (init + run + report), training (init + run + tune + tune-report), project init, resources, lease, handoff.

**Next**: data stage, deployment stage, exploration stage (architecture search), `/train-compare`.

**Enforced by `contracts/`**: the record layer only — see `lifecycle/references/run-mechanics.md` "Record integrity". Everything else in this file is a contract nothing checks; when you touch one of those, you are the check.

## Skills & Dependencies

| Skill | What it does |
|-------|-------------|
| `/project-init` | Create a new project: directory structure, project.json, git init |
| `/infer-init` | Analyze inference code → fill 4 JSON configs (config, artifacts, input, output) |
| `/infer-run` | Run inference: check sources → debug mode → production mode (local/remote) → collect results |
| `/eval-init` | Analyze evaluation code → fill 4 JSON configs (config with dataset info, artifacts, input with ground truth, output with baseline) |
| `/eval-run` | Run evaluation: check sources + GT → debug mode → production mode → collect metrics → baseline comparison |
| `/eval-report` | Generate self-contained HTML report from a completed eval run (metrics, baseline diff, per-class, bad cases) |
| `/train-init` | Analyze training code → sweep every reachable source (repo, git history, company docs, S3, tracking backend, compute) → fill 4 JSON configs (resources + param injection + hazards + env snapshot; data/weight candidates + preprocessing contract; streaming-metric schema + checkpoint selection + done signal + tracking locator) + `provenance.json` (sources checked, evidence, what's guessed) + `recipe.md` (handover doc) |
| `/train-run` | Run training: validate resources → resolve sources → background launch → monitor stream (heartbeat, last_step, latest_metrics) → detect done/crash → finalize (best ckpt + retention) |
| `/train-tune` | Adaptive HPO loop: agent observes prior runs → identifies coverage gaps → hypothesizes next config → launches trials via /train-run → iterates until budget or convergence. Auto-invokes /train-tune-report at close. |
| `/train-tune-report` | Render a tune session as markdown chain.md: headline, best-so-far curve, coverage map, decision timeline (with [fill_grid|refine_best|add_axis|verify] tags), confirmed/refuted distillation, recipe. |
| `/lease` | Acquire, renew, and release a rented compute host, with a dead-man switch so nothing is left running. Called by a run skill that needs a machine it doesn't have; also answers the user's own "what am I paying for right now" |
| `/handoff` | Send work to a party MLClaw doesn't control (annotation vendor, data owner, reviewer, customer) and verify the return against a manifest frozen at send time: freeze + spec snapshot → track what's outstanding → reconcile (coverage, named gaps, source drift) → accept/reject/rework. The only skill whose loop is closed by someone else |
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
                 └── steps (resource_validation, resolve_assets, create_run, execute,
                            monitor, collect_results)
```

- **Stage** is a lifecycle phase. **Skill** is an operation on that stage. **Execution** is one instantiation of running a skill.
- `/project-init` is project-level, not tied to a stage. `/resources` is workspace-level — `resources.json` lives at the workspace root and is shared across all projects.
- `/handoff` is project-level and **cross-stage on purpose**: it is an edge, not a node. Annotation hangs off data, human eval off evaluation, acceptance off delivery — one primitive, many stages, so its records live at `{PROJECT}/handoffs/` rather than under any one `stages/`. Its executions are tracked (`handoffs/handoff_20260731_.../handoff.json` + per-round reconciliations) but have no step chain: the state is `status`, because the work happens outside any process MLClaw can step through.
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
/lease             (none — utility, called on demand)     (return to caller)
/handoff           project.json exists                    on accept: the consuming stage's
                                                          -init or -run; on reject: rework round
```

`/data-check` and `/data-report` are **not built** — the data stage is in "Next" above. They are named here only so nobody re-invents the edge: they would sit between `project.json` and any `/{stage}-run`.

#### How skills use this graph

**On entry** — check `requires` column. If a requirement is not met, offer to run the upstream skill. If user agrees, invoke it as a sub-skill (see Workflow State Protocol below). If user declines, stop.

**On exit** — check `suggests` column. Offer the next skill. If user accepts, invoke it as a sub-skill.

**`/resources`** and **`/lease`** are utility skills — called on-demand by any run skill, `/resources` when credentials are missing, `/lease` when the run needs a machine that isn't already available. Both can be invoked standalone. Neither appears in a stage's dependency chain; they interrupt one and return.

**`/handoff`** is a utility skill too, but the asymmetric kind: it is entered from a stage and returns *weeks later*, so it must not hold the workflow stack while it waits. Its `suggests` edge fires at `close --accept`, not at `send`. A run that consumes an accepted handoff cites it in `lineage.parents` as `handoffs/<handoff_id>` — that citation, not the stack, is what connects the two.

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

**The fallback rule has one exception, and it is the important one.** A script that *refuses* is not a script that *failed*. When `code_snapshot.py` refuses a non-git tree, `retention.py` refuses a plan because a deletion target has no metric, or `reconcile_metrics.py` returns `fail` because the declared metric is absent from the stream — that is the answer, arrived at correctly. Redoing the work by hand there means overriding a safety check, which is the opposite of a fallback. Distinguish the two by exit code: **2 = the script broke, fall back and do it manually; 1 = the script worked and the answer is no.**

### Contracts

`contracts/` holds the executable form of the contracts stated in this file. Not unit tests — the distinction is load-bearing, so keep the vocabulary:

```
python -m unittest discover -s contracts -t contracts -p 'contract_*.py'
```

The `-p` is required; the default pattern is `test*.py` and would silently find nothing. Stdlib only, no dependencies, no virtualenv — that is why CI needs no install step. Scripts live in hyphenated directories that aren't importable package names, so `contracts/helpers.py` loads them by path and provides temp-dir and real-git-repo fixtures.

**Every check class cites the section it enforces**, in its docstring. That citation is the admission rule: a check that cannot point at a written contract is either a missing line in this file or padding — decide which, don't leave it. It is also how you find out what is *not* enforced: grep the citations, diff against this file's sections.

**When a check fails, the first question is "is the contract still right?", not "how do I make this pass."** If the contract changed, edit this file and let the check follow — the contract is upstream. That is the intended workflow, not a defeat. A check whose failure doesn't tell you which side to change is itself a liability; delete it.

**What earns a check**: a record written now and read later by someone who can no longer verify it, or an irreversible action. That is the whole bar — see `lifecycle/references/run-mechanics.md` "Record integrity". Most of this file is *not* enforceable (one question at a time, never guess a value, confirm before saving). **A green run means the record layer is intact. It does not mean MLClaw is correct.**
