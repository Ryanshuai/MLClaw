# Debate roles: prompts you can copy directly, plus the adjudication format

Companion to `SKILL.md`'s "multi-agent debate: only in four places". A subagent cannot see the
conversation, so **every prompt must carry the raw material of what is being debated**
(numbers, script paths, the config diff, code line numbers). Never write "look at that
conclusion we just discussed".

## First ask: should this debate happen at all

‼️ **If a question on the rebuttal checklist can be answered by running one command, run the
command instead of convening a debate.** "Has the noise floor been measured" → run the same
weights twice. "Is the control on today's code" → look at the git sha.
**Debate is for questions where the evidence is exhausted or expensive to obtain, not a
substitute for a cheap measurement.** Clear the directly-checkable items off the list before
convening; only what is left enters the debate.

## The blind-input protocol

The opposing side gets **exactly three things**: the claim under debate, the raw material
supporting it, and its own rebuttal checklist. **It is not given the proposer's reasoning** —
otherwise it will only patch holes in that reasoning and miss the confounder the reasoning never
mentioned. The proposer's reasoning is placed beside the rebuttal only at adjudication time.

---

## ① Stage 6 result interpretation (highest value)

Two agents, the same material, opposite tasks.

**Proponent prompt skeleton:**

```
Claim under debate: <change X moved <metric> from A to B; the mechanism is <one sentence>>
Material: training/eval log paths, the full command lines of both runs, git sha, weights
          paths, the path of the pre-registered metric's computation script, the path of the
          counting script for that failure in FINDINGS.
Your task: give the strongest argument that this claim holds. It must include:
  1. whether the pre-registered metric moved, and by how much;
  2. whether the count of that failure in FINDINGS moved;
  3. whether those two directions agree with the mechanism (say so if they do not).
Forbidden: citing the improvement reported in a paper as evidence about us.
```

**Skeptic prompt skeleton:**

```
Claim under debate: <as above>
Material: <as above, but without the proponent's argument>
Your task: falsify it. Walk this checklist item by item; each gets
「ruled out / not ruled out / cannot be checked」 plus the basis:
  1. Has the noise floor been measured? What is the spread between two runs at the same
     weights and same convention? Does this delta exceed it?
  2. Was the control re-run on today's code, or read off an old log?
  3. Do both sides' filtering thresholds (score threshold, NMS, visibility filtering) come
     from the same source?
  4. Did the metric / renderer / frame sampling change between the two runs?
  5. Did this round change two things at once? (count the semantic changes in the diff)
  6. Did the pre-registered metric move? If only AP moved and it did not, the mechanism was
     not confirmed.
  7. Does the conclusion's tier label (T1/T2/T3) match its actual verification strength?
When uncertain, default to 「not ruled out」. Closing with "I feel it's probably fine" is
forbidden.
```

## ② Red-teaming the quantification script for Input A

One agent is enough. It does not look at the conclusion; it reads only the script.

```
Material: the full text of the quantification script + the numbers it produced + the field
          list of 2-3 real samples from the dataset.
Your task: find the circumstances under which this script measures a false failure, or misses
a real one. Item by item:
  1. What is the denominator? How large is n? Is it "the whole corpus" or "the few frames I
     looked at"?
  2. Is one object's labelling convention being applied to a different object?
     (Typical: indexing a prediction's size by the GT's axis numbering — under equivalent
     coordinate frames this scores a correct box as a large error)
  3. Does the criterion contain a filtering step that removes the very class of failure it
     should detect?
  4. Is there a built-in control (a quantity known to be zero) and a control group?
  5. Are the control/comparison samples chosen by explicit pairing, or scraped from adjacent
     log lines?
  6. Is the threshold chosen from the data or given externally? If from the data, was it
     chosen on the same data it is applied to?
Output: a judgement per item plus a concrete input that exposes it (construct a case; do not
say vaguely "there might be a problem").
```

## ③ Stage 3.5 tiering

Three agents, one round each, none seeing the others (the V axis is objective and needs no
debate; only P and U are debated).

- **Optimist**: why will this work? How many times has it been reproduced in other repos or
  papers? Is it one flag in the reference implementation?
- **Pessimist**: does its benefit elsewhere come from a different data distribution or a
  different bottleneck? Does its premise hold here?
- **Downstream** (representing the consumer only): **if it works, which downstream action
  changes?** Argue from the share of that failure in FINDINGS, never from AP. If the downstream
  side cannot name one action that would change, U is low — whatever AP says.

## ④ Stage 3 proposal table

