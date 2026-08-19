---
name: data-freeze
description: >
  Freeze a citable snapshot of a dataset so a run can record exactly which units it consumed, and
  refuse to freeze one that cannot be trusted. Trigger for: pinning a training or eval set, cutting
  a dataset version before a run, and getting the `datasets/<id>@<snapshot>` string a run puts in
  its lineage. Also trigger for Chinese requests like "冻一个训练集", "把这批数据固定下来",
  "出个数据版本", "开训前先钉住数据", "这次训练用的是哪批数据". Not for scanning what exists
  (use /data-check) or for deciding where a dataset stands (use /data).
---

# /data-freeze — the boundary to the model lifecycle

Fourth box on the data line, and the only one that produces something the *model* side cites. Up to
here everything is about the data's own state; a snapshot is where it becomes a thing a run can name.

Crossing it takes **two moves, and confusing them is the mistake**:

```bash
S=<mlclaw_root>/scripts/data-check/census.py

# 1. pin MEMBERSHIP — the permanent, machine-independent claim
python $S snapshot --project <p> --dataset <d> --id <snapshot_id> \
                   [--layer <l>] [--at <location>] [--units-from <file>] \
                   [--allow-incomplete <n>]
→ datasets/<dataset_id>@<snapshot_id>

# 2. resolve it to OPENABLE PATHS on one machine — a derived, throwaway view
python $S resolve  --project <p> --dataset <d> --snapshot <snapshot_id> \
                   --at <location> [--layer <l>]... [--allow-missing <n>] [--out <f>]
```

**`snapshot` is what gets cited; `resolve` is what gets opened.** A manifest holds unit ids and
location *keys* — the right thing to freeze, and a thing no dataloader can open. Resolving is a
three-way join (manifest × `locations[].root` × `layers[].marker`) that only `dataset.json` can
perform, and the training side does not read `dataset.json`. Without the verb every consumer
reimplements that join, differently.

**The resolved view never enters the frozen record.** `dataset.json` is machine-independent on
purpose; a resolved path embeds one machine's root, and writing it into `snapshots/` would put a
machine inside the one record meant to outlive machines. Nothing is lost — snapshot + dataset.json
+ `--at` regenerate it byte for byte, which is exactly why a run cites `cite_as` and not the
resolved file.

Two refusals worth knowing on `resolve`: `--at` is **required and never defaults to the authority**
(picking a location silently is how a run trains off a copy nobody meant it to read), and a
location whose role is `backup` is refused outright — training off the backup is how a restore test
never happens.

**It does not convert, copy, shard, or hash.** "A standard structure for training" means two things
and only one is free: standardising the *manifest* costs nothing and asks nothing of the user's
code; standardising the *bytes* would make freezing cost terabytes, break "membership, not bytes",
and still require the dataloader to change. When the bytes genuinely must change, that is a
**curate** run consuming this snapshot and producing a new dataset — separately frozen, separately
citable, reproducible instead of a conversion nobody recorded.

**Resolved paths were true as of a census.** The header says which one and how long ago; `resolve`
stats nothing, because re-walking a multi-terabyte tree to reconfirm what `scan` established is the
cost `scan` exists to pay once. A resolve against a three-week-old census is a set of paths that
were real three weeks ago, and the consumer is told so rather than left to assume.

**It shares `census.py` with `/data-check` on purpose.** A snapshot is computed from a census —
`snapshot.json -> from_census` — so the code lives together while the skills stay apart: one
observes the world, one commits to a version of it. Skill boundary is not script boundary.

## Why it is a separate skill at all

Because a citation outlives everything around it. `datasets/boxes@260731_train` is what a training
run records, and it is read a year later by someone who cannot re-derive what was in it. Everything
else on the data line describes a state that can be re-observed by scanning again; **this is the one
step that makes a permanent claim.**

- **`--id` is an identity and is never reused.** A frozen set that changed under an existing
  citation is worse than no citation, because every run that cited it is now describing data that
  no longer exists in that shape. `snapshot` refuses a reused id outright.
- **A snapshot pins membership, not bytes.** No content hashes: the question is which units were in
  the set, and hashing a multi-terabyte tree to answer it would trade a real answer for one nobody
  waits for. Say this rather than letting someone assume byte-level pinning. (`/data-label` *does*
  hash, because there its manifest is the only authority for what a third party owes back — a
  different question.)

## The four refusals

None is an "are you sure". Each demands evidence, because a confirmation prompt carries no
information about whether the thing being confirmed is right.

| Refusal | What it protects |
|---|---|
| freezing against a **partial census** | The set pins what was seen; units on the machine that did not answer are silently not in it, and the snapshot cannot say so afterwards. |
| freezing units **nothing claims are complete** | A half-finished unit inside a frozen set becomes clean data in every record that follows. |
| `--allow-incomplete <n>` where n ≠ the measured count | Binds acceptance to what *this* scan measured, not a number remembered from the last one. |
| reusing a `--id` | See above — the citation silently starts describing something else. |

`--allow-incomplete` is the right call sometimes. It is recorded in
`snapshot.json -> unverified_units` and **travels forward** into everything that cites the snapshot,
which is the point: the concession is not a moment, it is a property of the data now.

## Before freezing, ask /data

`/data gate --to freeze` is the precondition check, and it catches the one thing a census cannot see
by itself: **an open handoff for this dataset.** Freezing while labels are still out pins a set that
excludes work about to land, and nothing in the snapshot records that it was cut mid-inflow.

The mirror of that check lives on the other side — `/data` reports a snapshot as **stale** when the
census it froze from never saw inflow that has since been accepted. Together they are the reason
this skill is worth separating: a freeze is easy to do at the wrong moment and impossible to notice
afterwards.

## After freezing

The consuming run puts the `cite_as` string in `run.json -> lineage.parents`, alongside
`training/run_...` and `handoffs/<id>`. **Never cite the dataset id alone** — a dataset grows, and a
citation that cannot say which afternoon is not lineage.

Two facts travel with it into the consuming stage's `input.json` source entry and must not be
dropped: the snapshot id and `unverified_units`. Schema: `/eval-init` references/schemas.md.

## Requires / suggests

- **Requires**: a `datasets/<id>/dataset.json`, and a census whose `complete` is true. If there is
  no census or it is partial, that is `/data-check scan` first — this skill refuses rather than
  freezing on a lower bound.
- **Suggests**: `/train-init` or `/train-run` for the stage that wanted the set; `/data` to confirm
  the dataset now reads as `ready`.

Per `<mlclaw_root>/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: null`,
`execution: <snapshot_id>`.
