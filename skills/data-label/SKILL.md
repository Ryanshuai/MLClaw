---
name: data-label
description: >
  Send work to a party MLClaw does not control — an annotation vendor, a colleague who owns the
  data, a reviewer, a customer signing off on results — and verify what comes back against a
  manifest frozen at send time. Trigger for: sending a batch out for labeling, asking someone for
  data, sending a checkpoint or eval report out for review or acceptance, checking what is still
  outstanding, and taking delivery when the other side says it is done. Also trigger for Chinese
  requests like "这批数据要送去标注", "标注回来了导一下", "催一下那批标注", "还有什么在外面没回来",
  "数据要发给供应商", "让他们重标". Not for renting a machine (use /lease) or discovering
  credentials (use /resources).
---

# /data-label — the exchange layer

Every other MLClaw skill closes its own loop: it starts the process, watches it, reads the
result. The completion signal is evidence MLClaw produced itself. This is the one skill where
**the loop is closed by somebody else**, and the completion signal arrives as a *claim* — "it's
labeled", "done, see the link", "uploaded to the share". Everything below follows from that.

There is no external authority to check the claim against. `lease.py reap` can ask the cloud API
what is actually running; nothing here can ask the vendor what they actually did. **The manifest
frozen at send time is the only authority**, which makes send-time rigor load-bearing in a way it
is nowhere else in MLClaw.

A handoff is **not a stage** — it is an edge any stage can hang off. Annotation hangs off data,
human eval off evaluation, acceptance off delivery. That is why records live at
`{PROJECT}/handoffs/` and not under `stages/`.

## Three records, not one

An outsourced labeling job is three separate facts with three different lifetimes, and
collapsing them is the mistake this section exists to prevent. None of the three is new
machinery — each slots into a shape MLClaw already has.

| Fact | Lives in | Existing shape it copies |
|---|---|---|
| **The party** — vendor-a: contact person, email or IM handle, typical turnaround, rate | `{WORKSPACE}/resources.json -> outsourcing.<key>` | `resources.servers.<key>`. A route to a capability you don't own, registered once, reused by every batch. Workspace-level: a vendor is not project-specific. |
| **The exchange** — this batch: sent, awaiting, coverage 0.94, round 2 | `{PROJECT}/handoffs/<handoff_id>/` | A run record. Stateful, project-scoped, has a lifecycle. |
| **The result** — the accepted labels, as something a stage consumes | `input.json -> candidates.<item>[]` with `location: "handoff:<id>"` | An ordinary source candidate, alongside `local` / `s3` / `server:<key>`. |

**The vendor is a resource; the batch result is a source; the exchange is a record.** Asking
"is outsourcing a source?" the answer is yes for the third row and no for the first two — and the third
row is where it matters, because that is the one every downstream skill already knows how to read.
Schema and resolution rules: `/train-init` references/schemas.md, `input.json / artifacts.json -> candidates`.

**Contact details never leave `resources.json`.** A handoff record stores the `outsourcing` *key*,
and the contact is resolved live whenever it is reported. Two reasons, and the second is the
binding one: `resources.json` is the never-committed file and `{PROJECT}/handoffs/` is git-tracked,
so copying a person's phone number into a handoff record commits personal data to the project repo.
Same rule as `/lease` "Safety" — never write a resolved address into a config file.

When the user names a party you have not seen before, offer to register them
(`/resources` owns that block). A one-off — someone on the next team over — does not need a registration; pass the
plain name to `--to` and move on. Recurring vendors do, because `turnaround_days` is what makes
`--due` a real date instead of a guess, and because the second batch should not re-ask for the
email.

## The script

```bash
S=<mlclaw_root>/scripts/data-label/handoff.py
python $S send    --project <p> --source <dir> --kind <k> --to <who> [--spec <f>|--no-spec] ...
python $S receive --project <p> --id <hid> --returned <dir> [--match-by stem|path|name]
python $S close   --project <p> --id <hid> (--accept | --reject | --cancel) [...]
python $S status  (--project <p> | --workspace <w>) [--open-only] [--stale-days N]
python $S show    --project <p> --id <hid>
```