One Proponent and one Skeptic per proposal. The Skeptic's checklist:

```
1. The mechanism does not match that failure in FINDINGS (say which step breaks);
2. The technique is already in the code — go grep, name the function and the call site; if it
   is "present but weakened", name the line that weakens it (`.detach()`, a flag defaulting to
   off, a branch that is never taken);
3. Output degrees of freedom do not match the information content (count output dimensions
   against true degrees of freedom; how many are redundant);
4. A dependency is unmet (it needs another one to land first);
5. The size of the change is underestimated (list the files/functions it will touch);
6. That failure's share is overestimated (recompute the share);
7. The benefit comes from a different data distribution (point density, number of classes,
   degree of occlusion, annotation quality).
```

## ⑤ Stage 5 port acceptance (three-way alignment)

Three agents, **none seeing the others**, each with different material — the difference in
material *is* the difference between the three alignment lines. Run to completion **before
turning the flag on and before any large run**.

**A · Fidelity** (our implementation ↔ the reference implementation):

```
Material: the Stage 4.5 interface comparison table + the full text of our diff + the full text
          of the corresponding reference-implementation files (fetched by sha).
Your task: walk the "semantic difference" column; each entry gets a judgement plus the basis:
  - irrelevant: a numeric/naming/shape-ordering difference the mechanism does not depend on.
    You must state why the mechanism does not depend on it.
  - undecided: not sure. **This is the default judgement**; you may not rule something
    irrelevant because "it looks about the same".
  - fatal: change this and the trick's mechanism no longer holds (say which step breaks).
Additionally answer three questions separately:
  1. Which hunks in the diff point at no line in the reference implementation (= invented), and
     what does each of them do?
  2. Branches that exist only in train in the reference implementation — did we port all of
     them? (denoising queries / auxiliary heads / EMA / scheduling)
  3. Of the parts of the reference implementation we ABANDONED, was what we abandoned core or
     peripheral?
Every 「undecided」 and 「fatal」 must come with a cheap check (pick from the catalogue below, or
design one). "Needs verification" alone is not acceptable.
```

**B · Requirement alignment** (our implementation ↔ FINDINGS) — ‼️ **give it neither the
reference implementation nor the paper**:

```
Material: the target entry F<n> in findings.json (including measure.script and the share) + the
          full text of our diff + this proposal's pre-registered metric.
Your task: answer one question only — can this implementation suppress the failure F<n>
describes?
  1. Starting from F<n>'s mechanism, point at the line in the diff where that mechanism starts
     to change; if you cannot point at one, say so.
  2. Will the pre-registered metric actually be affected by this implementation, or will only
     other quantities be?
  3. If it can suppress only part of F<n>, say which part, and who owns the rest.
Forbidden: "this is a method validated in the literature" as a reason — you cannot see the
paper, and that is deliberate.
```

**C · Mechanism alignment** (our requirement ↔ their trick):

```
Material: the reference implementation + the paper's own ablation table + our key statistics
          (point density, number of classes, GT per frame, positive/negative ratio, number of
          queries, distribution differences, annotation quality).
Your task: answer "what makes this trick work in ITS setting, and does that premise hold here".
  1. Under what conditions was its benefit measured in the original? (dataset, query count,
     training epochs, baseline strength)
  2. Which of those conditions do we not meet? Does failing them shrink the benefit, remove it,
     or make it negative?
  3. Is the problem it solves the same problem as our F<n>, or only a similar symptom?
This one is allowed to conclude "the implementation is entirely faithful, and the trick simply
does not address our symptom".
```

**Catalogue of cheap checks** (for 「undecided」 items; none requires full training):

- **Overfit a single batch**: with the mechanism wired up, one batch should drop to near-zero
  loss; failing to = the gradient does not flow, or the targets are constructed wrongly.
- **Count positives and negatives**: count on both sides of the change, enumerating
  independently — do not read `.shape`.
- **Hand-compute the loss**: construct a minimal input you can compute by hand, and compare to
  the decimal place.
- **Gradient-flow assertions**: assert that tensors that should have gradients have non-None
  `grad`, and that the ones that should be cut really are None (specifically for `.detach()`-
  class differences).
- **Flag off must be bit-identical to the old code**: with the flag at its default-off, the
  output is bit-exact with the pre-port state; if not, a default path was missed.
- **Reconcile the train/eval branches**: branches that exist only in train — confirm they really
  do not run at eval.

**Adjudication splits into four by a 2×2** (the actions differ completely; do not conflate them):

