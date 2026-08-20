---
name: roll-call
description: >
  Go down the list of machines that exist and make each one answer for itself: whose is it, what
  is it for, and is that still true. Human-invoked and it releases NOTHING — the output is a
  register plus a set of open questions. Trigger for: "这些机器都是谁的", "哪台是我的",
  "还在用吗", "问问他们还用不用", "点名", "谁开的这台", "这台还要留着吗", "帮我认领一下",
  "记一下这台是干嘛的", "explore 那轮用了哪几台机器", "whose box is this", "is anyone still
  using that GPU", "who opened this instance", "register this machine". Also trigger before
  proposing ANY teardown on a shared account — an unaccounted box and a forgotten box look
  identical and only one of them is yours. Not for finding out what is billing (that is /lease
  reap) and not for getting work off a box before it dies (that is /evacuate).
---

# /roll-call — make every machine answer for itself

`/lease reap` answers **what is burning money**. This answers **whose it is and whether they
still need it**, and those are different questions with different evidence behind them. Running
the first and acting on it is how a colleague's live training box gets proposed for deletion.

Two things happen here and neither is automatic:

1. **Register** what a machine is for — `lease.py claim`. A sentence in the ledger; nothing on
   the box moves.
2. **Ask** whoever holds it whether that is still true — but only after everything a machine
   could answer has been read, and never as a reclaim.

**This skill releases nothing and never will.** Not as an option at the end, not with
confirmation. Releasing is `/lease release`, and `/evacuate` comes before it. A management pass
that can also destroy is a pass nobody runs with a wide scope.

## The four states, and why only one of them is yours

`lease.py status --attribute` and `lease.py reap --attribute` stamp every swept row with which
of these accounts for it. The vocabulary is `lease.py`'s (`HOLDS`); the judgement about what to
do is here.

| | evidence | what you may do |
|---|---|---|
| `held` | an open lease row in **this** ledger | yours. Normal release path |
| `claimed` | somebody wrote it down — **a claim, never verified** | ask before touching. Check `review_at` |
| `attributed` | the provider's lifecycle log names who **created** it | not yours. And creation ≠ current use |
| `unaccounted` | swept, nothing named a holder | **not yours either.** This is the one that gets misread |

‼️ **`unaccounted` is not `orphan` and is not `unowned`.** It has four causes and they are not
interchangeable — `unaccounted_why` says which:

- `not_attributed` — **nobody asked.** You did not pass `--attribute`. Re-run; do not report.
- `attribution_unsupported` — this provider has no lifecycle log at all. On a shared static
  key **nothing distinguishes a colleague's box from yours**, and no amount of looking will.
- `no_create_event_in_window` — looked, did not find. The box predates the window. Widen it
  with `--attribute-window-s` before concluding anything.
- `attribution_unreached` — the log did not answer. Say so; a timeout is not an absence.

The same three-facts-not-two split `census.py` keeps between a machine that did not answer and
a directory that is genuinely empty, and `/discover` keeps between `gone` and `unreachable`.

‼️ **The tag proves MLClaw made it, not that you did.** `TAG_PREFIX` is a property of the tool.
Two people running MLClaw against one tenant produce boxes nobody can tell apart by prefix, and
each ledger sees the other's as untracked. That is why `--attribute` is not optional on a shared
account: without it every one of their boxes is `unaccounted`, sitting next to yours.

## Step 1 — sweep wide, and say how wide

```bash
L="<mlclaw_root>/scripts/lease/lease.py"
python "$L" status --attribute
python "$L" reap --attribute            # add --tag-prefix "" to include boxes MLClaw never made
```

**Read `scope.complete` and `attribution` before quoting any count.** They answer two different
questions and the report must keep them apart:

- `scope.complete: false` → some corner did not answer. **Every count is a lower bound** and
  has to be said as one.
- `attribution: {"asked": false}` → nobody ran the ownership join, so `attributed_to_others:
  null` means *nothing was asked*, not *everything here is yours*.

The default `--tag-prefix` scopes the sweep to MLClaw's own boxes. **Widening it to `""` is how
you see the whole account**, and it is also how other people's machines enter the picture — so
widen it and attribute in the same breath, never one without the other.

## Step 2 — register what you know

For every row you can account for and the ledger cannot:

```bash
python "$L" claim --provider nebius --instance-id <id> \
    --purpose "explore arm 3 — dinov2 backbone port, ablating the fusion head" \
    --holder shuai --project boxseg --run explore_arm3 --review-days 3
```

- **`--instance-id`, never a name.** Names are mutable and reusable; a failed box deleted and
  its name reused an hour later makes a name-keyed claim report the *live* machine as somebody's
  released one — confidently, and inverted.
- **`--purpose` is a sentence somebody else can act on**, not a label. "training" tells the next
  reader nothing they did not already know from the GPU being busy.
- **`--review-days` defaults to 3 and 0 means never.** A claim with no review date ages into
  furniture: it goes on saying "in use" for a box abandoned in March.
