# The skill graph — hierarchy, edges, checks, and state

Loaded when you are about to **run** a skill: what its position in the graph is,
what must exist before it starts, what to offer when it finishes, and how its
progress survives a session boundary.

Not in CLAUDE.md because none of it is needed to decide *what to do* — that is the
skill inventory, which stays always-loaded. This is needed once a skill has been
chosen, which is exactly when a reference can be read.

| You are… | Read |
|---|---|
| entering a skill | "Skill Dependency Graph" (requires column), then "Requirement checks" |
| leaving one | "Skill Dependency Graph" (suggests column) |
| writing any record | "Node hierarchy" — which level owns it, and whether it has a step chain |
| pushing, popping, or resuming | "Workflow State Protocol" |

## Node hierarchy

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
- **Run skill** executions are fully tracked: `runs/run_20260317_.../` with run.json, steps, outputs, logs.
- **Init skill** executions are currently not tracked separately — completion is defined by output files (4 JSON configs). Can be added later.
- **Report skill** executions are stored as output files within a run's `output/` directory.
- On disk, `runs/` directory = executions of the run skill. The naming is kept for simplicity.

`/explore` **is** a stage node (`stage: exploration`) but an unusual one: it owns records and
no runs. Its arms live in `stages/training/runs/` or `stages/evaluation/runs/`, cited from a
card by `run_id` — the same layering `/train-tune` has over `/train-run`, and the same reason:
a search that runs its own trials is a second run machinery that drifts from the first. So
`stages/exploration/` has no `runs/` directory, and that absence is correct rather than missing.

**Nine skills are not stage nodes.** Where each writes and why is one lookup; full paths in `lifecycle/references/layout.md`. **All but one have no step chain** — in those cases the work happens outside any process MLClaw can step through, so the state is a `status` field, not a resumable position.

`/data-audit` is the exception, and the exception is informative: it is not a stage node (a dataset is not a stage) yet it *is* a process MLClaw runs end to end, so it has steps and resumes like a run skill. Non-stage and non-resumable are two different properties that happened to coincide until this skill existed — a census has no steps because the world has no steps, not because it sits outside `stages/`.

| Skill | Records at | Level, and why |
|---|---|---|
| `/data-label` | Send work to a party MLClaw doesn't control and verify the return against a manifest frozen at send time. The only skill whose loop is closed by someone else |
| `/data-check` | Declare a dataset's layout contract, then census it across every machine: GAP / DRIFT / UNREPLICATED / UNARCHIVED / INCOMPLETE. Reports; moves no byte. **Freezing is `/data-freeze`'s** — same script, different skill |
| `/data-curate` | Derive a new dataset from a frozen one — convert, split, dedup, relabel, sample, merge — and record what it was made of. Executes nothing: the transform is an ordinary run |
| `/data-retire` | Delete data against evidence, and leave a record that outlives it. `plan` ranks by what would **survive**; `apply` deletes only paths a census listing enumerated. The only delete on the data line |
| `/repro` | Can a past run still be reproduced, and when its number moves, which axis moved it. Five axes of rot → `intact` / `drifted` / `gone` / `unverifiable`, then a loop judging each re-measurement against a band this pipeline measured **on itself**. Executes nothing itself |
| `/data-online-sample` | A dated reading of the **live input stream** — what production was seeing between two instants — the half every drift tool fakes with an exported CSV. `/data-freeze` pins the reference side. Uniform draw always; a reading can never be retaken |
| `/eval-triage` | What a bad case *is*, and whose it is. Ranks an eval run's worst per-sample scores, judges each, routes **three verdicts to three owners**: `label_wrong` → a `/data-label` rework · `sample_hard` → the data line · `model_wrong` → **never leaves the model line**. `label_wrong` may never enter the hard-example pile |
| `/data-audit` | `datasets/<id>/audits/<audit_id>/audit.json`. Dataset-level, `stage: null`, `execution: <audit_id>` — **and it has a step chain**, unlike everything else in this table. Steps: `integrity` / `compatibility` / `consistency` / `statistics` / `schema_diff` / `ad_hoc` / `record` |
| `/data-audit-report` | `datasets/<id>/audits/<audit_id>/audit_report.html`. Beside the audit it renders, not under a run's `output/` — its subject is a dataset, and it must stay readable after every run that touched that data is gone |

