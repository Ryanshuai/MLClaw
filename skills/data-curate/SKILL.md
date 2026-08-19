---
name: data-curate
description: >
  Derive a new dataset from a frozen one — convert, split, dedup, relabel, sample, merge — and
  record what it was made of, verified against the run that made it. Trigger for: converting a
  dataset to another format, splitting train/val, dropping duplicates or bad frames, re-labeling,
  subsampling, merging two datasets, and asking what a derived dataset actually contains. Also
  trigger for Chinese requests like "把这批数据转成", "切个 train/val", "去重", "抽 10% 出来",
  "合并这两批数据", "v2 是怎么来的", "这个数据集是从哪来的". Not for pulling data in
  (use /data-collect) or pinning a version (use /data-freeze).
---

# /data-curate — the derivation record

Third box on the line, and **the only one that is run-shaped**: it has inputs, outputs, code,
params and a reproduction story. That is a run, and MLClaw already has code snapshots, env capture,
param injection and lineage for one. Curate is the cheap box precisely because it needs no new
primitives — so this skill **executes nothing**. The transform runs through the ordinary run
machinery; what this owns is the record.

**The record is the whole point, because a derivation cannot be re-observed.** Every other box
describes a state you can scan again. Once `boxes_v2` is on disk it looks exactly like data
somebody captured, and the fact that it is a 30% sample with the blurry scenes dropped, produced by
a script at commit `abc123`, lives nowhere but in whoever's memory. That is the bar in CLAUDE.md
"Contracts": a record written now and read later by someone who can no longer verify it.

## The script

```bash
S=<mlclaw_root>/scripts/data-curate/curate.py

python $S plan     --project <p> --from datasets/<id>@<snap> --to <new_id> \
                   --op convert|split|dedup|relabel|sample|merge \
                   --into <output root> [--at <loc key>] [--like <ds>] [--note ...] \
                   [--acknowledge <n>]
python $S register --project <p> --plan <f> (--run <stage>/<run_id> | --claimed --because ...) \
                   [--like <ds> | --declare <dataset.json>] [--note ...] [--re-register]
python $S trace    --project <p> --dataset <id>
```

Exit 2 = broke, do it by hand. **Exit 1 = worked, the answer is no** — every exit-1 here guards
either an irreversible write or the claim/verified boundary, so redoing it by hand overrides the
check.

## Step 1 — `plan`, before the compute

Three refusals, and the middle one is the expensive mistake:

| Refusal | What it protects |
|---|---|
| the parent is not a frozen snapshot | `--from` takes `datasets/<id>@<snap>`, never a bare id. A dataset grows; "derived from boxes" names no particular afternoon, and a parent edge that cannot say which one is not lineage. |
| **`--into` overlaps a source location** | In-place transforms are the one curate failure with no undo. The parent's snapshots keep naming units whose bytes were rewritten underneath them, and **every one of those citations still resolves.** Checked both directions, through `realpath`, so a symlink cannot smuggle it past. |
| the output id already exists | A derived dataset is a new identity, not a new version of an old one — runs already cite the old id. Same rule as a snapshot `--id`. |

Then it calls **`/data`'s curate gate** — it does not reimplement it. The gate's one job here is
`UNREPLICATED`: deriving from a source layer that exists on one disk succeeds, and leaves the thing
it derived from still one failure from gone. A second copy of that rule in this script would drift,
and then two parts of MLClaw would disagree about whether a deletion is safe.

Ask the user, one at a time, what the script cannot infer: which frozen parent, what the operation
actually is (the `--op` value is what makes derivations queryable — free text would not be), and
where the output goes.

## Step 2 — run the transform, the ordinary way

Nothing special. Create the run, snapshot the code, execute. **One thing must not be skipped:** the
run cites every parent in `run.json -> lineage.parents`, exactly as the plan's `the_run_must_cite`
lists them. That citation is not decoration — it is the evidence `register` checks against.

## Step 2b — when the point is to satisfy a consumer: the adaptation loop

Skip this when the derivation stands on its own (a split, a dedup, a subsample). Run it when the
derivation exists **because some stage's code cannot read the parent** — the `--op convert` case, and
the one a user actually arrives at: `/train-init` wrote `match: "mismatch"` and training cannot start.

One transform is almost never right the first time, so Step 2 becomes a loop, and the loop needs a
ledger or it does not converge — round five re-tries what round two eliminated. That ledger is
`scripts/adaptation/adapt.py`; the record it writes is `{PROJECT}/adaptation/<id>/session.json`.

```
adapt.py open --project {P} --dataset <parent> --snapshot <frozen> --consumer-stage training
```

It refuses two things at open, and both refusals are the point: **an unfrozen parent** (this skill's
own rule — a derivation from a moving directory cannot say what it was made of) and **a consumer
whose `input.json -> items` declares no `requires`**. The second is what makes the loop possible at
all: with the contract filled, `mismatch` stops being a verdict and becomes a **diff**, which is both
the first converter's spec and the loop's progress bar.

**Each iteration is one ordinary Step 2 run, then two oracles, in order:**

| | what it answers | on failure |
|---|---|---|
| **dataloader probe** | does the consuming code accept this at all — format, field names, layout | `--probe fail` |
| **`/data-audit`** | is what it accepted actually right — values in range, category ids inside `num_classes`, distribution sane | `--audit dirty` |

```
adapt.py round --project {P} --id <sid> --run curate/run_<...> --probe pass|fail --audit clean|dirty
```

**Never stop at the first layer.** `round` refuses `--probe pass --audit not_run`, and the refusal is
the whole reason this loop is written down: a converter emitting all-zero boxes loads perfectly, and
a category id the dataloader silently clamps is not a crash. A loop whose bar is "it ran" certifies
exactly the converters that ruin a training run without ever erroring.

