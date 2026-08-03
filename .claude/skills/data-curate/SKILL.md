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
S=<mlclaw_root>/lifecycle/scripts/data-curate/curate.py

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

**It does not convert bytes, and it does not choose a shard format.** `/data-freeze` says the same
thing from the other side, and it is the same boundary: standardising a *manifest* is free;
standardising *bytes* is a curate run producing a new dataset. That run is the user's code, and it
stays theirs — zero code invasion is the project's first principle, and a built-in converter would
be the first violation of it.

## Requires / suggests

- **Requires**: a `datasets/<id>/dataset.json` for the parent, and **a frozen snapshot of it**. No
  snapshot means `/data-freeze` first — this skill refuses to derive from a moving target.
- **Suggests**: `/data-check` to declare the output's locations and scan it, then `/data` to confirm
  where the new dataset lands on the line.

Per `lifecycle/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: "curate"` when the
transform runs as an MLClaw run, `execution: <run_id>`; `stage: null` when it does not.
