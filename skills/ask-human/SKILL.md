---
name: ask-human
description: >
  Put a question or request to a person, track it until it is answered, and record the answer as
  the kind of thing it actually is — a claim, a verified fact, or their decision. Trigger for:
  asking an operator or colleague anything the pipeline is waiting on, requesting an action
  (mount a disk, power a box, re-shoot a site), getting an approval, chasing what has gone
  unanswered, and recording what somebody told you so it is not lost. Also trigger for Chinese
  requests like "问一下老李", "谁知道这批数据能不能用", "让现场把盘挂上", "催一下那个问题",
  "老王说可以用" , "有什么还没答复的". Not for sending artifacts out and reconciling a return
  (use /data-label) or for pulling data off a machine (use /data-collect).
---

# /ask-human — the human exchange

`/data-label` exchanges **artifacts**: something goes out, a manifest is frozen at send time, and
what comes back is reconciled against it. That rigor does not transfer to *"has 260731 been shot
yet?"* — there is no artifact and nothing to freeze. Forcing it through `/data-label` produced
`kind: data_request`: a handoff with an empty manifest, which is a hole where the rigor should be.

These are two different exchanges, and this is the other one.

|  | Exchanges | Rigor comes from |
|---|---|---|
| `/data-label` | an artifact | the frozen manifest — `returned ∩ sent == sent` |
| `/ask-human` | an answer | **the answer's evidential status** |

## The one idea

**An answer carries what kind of thing it is, and `claim` is the default.**

```
claim      they said so. Nothing checked it.
verified   something other than their word confirmed it.
decision   a judgement that is theirs to make — authoritative because of who made it.
refused    they said no.
unknown    they do not know.
```

This is the whole reason the skill exists rather than a notes field. *"The operator says the shoot
finished"* and *"the census counted 52 finished units"* are different facts, and they become
identical the moment someone writes "done" somewhere. The first one has been wrong before.

`lifecycle/references/run-mechanics.md` "Record integrity" says never record a metric you did not
read. This is that rule where the instrument is a person.

**Everything downstream branches on `answer.kind`, never on the text of `says`.**

## The script

```bash
S=<mlclaw_root>/lifecycle/scripts/ask-human/ask.py
python $S open   --project <p> --to <who> --asked "<question>" [--kind ...] [--verify "<cmd>"] [--due ...]
python $S answer --project <p> --id <id> --says "<reply>" --as claim|verified|decision|refused|unknown
python $S status --project <p> [--open-only] [--stale-days N]
python $S cancel --project <p> --id <id>
python $S show   --project <p> --id <id>
```

Exit 2 = broke, do it by hand. **Exit 1 = worked, the answer is no** — every exit-1 here guards
the claim/verified boundary, so redoing it by hand is overriding the check.

## Declaring `--verify` is the highest-leverage thing you can do

`--verify` is a command that could answer the question **without a person**. Declaring it costs one
line and does two things:

- It makes `verified` provable — the command runs at answer time and its output is recorded.
- **Its absence makes `verified` refusable.** Without it, and without `--evidence`, the only thing
  supporting the answer is what somebody said, so `verified` is refused and the honest `claim`
  is what gets filed.

And the case that pays for the whole feature: **the check can contradict the person.** If they say
the shoot finished and `census.py scan` disagrees, `answer --as verified` refuses and shows both.
Nothing else in MLClaw catches that, because nothing else is standing between a sentence and a
record.

Good `--verify` commands are the cheap ones already lying around:

| Question | `--verify` |
|---|---|
| "is the capture finished?" | `census.py show --dataset <d>` grepped for the unit count |
| "did you push the fix?" | `git ls-remote <repo> <branch>` |
| "is the box reachable now?" | `collect.py plan --from <box> ...` |
| "did the labels land?" | `handoff.py show --id <h>` |

**`--skip-verify` cannot produce a `verified` answer.** A check that did not run is not a check —
that path is refused unless `--evidence` names something else that corroborated it. Otherwise the
machine-readable `kind` would say "verified" while nothing verified anything, which is the exact
laundering this skill exists to stop, committed through its own escape hatch.

## While it is out

`status --open-only` is the mechanism, and it belongs in CLAUDE.md "On Conversation Start"
alongside handoffs. Default staleness is **7 days**, shorter than a handoff's 14: a question
someone could answer in a minute going unanswered for a week is a different and worse signal than
a labeling batch taking three weeks.

Two things `status` surfaces that nothing else can:

- **`unverified_claims`** — answers resting on somebody's word alone. Not wrong, just worth knowing
  before one of them becomes the reason a training set was frozen.
- **`expired_answers`** — `valid_until` has passed. *"This data is fine to use"* was true in July;
  nothing makes it true today. An answer quoted past its date is a stale metric wearing a sentence.

**MLClaw never sends anything.** Draft the message when asked, hand it over, let the user send it.
Chasing is theirs — surface who owes what and offer to write it.

## Adding a channel

Today `channel: manual` is the only value: a human carries the question and types the reply back.
The seam for a real channel (IM, mail, a ticket system) is deliberately left open and deliberately
empty, on the `/lease` provider pattern — one `channel_<name>.py` exposing `send(ask) -> ref` and
`poll(ref) -> answer | None`, discovered by filename, with everything else unchanged.

Two rules for whoever adds the first one:

- **`poll` may fill in `says`. It may never choose `kind`.** An adapter that reads "done ✅" off a
  thread and files it as `verified` has automated exactly the mistake the vocabulary exists to
  prevent. Adapters produce `claim` and nothing else; upgrading to `verified` requires the check.
- **Sending is outward-facing and irreversible.** It needs explicit confirmation per message, not a
  standing grant — the same rule `/data-label` follows for external parties.

## Requires / suggests

- **Requires**: `project.json`. Nothing else — a question can precede every stage.
- **Suggests**: whatever `why` said was blocked. When the answer unblocks a pull, `/data-collect`;
  a freeze, `/data-check`; a whole batch of work, `/data-label`.

Per `lifecycle/references/skill-graph.md` -> "Workflow State Protocol", push on entry and pop on exit. `stage: null`,
`execution: <ask_id>`. **Pop after `open`** — an ask waiting on a person is the expected long-lived
state, not unfinished work, and leaving it on the stack opens every session for the next week with
a false resume prompt. Same rule as a handoff at `await`.