Per CLAUDE.md "Script Integration": exit 2 means the script broke — do the same work by hand and
continue. **Exit 1 means it worked and the answer is no.** Every exit-1 path in this script is a
record-integrity check; redoing it by hand is not a fallback, it is overriding the check. The
refusals are listed under "The four refusals" below with what each is actually protecting.

## Step 1 — Send

One question at a time, and **only what only they know** — a value you can read is not a question, and a value nobody has is recorded absent rather than asked for: CLAUDE.md "Decide what evidence can decide". What the script cannot infer and you must establish in dialogue:

| Ask | Why it can't be defaulted |
|---|---|
| **What exactly goes out** — a directory, and `--include` / `--exclude` if it is a subset | The manifest is the definition of the batch. "the training images" is not a set. |
| **To whom** (`--to`), and through what channel (`--channel`, `--channel-ref`) | The channel_ref is how you find the thread three weeks later when nobody remembers who was asked. |
| **The spec** (`--spec`) — the labeling guideline, review criteria, acceptance conditions | See below. This is the one the user will want to skip. |
| **When it is due** (`--due`) | Without it, `status` can report age but not lateness, and nothing ever becomes overdue. |
| **What produced this** (`--parent`) | If a checkpoint or a report is going out, the run that made it is a lineage parent. |

**The spec is not paperwork.** Two annotation batches labeled under different guidelines are
different distributions. Train on the union and the model degrades with nothing raised anywhere —
the identical failure shape as `references/run-mechanics.md` "Preprocessing contract (cross-stage)", one domain over.
`send` refuses without either `--spec` or an explicit `--no-spec`, so that its absence is
something someone typed rather than something that just didn't happen. When the user has no
written spec, the right move is usually to write one with them in three lines and snapshot that —
not to reach for `--no-spec`.

**MLClaw does not move the bytes.** The channel is the user's (an object-store link, a shared drive, an IM file transfer, a NAS mount,
a labeling platform). `send` records what left and freezes its checksums; the human does the
transfer. Say this plainly rather than implying an integration exists.

**Before sending anything outward, look at what is in it.** Sending data to an external party is
outward-facing and effectively irreversible — the general rule in the harness prompt about
confirming outward actions applies with full force. Two things worth a look every time: personal
or customer-identifying content in the payload, and credentials/tokens in any config or notebook
swept up by a directory-wide `--source`. Neither is MLClaw's to decide, both are the user's to
know before it leaves.

## Step 2 — While it is out

The wait here is days to weeks and survives every session boundary — unlike a run's wait, which
is minutes to hours and dies with the process. So nothing about it can live in conversation
memory.

`status --open-only` is the whole mechanism. Report per handoff: party, kind, count,
age, due/overdue, round. A handoff older than `--stale-days` (default 14) with no return is not
"pending", it is **a stalled project nobody is tracking** — that is the failure this skill exists
to prevent, and it is the reason for the entry in CLAUDE.md "On Conversation Start".

Chasing is the user's job, not MLClaw's. Surface it, name who owes what, offer to draft the
message if asked. Do not send anything on the user's behalf to an external party.

## Step 3 — Receive

`receive` computes, it does not accept. The split is deliberate and mirrors `retention.py`
plan → apply for the same reason: the irreversible half must be a separate, evidenced decision.

Two things need your judgement before the call:

- **`--match-by`**. Default `stem`, because a labeling job returns `000123.json` for the
  `000123.jpg` you sent. Use `path` when the return mirrors the sent tree, `name` when extensions
  are preserved. Getting it wrong shows up immediately as near-zero coverage — read the counts
  before believing them.
