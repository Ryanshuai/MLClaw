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
- **JSON configs are the source of truth**: fixed keys, you fill values. Templates at `lifecycle/` top level and `lifecycle/<stage>/`, filled instances in user projects. `lifecycle/scripts/` and `lifecycle/references/` are the tool's own machinery and docs — they live under `lifecycle/` because they define the same model, but nothing copies them into a project.
- **Confirm before saving**: always show what you're about to write, wait for user confirmation. Never auto-overwrite existing values.

## Never silently

Ten rules that outrank convenience. They are here, in the always-loaded file, because the moment they matter most is when no skill is running — a user says "clean up the old checkpoints", "which run was best", or "the labels came back, load them in", nothing loads, and an obliging agent does the wrong thing without anything raising. Mechanism and rationale: `lifecycle/references/run-mechanics.md -> "Record integrity"`.

- **Never delete a checkpoint outside `retention.py plan` → `apply`.** Showing the user a list of filenames is not confirmation — the list carries no evidence the ranking behind it was right. Never delete a file you cannot rank.
- **Never record a metric you did not read.** Extraction failure and "the run never produced it" are different facts and must not both become `null`.
- **Never compare metrics across different `mode` or non-equivalent `scope`.** Query through `lifecycle/scripts/shared/list_runs.py`; do not hand-write the filter.
- **Never pass a param the code ignores.** Check `config.json -> param_injection` first; a silently-dropped flag produces a run whose recorded config lies about what trained.
- **Never treat an untracked file as clean.** `git diff HEAD` cannot see it, and the run will reproduce different code.
- **Never let somebody's word become a checked fact.** An answer is a `claim` until something other than the person confirms it; `/ask-human` refuses `verified` when nothing did. "The operator says it finished" and "the census counted 52 finished units" stop being distinguishable the moment either is written down as "done".
- **Never accept a claimed return as a verified one.** "It's labeled" is a claim; completeness is `handoff.py receive` computing it against the frozen manifest. Copying the files in because someone said they were done records a partial batch as a whole one, and every downstream number inherits that.
- **Never report data you could not look at.** A machine that did not answer, a path that is not there, and a directory that is genuinely empty are three facts, and only the last one means the data is gone. `census.py scan` keeps them apart and marks the census `complete: false`; a count from a partial census is a lower bound and must be said as one. Presenting it as an inventory is how a backup nobody can reach passes for a backup.
- **Never say a unit is complete because its directory exists.** Completion is the marker named in `dataset.json -> completeness`, written when the work ended. Directory-up-front is a normal and often correct capture design, which is exactly why its existence proves nothing — and a half-finished unit that reads as whole is the one defect that survives all the way into a trained model. No marker declared means every unit is `unverifiable`, never `complete`.
- **Never delete data a frozen snapshot still names.** The bytes go; the citation stays. `datasets/boxes@260731` goes on resolving, the manifest goes on listing the unit, and every run that cited it goes on reading as reproducible while it no longer is — nothing anywhere raises. `retire.py plan` is what knows, because it is the only thing that reads the manifests and the census together; a delete outside it cannot know. When it has to happen anyway, `--waive cited_by_snapshot` stamps the loss into the snapshot: a citation that resolves to nothing must at least say so.

## Reading order

**This file carries only what is needed before a skill is chosen**: the rules that
fire when nothing is loaded, the inventory you route from, and what to check when a
session opens. Everything else is one hop away, and the hop is cheap because by then
you know which one you need.

That split is the whole reason the rules below get read at all. A file that also
carried every requirement check and record layout would be skimmed, and the ten
rules would be skimmed with it.