- Claiming a box already claimed **refuses**. `--supersede` replaces it and keeps the old row.
  Refusing is deliberate — silently overwriting somebody's "this is mine" is this skill's own
  failure mode, one layer in.
- A box under an open lease **cannot** be claimed. `held` is stronger evidence than a claim, and
  a claim on top would give "what holds this box" two authors. Use `use` for that (Step 5).

## Step 3 — probe before you ask anybody anything

**A value you can read is never a question** (CLAUDE.md). Every question filed that a command
could have answered spends somebody's attention for nothing, and teaches them to skim the next
one. So for each row, read what is readable:

| Question | Read it from |
|---|---|
| is the owning run still going? | `stages/*/runs/*/run.json -> status`, via `lease.py whose --instance-id <id>` |
| is the box even up? | `lease.py addr <lease_id>`, or the sweep row's `state` |
| is anything computing on it? | `nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv` over ssh |
| has anything been written lately? | newest mtime under the work dir |
| how long has it been like that? | the sweep row's `age_s`, and `created_at` |

Report what each probe **said**, and where a probe could not run, report that as its own fact —
an unreachable box is not an idle one. A box nobody can reach is a stronger reason to ask a
person, not a weaker one.

‼️ **Idle is evidence, not a verdict.** A GPU at 0% is equally consistent with a dead job, a
staging step, a debugging session with a breakpoint in it, and someone at lunch mid-experiment.
Present the reading; never convert it into "not in use".

## Step 4 — ask, for what nothing could answer

Only now, and only for rows where the probes came back short:

```bash
python "<mlclaw_root>/scripts/ask-human/ask.py" open --project <PROJECT> \
   --to "<operator or holder>" \
   --asked "nebius <id> (8×H100, $X/hr, up 4d, GPUs idle 19h) — still need it?" \
   --why "it bills $X/hr and nothing accounts for it; nobody will release it without you" \
   --verify "ssh <host> nvidia-smi --query-gpu=utilization.gpu --format=csv" \
   --kind question
```

- **`--to` comes from the evidence, not from a guess.** `attributed` gives you the log's actor;
  `claimed` gives you the holder. `unaccounted` gives you **nobody** — and inventing a plausible
  addressee there is worse than filing nothing, because the answer that comes back will be
  someone else's guess recorded as a fact.
- **Put the money and the idleness in the question.** "Still using it?" gets shrugged at; "it
  has burned $340 and no GPU has moved in 19 hours" gets answered.
- **`--verify` is the probe from Step 3.** Its presence is what lets the answer ever be more
  than hearsay; its absence is exactly why `/ask-human` refuses `verified`.
- **Nothing is sent.** `ask.py` records the question; the channel is the user's. Never message
  an external party unprompted.

When the answer comes back: `ask.py answer`. ‼️ **"He said he's done with it" is a `claim`.**
It is enough for a *person* to decide to release, and it is not evidence that the disk is empty
— `/evacuate` still computes clearance, because the checkpoint on that box does not care what
anybody said.

## Step 5 — record what ran where, so the register survives the session

```bash
python "$L" use <lease_id> --run <run_id> --outcome ok      # pool.py does this per trial
python "$L" whose --run explore_arm3                        # which machines that work used
python "$L" whose --instance-id <id>                        # what accounts for that box
```

`up --run` stamps **one** run at create. A pooled box drains twelve trials under the first
one's name, and the other eleven live in `pool.json`, which dies with the search. `use` pushes
each finished trial down into the ledger so the register outlives the session that built it.

**`whose` never touches the network**, and that is what makes the second direction answerable
*after every box is gone* — which is when somebody reconstructing a round actually asks. Its
silence therefore means "this ledger was never told", never "that machine was not used".

## Step 6 — report

State, in this order:

1. **Scope first.** How wide the sweep went, whether it was complete, and whether attribution
   was even asked. A count before these is an inventory pretending to be one.
2. The four buckets with counts, `held` first and `unaccounted` last, each row carrying its
   evidence — never a bare list of ids.
3. `review_due` claims: registered, and past the date somebody said to re-check.
4. What you filed, to whom, and what is still open (`ask.py status --open-only`).
5. **What you did not do.** You released nothing. Say it, and name what a release would need:
   `/evacuate` first, then `/lease release`, and only on rows in `held`.

## Refusals

- **Never propose a teardown for a row that is not `held`.** Not with a confirmation prompt,
  not "just checking" — proposing is how it happens. For those, the output is a question.
- **Never fill an unknown holder with a plausible name.** Creation time is not ownership;
  neither is "it's on your project", "it's your machine type", or "who else would it be".
- **Never report `unaccounted` as `orphan`, `unowned`, or `idle`.** Four different sentences.
- **Never call an answer `verified` because a person was confident.** `/ask-human` enforces it;
  do not route around it by writing the claim into `--purpose` as though it were settled.
- **Never widen the sweep without attributing.** That combination — everybody's boxes, nobody's
  names — is the exact input that produces a wrong kill.