What a round turns up goes in as a **finding**, against one of three ends — `dataset`, `consumer`,
`contract`, never a person:

```
adapt.py raise    --project {P} --id <sid> --against dataset --what "..." --evidence "<probe stderr | audit id>"
adapt.py respond  --project {P} --id <sid> --n 1 --action fixed|partially_fixed|cannot_fix|disagree|needs_from_other_side
adapt.py distill  --project {P} --id <sid> --kind refuted --says "no converter can synthesise depth" --cites 1 --cites 2
```

`distill --kind refuted` is the one to actually use. Confirmed conclusions tend to get written down
on their own; the ruled-out ones are what stop the loop re-treading itself, and they are the half
every summary drops.

**When it stops converging, that is a result and not a failure.** If rounds establish that no
converter can fix it — the source never captured the field, the labels are wrong at the source — the
defect is upstream and the oracle has changed from "the dataloader accepts it" to "the party that
owns the data agrees". Two oracles are never one session:

```
adapt.py close --id <sid> --verdict degraded_to_rework --attributed-to dataset
```

It prints `unresolved_findings`; those are what the `/data-label` rework round carries. Same backward
edge as `/eval-triage`'s `label_wrong` — reached *before* a training run rather than through one.

On a clean round, close as `adapted` and go to Step 3. `close --verdict adapted` refuses when the
probe never ran (that is `unverifiable`, because a probe that fails open turns every unchecked round
into a pass) and when any finding is still open.

**This skill still executes nothing.** Every round's transform is the user's code through the
ordinary run machinery; what is new is only that the loop leaves a record.

## Step 3 — `register`, and the claim/verified split

`register` writes the new `dataset.json` with `derived_from` filled in. Two provenances, same
vocabulary as `/ask-human`, and never spelled the same way:

- **`run`** — a **completed** run cited these parents in its own record. Checked, not asserted.
- **`claimed`** — somebody transformed the data outside MLClaw. Legitimate and recorded; `--because`
  is required, because an unverified edge with no reason given is a hole nobody can evaluate later.

Two refusals on the `run` path:

- **the run is not `completed`.** A crashed conversion leaves a partial output tree that is
  indistinguishable from a whole one the moment it is registered as a dataset. Same shape as
  CLAUDE.md "Never say a unit is complete because its directory exists", one level up.
- **the run does not cite the plan's parents.** CLAUDE.md "Never let somebody's word become a
  checked fact" — the person registering it is not evidence for what the run consumed.

The new dataset's layout contract is inherited from the parent (`identity`, `layers`,
`completeness`, `replication`) — right for sample/dedup/split, which change membership and not
shape. **Locations are never inherited**: the output is somewhere new by construction, and copying
the parent's roots would declare it to be on machines that have never held it. When the shape really
does change — a format conversion with different layers — pass `--declare <dataset.json>`.

A registered dataset has **no census yet**, so `/data` reports it as `collect` and nothing may
freeze or train off it. That is correct: nothing has looked at what was actually written.

`--re-register` redoes a register that went wrong, and stops working the moment the id has a census
or a snapshot. That line is not arbitrary — either one means the identity has left this command, and
replacing its contract afterwards makes those records describe something else.

## Step 4 — `trace`, the reader

```
boxes_v3  ← sample     run curate/run_20260801_1015   [run]
  boxes_v2  ← convert  run curate/run_20260715_0900   [run]
    boxes   ← captured
```

A record nothing reads is a record nobody maintains. `trace` walks `derived_from.parents` back to
the captured roots and reports two things the chain would otherwise hide: links whose provenance is
`claimed`, and parents this project has no record of. **Neither truncates the chain silently** —
`trustworthy: false` says so, because a chain reads as authoritative either way and a reader who is
not told will not ask.

A cycle is a refusal, not a traversal limit: a dataset cannot be its own ancestor, so one of those
records is wrong.

## What this is not

**It does not convert bytes itself, and it does not choose a shard format.** `/data-freeze` says the
same thing from the other side, and it is the same boundary: standardising a *manifest* is free;
standardising *bytes* is a curate run producing a new dataset. That run is the user's code, and it
stays theirs.

**Which forbids a converter shipped in MLClaw — not a converter.** The line is between the two, and
it used to read as though it banned both, which is how the most common thing a user does ended up
with a diagnosis and no action. A `coco_to_yolo.py` living in this repo would be MLClaw carrying a
format library: it would rot against the next variant, get edited in place, and become a piece of the
tool that one project's data decided the shape of. A converter the agent writes for **this** source,
after reading a sample, that runs as an ordinary run and is frozen in that run's code snapshot, is an
*instance* — the same category as a filled `config.json`, and it satisfies zero code invasion more
completely than a built-in would, because nothing was added to the user's code *or* to the tool.

Concretising per project is the whole job. Step 2b is where it happens; what stays project-independent
is the loop and the record, not the transform.

## Requires / suggests

- **Requires**: a `datasets/<id>/dataset.json` for the parent, and **a frozen snapshot of it**. No
  snapshot means `/data-freeze` first — this skill refuses to derive from a moving target.
- **Requires for Step 2b only**: the consuming stage's `input.json -> items.<name>.requires`. Empty
  means there is no oracle, and `adapt.py open` refuses rather than let each round be judged by
  whoever is looking.
- **Suggests**: `/data-check` to declare the output's locations and scan it, then `/data` to confirm
  where the new dataset lands on the line. After a Step 2b campaign that ended
  `degraded_to_rework`, `/data-label` for the rework round the unresolved findings describe.

Per `references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: "curate"` when the
transform runs as an MLClaw run, `execution: <run_id>`; `stage: null` when it does not.
