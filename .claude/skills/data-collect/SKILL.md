---
name: data-collect
description: >
  Go to a named resource and bring data back into the project, recording what actually arrived.
  Point it at a server, an S3 prefix, or a mounted disk; it pulls, never pushes, and never
  overwrites by default. Optionally stamps the capture rig's facts into what was pulled. Trigger
  for: pulling data off a capture box or field machine, ingesting from S3 or a handed-over drive,
  checking what a pull would bring before running it, and listing what has been collected. Also
  trigger for Chinese requests like "把数据拉回来", "从那台机器上取数据", "导一下采集的数据",
  "看看能拉多少", "这批数据是什么时候收的". When the data is not there yet because a person has to
  go and capture it, that is /data-label, not this.
---

# /data-collect — bring the data in

First box on the data lifecycle. The operation is deliberately small: **name a resource, name a
path on it, pull.** What earns it a skill rather than a remembered `rsync` line is the record —
which resource, which session, how much arrived, and whether the transfer actually finished.

```bash
S=<mlclaw_root>/lifecycle/scripts/data-collect/collect.py
python $S plan   --project <p> --from <resource> --at <path> --into <dir>
python $S pull   --project <p> --from <resource> --at <path> --into <dir> [--session <s>] [--rig <r>]
python $S status --project <p> [--root <dir>]
```

`--from` is a key in `resources.json -> servers`, or the literal `s3` / `local`. Run `plan` first
when the source is large or unfamiliar — it is a dry run and costs nothing.

## Three rules the transfer obeys

**Ingest only.** One direction, always. Never pushes, never deletes at the source, never overwrites
an existing file unless `--overwrite` says so. A capture tree's source layers are irreplaceable,
and an ingest that quietly clobbers one has destroyed the thing it was rescuing.

This is *not* a contradiction of `/data-check`'s "never moves a byte" — worth being precise,
because it looks like one. That rule covers **bidirectional sync**, whose rules (refuse-if-exists
for source, newest-wins for derived) are every project's own and must not be reimplemented here,
and **deletion**, which is irreversible and needs `plan → apply` against evidence. A one-way copy
into a directory MLClaw controls is neither of those.

**The transfer is not ours.** `rsync` and `aws s3 sync` do the work. This decides what to invoke
and writes down what came of it.

**A transfer that did not finish is never recorded as one that did.** `pull` exits 1 and marks the
session `complete: false` when the tool exits non-zero. `files_added` is then a **lower bound** and
must be reported as one — re-run to continue. Partial transfers are the normal case on a flaky
field link, which is exactly why they must not read as success.

## Where people come in — and where they don't

**This skill never waits for a human.** When the data is not there yet because somebody has to go
capture it, visit a site, or post a disk, that is an exchange with a party MLClaw does not control:
**`/data-label`, `kind: data_request`**. It already owns the ledger, the counterparty registry
(`resources.json -> outsourcing`), the rounds, and the "what is still outstanding" report at
conversation start. A second waiting mechanism here would be a second, worse copy of that one.

`plan` makes the referral concrete: when the source cannot be reached it exits 1 and says so,
because unreachable is the finding that most often means a person has to act.

| Situation | Skill |
|---|---|
| The data exists on a machine you can reach | `/data-collect` |
| The data exists but somebody must connect / mount / power the machine | `/data-label` to ask, then `/data-collect` |
| The data does not exist yet — a site must be shot, a batch captured | `/data-label` (`data_request`) |
| It arrived and you want to know it is all there | `/data-check scan` |

A `data_request` handoff has **no frozen manifest to reconcile against** — the deliverable did not
exist at send time. Reconcile it against the *expectation* written in the spec (how many units,
which conditions), and say plainly that this is a weaker check than a manifest, because it is.

## Optional: stamp the rig

`--rig <id>` stamps a declared rig's reading into the pulled tree. **Most sources have no rig** —
an S3 prefix has no serial number — so this is optional and its absence is normal.

When the source *is* a capture rig it is worth it, for one asymmetry:

| A sensor swapped for a **dead** one | A sensor swapped for a **working** one |
|---|---|
| will not connect, found in seconds | connects, captures, looks perfect |
| `on_change: breaks` | its calibration anchor differs — every measurement off by a fixed ratio, forever |
| loud | `on_change: shifts` — **nothing raises, ever** |

The fact/tripwire vocabulary, and why a tripwire must never be the source of truth (watch a cheap
proxy like a serial; read the expensive anchor live via `runtime_only`), live in the template
`lifecycle/data/rig.json` and in `rig.py show | check | stamp` beside `collect.py`. A stamp failure
never fails a pull — the bytes are already in.

## Records

A session is written to `<into>/_collect/collect_<session>.json` — **beside the data, not beside
the project**, so it survives an rsync to a machine that has never heard of this project. That is
also why `status` takes `--root`: `--into` is often outside the project tree, and scanning only the
project would report "no sessions" for data that was in fact collected.

**The record names the resource key, never the address.** Host, username and key stay in
`resources.json`, the never-committed file; a collect record can be committed and cannot leak a
tailnet IP through it. Same split as `rig.json -> host.server` and `/data-label`'s counterparty.

## Requires / suggests

- **Requires**: `project.json`. A server source needs that server in `resources.json -> servers`;
  if it is missing, invoke `/resources` and resume.
- **Suggests**: `/data-check scan` — it is what turns "the pull said 400 files" into "the units are
  present at the authority", and it is what clears `UNARCHIVED`. Then `/data`.
- **Upstream, when the user cannot name a path**: `/discover` is what finds out where the data
  is; this skill pulls once somebody knows. A `verified` lead is exactly what `--from` / `--at` need.
  Don't go hunting from here — that is a sweep, and there is one.

Per `lifecycle/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: null`,
`execution: <session>`; `step` is `plan` / `pull`. A long pull is the one thing here that can be
interrupted — on resume just re-run `pull`, which is incremental by construction.
