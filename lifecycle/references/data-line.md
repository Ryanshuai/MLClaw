# The data line — what each phase owns, and what is not a phase

Loaded before working the data line. CLAUDE.md carries the picture and the skill
inventory, because that is what you route from; this carries the architecture rules
that no single skill states, because each of them is a fact about the *seam between*
two skills and would drift if it lived inside either one.

Every rule here is cited by a check in `contracts/`. That is the reason it is here
rather than in CLAUDE.md: a check has to be able to point at the sentence it
enforces, and a sentence compressed out of the always-loaded file leaves the check
enforcing something nobody wrote down.

## The line, and what each phase owns

```
   Collect   →   Label    →   Curate    →   Freeze    →   Retire
/data-collect  /data-label  /data-curate  /data-freeze  /data-retire

        /data  composes them · /data-check censuses · /data-report renders
   /data-online-sample reads the live stream the frozen side gets compared against
```

**One skill per phase, including the small ones**, because a phase whose skill is
"part of another skill" is a phase nobody can name, and an unnamed box reads as a
box that does not exist.

**The order is the order**, and it decides what gets reported: `/data`'s answer is
always the **earliest unfinished box**, never the most alarming finding. A dataset
with both incomplete units and a stale snapshot needs the units first, and leading
with the scarier item sends people to fix the wrong end of the line.

**The line is a *record* layer end to end, with exactly one exception.** It can say
where data is, what produced it, what it was derived from, what state it is in and
where it sits on the line. It converts no format, and it moves one byte in one
direction only (`/data-collect` pulls in). The exception is `/data-retire apply`.

Each skill's SKILL.md owns its own detail; what follows is only what no single one
of them can say.

## Curate: a derivation cannot be re-observed

**Curate borrows a stage and executes nothing.** It is the only phase that *is*
run-shaped — inputs, outputs, code, params, reproduction — so the transform is an
ordinary run in `stages/curate/` and `list_runs.py`, `build_dag.py` and `/data`'s
`consumers()` see it for free. That reuse is the entire reason Curate needed no new
primitives.

What it owns is the **record**, because a derivation is the one thing on this line
that cannot be re-observed: once `boxes_v2` is on disk it looks exactly like data
somebody captured, and that it is a 30% sample with the blurry scenes dropped lives
nowhere but in a memory. So `dataset.json -> derived_from` is written by `register`
and checked against the **run's own `lineage.parents`** — `provenance: "run"` vs
`"claimed"` is the `/ask-human` split one domain over.

**Its one hard refusal is an output root overlapping a source location.** An
in-place transform rewrites bytes that frozen snapshots still name, and every one of
those citations goes on resolving.

## Freeze: the boundary to the model lifecycle

**Freeze is the boundary**, and it is crossed in two moves that must not be
confused.

`snapshot` pins **membership** — N specific unit ids against a dated census — and
`datasets/<id>@<snapshot>` in `lineage.parents` is what makes data and models one
graph instead of two.

`resolve` then joins that against one location's root and the layer markers to
produce **openable paths** for a dataloader, because a manifest carries location
keys and cannot open a file. The resolved view is scratch that lives beside the
consuming run and is refused inside `snapshots/`: dataset records are
machine-independent on purpose, and a resolved path names a machine.

**Standardising the manifest is free and asks nothing of the user's code.**
Standardising the *bytes* into one shard format is a **curate** run producing a new
dataset, never a thing Freeze does — it would make freezing cost terabytes and still
require the dataloader to change.

## Retire: the only delete on this line

**Retire is the only thing in MLClaw that deletes data**, so it is `plan → apply`
against evidence like `retention.py`, with the bar raised: a checkpoint deleted by
mistake costs a retrain, a capture deleted by mistake is gone.

`plan` ranks every unit by **what survives the deletion** and excludes the ones that
would not — cited by a live snapshot, under `min_source_copies`, never archived, or
the last copy anything claims finished — each waivable by name and none by default.
Two refusals have no override: a partial census (the machine that did not answer may
be the survivor the plan assumes) and an unreachable target.

**`apply` is allowed to delete because of one rule: a path is deletable only if a
census listing enumerated it**, never one assembled from config. `locations[].root`
is a string somebody edits; a unit id is something a scan came back with. Joining
the two and trusting the result is how a typo in `root` becomes `rm -rf /`.

**The record goes down before the first `rm`, and it lives one level above what it
deletes** — in the project, git-tracked, on a different machine from the bytes — so
a deletion cannot take its own record with it. A crash in between leaves a record
saying what was being deleted, which is the whole reason for the ordering.

Waiving a snapshot citation stamps `data_retired` into that snapshot: the citation
still resolves *and says the bytes were freed at that location*. The alternative is
the user running `rm` outside the tool, where nothing is recorded at all. What the
stamp does **not** say is that the data is gone — that needs a census taken since;
`repro.py` does the join, and `/repro` references/axes.md states the verdicts.

## What is not a phase

**Archive.** `UNARCHIVED` is a per-unit *condition* tracked from Collect onward —
the risk peaks on the capture machine, before anything else has happened. Reading it
as a final step yields "archive when done", which is the wrong operational
instruction and the expensive one.

**Train.** It consumes a frozen snapshot; that is the model lifecycle. The two meet
at `lineage.parents` and do not compose across.

**`/data-check` and `/data-report`** are cross-cutting like Archive: every phase
reads the census, and the board renders all of them.

**`/data-online-sample`** is cross-cutting for a different reason — **it does not
observe this line at all.** It reads the live input stream, so its readings sit under
a dataset (`datasets/<id>/online/`) while describing something that never enters it.
It moves no bytes and it is not Collect: the biased pull that *does* move bytes is
`/data-collect`, citing a reading from here as its denominator. Two chains, opposite
sampling policies, joined in one direction only.

## How /data composes the line

**`/data` is the only thing that knows a dataset's *position***: the five records
that decide it — census, handoffs, snapshots, retirements, citing runs — live in
three directories and no other skill reads them all. `census.py` sees only the
world's physical state; `/data-label` sees one exchange; `/data-retire` sees one
deletion. None is wrong; each is looking at one box.

**It composes through the Skill Dependency Graph and never through a second
mechanism of its own**, by two doors only — forward through the phase's
`next_skill`, or sideways through a blocker's `fix`. Both values come out of
`phase.py`, so there is one routing table rather than a copy in the skill that
drifts.

**It stops at every gate, and `/data-retire` is never suggested by composition.** A
router that offers to free 400GB has made a deletion into a step on the way to
something else, which is exactly what `plan → apply` exists to prevent.
`/data-freeze` is on the forward path and still runs `gate --to freeze` when reached
that way.
