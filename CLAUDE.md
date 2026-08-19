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
- **JSON configs are the source of truth**: fixed keys, you fill values. Templates at `lifecycle/` top level and `lifecycle/<stage>/`, filled instances in user projects. **`lifecycle/` is now only that** — the set `/project-init` copies. The tool's own halves sit at the repo root beside `skills/`, where the plugin spec puts them: `scripts/` ("Helper scripts and utilities") and `references/` ("documentation intended to be loaded into context as needed"). Nothing at the root is copied into a project.
- **Confirm before saving**: always show what you're about to write, wait for user confirmation. Never auto-overwrite existing values. **This is about the record, not the work that produces it** — extracting an archive to read it, taking a listing, installing the pinned package are not saves.
- **Decide what evidence can decide.** The two rules above tell you how to ask and say nothing about what is yours to settle, so every gap defaults to a question and the skills read as needing permission for a `pip install`. They do not. Three buckets, and the middle one is the only one that goes to the user:

  | | |
  |---|---|
  | **Just do it** | anything recoverable and inward-facing whose answer is *readable*: install the version `code_source.path` pins, extract the archive, take the listing, walk the tree, open the checkpoint, create the directory, re-probe a lead. **A value you can read is never a question.** Say what you did and what it showed |
  | **Ask** | a value only the user has — which metric is primary, whether a delta matters enough to act on, what a label means, which of two genuinely-forking routes to take. And **every irreversible or outward-facing action**: a delete, anything sent to an external party, a push, money |
  | **Neither** | a value *nobody* has. Record it absent — `null`, `claim`, `unverifiable`, `not measured` — and never ask a person for a fact so that it can be written down as one. That is what the four statuses are for, and "Never let somebody's word become a checked fact" is this bucket seen from the record's side |

  **Asking has a cost nothing else in this file names.** Every unnecessary question spends the user's attention, and their attention is what decides whether the ten rules below get read on the day they matter. A skill that stops to ask about a package install has taught its reader to skim — and the next thing skimmed is a checkpoint deletion.

- **File the question; do not block on it.** The `Ask` bucket says whose answer it is, not that you stop and wait for it. **This runs unattended on a server**, where a question is not expensive — it is a *deadlock*: nothing answers, and the skill sits at question 3 of 9 having produced nothing. Every skill here is written as an interview, and an interview cannot run with nobody in the room. So:

  1. do everything that does **not** depend on the answer, first;
  2. `ask.py open --to <who> --asked "<verbatim>" --why "<what is blocked>"` — the question becomes a record that outlives the session and is surfaced at every conversation start, stale at 7 days;
  3. mark **at the field** what is outstanding, so the config says which values are open rather than being silently half-written;
  4. carry on.

  `--verify` is the bucket rule one level down: *a command that could answer this without a person*. If you can write one, run it instead of filing — and its absence is exactly why `/ask-human` refuses to call an answer `verified`.

  Block only where proceeding under any assumption would be unsafe or would make the work useless if wrong. **A halted interview and a finished record with three open asks look nothing alike to whoever picks this up**: the first is nothing, the second is most of the work plus a worklist.

## Never silently

**One of these is now mechanized.** `hooks/guard_destructive.py` is a `PreToolUse` refusal on
`Bash`: it blocks a delete aimed at a checkpoint, at frozen data, at a run record or at a
`knowledge/` file, and names the `plan` → `apply` script that owns it. It is a hook and not a
skill's `allowed-tools` for the reason this whole section exists — **`allowed-tools` binds only
while a skill runs, and these rules are for the moment when none does.** It blocks rather than
warns, on `identity.md` build step 8's line: non-blocking is right for a capture operator who
cannot re-shoot the frames, and wrong for a script whose next statement is `rm -rf` over ssh.
The other rules below are judgment, and nothing can mechanize them.

Ten rules that outrank convenience. They are here, in the always-loaded file, because the moment they matter most is when no skill is running — a user says "clean up the old checkpoints", "which run was best", or "the labels came back, load them in", nothing loads, and an obliging agent does the wrong thing without anything raising. Mechanism and rationale: `references/run-mechanics.md -> "Record integrity"`.