| Before you… | Read |
|---|---|
| **run any skill** — check its requirements, offer the next one, push/pop state, or write any record | `lifecycle/references/skill-graph.md` — node hierarchy, the requires/suggests table, requirement checks, workflow state protocol |
| execute or finalize a run (`/train-run`, `/eval-run`, `/infer-run`, `/refactor-run`) | `lifecycle/references/run-mechanics.md` — step chain, launch contract, asset resolution, code snapshot, record integrity, checkpoint selection, retention, listing runs, path mapping |
| find code/data on disk, resolve a `${}` reference, or create a project | `lifecycle/references/layout.md` — workspace and tool-repo location, code-source resolution, full file layout, dataset identity and census records, `${}` syntax |
| work the data line — freeze, curate, retire, or route a dataset | `lifecycle/references/data-line.md` — what each phase owns, what is not a phase, how `/data` composes them |
| pick up something designed but not built | `lifecycle/references/roadmap.md` — and note that nothing in it has a script to call |
| change a script, a template, or a contract | `contracts/` and "Contracts" below |

## Status

**Implemented**: inference (init + run), evaluation (init + run + report + triage), refactor (init + run + report), training (init + run + tune + tune-report), project init, resources, lease, handoff, reproduction, and the whole data line (collect + label + curate + freeze + retire, plus check / route / report / online-sample).

**Next**, in dependency order: `models/<id>@<release>` (the model-identity primitive three things wait on), then deployment (`/deploy-init` + `/deploy-run`) and model curate, plus `/data-drift`'s comparison half — its online half is built. Then exploration and `/train-compare`. **Designed, not built: there is no script to call.** Reasoning, and the traps that make the obvious implementation wrong: `lifecycle/references/roadmap.md`.

**The data lifecycle**, one skill per phase — including the small ones, because a phase whose skill is "part of another skill" is a phase nobody can name, and an unnamed box reads as a box that does not exist:

```
   Collect   →   Label    →   Curate    →   Freeze    →   Retire
/data-collect  /data-label  /data-curate  /data-freeze  /data-retire

        /data  composes them · /data-check censuses · /data-report renders
   /data-online-sample reads the live stream the frozen side gets compared against
```

It is a **record** layer end to end, with one exception: `/data-retire apply` deletes,
earned by `plan → apply` against evidence plus the containment rule. Which phase owns
what, what is deliberately *not* a phase (Archive, Train), and how `/data` composes it:
`lifecycle/references/data-line.md`. Every rule there is cited by a check.

## Skills & Dependencies

