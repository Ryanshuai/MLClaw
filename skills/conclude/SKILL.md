---
name: conclude
description: >
  Record what a round CONCLUDED — the belief, its evidence with quotes, what would overturn
  it, and what it rests on — as a checkable artifact rather than a sentence. Trigger at the
  end of an exploration, a tune session, an eval round, or an audit; whenever somebody says
  "所以结论是什么", "这轮学到了什么", "记一下这个结论", "把结论写下来", "以后别再试这个了";
  and whenever a past conclusion is being quoted — "我们试过了没用", "那个不是早就否掉了吗",
  "这个还成立吗" — because the answer depends on a corpus, a tier and a noise floor that the
  sentence does not carry. Also trigger when a dataset is retired or a run deleted, to find
  which conclusions just became unverifiable. Not for recording what HAPPENED (that is the run
  record) or which arm won (that is /explore's graph).
---

# /conclude — the belief layer

A run record says **what happened**. A graph card says **which arm won**. Neither records
**what is now believed** — and six weeks later, the belief is the only one anybody repeats,
with not one of its three qualifiers left attached:

> "We tried multi-frame fusion. It didn't help."

That sentence is about **a particular corpus**, **a particular tier**, and **a particular
noise floor**. None of the three is in it, so it can be neither refuted nor applied — and it
*will* be applied, to a corpus it was never measured on.

The structure is borrowed from ARA (arXiv:2604.24658) `logic/claims.md`. Three things taken:

1. **`Evidence basis` and `Interpretation` are two columns, not one.** Merged into one, a
   conclusion reads as if the mechanism had been measured, and the next round is designed
   around a mechanism nobody ever tested.
2. **`Falsification criteria` is mandatory.** A belief with no falsifier is a preference, and
   `check` refuses it.
3. **Conclusions depend on conclusions.** So refuting one must *move* the others rather than
   only editing itself.

What was **not** taken is its word: in MLClaw `claim` already means the opposite —
`/ask-human` and `/discover` use it for "somebody said so, and nothing confirmed it". Calling
the evidenced object a claim would collide exactly where the distinction matters most.

**One state was added that ARA does not have: `unverifiable`.** ARA's states assume the
evidence stays put. MLClaw retires datasets, deletes checkpoints and loses snapshots, so the
state that actually occurs is "**nobody can check any more**". It is not a weaker `supported`,
and it is certainly not `refuted` — the same discipline as `census.py` separating `gone` from
`unreachable`, and `/repro` giving it its own tier.

## Where the record lives

`{project}/knowledge/conclusions.json` (the record) + `knowledge/conclusions.md` (the rendered
artifact). Project-level, not under `stages/`: a conclusion outlives the exploration that
produced it, and it may equally have come from an eval, a tune or an audit.

The script is `<mlclaw_root>/scripts/conclude/conclude.py`, with nine verbs:
`new | add | evidence | set | refute | supersede | check | status | render`.

## ‼️ `status` and `tier` are computed, never written

`set --field status` **refuses** (exit 1). This is not fastidiousness: a confidence that
outlived its evidence looks **identical in JSON** to one whose evidence is intact, and that is
the entire reason this file exists.

- **`tier` takes the WEAKEST tier among the evidence**, not the strongest. A conclusion resting
  on one T3 arm and one T1 probe is a **T1 conclusion**. Taking the strongest is CLAUDE.md's
  "a soft number becomes a hard one" mechanism, one level up.
- **`status` is computed by `check` from what currently resolves**, and it **reports** any
  disagreement between the stored value and the computed one — reports, never repairs.
  Repairing erases the only evidence that a conclusion outlived its evidence, and turns the
  report green while doing it.

## The flow

Five steps in one pass. Do not turn them into five questions asked of a person.

1. **`add`** — one sentence saying what is believed, plus `--falsified-if` and `--corpus`.
   The falsifier must **name a number or the metric named in the scope**; "if it later turns
   out not to work" is a tautology, and it is worse than leaving the field empty, because it
   looks filled and no measurement can ever satisfy it.
2. **`evidence`** — one `--ref` and one `--quote` per piece. **The quote is the transcribed
   line, not a summary**: a path alone is not grounding, and the transcribed line is the
   evidence that this source was actually opened. `check` verifies that every number in the
   statement appears in some quote.
3. **`--interpretation`** — the part that is *argued* on top of the evidence and was not
   measured. ARA's own example puts "the authors argue (but do not formally prove)" in this
   column. Do not push it back into the statement.
4. **`check`** — a critical finding exits 1.
5. **`render`** — produce the artifact.

## The verdict table

| Situation | Status | Who can move it |
|---|---|---|
| Every piece of evidence resolves, and no dependency is weak | `supported` | — |
| Disputed, or a dependency is refuted or in doubt | `contested` | somebody has to look |
| The falsifier was satisfied by a **recorded measurement** | `refuted` | `refute --by <a ref that resolves>` |
| An evidence ref **no longer resolves** | `unverifiable` | fix the evidence, or accept that it cannot be checked |
| Replaced by something more precise | `superseded` | `supersede --by K0N` |

**Refuting one conclusion does not delete the ones resting on it** — it moves them to
`contested`. Deleting them erases the only record of why that batch of runs was launched;
leaving them `supported` lets a refuted premise go on being cited. `contested` is the
"somebody needs to take a look" cell.

**`refute --by` must resolve.** A falsification that cannot be opened is opinion overturning
record — which is exactly CLAUDE.md's *"Never let somebody's word become a checked fact"*.

## When a pile of `unverifiable` suddenly appears

After `/data-retire apply`, after a run is deleted, after a snapshot is gone. Run
`conclude.py check` to find out **which ones** just became uncheckable. When a snapshot
stamped `data_retired` is cited, this **reports and does not adjudicate** — `/repro`'s
`survivors_of_retirement` already performs the deletion-time vs census-time join, and the same
judgement written twice means only one copy ever gets fixed.

## How other skills call it

- **`/explore` always calls it on the way out**: what an architecture search produces is a set
  of conclusions. What lands in `graph.json` is "which arm won", not "so what is now believed".
- Optional after `/train-tune-report`, `/eval-report` and `/data-audit`.
- **Whenever somebody quotes an old conclusion** ("wasn't that ruled out ages ago?"): run
  `status` first, never answer from memory.

## Three things it does not do

- **It executes nothing.** Conclusions rest on runs that already exist; this touches no GPU.
- **It does not conclude on the user's behalf.** `provenance` distinguishes `user` /
  `ai-suggested` / `ai-executed` / `user-revised`, and **never auto-upgrades** — the same rule
  as `graph.py`. A screen of conclusions that are all `ai-suggested` is a screen of conclusions
  nobody asked for, and `check` says so.
- **It does not repair the record.** `check` only reports.