- **Never delete a checkpoint outside `retention.py plan` → `apply`.** Showing the user a list of filenames is not confirmation — the list carries no evidence the ranking behind it was right. Never delete a file you cannot rank.
- **Never record a metric you did not read.** Extraction failure and "the run never produced it" are different facts and must not both become `null`.
- **Never compare metrics across different `mode` or non-equivalent `scope`.** Query through `scripts/shared/list_runs.py`; do not hand-write the filter.
- **Never pass a param the code ignores.** Check `config.json -> param_injection` first; a silently-dropped flag produces a run whose recorded config lies about what trained.
- **Never treat an untracked file as clean.** `git diff HEAD` cannot see it, and the run will reproduce different code.
- **Never let somebody's word become a checked fact.** An answer is a `claim` until something other than the person confirms it; `/ask-human` refuses `verified` when nothing did. "The operator says it finished" and "the census counted 52 finished units" stop being distinguishable the moment either is written down as "done".
- **Never accept a claimed return as a verified one.** "It's labeled" is a claim; completeness is `handoff.py receive` computing it against the frozen manifest. Copying the files in because someone said they were done records a partial batch as a whole one, and every downstream number inherits that.
- **Never report data you could not look at.** A machine that did not answer, a path that is not there, and a directory that is genuinely empty are three facts, and only the last one means the data is gone. `census.py scan` keeps them apart and marks the census `complete: false`; a count from a partial census is a lower bound and must be said as one. Presenting it as an inventory is how a backup nobody can reach passes for a backup.
- **Never say a unit is complete because its directory exists.** Completion is the marker named in `dataset.json -> completeness`, written when the work ended. Directory-up-front is a normal and often correct capture design, which is exactly why its existence proves nothing — and a half-finished unit that reads as whole is the one defect that survives all the way into a trained model. No marker declared means every unit is `unverifiable`, never `complete`.
- **Never let a share measured somewhere else stand for this corpus.** A fault's share is a property of the corpus, not of the fault — and the number arrives sounding like the fault's ("this affects 47% of frames"). Measured on the corpus a round actually used, that one was 4.62%: an order of magnitude, five arms already queued, and the fault did not exist. This is `Never compare metrics across different mode or scope` one level down — that rule stops two numbers being subtracted, this one stops a proposal existing — and `graph.py` enforces it by treating an out-of-corpus share as **absent** rather than as weak evidence.
- **Never release a machine you did not evacuate.** This is the only place where *doing nothing* is itself the destructive act: the lease ends, the disk goes with it, and whatever was not pulled off is gone with no `rm` in the log. The recorded failure is a checkpoint that came back half-way — a `.pth` with a plausible name that no longer loads, beside a release that reported success, because `os.path.exists` said yes the whole time. ‼️ **Leaving a file on a box you are about to destroy IS a delete**, so the checkpoint rule above applies to it: `/evacuate plan` refuses to leave one nothing ranked. Completeness is computed against a manifest frozen **at the source before the transfer** — computed from what arrived, it is a tautology that passes every partial pull — and `pool.py release --artifacts recovered` now requires the resulting clearance, because that word means *verified* and until it was gated nothing had verified anything.
- **Never repeat a conclusion without re-reading its status.** 「多帧融合我们试过了，没用」 is a sentence about a corpus, a tier and a noise floor, and it carries none of them — which is why it goes on being applied to corpora it was never measured on, and why nothing raises when the run behind it was deleted last month. `knowledge/conclusions.json` holds the three qualifiers and `conclude.py status` re-derives what the evidence *currently* supports; a belief quoted from memory is a claim wearing a conclusion's clothes. ‼️ The state that actually occurs is **`unverifiable`** — the evidence stopped resolving — and it is neither a weak `supported` nor a `refuted`: nobody can check, which is the third fact, exactly as with a census that could not reach a machine.
- **Never quote a number without the tier it was measured at.** A `[T1 trend]` conclusion cited next week as a controlled one is how a soft number becomes a hard one, and it is how a false noise floor entered a record once already. The tier travels with the number forever, in every file and every sentence. Its companion: **with no measured noise floor, "no significant improvement" is undecidable, not negative** — three facts, not two, exactly as with a census that could not reach a machine.
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
| **run any skill** — check its requirements, offer the next one, push/pop state, or write any record | `references/skill-graph.md` — node hierarchy, the requires/suggests table, requirement checks, workflow state protocol |
| execute or finalize a run (`/train-run`, `/eval-run`, `/infer-run`, `/refactor-run`) | `references/run-mechanics.md` — step chain, launch contract, asset resolution, code snapshot, record integrity, checkpoint selection, retention, listing runs, path mapping |
| run `/explore` — propose, port, ablate, or adjudicate an architecture change | `skills/explore/references/experiment-graph.md` — node schema, the seven states, the four operations, and what `graph.py check` enforces |
| **decide whether a search is `/train-tune`'s or `/explore`'s** — the user said "搜一下" and it is not obvious which | `references/skill-graph.md` — "`/train-tune` vs `/explore`". The test is not params-vs-code, and getting it backwards costs a whole tune session |
| **hold more than one machine at a time** — `/train-tune` with `max_concurrent > 1`, any search that rents rather than borrows | `references/fleet.md` — slots vs machines, owned-before-rented, what a partial sweep is worth, preemptible as the search default, and why a preempted trial must never be read as a refuted hypothesis |
| find code/data on disk, resolve a `${}` reference, or create a project | `references/layout.md` — workspace and tool-repo location, code-source resolution, full file layout, dataset identity and census records, `${}` syntax |
| work the data line — freeze, curate, retire, or route a dataset | `references/data-line.md` — what each phase owns, what is not a phase, how `/data` composes them |
| pick up something designed but not built | `docs/roadmap.md` — and note that nothing in it has a script to call |
| **argue about an id** — mint one, scope one, or decide what a name may resolve to | `docs/identity.md` — 4 + 3 + 4 kinds and zero machine ids, adjudicated against the code. A settled debate, not a contract: it is in `docs/` for that reason, and the normative half lives in `references/layout.md` |
| change a script, a template, or a contract | `contracts/` and "Contracts" below |