| Skill | What it does |
|-------|-------------|
| `/project-init` | Create a new project: directory structure, project.json, git init |
| `/infer-init` | Analyze inference code → fill 4 JSON configs (config, artifacts, input, output) |
| `/infer-run` | Run inference: check sources → debug mode → production mode (local/remote) → collect results |
| `/eval-init` | Analyze evaluation code → 4 JSON configs + **`candidates`**. Calls `/discover` for the data; finds checkpoints itself from this project's production training runs. Gate: **`samples` ≠ `dataset.num_samples` is `mismatch`** — a subset is a different measurement |
| `/eval-run` | Run evaluation: check sources + GT → debug mode → production mode → collect metrics → baseline comparison |
| `/eval-report` | Self-contained HTML report from a completed eval run. **Not bad cases** — rendering the worst 40 images is not analysis; that is `/eval-triage`'s |
| `/eval-triage` | What a bad case *is*, and whose it is. Ranks an eval run's worst per-sample scores, judges each, routes **three verdicts to three owners**: `label_wrong` → a `/data-label` rework · `sample_hard` → the data line · `model_wrong` → **never leaves the model line**. `label_wrong` may never enter the hard-example pile |
| `/train-init` | Analyze training code → sweep every reachable source (repo, git history, docs, S3, tracking, compute) → 4 JSON configs + `provenance.json` (what was checked, what's guessed) + `recipe.md` (handover doc) |
| `/train-run` | Run training: validate resources → resolve sources → background launch → monitor stream (heartbeat, last_step, latest_metrics) → detect done/crash → finalize (best ckpt + retention) |
| `/train-tune` | Adaptive HPO loop: observe prior runs → find coverage gaps → hypothesize the next config → launch trials via `/train-run` → iterate until budget or convergence |
| `/train-tune-report` | Render a tune session as `chain.md`: headline, best-so-far curve, coverage map, decision timeline, confirmed/refuted distillation, recipe |
| `/lease` | Acquire, renew and release a rented compute host, with a dead-man switch so nothing is left running. Also answers "what am I paying for right now" |
| `/data-label` | Send work to a party MLClaw doesn't control and verify the return against a manifest frozen at send time. The only skill whose loop is closed by someone else |
| `/ask-human` | Put a question to a person and record the answer as what it is — `claim` / `verified` / `decision`. Sibling of `/data-label`: that one exchanges artifacts, this one exchanges answers |
| `/discover` | Find out what exists when nobody can tell you — the taking-over case. Sweeps code, git history, tracking backends, S3, servers, docs, people and probes each lead: `claim` / `verified` / **`gone`** (looked, not there) / **`unreachable`** (could not look). Data, weights, somebody's recorded results, and the credentials the other probes turned out to need — one engine, one lead register, because a missing key and the runs behind it are **one fact**. `reconcile` joins the leads against a stage's `candidates` both ways (drift + a need nothing is searching for); `--access-expires-at` records a source that stops being reachable on a known date. Never declares a dataset — that is `/data-check` |
| `/data-collect` | Name a resource, name a path on it, pull — and record what arrived. Ingest only, one direction. Never waits for a human — that is `/data-label` |
| `/data-online-sample` | A dated reading of the **live input stream** — what production was seeing between two instants — the half every drift tool fakes with an exported CSV. `/data-freeze` pins the reference side. Uniform draw always; a reading can never be retaken |
| `/data-check` | Declare a dataset's layout contract, then census it across every machine: GAP / DRIFT / UNREPLICATED / UNARCHIVED / INCOMPLETE. Reports; moves no byte. **Freezing is `/data-freeze`'s** — same script, different skill |
| `/data-curate` | Derive a new dataset from a frozen one — convert, split, dedup, relabel, sample, merge — and record what it was made of. Executes nothing: the transform is an ordinary run |
| `/data-freeze` | Freeze a citable snapshot so a run can record exactly which units it consumed — `datasets/<id>@<snapshot>`, the boundary the model lifecycle cites |
| `/data-report` | The whole line as one self-contained HTML board, Airflow-grid shaped: datasets × censuses, each column that census **replayed**. Computes nothing; no auto-refresh on purpose |
| `/data-retire` | Delete data against evidence, and leave a record that outlives it. `plan` ranks by what would **survive**; `apply` deletes only paths a census listing enumerated. The only delete on the data line |
| `/data` | Where a dataset sits on the line, what blocks it, what is next — a join across census + handoffs + snapshots + runs that no single skill can perform. Refuses transitions whose preconditions fail |
| `/repro` | Can a past run still be reproduced, and when its number moves, which axis moved it. Five axes of rot → `intact` / `drifted` / `gone` / `unverifiable`, then a loop judging each re-measurement against a band this pipeline measured **on itself**. Executes nothing itself |
| `/refactor-init` | Clone research repo, analyze codebase, classify modules (core/support/dead), extract paper benchmark targets |
| `/refactor-run` | Execute one refactoring round: make changes → run benchmark → compare with paper → commit or revert |
| `/refactor-report` | Generate refactoring audit report: round changelog, rollback points, verification results, reproduction instructions |
| `/resources` | Keeper of `resources.json` — the workspace-level **registry** of what is configured and usable, which every run skill reads through `${}`. **Finding** things is `/discover`'s; this is the declaration the sweep's verified results are written into. Same split as `census.py` (goes and looks, dated, may be partial) versus `dataset.json` (the durable contract) |

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

3. **Check what is out with someone else**: `handoff.py status --project {PROJECT} --open-only` **and** `ask.py status --project {PROJECT} --open-only` (stale at 7 days, not 14 — a question someone could answer in a minute going unanswered for a week is a worse signal than a labeling batch taking three weeks). Report anything `overdue`, or `stale` (open ≥ 14 days). Skip silently when there are none, and when the project has no `handoffs/` at all.
   - This is the only one of the three that gets *worse* by being missed. A stalled run wastes a GPU; a batch nobody chased is three weeks of calendar time that never comes back, and by construction there is no process to notice — the work is happening on somebody else's desk.
   - Report and offer to draft a chase message. Never send anything to an external party unprompted.

4. **Check how stale the data picture is**: `python lifecycle/scripts/data-check/census.py status --project {PROJECT}`. Records only — this verb never touches the network, which is what makes it safe here. Skip silently when the project has no `datasets/`.
   - Report the census **age** and any standing `unarchived` / `unreplicated` / `incomplete` counts, then offer to re-scan. Do not re-scan unprompted: `scan` goes out and asks every machine, and four ssh timeouts before the user's first sentence is not a greeting.
   - **State the age before quoting any count.** A three-week-old census is a description of a disk that has since been written to, rolled, and possibly filled — quoting its numbers as current is the same error as reading a stale metric off an old run.
   - `unarchived` is the entry that gets worse by being missed, and worse than a stalled handoff: a capture machine reclaiming space to keep shooting cannot know whether the day it is about to delete was ever copied off. Nothing on that machine can compute it. This check is the only thing standing there.
   - **In the same pass, if `{PROJECT}/discovery/` exists**: `discover.py report --project {PROJECT}` (records only, no network). Report `unprobed_leads` and anything `unreachable` — **not the findings.** `/discover`'s entire premise is that access arrives weeks after responsibility does, and that only helps if something re-opens the file. Without this line `leads.json` becomes the Confluence page it was written to replace: accurate the day it was made, never looked at again. A lead that has been `unreachable` for three weeks usually means a credential arrived and nobody re-probed — offer that, don't do it unprompted.
     - **Report `access_expiring_soon` and `access_expired_and_unresolved` first, ahead of both.** This is the only entry on the whole conversation-start list with a *deadline* rather than a staleness, and it is the one nothing else can recover: an unarchived day can still be copied tomorrow, a stale census can be re-scanned, an unprobed lead keeps waiting. A key that rotates on the 14th, or a departing colleague's account, takes the history with it on a date somebody already knows — and `access_expired_and_unresolved` means that date passed while the lead was still `claim` or `unreachable`, so the loss is now permanent and no probe will ever say `gone`. Say the days remaining, and say which lead.

Steps 1, 3 and 4 all scan the project; do them in one pass. `lease.py reap` belongs here as a fifth (cloud-side orphan check, gated on a provider being registered) and is **still not wired** — see `/lease` "The human's window".

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

**What is enforced**: the record layer only — `lifecycle/references/run-mechanics.md` "Record integrity". Everything else in this file is a contract nothing checks; when you touch one of those, you are the check.

`contracts/` holds the executable form of the contracts stated in this file. Not unit tests — the distinction is load-bearing, so keep the vocabulary:

```
python -m unittest discover -s contracts -t contracts -p 'contract_*.py'
```

The `-p` is required; the default pattern is `test*.py` and would silently find nothing. Stdlib only, no dependencies, no virtualenv — that is why CI needs no install step. Scripts live in hyphenated directories that aren't importable package names, so `contracts/helpers.py` loads them by path and provides temp-dir and real-git-repo fixtures.

**Every check class cites the section it enforces**, in its docstring. That citation is the admission rule: a check that cannot point at a written contract is either a missing line in this file or padding — decide which, don't leave it. It is also how you find out what is *not* enforced: grep the citations, diff against this file's sections.

**When a check fails, the first question is "is the contract still right?", not "how do I make this pass."** If the contract changed, edit this file and let the check follow — the contract is upstream. That is the intended workflow, not a defeat. A check whose failure doesn't tell you which side to change is itself a liability; delete it.

**What earns a check**: a record written now and read later by someone who can no longer verify it, or an irreversible action. That is the whole bar — see `lifecycle/references/run-mechanics.md` "Record integrity". Most of this file is *not* enforceable (one question at a time, never guess a value, confirm before saving). **A green run means the record layer is intact. It does not mean MLClaw is correct.**
