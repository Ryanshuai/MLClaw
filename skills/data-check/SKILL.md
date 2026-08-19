---
name: data-check
description: >
  Maintain the census of a dataset — where every unit physically is, which layers it carries, and
  whether anything claims it finished. Declares a dataset's layout contract once, then answers
  "what do we have / what is missing / what is about to be lost" on demand. Trigger for: taking
  stock of collected data, finding out what still needs computing or syncing, checking whether
  anything exists in only one place, and checking whether a capture ever came off the machine that
  made it. Also trigger for Chinese requests like "数据都在哪", "有多少数据了", "哪些还没算",
  "哪些没备份", "数据同步了吗", "这批数据完整吗", "盘一下数据". Not for freezing a citable snapshot
  (use /data-freeze, which shares this skill's script) or for finding data nobody has declared yet
  (use /discover).
  Not for sending data to an outside party (use /data-label) or for judging the data's quality
  (that is an evaluation stage).
---

# /data-check — the census layer

Every other MLClaw skill asks about something MLClaw did. This one asks about **something that is
just true**: five machines, a few terabytes, and a question — where is it, and what state is it in.

That difference decides the whole shape. A run has a beginning, an end, and a step chain, so its
record is an execution. A census has none of those: **its state is the matrix**, because the state
is a property of the world rather than of a process MLClaw ran. So there is no `steps` block, no
resume, and nothing to monitor — the same argument as `/data-label`, one domain over.

A dataset is **not a stage**, for the same reason a handoff isn't: training consumes it, evaluation
consumes it, export consumes it. Records live at `{PROJECT}/datasets/`, not under `stages/`.

## The judging rule

**Read existence, location, and completeness markers. Never content.**

Borrowed verbatim from a working data pipeline, because it is the line that keeps this skill one
small script instead of a second pipeline. The moment something here opens a file to judge whether
the data inside is any good, it has become an evaluation stage — and then there are two things in
the project computing quality, which is how you get two numbers that are not the same quantity.

One exception, `completeness.partial_marker_field`, and it is permitted only because that field is
the *completion claim itself*, not a statement about the data.

## Step 1 — Declare the layout contract (once)

`dataset.json` is filled in dialogue, one question at a time — and only with what only the user knows; anything a census or a listing can read is read, not asked (CLAUDE.md "Decide what evidence can decide"). The template
(`lifecycle/data/dataset.json`) carries the reasoning for every field. Four of them cannot be
defaulted, and each has a specific failure waiting behind a guess:

| Ask | Why a guess is worse than a question |
|---|---|
| **What is one unit, and what is its path shape** (`identity.unit_glob`) | Path is identity — that is what makes a union across machines a set union with nothing to reconcile. The glob's *depth* is load-bearing: a rig writing `<date>/<scene>` where the tooling globs `<date>/<seq>/<scene>` is not a variation, it is **invisible**. Every unit from that rig silently counts as zero. |
| **What layers can a unit carry, and which are `source`** (`layers[].kind`) | `derived` missing is an inconvenience — recompute it. `source` missing is the data's non-existence. If one field decides whether a report means "run the job again" or "it's gone", it is not a field to infer. |
| **What proves a unit finished** (`completeness.marker`) | See below. This is the one the user will want to skip. |
| **Every place it is supposed to live** (`locations`) | A location you don't declare is a location whose copy doesn't count and whose absence never reports. Include the capture machine and the backup, not just the ones you compute on. |

**Directory existence is not completion.** A capture process that creates its output directory *up
front* is often correct — an operator with no screen has to see the number they are shooting into —
and it makes directory existence worthless as a completion signal. What proves completion is a
marker written at the END. Ask for it. If the answer is genuinely "there isn't one", record
`marker: null` and every unit reports `unverifiable` forever — which is the honest outcome and
visibly worse than a real marker, so it tends to produce one. What must never happen is `null`
arriving by default, because then a half-finished unit reads as complete, and **a unit that looks
whole and is not is the only defect here that survives all the way into a trained model.**

Confirm the file before writing it, per CLAUDE.md "Confirm before saving".

## Step 2 — Scan

```bash
S=<mlclaw_root>/scripts/data-check/census.py
python $S scan     --project <p> --dataset <d> [--allow-unreachable]
python $S show     --project <p> --dataset <d> [--census <id>] [--units]
python $S snapshot --project <p> --dataset <d> --id <sid> [--layer <l>] [--at <loc>]
python $S resolve  --project <p> --dataset <d> --snapshot <sid> --at <loc> \
                   [--layer <l> ...] [--allow-missing <n>] [--out <file>]
python $S status   (--project <p> | --workspace <w>)
```

Exit 2 means the script broke — do the same work by hand and continue. **Exit 1 means it worked
and the answer is no**; every exit-1 path here is a record-integrity check, so redoing it by hand
is overriding the check, not falling back. They are listed under "The four refusals".

Read the counts out loud. **The counts are the finding, not a preamble to it.**

### The five verdicts

The first two are the familiar ones. The three after them are the reason this skill exists.

| Verdict | Means | Action |
|---|---|---|
| **GAP** | a layer missing at every location that expects it | needs compute |
| **DRIFT** | present somewhere, absent somewhere expected | needs sync, toward the authority |
| **UNREPLICATED** | a `source` layer under `min_source_copies` | one disk failure from total loss |
| **UNARCHIVED** | exists somewhere, never reached the authority | still only where it was born |
| **INCOMPLETE** | nothing anywhere claims this unit finished | do not feed it downstream |

**UNREPLICATED is not a kind of DRIFT**, and collapsing them loses the only distinction that
matters before an irreversible decision. Both read as "a copy is missing" if the verdict is only
ever about sync — but one is fixed by re-running a job and the other is the data's entire existence.

**UNARCHIVED is not a kind of UNREPLICATED**, for a parallel reason: copy count and reach are
different questions. Data on a capture box plus its own mirror has two copies and has still never
entered the pipeline; data at the authority alone has one copy and is fully in play. Only one of
those is fixed by buying a bigger disk.

UNARCHIVED is also the one verdict that **cannot be computed where the risk lives**. A capture
machine deleting its oldest day to make room for the next shot cannot answer "was this ever copied
off" — it can only see itself. Someone who can see both ends has to compute it, and this is that
someone. If a `min_source_copies` breach and an autonomous space-reclaiming policy exist on the
same machine, say so plainly: that combination deletes data nobody knows was unique.

### Three states, never two

`scan` distinguishes **unreachable** / **root does not exist there** / **answered, and it is
empty** — because they are three different facts and only the third means the data is not there.
This is CLAUDE.md "Never record a metric you did not read", one domain over: a broken ssh and an
empty disk must not both print `0`.

So a census with any unreachable location is marked `complete: false`, **every count in it is a
lower bound**, and `scan` exits 1 unless `--allow-unreachable` was passed. Never present a partial
census as an inventory. When a location is unreachable, that is itself the report — and often the
real finding, because a backup nobody can reach has not been a backup for however long that has
been true.

## Step 3 — Hand off to /data-freeze

**Freezing is `/data-freeze`'s, not this skill's.** This section used to document the whole thing —
the `snapshot` verb, the citation rule, the four refusals — and that was a second copy of another
skill's contract, which is the drift this repo keeps paying for. Read `/data-freeze` for all of it.

What belongs here is the one fact this skill is responsible for: **a snapshot is computed from a
census, so a clean census is the thing that makes a freeze possible.** `snapshot.json -> from_census`
records which one. That is also why `census.py` still carries the `snapshot` and `resolve` verbs —
the code lives with the census because it reads one, while the skills stay apart: this one observes
the world, that one commits to a version of it. **Skill boundary is not script boundary.**

So when a census comes back clean, say so and offer `/data-freeze`. When it comes back partial,
`/data-freeze` will refuse it and this is the cheaper place to find that out.

Why it matters at all, stated once because it is the reason the data line exists: MLClaw's lineage
would otherwise start at code + config + weights, which for anything trained on collected data is
the wrong end of the chain. The numbers trace back through labels, through a solve, through a
capture made by a specific rig on a specific day — and a rig whose swapped sensor silently changes
the measurement scale is a documented failure mode. A run that cannot name its data cannot be
audited backwards past its own directory.

**A snapshot pins membership, not bytes.** No content hashes: `/data-label` hashes every item because
its manifest is the only authority for what a third party owes back, and here the question is which
units were in the set. Hashing a multi-terabyte tree to answer it would trade a real answer for one
nobody would wait for. Say this rather than letting someone assume byte-level pinning.

### The four refusals

None is an "are you sure". Each demands a specific piece of evidence, because a confirmation prompt
carries no information about whether the thing being confirmed is right.

| Refusal | What it protects |
|---|---|
| `snapshot` against a partial census | Freezing pins a set that was never fully seen; the units on the machine that didn't answer are silently not in it. |
| `snapshot` containing unverified-complete units | A half-finished unit inside a frozen set becomes clean data in every record that follows. |
| `--allow-incomplete <n>` where n ≠ the measured count | Binds acceptance to what this scan measured, not to a number remembered from the last one. |
| reusing a `snapshot --id` | A frozen set that changed under an existing citation is worse than no citation. |

`--allow-incomplete` is the right call sometimes. What it is not is a step to type past on the way
to the outcome you wanted — when you reach for it, say what is being accepted in the same breath.
It is recorded in `snapshot.json -> unverified_units` and travels forward.

## Step 4 — Resolve it, so the training side can open a file

A manifest line reads `{"unit": "260725/s003", "layers": {"rgbd": ["nas","box"]}}`. Those are
location **keys**, not paths. Opening anything needs a three-way join — manifest ×
`locations[].root` × `layers[].marker` — and two of the three live only in `dataset.json`, which no
stage config reads. So a manifest handed to a dataloader as-is cannot open one file, and every
consumer that tries reimplements the join differently.

```
python $S resolve --project <p> --dataset <d> --snapshot 260731_train \
                  --at nas --layer rgbd --layer gt \
                  --out <run_dir>/data_resolved.jsonl
```

One JSONL line per unit, `{"unit", "paths": {layer: path}, "completeness"}`, behind a `_resolved`
header carrying the citation, the location, which census the paths were true as of, and how old the
snapshot is.

**It is a view, never a record — and the `--out` guard enforces that.** Writing it into
`snapshots/` is refused, because `dataset.json` is machine-independent on purpose (`locations` names
the machines, everything else describes the data) and a resolved path embeds one machine's root into
the record that is supposed to survive moving a disk. Nothing is lost: snapshot + `dataset.json` +
`--at` regenerate it exactly, which is why the run cites `datasets/<id>@<sid>` and **not this file**.

**Standardising the manifest is free; standardising the bytes is not.** If someone asks for one
on-disk format for training to consume — shards, a columnar store, a single archive — that is not
this step. It would make freezing cost terabytes, break "a snapshot pins membership, not bytes",
and *still* require the user's dataloader to change, which is the one thing MLClaw does not ask for.
A byte conversion is a **curate run** that consumes this snapshot and produces a new dataset,
separately frozen and separately citable — reproducible, instead of a conversion nobody recorded.

Two more refusals, same character as the four above:

| Refusal | What it protects |
|---|---|
| a requested layer absent at that location for some units | Emitting 900 units under a citation that says 1240 is a run whose recorded data lies. `--allow-missing <measured count>` accepts it explicitly; the count binds to what is measured now. |
| `--at` names a `backup` | `dataset.json`'s own words: written to, never read from for compute. Training off the backup is how a restore test never happens. |

A requested layer that the location declares it never holds (`has_layers`) is refused differently
and on purpose: that is a wrong request, not missing data, and reporting it as N missing units
would send someone to compute a layer that was never supposed to be there.

`resolve` stats nothing. The paths were true as of the census in its header — quoting them as
current has the same defect as quoting a three-week-old count as an inventory.

## What this skill does not do

**It never moves or deletes a byte.** It reports; the user's own tooling (`rsync`, `dsync`, a sync
script) moves things. Two reasons, and the second is the binding one:

- Every project already has its transfer conventions, and they encode real rules — SOURCE is
  refuse-if-exists in every direction, DERIVED is newest-wins, human-locked layers never
  auto-overwrite. Reimplementing those here would put a second, worse copy of them in play.
- **Deletion needs evidence, and the census is that evidence** — it is not also the actor. Same
  split as `retention.py` plan → apply, and the same reason: the irreversible half must be a
  separate, evidenced decision. When the user asks "can I delete this", the answer is a
  replication count and an archive verdict, not a checkbox.

If someone asks this skill to clean up disk space, give them the numbers and let them run their own
delete. Point at CLAUDE.md "Never silently" — a list of paths carries no evidence that the ranking
behind it was right.

## Requires / suggests

- **Requires**: `project.json` exists. Nothing else — data precedes every stage, which is the point
  of it being project-level. Reaching a `via: server` location needs that server in
  `{WORKSPACE}/resources.json -> servers`; if it is missing, invoke `/resources` and resume.
- **Suggests**: on GAP, whatever `layers[].produced_by` names (a `run:<stage>` value points at that
  stage's run skill). **On a clean census, `/data-freeze`** — not `/train-init` directly, because a
  training run cites a snapshot and this skill does not make one. On INCOMPLETE or UNARCHIVED,
  nothing — those are the user's to act on, and offering a next skill buries them.

Per `<mlclaw_root>/references/skill-graph.md` -> "Workflow State Protocol", push to the stack on entry and pop on exit. Use
`stage: null` and `execution: <census_id>` once a scan has run; `step` is one of `declare` / `scan`
/ `snapshot`. **Pop before reporting a long verdict list** — a census is not unfinished work, and
leaving it on the stack opens the next session with a false resume prompt.

`status` reads records only and never touches the network, which is what makes it safe for
CLAUDE.md "On Conversation Start". `scan` goes out and asks every machine, so it is never what a
session opens with — report the census age from `status` and offer to re-scan.