## Status

**Implemented**: inference (init + run), evaluation (init + run + report + triage), refactor (init + run + report), training (init + run + tune + tune-report), **exploration** (`/explore` — architecture search, ported from the `arch-transplant` skill) with **`/conclude`** closing it (the belief layer: what is now believed, on what evidence, and what would overturn it — borrowed structurally from ARA's `logic/claims.md`), project init, resources, lease, **`/evacuate`** (empty a machine before it is released, prove every byte arrived, and store the result as an ARA-shaped artifact — `src` code+config, `evidence` numbers, `logic` conclusions, `trace` the ablation graph, `weights`), handoff, reproduction, and the whole data line (collect + label + curate + freeze + retire, plus check / audit / route / report / online-sample). **Multi-machine is real** as of the fleet layer: two rented-compute adapters (`nebius`, `lambda`) beside the owned-hardware one, and `scripts/shared/pool.py` holding N slots for one search, which `/train-tune` calls. What is *not* there: **multi-node distributed training** — a slot is one machine — and no bin-packing across concurrent searches. Full list: `references/fleet.md` "What this does not do".

**Shipped as a plugin**, and the repo root is the plugin root — `.claude-plugin/` holds both the
manifest and the catalog, `skills/` `agents/` `hooks/` `scripts/` `references/` sit beside `lifecycle/` — every one of those a directory the plugin spec names. That layout is not
cosmetic: install *copies the plugin directory*, so a plugin rooted at `.claude/` delivered 34 skills
and none of their scripts, with `validate` green the whole way. ‼️ `skills/` at the root is **not**
auto-loaded by working directory — `claude --plugin-dir .` or an install is now the only load path,
and an install reaches the project directories under `workspace_root` where cwd-loading never could.
Plugin skills are namespaced: see the note above the skill table.

**Three boundary gates and one verifier** close the places where a downstream stage consumed an
upstream artifact without checking it — the shape `handoff.py receive` has always had and nothing else
did. `provenance_gate.py` refuses a production run while `provenance.json` still calls a value a guess
(that file had no reader at all). `audit_gate.py` refuses one whose data is `fatal` / `never_audited` /
`unverifiable` / `stale` / `unreadable` — five states, because *an audit missing its compatibility
section reads identically to one that passed it*. `hooks/guard_destructive.py` is a `PreToolUse`
refusal on the deletes the rules reserve for a `plan` → `apply` script, and it is the only one that
works when **no skill is loaded**, which is the moment "Never silently" exists for. `triage-verifier`
is the one agent: `/eval-triage`'s verdict decided nothing could check, checked — read-only, and its
disagreement is a `disputed`, never an overrule. Both waivers stamp themselves into `run.json`; a
waiver outside the record is a flag.

**The gap those four found and did not fill**: `adaptation/adapt.py` is nine verbs of the data↔model
feedback loop and **no skill walks it** — roadmap.md's *"the one path in MLClaw that every skill points
at and none of them walks"*, still true. Two of the gates' failure routes point straight at it.

**Next**, in dependency order: `/train-triage` (depends on nothing, and covers the one failure the record layer actively disguises — a run that finished and was void), then `models/<id>@<release>` (the model-identity primitive three things wait on), then deployment (`/deploy-init` + `/deploy-run`) and model curate, plus `/data-drift`'s comparison half — its online half is built. Then `/train-compare`. **Designed, not built: there is no script to call.** Reasoning, and the traps that make the obvious implementation wrong: `docs/roadmap.md`.

**The data lifecycle**, one skill per phase — including the small ones, because a phase whose skill is "part of another skill" is a phase nobody can name, and an unnamed box reads as a box that does not exist:

```
   Collect   →   Label    →   Curate    →   Freeze    →   Retire
/data-collect  /data-label  /data-curate  /data-freeze  /data-retire

        /data  composes them · /data-check censuses · /data-report renders
   /data-audit opens the files · /data-audit-report shows what it flagged
   /data-online-sample reads the live stream the frozen side gets compared against
```

**`/data-check` and `/data-audit` are the two readings, and they are disjoint on purpose**: the
census reads existence, location and completeness markers across machines and never opens a file;
the audit opens the file and never asks where else it lives. A unit can be present everywhere,
replicated three times and marked complete, and still carry category ids the dataloader will
silently clamp — which is why one of them being clean says nothing about the other.

It is a **record** layer end to end, with one exception: `/data-retire apply` deletes,
earned by `plan → apply` against evidence plus the containment rule. Which phase owns
what, what is deliberately *not* a phase (Archive, Train), and how `/data` composes it:
`references/data-line.md`. Every rule there is cited by a check.

## Skills & Dependencies

**Every skill below is written `/name`, and that is deliberate.** Installed as a plugin the
invocable name is `mlclaw:<name>` — plugin skills are *always* namespaced, so that two
marketplaces cannot collide. Loaded standalone from `skills/` it is the bare `/name`.
Both forms resolve to the same `SKILL.md`, so the prose keeps the short one: there are ~1270
skill references across this repo, and rewriting them would be wrong in whichever of the two
modes it was not written for. ‼️ Read a bare `/train-run` as *"the train-run skill"*, and take
the name you actually invoke from the skills listing — never from this table.

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
| `/train-run` | Run training: validate resources → resolve sources → background launch → monitor stream (heartbeat, last_step, latest_metrics) → detect done/crash → finalize (best ckpt + retention). **A fine-tune also measures its base first**, via `/eval-run` — the child's score has no scale without it, the base's published number was measured on another scope, and after the run nobody measures a model they aren't shipping. So a fine-tune needs an evaluation stage. `baseline_delta.py` refuses one without a base measurement, and refuses two measurements taken under different settings — the case `compare_baseline.py` correctly passes because nothing about the *scale* is wrong |
| `/train-tune` | **The model is settled; find its operating point.** Adaptive HPO loop: observe prior runs → find coverage gaps → hypothesize the next config → launch trials via `/train-run` → iterate until budget or convergence. Its unit is one point in `runtime_params`, and the architecture, code SHA, dataset and split hold still — that is what makes the trials a *series* rather than a pile. **What the model should BE is not its question**; that is `/explore`'s, and it comes first — hyperparameters tuned around a component you are about to remove are paid for twice |
| `/train-tune-report` | Render a tune session as `chain.md`: headline, best-so-far curve, coverage map, decision timeline, confirmed/refuted distillation, recipe |
| `/explore` | **Architecture search** — turn "the results are bad / the architecture is primitive" into numbered, pre-registered, controlled changes: count the phenomenon, audit your own code in four states (**most "missing" techniques are present and defeated by one line**), price with existing flags, then port one flag at a time and ablate. **The model is not settled, and settling it is the work**: structure, components, and network selection — which model this should be, not how to configure the one you have. Its unit is a **proposal**, not a point in a config. It searches parameters too, when the parameter *is* the hypothesis (「是不是容量不够」) or when a ported component needs a fair operating point — but each such search is a card with its own pre-registered criterion, never a free axis mixed into an architecture arm, and the control arm gets the same budget. `/train-tune` is the same layer one step later: `/explore` decides what to tune, `/train-tune` tunes it. The dividing test, and the three shapes of parameter search: `references/skill-graph.md` -> "`/train-tune` vs `/explore`". Executes nothing: an arm is an ordinary run. Its record is `graph.json`, and `graph.py check` is the thing the source design asked for and never had — **nine invariants, reported, never repaired** |
| `/lease` | Acquire, renew and release a rented compute host, with a dead-man switch so nothing is left running. Also answers "what am I paying for right now" — and **"did I actually release that one"**, which no list can answer: absence from an inventory is equally consistent with a scope nobody enumerated, so only `history`'s lifecycle record proves release. Reports **two meters**: compute stops on its own, storage does not — it starts at create, survives the box, and is the half a sweep of instances cannot see. Adapters: `ssh` (owned), `nebius` and `lambda` (rented), and **they disagree usefully** — live price vs hand-written claim, an audit log vs none, a guest-side dead-man switch vs one that is *worse* than absent. **Holding several at once is `pool.py`'s**, above this layer — `references/fleet.md` |
| `/data-label` | Send work to a party MLClaw doesn't control and verify the return against a manifest frozen at send time. The only skill whose loop is closed by someone else |
| `/ask-human` | Put a question to a person and record the answer as what it is — `claim` / `verified` / `decision`. Sibling of `/data-label`: that one exchanges artifacts, this one exchanges answers |
| `/discover` | Find out what exists when nobody can tell you — the taking-over case. Sweeps code, git history, tracking backends, S3, servers, docs, people and probes each lead: `claim` / `verified` / **`gone`** (looked, not there) / **`unreachable`** (could not look). Data, weights, somebody's recorded results, and the credentials the other probes turned out to need — one engine, one lead register, because a missing key and the runs behind it are **one fact**. `reconcile` joins the leads against a stage's `candidates` both ways (drift + a need nothing is searching for); `--access-expires-at` records a source that stops being reachable on a known date. **`surface` measures how far a credential actually reaches** — the declared bucket is a run's default, not the sweepable surface, and a sweep scoped to it looks complete at 5% coverage. **`verify-framework` checks whether a pinned package is the published artifact** or was edited in place, against pip's `RECORD`. Never declares a dataset — that is `/data-check` |
| `/data-collect` | Name a resource, name a path on it, pull — and record what arrived. Ingest only, one direction. Never waits for a human — that is `/data-label` |
| `/data-online-sample` | A dated reading of the **live input stream** — what production was seeing between two instants — the half every drift tool fakes with an exported CSV. `/data-freeze` pins the reference side. Uniform draw always; a reading can never be retaken |
| `/data-check` | Declare a dataset's layout contract, then census it across every machine: GAP / DRIFT / UNREPLICATED / UNARCHIVED / INCOMPLETE. Reports; moves no byte. **Freezing is `/data-freeze`'s** — same script, different skill |
| `/data-audit` | **The only skill on the data line that opens a file.** Judges the data against its declared contract and the code about to consume it — integrity → compatibility → consistency → statistics → schema diff, fatal before advisory. Needs no model, which is what separates it from the evaluation stage; finds the wrong label *before* a run, where `/eval-triage` finds it through one. **Fixes nothing** — a repair is a `/data-curate` run |
| `/data-audit-report` | The half of an audit a person has to *look* at: flagged samples with annotations drawn on, distributions with outliers marked, the schema diff. Computes nothing. Every gallery states its denominator |
| `/data-curate` | Derive a new dataset from a frozen one — convert, split, dedup, relabel, sample, merge — and record what it was made of. Executes nothing: the transform is an ordinary run |
| `/data-freeze` | Freeze a citable snapshot so a run can record exactly which units it consumed — `datasets/<id>@<snapshot>`, the boundary the model lifecycle cites |
| `/data-report` | The whole line as one self-contained HTML board, Airflow-grid shaped: datasets × censuses, each column that census **replayed**. Computes nothing; no auto-refresh on purpose |
| `/data-retire` | Delete data against evidence, and leave a record that outlives it. `plan` ranks by what would **survive**; `apply` deletes only paths a census listing enumerated. The only delete on the data line |
| `/data` | Where a dataset sits on the line, what blocks it, what is next — a join across census + handoffs + snapshots + runs that no single skill can perform. Refuses transitions whose preconditions fail |
| `/conclude` | **The belief layer.** A run record says what happened and a graph card says which arm won; neither says what is now *believed* — and the belief is the only thing anybody repeats six weeks later, with none of the three qualifiers that made it true. Statement, evidence with **transcribed quotes**, `interpretation` kept in its own column (what is argued and was *not* measured), and a **mandatory** falsifier. **`status` and `tier` are computed, never written** — the tier is the **weakest** evidence's, and `check` reports the drift rather than repairing it, because a stored confidence that outlived its evidence is the whole failure. Its fifth state, **`unverifiable`**, is the one ARA has no word for |
| `/evacuate` | **Get the work off a machine before it disappears, and prove it.** The one place where doing nothing is the destructive act. Freezes a manifest **at the source** (computing completeness from what arrived passes every partial pull by construction), then rules on arrival in states `exists()` cannot tell apart — `truncated` (the recurring failure), `corrupt` (right length, wrong bytes, only the hash sees it), `unverifiable` (the destination did not answer, never `missing`). Stores it ARA-shaped: `src` is the input (code + config, which in an architecture search **is** the reproducibility claim), `evidence` / `logic` / `trace` / `weights` are the output — the fifth layer being MLClaw's, since a paper's knowledge regenerates from src+evidence and a 4GB checkpoint does not. **Refuses to leave behind a checkpoint nothing ranked**, and refuses clearance while a conclusion cites something that is not in the manifest |
| `/ara` | **A round's work as a readable artifact**, ARA-shaped (arXiv:2604.24658): `src` the input — code + config, which in an architecture search **is** the reproducibility claim — and `evidence` / `logic` / `trace` / `weights` the output. The fifth layer is MLClaw's, because a paper's knowledge regenerates from src+evidence and a 4GB checkpoint does not. **`check` catches CLAUDE.md's conclusion rule one level up**: the artifact FREEZES each belief's `status` and `tier`, nothing about the frozen copy changes when its evidence rots, and the frozen copy is the one handed to whoever takes over. Reports the drift, never repairs it. Needs no machine — `/evacuate` calls this because a dying box is the last moment its source can be read, which makes the deadline a forcing function rather than a container |
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
   - **In the same pass**: `python scripts/adaptation/adapt.py status --project {PROJECT} --open-only`. Skip silently when the project has no `adaptation/`. Report `blocked_anywhere` first and name the sessions — a **blocked** finding is one the other end has already said it cannot move, so unlike an open one nothing further happens on its own; it is a handoff waiting to be opened that nobody opened. Then report `stale` sessions. An adaptation campaign left open is the case where a converter half-fits, somebody trained on it anyway, and the record of what was still wrong sits in a file nobody re-read.

4. **Check how stale the data picture is**: `python scripts/data-check/census.py status --project {PROJECT}`. Records only — this verb never touches the network, which is what makes it safe here. Skip silently when the project has no `datasets/`.
   - Report the census **age** and any standing `unarchived` / `unreplicated` / `incomplete` counts, then offer to re-scan. Do not re-scan unprompted: `scan` goes out and asks every machine, and four ssh timeouts before the user's first sentence is not a greeting.
   - **State the age before quoting any count.** A three-week-old census is a description of a disk that has since been written to, rolled, and possibly filled — quoting its numbers as current is the same error as reading a stale metric off an old run.
   - `unarchived` is the entry that gets worse by being missed, and worse than a stalled handoff: a capture machine reclaiming space to keep shooting cannot know whether the day it is about to delete was ever copied off. Nothing on that machine can compute it. This check is the only thing standing there.
   - **In the same pass, if `{PROJECT}/discovery/` exists**: `discover.py report --project {PROJECT}` (records only, no network). Report `unprobed_leads` and anything `unreachable` — **not the findings.** `/discover`'s entire premise is that access arrives weeks after responsibility does, and that only helps if something re-opens the file. Without this line `leads.json` becomes the Confluence page it was written to replace: accurate the day it was made, never looked at again. A lead that has been `unreachable` for three weeks usually means a credential arrived and nobody re-probed — offer that, don't do it unprompted.
     - **`record_unsaved: true` means the sweep exists on one disk and nowhere else.** Offer `discover.py save`. Nothing else will notice: an untracked record reads identically to a tracked one, right up until a clone or a `git clean`, and a handover is a clone.
     - **Report `access_expiring_soon` and `access_expired_and_unresolved` first, ahead of both.** This is the only entry on the whole conversation-start list with a *deadline* rather than a staleness, and it is the one nothing else can recover: an unarchived day can still be copied tomorrow, a stale census can be re-scanned, an unprobed lead keeps waiting. A key that rotates on the 14th, or a departing colleague's account, takes the history with it on a date somebody already knows — and `access_expired_and_unresolved` means that date passed while the lead was still `claim` or `unreachable`, so the loss is now permanent and no probe will ever say `gone`. Say the days remaining, and say which lead.

Steps 1, 3 and 4 all scan the project; do them in one pass. `lease.py reap` belongs here as a fifth (cloud-side orphan check, gated on a provider being registered) and is **still not wired** — see `/lease` "The human's window".

---

## Conventions

### Script Integration

Skills use Python scripts from `scripts/<skill>/` for mechanical tasks. Each skill's scripts are in a matching subdirectory, invoked via `python <mlclaw_root>/scripts/<skill>/<name>.py <args>`.

**`<mlclaw_root>` is resolved, never assumed** — `python "${CLAUDE_PLUGIN_ROOT:-<repo>}/scripts/shared/workspaces.py" tool` prints it. Three sources, first hit wins: **`$CLAUDE_PLUGIN_ROOT`** (the official plugin mechanism, verified against the manifest, never cached), then `~/.mlclaw/state.json`, then self-bootstrap from `__file__` (`references/layout.md` -> "Workspace and tool-repo location"). One placeholder bound once and used at every call site is also the official shape — `claude-security` binds `SCRIPTS` the same way rather than inlining the variable 40 times. ‼️ A bare `python scripts/…` is only correct when the working directory happens to be this repo, and when it is not, **nothing raises**: the fallback rule below turns a missing script into "do the same work manually", so a `retention.py` refusal, a `graph.py check` or an `evacuate.py clearance` silently stops running while the flow still reads as working. `contract_docs.ScriptPathsAreResolvedNotAssumed` holds the line.

**Fallback rule**: If a script fails (non-zero exit, import error, file not found), the skill MUST NOT stop. Instead:
1. Log the error silently
2. Perform the same operation manually (Claude does it inline using Bash/Read/Write tools)
3. Continue the flow as if the script had succeeded

Scripts are an optimization, not a dependency.

**The fallback rule has one exception, and it is the important one.** A script that *refuses* is not a script that *failed*. When `code_snapshot.py` refuses a non-git tree, `retention.py` refuses a plan because a deletion target has no metric, or `reconcile_metrics.py` returns `fail` because the declared metric is absent from the stream — that is the answer, arrived at correctly. Redoing the work by hand there means overriding a safety check, which is the opposite of a fallback. Distinguish the two by exit code: **2 = the script broke, fall back and do it manually; 1 = the script worked and the answer is no.**

### Contracts

**What is enforced**: the record layer only — `references/run-mechanics.md` "Record integrity". Everything else in this file is a contract nothing checks; when you touch one of those, you are the check.

`contracts/` holds the executable form of the contracts stated in this file. Not unit tests — the distinction is load-bearing, so keep the vocabulary:

```
python -m unittest discover -s contracts -t contracts -p 'contract_*.py'
```

The `-p` is required; the default pattern is `test*.py` and would silently find nothing. Stdlib only — that is why CI needs no install step for the suite and why a green run means something on any machine. `pixi.toml` pins the *interpreter* and nothing else; `contract_pixi.py` holds the line (default environment declares only python, and a third-party import must be guarded). Vendor SDKs live in one opt-in `probes` environment, which is why `discover.py` answers "the package is not installed in this interpreter" instead of a false `gone`. Scripts live in hyphenated directories that aren't importable package names, so `contracts/helpers.py` loads them by path and provides temp-dir and real-git-repo fixtures.

**Every check class cites the section it enforces**, in its docstring. That citation is the admission rule: a check that cannot point at a written contract is either a missing line in this file or padding — decide which, don't leave it. It is also how you find out what is *not* enforced: grep the citations, diff against this file's sections.

**When a check fails, the first question is "is the contract still right?", not "how do I make this pass."** If the contract changed, edit this file and let the check follow — the contract is upstream. That is the intended workflow, not a defeat. A check whose failure doesn't tell you which side to change is itself a liability; delete it.

**What earns a check**: a record written now and read later by someone who can no longer verify it, or an irreversible action. That is the whole bar — see `references/run-mechanics.md` "Record integrity". Most of this file is *not* enforceable (one question at a time, never guess a value, confirm before saving). **A green run means the record layer is intact. It does not mean MLClaw is correct.**