## Skill Dependency Graph

Every skill knows its position in this graph. Two types of edges:

- **requires** (↑ upstream): must be done before this skill can run. If missing, pause and prompt user to do the upstream skill first.
- **suggests** (↓ downstream): after completing, offer the user the next logical step.

| Skill | Requires (check on entry) | Suggests (offer on exit) |
|---|---|---|
| `/project-init` | — (root) | `/resources`, then any `-init` |
| `/resources` | — (workspace-level utility) | return to caller |
| `/lease` | — (utility, on demand) | return to caller |
| `/ask-human` | project.json | whatever `why` said was blocked |
| `/infer-init` | project.json, code available. May call `/discover` per run — infer inputs change every run, so it has **no `candidates` by design** | `/infer-run` |
| `/infer-run` | infer-init done | (done) |
| `/eval-init` | project.json, code available. Calls `/discover` for the data; finds checkpoints itself | `/eval-run` |
| `/eval-run` | eval-init done | `/eval-report`, `/eval-triage` |
| `/eval-report` | eval-run completed | (done) |
| `/eval-triage` | eval-run completed **and** `output.json -> per_sample.path` non-null | whatever the piles named: `/data-label` (`label_wrong`), the data line (`sample_hard`), `/train-init` or a `/train-run` fork (`model_wrong`). Never `/eval-run` "to try again" |
| `/train-init` | project.json, code available. Calls `/discover` for the data half of Step 0 | `/train-run` |
| `/train-run` | train-init done. **A fine-tune also requires an evaluation stage** — the base must be measured on this run's data before launch, and that is `/eval-run`'s to perform, not this skill's. No eval stage → route to `/eval-init`, never measure it by hand here | `/eval-run`, `/train-tune` |
| `/train-tune` | train-init done, ≥1 prior train-run completed | `/train-tune-report` (auto at close) |
| `/explore` | project.json, code available, **a declared corpus** (`datasets/<id>/dataset.json` + a frozen snapshot). Calls `/eval-run` for the noise floor and `/discover` when sourcing a paper's code | `/train-run` or `/eval-run` (open an arm), then **`/train-tune` — after the architecture settles, never before**: tuning hyperparameters around a component you are about to remove spends the budget twice |
| `/train-tune-report` | a tune session with ≥1 run | (done) |
| `/evacuate` | a machine with work on it, and somewhere durable to put it (`resources.json -> aws.s3_bucket`). **Runs BEFORE `/lease release` or `pool.py release`, never after** — after is not a workflow, it is an archaeology | `/lease` (release, now safe), `/conclude` (the bundle's `logic/` layer is its output) |
| `/conclude` | ≥1 record worth citing — a completed run, a closed graph card, an audit. Nothing else: a conclusion cites, it does not produce | (done). **`/explore` routes here at close**, and so should any round that ended in a belief rather than a number |
| `/refactor-init` | project.json | `/refactor-run` |
| `/refactor-run` | refactor-init done (plan.json) | `/refactor-run` (next round), `/refactor-report` when complete |
| `/refactor-report` | refactor-run completed | (done) |
| `/discover` | project.json — *nothing declared yet, which is the point* | `/data-check` to declare + census what is verified; `/data-collect` to pull it; `/ask-human` for `gone` |
| `/data-collect` | project.json (+ resources.json for a server) | `/data-check scan` to confirm it landed, then `/data` |
| `/data-online-sample` | `dataset.json` — its `identity` is what both sides count. **Not** a frozen snapshot: that is needed to *compare* a reading, not to take one | a drift comparison against a frozen snapshot; `/data-collect --cite-window` to pull the interesting units; `/ask-human` for a `decision` on retention or vendor access |
| `/data-check` | project.json | on GAP: whatever `layers[].produced_by` names. On a clean census: `/data-freeze` |
| `/data-audit` | project.json **and** either a declared dataset or a user-named path. Step 2 additionally needs a stage with a non-empty `entry_command` — absent, that section is recorded `skipped`, never inferred | by finding, never generically: `label_wrong`-shaped → a `/data-label` rework · format defect → `/data-curate` · fatal on frozen data → `/data-freeze` for a corrected snapshot · clean → the run the user was heading for · `/data-audit-report` when there is something to look at |
| `/data-audit-report` | an `audits/*/audit.json` — including one that stopped at a Step 1 fatal, which is often the useful one | exactly where the audit routed; rendering never softens an owner |
| `/data-freeze` | a census whose `complete` is true | `/train-init` or `/train-run`; `/data` |
| `/data-curate` | a frozen snapshot of **every** parent (`datasets/<id>@<snap>` resolves) | `/data-check` (declare the output's locations, then scan), then `/data` |
| `/data-retire` | a census whose `complete` is true **and** the target location answered | `/data-check scan` — the disk changed under the census — then `/data` |
| `/data` | project.json + ≥1 `dataset.json` | `next_skill`, or the blocker's owner — **never `/data-retire`** |
| `/data-report` | project.json + ≥1 `dataset.json` | whatever the blocker table names |
| `/repro` | a completed run with non-null `metrics.best.primary_metric_value` **and** non-null `mode`; the re-measuring stage initialized (usually evaluation) | whatever the attributed axis owns (`/data-freeze`, an env rebuild, an edit at `param_injection`'s evidence); `/eval-report` when the probe was an eval set. Never a stage's `-run` "to try again" |

**Three rows suggest the owner of a finding, never a retry** — `/repro`, `/eval-triage`, and any
`-run` that failed. A skill offering "run it again" as its exit turns a measurement into a slot
machine, and on `diverged_unattributed` `/repro` suggests nothing at all beyond widening the band:
when no recorded axis explains a difference, proposing a fix would be inventing one.

**`/data-curate` and `/data-retire` both suggest `/data-check`, neither suggests a stage.** A fresh
derived dataset has no census and a retirement invalidates the one that justified it — the next
honest step is to go look, not to train.

## How skills use this graph

**On entry** — check `requires` column. If a requirement is not met, offer to run the upstream skill. If user agrees, invoke it as a sub-skill (see Workflow State Protocol below). If user declines, stop.

**On exit** — check `suggests` column. Offer the next skill. If user accepts, invoke it as a sub-skill.

**`/resources`**, **`/lease`** and **`/discover`** are utility skills — called on-demand, invoked standalone too, and none appears in a stage's dependency chain; they interrupt one and return. But they are three different jobs and the first two used to be described as one:

| Skill | Job |
|---|---|
| **`/discover`** | **goes and looks.** The sweep: leads, probes, four verdicts, and the access worklist that falls out of every `unreachable` naming what it was missing |
| **`/resources`** | **keeps the registry.** `resources.json` is the declaration of what is configured and usable, which every run skill reads through `${}`. Verified sweep results are written *into* it |
| **`/lease`** | **acquires.** A machine that does not exist yet, with a dead-man switch |

The first two split the way `census.py` and `dataset.json` do — one is a dated observation that may be partial, the other is the durable contract — and for the same reason: a missing credential is not the substrate beneath discovery, it is discovery's most common **finding**.

Two reasons the sweep is not `/train-init`'s, and be precise about the first: **`/train-init` was the only skill that ever owned one**, so nothing was de-duplicated — what it recorded was a *plan to copy it into `/eval-init` later*, and extracting it **prevented the fork rather than repairing one**. A second copy drifts, so the copy never happened. The binding reason is the second, and it holds however many copies exist: **a lead outlives an init.** Access arrives weeks after a handover starts, and `discovery/leads.json` carries the unresolved ones forward where a `provenance.json` written once and declared done cannot. So the data half moved out; the model half (weights, params, tracking, compute, hazards) stayed.

**`/data-label`** is a utility skill too, but the asymmetric kind: it is entered from a stage and returns *weeks later*, so it must not hold the workflow stack while it waits. Its `suggests` edge fires at `close --accept`, not at `send`. A run that consumes an accepted handoff cites it in `lineage.parents` as `handoffs/<handoff_id>` — that citation, not the stack, is what connects the two.

## Requirement checks

| Requirement | How to check |
|-------------|-------------|
| project.json exists | file exists at `{PROJECT}/project.json` |
| code available | `code_source` is configured AND code directory has files — **or** `code_source.source` is `framework`, where an empty code directory is the correct state and the pinned spec in `code_source.path` is the code. Do not offer `git init` on that branch: there is no tree, and `code_snapshot.py --framework` is the call. See `layout.md` → "Code Source Resolution" |
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
| a corpus is declared for `/explore` | `{PROJECT}/stages/exploration/graph.json -> corpus.dataset_id` non-empty **and** that dataset has a frozen snapshot. Without it every `premise_share` is unscoped, and an unscoped share is what queued five arms against a fault that did not exist |
| a machine may be released | `evacuate.py clearance --project {PROJECT} --host {HOST}` exits 0. **Exit 1 means do not release it** — the disk goes with the lease, and the verdict names what is still on it. `pool.py release --artifacts recovered` enforces the same thing one layer down by requiring the record |
| the conclusions are intact | `conclude.py check --project {PROJECT}` exits 0. **Exit 1 is a refusal, not a breakage** — the same fallback exception applies. The finding to look for first is a `status` recorded as `supported` against evidence that no longer resolves; re-deriving it by hand is overriding the check, not falling back to it |
| the explore graph is intact | `graph.py check --project {PROJECT}` exits 0. **Exit 1 is a refusal, not a breakage** — do not open another arm, and do not fall back to doing it by hand: the fallback rule's exception applies (CLAUDE.md -> "Script Integration") |
| dataset declared | `{PROJECT}/datasets/<id>/dataset.json` exists with non-empty `identity.unit_glob`, `layers`, `locations` |
| census usable for a decision | `datasets/<id>/census/*.json` exists **and** its `complete` is true. `complete: false` means a location didn't answer, so every count is a lower bound — fine to report, not fine to rank, freeze or delete on |
| an online contract is declared | `datasets/<id>/dataset.json → online` is non-empty. Nothing else says where this dataset's live counterpart arrives, and a guessed production layout yields a reading of a directory nobody serves from |
| a reading is comparable | `datasets/<id>/online/window_*.json` exists, `complete` is true, `policy` is `uniform`. A drift verdict against a window missing a day is a verdict about the outage. Report such a reading; never compare it |
| a rate off production is exact rather than a floor | that reading's `population_basis` is `declared`. `enumeration_only` means nothing counted what production actually handled, so `sample_rate` is null and every derived rate is a lower bound that must be said as one |
| a frozen parent exists | `datasets/<id>/snapshots/<sid>/snapshot.json` exists for the id being cited. A bare dataset id never satisfies this — a dataset grows, and a parent edge that cannot say which afternoon is not lineage |
| a derivation is checked, not claimed | `dataset.json → derived_from.provenance` is `"run"`, and that run has `status: "completed"` and cites the same parents. `"claimed"` is a legitimate record and is **not** this |
| a run is reproducible at all | `repro.py check`'s `overall` is not `not_reproducible`. That verdict means an axis is `gone` and **no amount of relaunching gets past it**. A `data_retired` stamp alone is *not* this — it names one location, so until a census taken since says whether a copy survived the honest verdict is `unverifiable`. `reproducible_with_drift` and `reproducible_unverifiably` both satisfy this and both cap the verdict below `reproduced` |
| a number is verified rather than claimed | a **closed** `repro/*/session.json` whose `verdict` is `reproduced` or `reproduced_with_drift` — **not `remeasured*`**, which asserts only that an artifact still scores this and leaves the recipe unexercised. The only thing that moves an inherited checkpoint's `origin.confidence` off `claimed`; a session left open is not it |
| a delta was judged against measured noise | that session's `band` is non-null with `n >= 3`. A verdict resting on the declared tolerance instead is the guess `/repro` exists to replace |
| bad cases can be named at all | `stages/evaluation/output.json → per_sample.path` is non-null **and** `score.field` + `score.direction` are both set. A declared path with no `direction` does not satisfy this: sorting the wrong end reviews the model's *best* predictions as its worst, and nothing errors |
| a bad case was judged rather than guessed | its `provenance` in `triage/*/session.json → cases` is `claim` or `verified`, never `unreviewed` or `disputed`. `verified` means two **different kinds** of source agreed — two agent passes over one image are one source sampled twice, so they stay a `claim` |
| an audit exists | `datasets/<id>/audits/*/audit.json` exists. **A clean census does not satisfy this** — the census never opened a file, so it has no opinion on what is inside one |
| an audit is decidable | that audit's fatal sections (`integrity`, `compatibility`) are `passed`, not `skipped`. `skipped` means the check did not run — most often because no consuming stage was named — and a `skipped` compatibility section is exactly the state that looks like a pass in every summary |

## Workflow State Protocol

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