| | Faithful (A passes) | Unfaithful (A says fatal) |
|---|---|---|
| **On target (B passes)** | ship it | fix fidelity before evaluating — the current benefit may be coming from something else |
| **Off target (B fails)** | ‼️ **the proposal is wrong, not the implementation. Go back to Stage 3; do not tune the implementation** | both are wrong; go back to Stage 3 |

C is an independent veto spanning both columns: **C failing = this trick does not address our
symptom**, and the more faithful A is at that point, the more clearly it says to go back to
Stage 3.

## ⑥ How to set the pre-registered metric (the highest-value one of all)

Division of labour with ②: **② asks "does the ruler compute correctly", ⑥ asks "should the ruler
have this shape at all".** In the AABB incident the script computed entirely correctly and
computed the wrong thing — not one item on ②'s checklist would have stopped it; only ⑥ would.
Convene it **at the moment the pre-registered metric is written down**, before any run.

**Blind-spot attacker:**

```
Material: the pre-registered metric's definition (or computation script) + the FINDINGS entry
          it is meant to measure + the model's output field list (each field's geometric
          meaning, coordinate frame, units).
Your task: construct a CONCRETE situation where the implementation is wrong and this metric
does not move, or even improves.
  Give a specific way of being wrong (not "if there were a bug"), for example:
  - a degree of freedom is projected away inside the metric (axis-alignment, modulo, absolute
    value, sorting);
  - the metric and the implementation read the same possibly-wrong intermediate (the same
    matching, the same thresholds, the same cache);
  - some class of error happens to shrink the metric's denominator.
Constructed → the ruler is blind there: a second independent ruler is required, or move to
blind human review.
Not constructed → say explicitly "I could not construct one", and state which degrees of
freedom you checked.
```

**Goodhart attacker:**

```
Material: the pre-registered metric's definition + every knob we can turn (thresholds, NMS
          parameters, query count, post-processing).
Your task: give a change that raises this metric while making downstream worse.
  Common routes: raise/lower the score threshold, loosen NMS, degenerate the output into one
  large box covering everything, trade recall for precision (or the reverse), improve only on
  the easy subset.
If you can give one → the ruler is unfit: either replace it, or pair it with a guardrail metric
that closes off that route.
```

**Downstream** (same as ③'s downstream role, with the metric definition as its material): once
it goes up, **which downstream action changes**? If no concrete action can be named → U is low,
and this metric is not worth being the round's primary criterion.

**These four are answered from a table; do not convene for them**: has the noise floor been
measured (one that has not may not be a pre-registered metric), how far it is from saturation,
how much of its range is manufactured by artificial constants, and the direction of `worse` plus
its units.

**⑥'s adjudication**: if either the blind spot or the Goodhart route is **successfully
constructed and not closed off** → **this ruler may not be the round's criterion.** Add a second
independent ruler, add a guardrail metric, or escalate to blind human review (`SKILL.md`
Stage 6.5). **Note that the debates in ① and ⑤ are not a substitute for a person's eye** — an
agent may not look at a rendered screenshot and declare "it looks right".

---

## Adjudication

**The main agent adjudicates, not the debating sides.** The debaters only argue both sides to
the full.

➜ **The default-verdict table (three rows by what is under debate: conclusion/number ·
proposal · implementation) lives in `SKILL.md`'s "Adjudication" section and is not repeated
here.** It is a judgement that must be present at decision time, whereas this file is the
operating manual read only when convening a debate — the same table in two places means
following the pointer loops you back to where you started, which is the same as having no
document. This section covers only **how to record the outcome**.

‼️ One thing is repeated, because it is needed *during* the debate: **a score is not an answer
to a structural objection.** "But AP went up 0.4" cannot be used to answer "your two sides'
filtering thresholds come from different sources".

Recording format (goes into `model_design.md`, no more than six lines per entry):

```markdown
#### Debate D3 — does IoU-aware classification explain the 0.999 saturation
- Roles: Proponent / Skeptic (2 agents, blind input)
- Claim: making the classification target the IoU with the matched GT lets NMS ordering carry
  localisation-quality information
- Strongest rebuttal: checklist #6 — the share of half-pitch wrong boxes among misses was
  computed at IoU 0.2–0.5; at 0.3–0.5 only <x>% remains (not ruled out)
- Verdict: proposal retained, **dropped to T2**; recompute the share first (script <path>),
  then decide whether to raise it to T3
- Conclusion tier: [T2 controlled]
```

**A debate whose adjudication is not written down did not happen.** Keeping only the conclusion
and not the strongest rebuttal means nobody next round knows what this has already been
challenged on.