- **Drift checking**. On by default: it re-hashes the source files to catch the case where the
  local data changed while the batch was out. Skipping it is fine for a huge batch on slow
  storage, but it records `source_drift_checked: false` and will block `close --accept` until
  someone acknowledges it. That is the point.

Then read the reconciliation *out loud* to the user, in this order — the counts are the finding,
not a preamble to it:

```
sent 5000 · matched 4712 · missing 288 · unexpected 3 · ambiguous 0 · coverage 0.9424
```

- **`missing`** — named, in the round's `reconciliation.json`. Partial return is the normal case
  ("these were too blurry to label, skipped"), not an error state. It is only a problem when it becomes invisible.
- **`unexpected`** — they returned work for something you never sent. Usually a stale batch on
  their side, occasionally a sign the wrong data went out. Always worth one question.
- **`ambiguous`** — one match key hit multiple files on either side. Never resolved by picking
  one; fix the match strategy or the return and re-run.
- **`source_drift`** — the pairing is broken for those items. The returned labels describe bytes
  that are no longer on disk here, and the filenames still match, so nothing downstream can see it.

## Step 4 — Close

`--accept`, `--reject`, or `--cancel`. Rejecting is not a failure state — it opens the next
round, and `send --rework <id>` carries the deficit forward as its own manifest, so the second
batch out contains exactly what the first one did not bring back.

### The four refusals

None of them is an "are you sure". Each demands a specific piece of evidence, because a
confirmation prompt carries no information about whether the thing being confirmed is right.

| Refusal | What it protects |
|---|---|
| `--accept` on coverage < 1.0 | A partial return that closes as plain "accepted" becomes a full-coverage artifact in every downstream record. |
| `--accept-partial <n>` where n ≠ measured coverage | Binds acceptance to the number actually measured this round, not one remembered from the last one. |
| `--accept` with drifted sources | The returned work is paired to bytes that changed. Nothing downstream can detect it. |
| `--accept` when drift was never checked | "Not checked" is not "clean". Accepting here records an unverified pairing as a verified one. |

`--accept-partial`, `--accept-drift`, `--accept-unchecked-drift` all exist, and all are the right
call sometimes. What they are not is a step to type past on the way to the outcome you wanted —
when you reach for one, say what is being accepted in the same breath.

## After acceptance — handing it to the stage that wanted it

The exchange is over; what remains is a source. Write it into the consuming stage as
`source: "handoff"` — schema, required fields, and the reason each one exists are in
`/eval-init` references/schemas.md, `source: "handoff"` — data that came from outside. Don't
restate them here; a second copy is a copy that drifts.

The one thing that belongs in this file, because it is about the moment of acceptance rather than
about the schema: **`accepted` is the only status a stage may draw from.** A `returned` handoff has
a reconciliation but no decision, and its data on disk is whatever the vendor happened to upload.
If a user points a stage at a handoff directory before close, that is the bug this skill exists to
catch — say so and finish the close first.

Everything else follows from the schema: `spec_version` and `coverage` travel into the source
entry, and the consuming run cites `handoffs/<handoff_id>` in `run.json -> lineage.parents`.

## Requires / suggests

- **Requires**: `project.json` exists. Nothing else — a data request can precede every stage,
  which is the point of it being project-level.
- **Suggests**: on `close --accept`, the stage that will consume the data — `/train-init` or
  `/eval-init` if the stage is unconfigured, `/train-run` or `/eval-run` if it is. On
  `close --reject`, `send --rework`.

Per `references/skill-graph.md` -> "Workflow State Protocol", push to the stack on entry and pop on exit. Use
`execution: <handoff_id>` and `stage: <the stage it serves, or null>`; `step` is one of
`send` / `await` / `receive` / `close`. A handoff sitting at `await` is the expected long-lived
state and is **not** unfinished work to resume — the stack entry should be popped after `send`
and re-pushed at `receive`, or every session for the next three weeks opens with a false resume
prompt.
