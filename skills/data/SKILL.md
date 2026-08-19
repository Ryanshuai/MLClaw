---
name: data
description: >
  Say where a dataset sits on the data lifecycle — Collect, Label, Curate, Freeze — what is
  blocking it, and what to do next; and refuse a transition whose preconditions fail. Trigger for:
  asking what state a dataset is in, what to do next with it, whether it is ready to train on,
  whether a frozen snapshot is still current, and why a dataset is stuck. Also trigger for Chinese
  requests like "这个数据集到哪一步了", "数据能开训了吗", "这批数据卡在哪", "还差什么", "快照还是最新的吗",
  "下一步该干嘛". Not for scanning what physically exists (use /data-check) or sending data out
  (use /data-label).
---

# /data — the lifecycle router

```
 Collect   →   Label    →   Curate    →   Freeze    →   Retire
/data-collect /data-label /data-curate /data-freeze /data-retire
```

**A dataset's position is a join nothing else can perform.** It needs five record types living in
three directories — `datasets/<id>/census/`, `datasets/<id>/snapshots/`, `datasets/<id>/retire/`,
`{PROJECT}/handoffs/`, and `stages/*/runs/` — and no existing skill sees them all. `census.py`
deliberately sees only the world's physical state; `/data-label` sees one exchange; `/data-retire`
sees one deletion. None is wrong. They are each looking at one box on the line.

**This is a router, not a dashboard**, and the distinction is load-bearing — README says outright
MLClaw has no dashboard. It earns its place on three facts that are silent today, not on narration:

| Fact | Why nothing else can say it |
|---|---|
| **phase** | the four-way join above |
| **staleness** | a snapshot whose census never saw inflow that has since been accepted |
| **gates** | transitions whose preconditions no skill enforces |

If you find yourself using this skill to *describe* state the user can already read from
`/data-check`, stop — that is the dashboard failure mode, and `census.py show` is the right tool.

## The script

```bash
S=<mlclaw_root>/scripts/data/phase.py
python $S phase --project <p> [--dataset <d>] [--stale-days N]
python $S gate  --project <p> --dataset <d> --to freeze|curate|consume [--acknowledge <n>]
```

Per CLAUDE.md "Script Integration": exit 2 means the script broke — do the work by hand and
continue. **Exit 1 means it worked and the answer is no**; every exit-1 here is a gate refusing, so
redoing it by hand is overriding the check rather than falling back. `phase` never exits 1 —
reporting a bad position is not a refusal.

## Reading a phase

The answer is always **the earliest unfinished box**, not the most alarming finding. That ordering
is deliberate: a dataset with both incomplete units and a stale snapshot needs the units first, and
leading with the scarier item sends people to fix the wrong end.

| Phase | Means | `next_skill` | `next` |
|---|---|---|---|
| `collect` | no census, no units, or units nothing claims are finished | `/data-check` | let capture finish, then `census.py scan` |
| `label` | a handoff is still out, or a GAP layer whose `produced_by` names one | `/data-label` | `handoff.py status --open-only` |
| `curate` | a layer is missing everywhere | `/data-curate`, or `/<stage>-run` when `produced_by` is `run:<stage>` | whatever `layers[].produced_by` names |
| `freeze` | everything present and complete, but nothing frozen — or the newest freeze is stale | `/data-freeze` | `census.py snapshot` |
| `ready` | a current snapshot exists | *(none)* | cite `datasets/<id>@<sid>` in `run.json -> lineage.parents` |

**Two `next` fields, because they answer different questions.** `next` is what to type; `next_skill`
is what this skill *composes*. Composition goes through the Skill Dependency Graph and nothing else,
so a phase that could only name a shell command would be a phase `/data` cannot route.

`ready` naming no skill is deliberate, not an omission: the data line is finished with the dataset
and what happens next is the model lifecycle. The two meet at `lineage.parents`; they do not compose
across.

**`retire` is never returned as a position**, and that is not because it is unbuilt — it is because
retirement is an action on units, not a state a dataset arrives at. A dataset that has had forty
days deleted off the rig is still `ready`, decided by the units it has left. So the `retire` block
reports what *has* been deleted (`units_deleted`, which risks were waived, and why) rather than a
phase, because a census counts what is left and says nothing at all about what used to be there.

**A GAP routes by the layer's own `produced_by`**, which is where `/data-check` already sends
people. Whether a missing layer is Label work or Curate work is a property of that layer, not
something to re-derive per call.

## Staleness — the subtle one

A snapshot is stale when **the census it froze from** never saw inflow that has since been
accepted. Note what is being compared: not the snapshot's `frozen_at`.

