---
name: data-retire
description: >
  Delete data against evidence, and leave a record that outlives what it deleted — rank every unit
  by what would survive the deletion, exclude the ones that would not, then plan → apply. Trigger
  for: freeing space on a capture rig or NAS, deleting old captures, cleaning up after data was
  copied to the authority, and asking what has been deleted before. Also trigger for Chinese
  requests like "采集机满了", "能删哪些", "清一下旧数据", "这批数据还需要留着吗", "删过什么".
  Not for deleting checkpoints (that is retention.py) and not for scanning (use /data-check).
---

# /data-retire — the irreversible box

Last on the line and the only irreversible one on it. `retention.py` is the precedent and this
follows it — `plan` → `apply`, with the plan carrying the numbers — but the bar is higher in one
respect that decides the whole design: **a checkpoint deleted by mistake costs a retrain; a capture
deleted by mistake is gone.** 260731 cannot be re-shot.

```bash
S=<mlclaw_root>/scripts/data-retire/retire.py

python $S plan  --project <p> --dataset <d> --at <location key> \
                [--unit U ...| --units-from <f>] [--waive <reason>] [--because "..."]
python $S apply --plan <f> --confirm <token from the plan file>
python $S log   --project <p> [--dataset <d>]
```

Exit 2 = broke, do it by hand. **Exit 1 = worked, the answer is no.** Every exit-1 here stands in
front of an `rm -rf`; redoing one by hand is not a fallback, it is deleting the data the check
refused to delete.

## The one nothing else can see

**A unit that a frozen snapshot still names.** Deleting it does not break the citation.
`datasets/boxes@260731` goes on resolving, the manifest goes on listing the unit, and every run that
cited it goes on reading as reproducible while it no longer is. Nothing anywhere raises.

That is the same silence as `/data`'s staleness check, one step more expensive: staleness makes a
run describe *older* data than it should; this makes it describe data that **does not exist.**

## Two refusals that have no override

| Refusal | Why there is no flag |
|---|---|
| **no census, or `complete: false`** | A partial census undercounts copies, which sounds like the safe direction and is not — the machine that did not answer may be the very survivor the plan is counting on. "2 copies remain" would then be a guess about a disk nobody could reach. CLAUDE.md "Never report data you could not look at". |
| **the target location was unreachable in that census** | The containment rule below means there is no listing to delete from. |

## Four exclusions, each waivable by name

A unit that fails one is **kept out of the plan with the reason named** — not an abort. "Delete the
40 days that are safe and keep the 3 that are not" is the normal outcome.

| Exclusion | What it means |
|---|---|
| `cited_by_snapshot` | see above |
| `below_min_copies` | deleting this copy drops a **source** layer under `replication.min_source_copies` |
| `unarchived` | never reached the authority — this copy is the only one there has ever been |
| `survivor_less_complete` | the copies that would survive carry no completeness marker while this one does. Deleting it destroys the only version anything claims finished. Computed from `census -> units[u].done_at`, so it is a set difference, not a guess. |

`--waive <reason>` accepts one named risk. **Waiving `cited_by_snapshot` is not the same as ignoring
it**: `apply` writes the loss back into the snapshot as `data_retired`, so the citation still
resolves *and says the data is gone*. The concession is recorded, never made invisible — which is
also why the refusal is waivable at all. Refusing outright would send the user to `rm` outside the
tool, where no record is written by anybody.

If nothing survives the checks, `plan` exits 1 with the reasons tallied. **"0 of 43 units are safe
to retire" is an answer**, not a failure.

## The containment rule

**A path is deletable only if a census listing enumerated it.** Never a path assembled from config
and hoped about.

`locations[].root` is a string in a JSON file somebody edits; a unit id is something a scan came
back with. Joining the two and trusting the result is how a typo in `root` becomes `rm -rf /`. So
`apply` resolves every path through a guard — no `..`, no absolute unit id, no empty or `/` root,
result must be strictly under the root after `normpath` — and **one bad join aborts the whole
apply** rather than deleting the rest and reporting a partial success.

Then it re-probes: every planned path must exist *right now*. A unit that vanished since the plan is
a refusal, not a silent success, because recording it as deleted would put a deletion in the log
this tool did not perform.

## The log lives one level above what it deletes

The rule from the field, and here it is at its most literal: the record is at
`{PROJECT}/datasets/<id>/retire/retire_<ts>.json` — git-tracked, on this machine — and the bytes are
on that one. **A deletion that can take its own record with it is a deletion nobody can audit.**

`apply` writes the record **before the first `rm`**, with `status: in_progress`, then rewrites it
after. A crash in between leaves a record saying what was being deleted, not silence. Three outcomes
are kept apart in it, because collapsing them is the same bug as everywhere else in MLClaw:
`deleted` / `failed` / `unreported` — a unit the shell never reported on is neither, and saying so
is the point.

## Reading a plan to the user

The counts are the finding, not a preamble:

```
43 considered · 40 to delete · 3 excluded
  cited_by_snapshot 2 · unarchived 1
freeing: /mnt/capture/boxes on rig   (role: origin)
```

Then, before `apply`, say plainly: what survives each deletion, and what is being waived. A
confirmation prompt carries no information about whether the thing being confirmed is right — the
plan does, so read the plan.

## What this never does

- **It does not decide that data is worthless.** Quality is not a survivability question and this
  skill has no opinion on it. It answers one thing: if this copy goes, what is left.
- **It does not touch checkpoints.** That is `retention.py` under `/train-run`, ranked by a metric.
- **It does not delete a dataset's records.** `dataset.json`, censuses and snapshots stay: they are
  the description of what existed, and they matter more once the bytes do not.

## Requires / suggests

- **Requires**: `datasets/<id>/dataset.json` and a **complete** census. No census, or a partial one,
  is a refusal with no override — that is `/data-check scan` first.
- **Suggests**: `/data-check scan` again afterwards, so the census reflects the disk; then `/data`,
  which will now report anything the deletion pushed below its replication floor.

Per `references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: null`,
`execution: <retire_id>`.