A snapshot's contents come from its originating scan. Freezing at 5pm off a census scanned
yesterday cannot include labels that landed at noon, however recent the freeze timestamp looks.
Comparing freeze times reports exactly that snapshot as current — the failure this check exists to
catch, inverted. The comparison is `census.scanned_at` vs `handoff.closed_at`, via
`snapshot.json -> from_census`.

Why it has to be checked at all: **the stale citation still resolves.** `datasets/boxes@260731`
still names a real frozen set, downstream still reads it as authoritative, and "we trained on the
latest data" is false with nothing raising anywhere.

Two orderings are not staleness and must not be reported as clean either:

- **equal timestamps** — the scan and the acceptance are indistinguishable at one-second
  resolution, so the ordering is unknown. Unknown counts as stale.
- **a timestamp missing or without a UTC offset** — reported as `staleness_undetermined`, its own
  blocker. Recording it as "not stale" would be the extraction-failure-vs-absence bug from
  `<mlclaw_root>/references/run-mechanics.md` "Record integrity", committed by the checker itself.

## Gates

`gate --to <transition>` exits 1 when a precondition fails. Each blocker names the one transition
it stops, because a gate that fires on everything trains people to pass the override.

| Transition | Refused when | What it protects |
|---|---|---|
| `freeze` | a handoff for this dataset is still open | the frozen set excludes work that is about to land, and nothing in the snapshot says so |
| `curate` | a source layer is `UNREPLICATED` | the derivation succeeds and the thing it derived from is still one disk failure from gone |
| `consume` | the snapshot is stale or undetermined | see above |
| *(any)* | no census, or the census is `complete: false` | every count is a lower bound; a partial census is not an inventory |

`--acknowledge <n>` proceeds anyway, and `n` must equal the blocker count measured **now** — the
same restatement pattern as `--accept-partial` and `--allow-incomplete`. It binds the override to
what this assessment found rather than to a number remembered from a previous one.

Overriding is the right call sometimes: reproducing an old run legitimately consumes a stale
snapshot. What it is not is a step to type past on the way to the outcome you wanted. When you
reach for it, say what is being accepted in the same breath.

## Composing the line

This is the skill that runs the others, and there are exactly **two doors into them** — which is
the structure, not a list. Everything reachable from `/data` comes through one of these.

**Forward, through `next_skill`.** The dataset is fine and simply has further to go. Take the value
from the script; never re-derive it here, or there are two routing tables and one of them is wrong.

**Sideways, through a blocker's `fix`.** The dataset is stuck, and what unsticks it is usually a
different skill from the one the phase names:

| Blocker | Who fixes it |
|---|---|
| `no_census`, `census_incomplete`, `census_stale`, `unarchived_unchecked` | `/data-check` — go and look |
| `unarchived`, `unreplicated` | `/data-collect` — pull it off the machine that is holding the only copy. This is the case where "ingest only" is exactly right: copying *toward* the authority is the fix, copying away from it is not |
| `inflow_in_flight` | `/data-label` — it is out with someone; `status --open-only` says who and for how long |
| `snapshot_stale` | `/data-check` scan, **then** `/data-freeze`. Both, in that order — re-freezing off the same census reproduces the staleness |
| `staleness_undetermined` | nobody. A timestamp with no UTC offset is a repair, not a workflow step |
| a `collect` phase whose units nothing claims are finished | often `/ask-human` — "is 260731 done shooting?" is a question for the operator, and its answer is a `claim` until a census agrees |

**`/data-report` is not on either path.** The board renders every dataset at once; `/data` answers
about one. Offer it when the user's question is plural — "what is the state of everything" — and
route to `/data` when it is singular.

### The one it will not compose

**`/data-retire` is never suggested by this skill.** Not because it is unfinished — because
composition must not propose an irreversible action. A router that says "you could free 400GB here"
has turned a deletion into a step on the way to something else, and the plan-then-apply split exists
precisely to stop deletion from ever being that. `/data-retire` is entered because a human decided
to free space, and it then demands its own evidence.

The same rule with a lighter touch applies to `/data-freeze`: it is on the forward path, so `/data`
does route into it — but reaching it through composition earns no credit. `gate --to freeze` still
runs, and an open handoff still refuses.

## Requires / suggests

- **Requires**: `project.json`, and at least one `datasets/<id>/dataset.json`. If none exists,
  `/data-check` Step 1 declares the layout contract — send the user there, don't invent one.
- **Suggests**: whatever `next_skill` names, or the blocker's owner from the table above. Both come
  from the script's output so this file does not carry a second copy of the routing table.

Per `<mlclaw_root>/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. Use `stage: null` and
`execution: null` — like a census, an assessment is a dated observation, not an execution to
resume. **Pop before reporting**, or a routing question leaves a false resume prompt behind it.
