---
name: explore
description: >
  Use this skill for ARCHITECTURE SEARCH — the model is NOT settled yet, and settling it
  is the work: structure, components, and network selection, down to which family of model
  this should be. It turns "our results are bad" or "the architecture is still primitive"
  into a numbered, pre-registered, controlled list of changes, then sources the paper plus
  its open-source code, ports one flag at a time, and ablates. Trigger for: what is worth
  borrowing, what technique are we missing, should we adopt X, which backbone / which
  network should we use, is this improvement real, did we port it correctly. Also trigger
  for Chinese requests like "什么值得借鉴", "缺什么技巧", "要不要上 X", "架构是不是太原始",
  "换个网络试试", "选哪个模型", "把某篇论文的做法移植进来", "这版比上版好吗",
  "这个提升是真的吗", "搬对了没有".
  Parameter search belongs here too when the parameter IS the hypothesis — "是不是容量不够",
  "这个模块没用是不是 lr 太保守", a width / depth / layer-count sweep — because those decide
  what the model is, not how to configure a settled one.
  Pushy trigger: invoke it even when the user only pasted a screenshot or a paper quote
  and asked "你看这个对吗" — that is exactly the moment the proposal list gets made from
  vibes instead of counts, which is the failure this skill exists to prevent. Also invoke
  before writing any port of a published technique into an existing training codebase.
  Not for tuning a model that is already settled — finding the best lr / batch size /
  warmup on a fixed architecture is /train-tune, which runs AFTER this skill, never before.
  Not for data or label quality (use /data-audit), pure engineering speedups that do not
  change prediction quality, or a one-line change whose location is already known.
---

# /explore — architecture search: from a measured failure to a ported technique

The commonest mistake in changing a model's architecture is not changing it wrongly. It is
**changing it correctly without knowing why it worked**, or **copying a technique to treat a
disease that does not exist**. This skill is a pipeline turning "the results are bad" into
"numbered changes with pre-registered metrics and controls".

**Three hard rules:**

1. A phenomenon must become a **count** before a proposal is eligible for the table;
2. A proposal must have a **pre-registered metric** before the code is eligible to change;
3. ‼️ A pre-registered metric must pass an **independence test** before it is eligible to be a
   criterion — **if you can construct a case where the implementation is wrong and it does not
   move, or even improves, it is not independent** (AABB got through exactly that way).

---

**Not applicable**, and each has somewhere else to go: data / annotation quality is
`/data-audit` (it opens files and needs no model); **the model is already settled and you only
want its best operating point** is `/train-tune` (same layer, one step later — see the next
section); a round trip over a converter that does not fit is `adaptation`; pure engineering
speedups (which do not change prediction quality) and a one-line change whose location is
already known — just do those, without this pipeline.

## references — seven of them, **this file keeps only the judgement; the operating detail is in them**

This file answers "should we, is it worth it, do we believe it"; the seven below answer "how".
**Read the relevant one before starting.**

| File | When to read it | What it owns |
|---|---|---|
| `references/experiment-graph.md` | the first thing every working session | node schema · state machine (⬜🟨🟩🔵🟪✅❌) · the four operations (ADD / TAKE / FILL / CLOSE) · the graph's invariants. **How the ready set is computed, how a fill propagates** |
| `references/porting.md` | Stage 4 / 4.5 / 5 | how to search (three keyword layers, across fields) · grading evidence (**used ≠ ablated**) · ‼️ **deviation splits into adaptation and breakage, and afterwards they look the same** · the four verdicts on whether it worked |
| `references/cluster-ops.md` | before starting, and when wrapping up | the stable-capacity inequality · the four silent capacity failures · **GPU selection is clock ÷ price, not FLOPS** · one GPU model per comparison group · **release has been handed to a resident janitor; only the two things it cannot see are left** |
| `references/explore-or-stop.md` | after a few arms have run | the criteria for continuing vs stopping · ‼️ **six false "the search is finished"** · stopping ≠ finishing |
| `references/debate-roles.md` | when convening a debate | the **role prompts and rebuttal checklists** for each of the six (copy them directly) · the blind-input protocol · the six-line adjudication record format |
| `references/run-card.md` | before opening an arm | which parts MLClaw's `run.json` already covers, **and which three it does not** · the full argument for the four hard rules · ‼️ **the four shapes in which the record layer breaks** (none of which raises) |
| `references/human-review.md` | Stage 6.5, when a person must look | choose frames in advance with a control group · **two independent visual channels for blind review** (one channel cannot detect its own errors) · the discard rate as a metric · how a person's judgement is written back to `findings.json` |
| `stages/exploration/state.json` | **check it first, every round** | the currently measured constants, the tiering table, the killed list. ‼️ Change the weights, the frame sampling or the metric and **the whole file is void** — the template is `lifecycle/exploration/state.json`. **It is a project record, not a skill file**, so there is no global copy to keep in sync |

## What this stage is, inside MLClaw

**It is a search whose unit is a PROPOSAL, not a trial.** One card = one hypothesis, one
pre-registered criterion, one guardrail, one kill condition. What is being searched is
**structure**: which component to add, which to remove, which network to swap in — up to
whether this model should be this kind of model at all.

### The boundary with `/train-tune`

**`/explore` answers *what should this model BE*; `/train-tune` answers *how should this model
be configured*.** Same layer, one step apart: before the model is settled it is this side;
after, it is that side.

‼️ **The test is NOT "parameters versus code".** Ask this instead: **after this change, are the
previous runs still answers to the same question?** Yes → `/train-tune`, and you are finding a
point on a curve that already holds. No → this side: the question changed, the criterion and
the noise floor must both be re-established, and none of the previous numbers is directly
comparable.

**Whether it can be changed on the command line decides nothing.** `--num-layers`, `--width`
and `--use-fpn` are flags, but they change **the network itself**; `--lr`, `--batch-size` and
`--warmup` are also flags, and they do not. A capacity sweep done entirely with existing flags
is **`/explore` wearing `/train-tune`'s clothes** — being cheap to run is a good thing, but its
conclusion lands on a card, because it settles the model's identity and the next round will read
it as identity.

**Parameter search happens here too**, in three shapes, and only the middle one is
`/train-tune`'s:

| Shape | Whose |
|---|---|
| **The parameter IS the proposal** — "maybe it just does not have the capacity", "maybe that module looks useless only because the lr was set too conservatively" | **This side.** It is a claim about *why that arm came out flat*, not an optimisation — so it gets a card and a pre-registered criterion like anything else |
| **The architecture is settled; find this model's best point** | `/train-tune`. Only this shape produces a configuration as its output |
| **A bounded search INSIDE one arm**, so that a ported component is judged at a fair operating point | **This side, as part of that arm.** `/train-tune` may be invoked to execute it |

The third shape is necessary: a technique that is good at the paper's lr and worthless at this
repo's lr, called "ruled out", is a false negative. But it has **two conditions, neither
optional** — the result belongs to **that card**, not to the model (it does not become the
project's configuration); and **the control arm must get the same budget**. Tuning only the
experimental arm and comparing it against a default-configured control manufactures an
"improvement" out of search budget, and afterwards the two arms' records look identical, so the
record layer cannot see it.

**Order: `/explore` first, then `/train-tune`.** The reverse loses on both ends — hyperparameters
tuned around a component you are about to delete are void the moment it goes; and an
architecture judged at "whatever parameters happened to be lying around" was judged at a point
nobody chose. The third shape is how the second half of that gets paid for without inverting the
order.

The full comparison table is in `<mlclaw_root>/references/skill-graph.md` ->
"`/train-tune` vs `/explore`".

**It runs nothing.** An arm is an **ordinary run** in `stages/<target_stage>/runs/`, started by
`/train-run` or `/eval-run`, and the card cites it by `run_id`. This is the same boundary
`/data-curate` draws around a transform and `adaptation` draws around a converter, for the same
reason: a search that runs its own trials is a second run mechanism, and it will drift from the
first.

### Where the record lives

```
stages/exploration/
  config.json      entry points, corpus, metric script, comparability fingerprint, where design_doc points
  findings.json    Input A — phenomena turned into counts. This is what Stage 8 re-weighs
  baseline.json    the noise floor: one number plus which two runs it was computed from
  audit.json       the four-state audit table + the cost profile (Stage 1 / 1.5)
  graph.json       ‼️ the experiment graph. Node cards · seven states · dependency edges — the single source of truth about order
  state.json       the things that expire: the constants table · the tiering table · the killed list with revival conditions
stages/<target_stage>/runs/   ← arms live here, not under exploration
```

**`graph.json` is the half a machine reads; `config.json -> design_doc` points at the half a
person reads.** They are not copies of each other: `graph.py check` reads the first, a person
reads the second. The original put both in one section of `model_design.md`, and that is
precisely why the original's seven invariants said "scan periodically" while nobody ever
scanned.

### Those invariants now have an executor

```bash
python <mlclaw_root>/scripts/explore/graph.py <verb> --project <PROJECT>
```

| verb | What it does |
|---|---|
| `add` | a one-line idea → a card (`draft`). A card missing fields cannot enter the ready set |
| ‼️ `claim` | **take a card into a tree of its own, before any of its code is written.** Allocates the branch, records who has it, prints the `git worktree add`. Creates nothing on disk |
| `set` | complete or amend a card. ‼️ **A closed card refuses edits** — a changed conclusion means a new card citing the old one. Also refuses (exit 1) a **second concurrent arm into an unnamed tree**, and a branch name another card already owns |
| `ready` | compute the ready set. Empty set + non-empty queue = **deadlock**, and it says whether that is a cycle or one shared predecessor. ‼️ Also hands out **one `git worktree add` per code-writing card** — this is where parallel arms are given out, so it is where the isolation belongs |
| `fill` | results onto the card, **plus a list of what this result may have voided** (whatever depends on it, whatever cites it in prose, the constants that came from this run) |
| `close` | a verdict, or one of the four deaths |
| ‼️ `land` | **give the trees back**: what may be merged, in what order, each with its re-verification, and which branches must not be deleted. Refuses while the round is still open. Plans; merges nothing |
| `check` | ‼️ **every invariant in `references/experiment-graph.md` → §4. Reports, never repairs** |
| `status` | a one-screen summary. ⬜🟨🟩 are **computed live** (the same function as `ready`), and a stored label lagging the derivation is reported separately as `state_drift` |

**`check`'s exit code follows CLAUDE.md "Script Integration": a critical finding exits 1 — the
script worked and the answer is no. That is not a failure, so do not fall back and work around
it by hand.**

‼️ **No count anywhere on this page, deliberately.** The one that used to be here said *seven
plus two* while `check` emitted more than twenty, and it had already been wrong for several
rounds — a number with two authors, drifting exactly the way `/agent-refactor` calls a double
protocol, in the one direction that reads as reassuring. **The list is §4's table**; whichever of
the two you change, change both.

Two of MLClaw's additions are worth reading here, because they are rules the original executed
from memory:

- **`premise_share`'s `measured_on` must equal this graph's `corpus`.** A share quoted from
  elsewhere is not weak evidence; it is evidence about a different question — so it is treated as
  **having no premise**, and the card stays in `draft`. This is the original's most expensive
  crash (predicted 47%, measured 4.62% on this corpus, with five arms already queued behind it),
  and it is CLAUDE.md's *"Never compare metrics across different `mode` or non-equivalent
  `scope`"* one level down: that rule governs whether two numbers may be subtracted, this one
  governs whether a proposal may exist.
- **Every result must carry a tier.** A number with no tier gets promoted, and that is exactly
  how a false noise floor enters the record.

### On entry

Per `<mlclaw_root>/references/skill-graph.md` -> "Workflow State Protocol". Stage = `exploration`.

| | |
|---|---|
| **Requires** | `project.json`; code available; **a declared corpus** (`datasets/<id>/dataset.json` plus a frozen snapshot) — `premise_share` is meaningless without one |
| **Suggests** | `/train-run` (open an arm) · `/eval-run` (measurement cards, the noise floor) · `/train-tune` (**after** the architecture is settled, never before) · `/conclude` → `/ara` (wrapping up) |
| **Calls** | `/discover` (Stage 4, finding the paper and the code) · `/eval-run` (Stage 0's noise floor needs two measurements at the same weights and convention) · `/train-tune` (**bounded**, inside one arm, to give a ported component a fair operating point — the third shape above, with the control arm on the same budget) · `/ask-human` (‼️ see below) |

‼️ **This pipeline is full of questions only a person can answer** (which metric is primary,
whether this delta is worth acting on, whether the blind review passed). Per CLAUDE.md's
*"File the question; do not block on it"*: **do not stop at question 3 waiting for somebody.**
Do everything that does not depend on that answer, turn the question into a record with
`ask.py open`, mark which field is open in `config.json -> open_asks`, and carry on. A halted
interview and a complete record with three open questions are entirely different things to
whoever picks this up.

‼️ **Every "this repo" below means the project this pipeline was born in**
(`e2e_3D_detection`) — not the current project, and not MLClaw. They are **measured records**,
not configuration: the crash behind each rule is the reason that rule exists, which is why the
numbers are kept verbatim. **Copying those numbers into your project makes them fake data**;
what you copy is the rule.

### "So what new conclusions are there?"

```bash
python <mlclaw_root>/scripts/explore/graph.py new --project <P> [--since 12h]
```

‼️ **This verb exists not for convenience but to block one specific slide.** The question was
asked roughly fifteen times over the original round, and it is exactly the moment when it is
most tempting to report a 🟪 as a ✅: an arm finished, the numbers are on the card, and the
sentence that comes out is "e13h came back, 92.15" — which is treating a **result** as a
**conclusion**. Much of the time the verdict genuinely cannot be reached yet, because it is
waiting on another arm.

So the answer comes in two lists, the second annotated: `conclusions` (genuinely adjudicated)
and `results_without_a_verdict` (numbers in, no conclusion). **When reporting the second, say
what it is waiting for.** When the window holds nothing it says so explicitly: "this is a real
answer; do not go looking for something to report."

## What one round looks like

**Every step's artifact is hard** — without it, that step is not done:

| Step | Artifact |
|---|---|
| 0 | one number: the noise floor |
| 1 | the four-state audit table (+1.5, the cost profile) |
| 2 | a price for each candidate technique, from existing flags |
| 3 | **the plan lands**: the ablation chapter in the design document — the proposal table (one pre-registered metric **plus one guardrail metric plus a premise and its share on this corpus** per row). ‼️ **That chapter begins to exist HERE, and every later step only updates it, never writes a new one** |
| 3.5 | one tier per row, T0–T4 |
| 4 | `repo@sha` plus the key line numbers |
| 4.5 | the interface comparison table (with a "first judgement" column) |
| 5 | a diff whose flag defaults to off, **with a `ref:` on every hunk** |
| 6 | three numbers moving the same way, plus a tier label |
| 6.5 | the human blind-review ratio (both channels reconciled) |
| 7 | **update** step 3's chapter (do not open a new one); complete the killed list and the experiments not run |
| 8 | `findings.json`'s `value` re-weighed, P updated, technical debt walked once |
| every run | **a run card**: declared before the run (including the code snapshot), attached after, checked against earlier ones |

## Where you come in (users essentially never start from scratch)

**Entry at any stage is allowed, but a few steps must be filled in afterwards regardless** — the
consequence of not doing so is in the last column:

| The user says | Enter at | Must be filled in | What happens if it is not |
|---|---|---|---|
| throws a screenshot / "this batch feels wrong" | Input A | — (this IS the start) | — |
| "is the architecture too primitive / what technique are we missing" | Stage 1 | Input A's counts | copying techniques for a disease that does not exist |
| "should we adopt X" | Stage 1 first: is X already here → Stage 3.5 to tier it | the four-state table + one FINDINGS entry | porting something already in the code |
| "port the method from paper X" | Stage 4 | the four-state table + Stage 3.5's V/U | running naked in the hallucination-risk zone |
| "it finished — is this improvement real" | Stage 6 + debate ① | **Stage 0's noise floor** | mistaking noise for a result |
| "the port is done — did we do it right" | debate ⑤ | **Stage 4.5's interface comparison table** | no target, so fidelity cannot be argued |

Filling in is not redoing everything: it is **that one table or that one number**, usually
fifteen minutes.

‼️ **The noise floor is the one exception here, and it is exactly what somebody taking over runs
into.** The floor is the difference between two measurements at the same weights and the same
convention, so filling it in means two `/eval-run`s — which needs a machine, and needs that
checkpoint to still exist. Taking over an existing project, neither is usually true: the machine
was released, the disk is stopped, that pipeline never wrote `run.json` at all, so nothing can be
put in `runs`. **In that case, do not invent two run ids to fool `check`** (which is precisely
the one form the record layer used to let through). Write
`baseline.json -> origin: "external"`, fill in the number and its `sources` as normal, and use
`unchecked` to state why it cannot be re-measured here. It is a `claim`: **it gates T2 as normal
and never supports T3.** The day the checkpoint comes back, two evals replace it with a
`verified` one.

---

## Two inputs

What a user gives you is not fixed. What is fixed here is the interface, not the source.

### Input A — results (multi-source, pluggable)

Permitted sources: **a person's eye** (a screenshot, "these boxes feel wrong"), **agent analysis**
(error-decomposition scripts, metric sweeps), **production / deployment feedback** (field
complaints, grasp failure rates), **training logs / metrics** (loss curves, AP, tfevents).

**Every source lands first in the same `stages/exploration/findings.json`** (template at
`lifecycle/exploration/findings.json`). JSON rather than a table, so that **after a weight change
one command re-measures with the same ruler**, and the two can be diffed:

```json
[{
  "id": "F1",
  "source": "human|agent|production|metrics",
  "claim": "a wrong box occupies the right box's place, so the right box never comes out",
  "measure": {"script": "scratchpad/miss_mechanism.py", "corpus": "ep49_0813/rig",
              "n": 5495, "gate": "IoU 0.2-0.5",
              "value": 0.488, "unit": "share", "worse": "up"},
  "status": "live|killed|blocked-on-data",
  "killed_by": "share_too_small|wrong_mechanism|faithful_but_inert|blocked_on_data",
  "revive_if": "the metric changed / the data distribution changed / the proposal it depends on landed",
  "note": "56.9% of the wrong boxes straddle >=2 labels"
}]
```

‼️ **`value` / `unit` / `worse` must be comparable scalars**; do not stuff statistics into a free
dict — otherwise "re-measure with one command after a weight change and diff it" is impossible,
and this interface degenerates into a hand-copied table. `worse: "up"` says that a larger value
is worse, which is how the diff knows the direction. Render it as a table for people to read; do
not maintain two copies by hand.

- **Human input is a hypothesis generator, not evidence.** A screenshot can only propose the
  hypothesis "a wrong box occupies the right box's place"; it has to become "48.8% of missed
  labels have a wrong box at IoU 0.2–0.5 sitting on them, and 56.9% of the wrong boxes straddle
  ≥2 labels" before the next step. A phenomenon with no denominator may not enter the proposal
  table.
- **Quantification must land on a corpus, not a few frames.** A handful of odd samples can pull
  the tail up by themselves; report p50/p90/p95 plus n, not "I looked at 9 frames".
- ‼️ **And it must land on THE corpus this round will train or evaluate on.** This rule was
  bought with thirteen machines on 2026-08-14: F1's mechanism is
  `negatives = nqueries − repeat_num·G`, and the justification said "**47% of production frames**
  have G ≥ 52", on which five arms were opened. Measured on **the corpus this round actually
  used**: frames with `G ≥ 52` are **4.62%**, `G`'s **max is 56**, and
  `match_neg_zero_frames = 0.0000`. **An order of magnitude out; the disease does not exist.**
  The result side agreed: one-to-one at the first clean point after warmup scored AP50 **5.57**,
  below the baseline's **7.88**. A share is **a property of the corpus, not of the phenomenon** —
  the `measure.corpus` field exists for exactly this, and I filled it in without checking it
  against the training corpus.
- ‼️ **Mechanical quantities must be read before starting a machine.** Two of the three numbers
  above (`match_neg_frac`, `match_neg_zero_frames`), like G's distribution, **need no trained
  model**: one pass through the loader and one through the matcher produce them. They are
  zero-cost, and this time they would have saved five arms. **Any pre-registered metric readable
  without weights: read it first, then decide whether to spend money.**
- **`measure.script` is the core of this interface**, not decoration: next round, after a weight
  change, the **same ruler** must re-measure it — "feels better" is not available. Record
  `corpus` too, or a change of frame sampling will look like a change in the model.
- **`status: killed` is worth more than live.** A phenomenon the data refuted stays in the file,
  so nobody proposes it again next week.

### Input B — the task

The same failure is worth different amounts on different tasks. Write down:

- what the task is, and how downstream consumes this output ("the suction cup lands on the near
  surface" ≠ "IoU is high");
- **which degree of freedom's precision actually matters**, and which was copied from a
  catalogue (here: face precision at ±12 mm matters; depth labels are themselves ambiguous by
  74–210 mm on 25.3% of boxes, so more precision there buys nothing);
- deployment constraints: latency, memory, operator support (whether deformable attention is
  available is sometimes TensorRT's decision);
- **operating conditions absent from the data** (we have not one frame of "a whole wall"; the
  closest is 69% face-on).

Input B may drive a proposal on its own — see "task-driven proposals" below.

---

## Stage 0 — measure the noise floor first

**Before believing any delta, run twice at exactly the same weights and exactly the same
convention.**

Measured here: 91.66 / 91.41 at the same weights and convention, a **noise floor of 0.25 AP**.
Any improvement smaller than the floor is not a result. I crashed on this one: I reported 0.06
first, wrote a "+0.30 is real" conclusion on top of it, and retracted all of it.

‼️ **When this number goes into `baseline.json`, it needs a ruler, not mental arithmetic.** The
floor is the **difference** between two measurements, and no log prints it — so do not write
`sources` as two log lines (neither contains the difference), and certainly do not invent
`"31.09 - 28.16 = 2.93"` as a quote (which is precisely why the grounding check exists). Write a
small script at `stages/exploration/scripts/<name>.py` that reads both runs' metric lines and
prints the difference at every tier, use **its stdout** as the quote with `kind: "derived"` and a
`command` alongside; the two endpoint logs sit beside it as `kind: "result"` as usual. Then a
weight change means **re-running the ruler**, not hand-editing JSON. Details in
`baseline.json -> _comment_value`.

‼️ **When the floor cannot be measured, `null` is not the only honest answer.** The floor has
four states, carried on `origin`: measured by this project (two or more `runs`, whose `run.json`
`check` reads back to verify `mode`/`scope`) is `verified`; measured before MLClaw, or on a
machine already released, is `origin: "external"` + `unchecked`, and is a `claim`; claimed to be
this project's but with unreadable run records is `unverifiable`; only what is left is
`not_measured`. **Of the last three, only `not_measured` knocks the whole round's T2/T3 back to
`[T1 trend]`.** `claim` and `unverifiable` gate T2 and do not support T3 — T3 is the last gate
before a large run, and it cannot rest on a floor this pipeline never measured itself. The
reasoning behind the four states is in `references/experiment-graph.md -> §4`.

Two rules from the same root, while we are here:
- **Cross-source comparison requires one convention.** When two sides' filtering thresholds come
  from different places, what you measured is the threshold difference, not the model difference.
- **Change the metric, the renderer or the frame sampling and the old curves are void.** To
  compare against yesterday, first split the day-over-day change into three parts (renderer
  change / frame-sampling change / model change), or you will invent an entire "overfitting"
  story, as I did.

---

## Stage 1 — audit your own code before reading any paper

**Most "techniques we don't have" are in fact "techniques we have, weakened".** This step
produces a four-state table, not a two-state one:

| State | Meaning | Evidence required |
|---|---|---|
| ✅ present | fully implemented | point at the function and the call site |
| ⚠️ present but weakened | the implementation is there, and one line kills it | **name that line** |
| ❌ absent | genuinely not there | grepped under at least two namings |
| 🅾️ absent, but deliberately | the authors spent that budget on something else | say what they spent it on |

Measured payoff: in this round's audit, "layer-by-layer iterative refinement" is ✅, but the
inter-layer references are all `.clone().detach()` → so it is actually look-forward **once**, and
DINO's look-forward-twice is the ❌; "one-to-many positives" is ✅ but **sits on the main branch**
rather than an auxiliary one (which is the actual disease). Skipping this step and listing "what
is missing" would have written all three as ❌, and sent us porting things that already exist.

🅾️ matters: a 3D paper not using deformable attention may not have overlooked it — it may have
spent the budget on its own contribution (here, 3DV-RPE). **Ask why the authors would skip it
before deciding to copy it back.**

### Stage 1.5 — take a cost profile while you are here

**A hyperparameter forced by cost may be the root of an accuracy bug.** The chain here:
3DV-RPE's cost = `nqueries × preenc_npoints` (the atomicAdd in `grid_sample`'s backward), so the
script pinned `nqueries` from the upstream default of 1024 down to 256 → in 47% of production
frames the positives filled all 256 queries → no negatives → objectness saturated at 0.999 →
NMS's greedy ordering became a coin flip.

**An accuracy failure's root cause was in the cost table.** Recognise this signature: 100%
reported utilisation at only 154 W of 700 W, TF32 entirely ineffective (0.93×), and throughput
unchanged from batch 8 to 48 — everything serialised on one kernel.

---

## Stage 2 — run the experiments that need no code change first

‼️ **This step and Stage 3 form a small loop rather than a sequence: rough proposals (a few
candidate techniques) → Stage 2 prices them → the proposal table is finalised.** So by now you
should already hold an unpriced candidate list; if not, jump to Stage 3, draft one, and come back.

Before porting, grep argparse for **switches that already exist**, and use them to price the
proposals.

Available here: `--test_no_nms` ("was the right box never proposed, or proposed and suppressed"),
`--nms_iou` (raise the threshold), `--repeat_num` (5→2→1, warm-startable), and `--nqueries`. Cost
= one eval; benefit = knowing what that Stage 3 proposal is actually worth.

**Skip this and you will spend a week porting something worth 0.2 AP.**

---

## Stage 3 — the proposal table ‼️ this step's artifact is a plan IN A FILE, not a ranking in somebody's head

**This chapter begins to exist here.** Two halves: the machine-readable half is
`stages/exploration/graph.json` (`graph.py add` creates cards), and the human-readable half is
the design document that `config.json -> design_doc` points at. ‼️ They are not copies — `check`
can only read the first, while **stopping a proposal** happens when a person reads the second.
Stage 3.5 adds tiers, Stage 5 adds implementation status, Stage 6 fills in results, Stage 8 fills
in verdicts — **all of it filling cells in the same table, never opening a new section per step.**

The reason is this pipeline's own injury: an ablation chapter written only after the runs finish
records **what already happened**, whereas its most valuable use is **stopping a proposal before
it starts**. The cost of not stopping one is rule 2.5.

### ‼️ This chapter is a queue MAINTAINED BY THE USER, not the agent's notebook

This is its normal mode of operation, not an exception: **the user adds entries at any time**
(usually one sentence, e.g. "has anyone done cross attention between SKUs"), and **the agent
works down the list in order**. So:

| The agent's three jobs | Notes |
|---|---|
| **1. Complete the card** | turn a one-line idea into a full proposal card. ‼️ **An entry missing a required field may not start** |
| **2. Execute in queue order** | finish one and fill it back before moving on. The order is the user's, but the agent must actively flag "predecessor unmet" |
| **3. Fill back** | `graph.py fill` — results onto the card, **and read the `must_review` it returns**: whatever depends on this card, whatever cites it in prose, the constants that came from this run. ‼️ That list is candidates, not a verdict; the judgement is yours. If a constant changed, sync `state.json` |

**The chapter must open with a queue table** (`# | entry | kind | parent | primary criterion |
state | card`), and it is the **single source of truth about order** — change the order by
changing that table, never by changing section numbers (those are cross-referenced).
State words: `draft` → `awaiting oracle / awaiting predecessor` → `ready` → `running` →
`filled` → `killed`.

**Required fields** (all of them): `premise` · `the premise's share on this corpus` (rule 2.5) ·
`primary criterion + predicted direction` · `guardrail` · `parent (single-key delta)` ·
**`depends_on: [entry id + what it blocks]`** (see below — blocks launching / blocks reading /
blocks nothing) · `oracle ceiling` · `what result counts as killing it` (rule 6).

### ‼️ The queue is really a DEPENDENCY GRAPH — compute the ready set first and run in parallel

A linear queue **serialises things that could have run at once**, which is pure waste. So each
card carries a `depends_on: [entry ids]` field, and the first action of any working session is:
**compute every entry whose dependencies are met (the ready set) and open all of them in parallel,
within capacity.**

#### ‼️ Parallel arms share a working tree BY CONSTRUCTION — give each one its own

The sentence above is the one that creates the problem, so it is answered here. MLClaw resolves
**one** code path per stage (`run-mechanics.md → Code snapshot`), and `code_snapshot.py` reads
that directory at launch. Two ports half-written in it, and arm A's `code_dirty.patch` carries
arm B's edits — **it applies, it reproduces exactly, and nothing raises**: `graph.py check`'s
delta guard compares `runtime_params` + `workload`, and an uncommitted edit to a model file moves
neither. A clean record of a run that did not happen.

**`graph.py claim --id N07 --by <who>` before writing a line of that card's code** — it allocates
the branch off the frozen base, records who has it, and prints the `git worktree add` to run.
‼️ **Not at launch: `run_id` is set hours later, and the contamination happens in between.** The
claim is also the only thing that works when you dispatched the two agents **separately** —
there is no orchestrator then, and nothing in common but `graph.json`. Its limit belongs with it:
an agent that never calls `graph.py` cannot be protected by it. `ready` marks what is already
taken, `set --set run_id=` refuses a second nameless concurrent arm, and `check` is the backstop —
not the gate, because by the time it runs the hours are spent. Freeze `graph.json → base.commit` when the
round opens, and **do not merge a winner while another arm is in flight** — that redefines the
control under everything still running. Mechanics, the four keys, and what may not be thrown
away: `references/experiment-graph.md → §1.5`.

#### ‼️ An edge answers "what does it block" before "does it exist"

**This is the most expensive line in this section.** There used to be only true and false here,
so every **true** dependency automatically became serial — and half of the true ones were never
about ordering at all. Three answers, written differently and with different consequences:

| What it blocks | How to write it | When it can run | When it can be adjudicated |
|---|---|---|---|
| **launching** | `"N06"` (a bare id means this) or `{"id":"N06","blocks":"launch"}` | after N06 closes | after |
| **reading** | `{"id":"N06","blocks":"reading"}` | ‼️ **now** | now as well; the verdict is automatically stamped `conditional_on` |
| nothing | **do not write the edge at all** | now | now |

**The test in one sentence: do I need its CONCLUSION, or only one value / one artifact / one line
of code from it?** Only the former is `launch`. ‼️ **Needing one parameter is not a dependency,
it is a parameter** — a parameter can be assumed, parallelised, and substituted back afterwards;
a conclusion cannot.

‼️ "Running first is not reading first" used to be written inside the noise-floor rule below,
which read like an exemption for that one number. **It is the general rule**: this graph exists
to govern the order of **reading**; the order of running is only a by-product. A default posture
of "wait for A's conclusion before opening B" costs the number of innovations times each one's
wall clock — and what it buys is, most of the time, a subtraction you could have done afterwards.

**`launch` edges are only these:**

| Dependency | Why it really does block launching |
|---|---|
| **a predecessor measurement → an arm** | rules 2.5 / 2.6: without the premise share or the oracle ceiling, the arm **may not** open |
| **a parent → a child arm** | a single-key delta needs the parent's checkpoint to **physically exist** — what is needed is that checkpoint, not the parent's verdict |
| **a shaping measurement → writing the code** | when a measurement decides "where this thing attaches", the flag cannot be written until it returns |
| **a faithful port → a self-invented deviation** | the deviation is written on top of that code; without it there is nothing to modify |

‼️ **The last row used to read "faithful port → self-invented deviation" with the reason "the
deviation's delta is measured against the faithful version, so with no faithful run there is no
baseline" — that reason describes `reading`, and it was being used as `launch`.** The two arms can
perfectly well run at once, with the delta subtracted afterwards. **There was a wrong edge sitting
in this "real dependencies" table**, which is exactly why the question above must be asked of
every edge, including the ones that seem least in need of it.

**`reading` edges: it can run, it cannot be read.**

The case: N07 (MonoFlex fusion) needed N06's σ — one value, and σ and the fusion were already two
flags in the same source file — and the graph assigned it N06's **verdict**, four and a half
hours. Until now the only way to parallelise was to delete the edge, and deleting it removed the
real half too: with σ uncalibrated, an N07 loss says **nothing about whose fault it was**.

So `reading` does two things at once: the arm opens **immediately**, and at `close` the verdict is
stamped `conditional_on: [N06]`. ‼️ **It does not block the verdict.** Blocking only shifts the
same wait one step later — into a pile of 🟪 nobody dares adjudicate, with the GPU hours still
being spent. What must be prevented was never "a conclusion reached too early"; it is **a
conclusion travelling apart from its condition**. When the upstream lands, `close` actively names
`re_read`, and `check` escalates it from `minor` to `major`; **nothing clears `conditional_on`
automatically**, which is exactly why it exists.

What if the condition does not hold — the user's own words are the criterion: **"if it loses, say
this one does not hold, and stop it."** An arm overturned by its condition costs its own machine
time; an arm not opened because it was waiting for a condition costs the whole round's wall clock.
The first is countable; the second is not.

‼️ **Three fake dependencies — edges that should not exist at all, and the commonest waste:**

1. **"Logically related" is not a dependency.** Two changes both touching geometry and both
   touching the loss can run at once, as long as the flags are independent and each parent holds.
   Related ≠ ordered.
2. **"Shares a measurement script" is not a dependency.** Write the script once and both use it;
   the two **entries** still run in parallel. A dependency hangs on an **artifact**, not on a
   **tool**.
3. ‼️ **The noise floor is nobody's predecessor.** It does **not** have to run alongside the main
   arms — the run card pins the code, data and config, so measuring it days later is equally
   valid. The floor blocks only **the wording of a reported number** (`[T1 trend]` vs "measured"),
   and blocks no arm from starting. This repo once treated it as a predecessor and believed the
   whole round was stuck.
   ‼️ **This is not a privilege of the noise floor; it is an instance of the general rule above** —
   it is listed separately only because it is the one most often mis-hung as a predecessor, not
   because its reasoning differs.

‼️ **When a user hands over a list of changes and asks "right?", that is the moment to draw the
dependency graph, not the moment to nod.** That "right?" sounds like a request for confirmation,
but it is submitting **an order nobody has checked**, and the cost of a wrong order does not
surface until several arms have run. The action is fixed: redraw the list as a dependency graph
and **ask of every edge "what does it block"** —
- it fits one of the four `launch` shapes above → keep it serial, and say why;
- "I only need one value / one artifact / one line of code from it" → `reading`, **open it now**,
  with the verdict conditional;
- it fits none of them ("they both touch the same mechanism", "logically related", "feels like it
  should come first") → **do not write the edge at all**.

Say explicitly which ones can actually run in parallel and which can be reordered. Simply
confirming the user's order pushes a check that costs nothing now into a position several arms
later. ‼️ **And the default answer leans toward parallelism**: an edge mislabelled `launch` costs
wall clock and does not surface (the queue looks entirely normal, only slower); mislabelled
`reading`, it costs one arm's machine time and `conditional_on` shouts about it. The two errors
differ in visibility by an order of magnitude.

**The hard ceiling on parallelism is stable capacity, and exceeding it is a total loss rather than
a slowdown.** The criteria and the four silent failures are in **`references/cluster-ops.md`**; in
one sentence: an arm whose "preemptible residency < checkpoint interval" produces **structurally
zero**, and it churns the arms that already hold slots; arms compared within one group **must be
on the same GPU model** (a different card is a different kernel); **offline batches use no GPU and
always come first**.

**Parallelism introduces three failure modes that do not exist when running serially:**

1. **Uneven progress.** Comparisons must be locked to **the same step/epoch**, never "each one's
   best" — and especially not across a warmup boundary, where the ranking reshuffles wholesale.
2. **Seeds mixed into the delta.** Parallel arms each carry a seed, so training randomness is
   inside the difference, and the noise floor is not there yet ⇒ the report must explicitly say
   "single seed".
3. ‼️ **Code drift.** Serially, you know when you changed the code; in parallel, "the current
   code" is an ambiguous notion. **Declare the code snapshot hash at the moment each arm starts**
   and check consistency afterwards — otherwise several arms were not running the same code, and
   nothing raises.

‼️ **How nodes are created, how the ready set is computed, how a fill propagates, how to discover
the graph has broken** — all of it is in **`references/experiment-graph.md`**. That one is the
operating manual; this keeps only the judgement.

### Rule 2.6 — **anything whose ceiling is measurable at zero cost must have its ceiling measured first**

If a proposal can be asked, by pure offline computation **on existing predictions or
checkpoints**, "how much would it win if it worked perfectly", that oracle **must come first**,
and the arm queues behind it.

**The cost is measured**: this repo spent an entire section arguing for "regress the near face
first, then the depth", and one oracle then pinned it — **fixing size to perfect changes IoU p50
by −0.000 rig / +0.015 fleet**, so the ceiling is zero. An oracle costs **three orders of
magnitude** less than an arm, occupies no GPU, joins no queue, and needs no noise floor.

‼️ The corollary: **when the queue has "nothing to do", the right move is to pull an oracle
forward, not to open an arm.** Offline batches use no GPU, so they should never be held up by
capacity.

| # | Technique | Source | Which measured failure (FINDINGS #) | Mechanism in one line | **Premise** | **Premise's share on this corpus** | **Pre-registered metric + predicted direction** | **Guardrail** | Size of change | Rank |
|---|---|---|---|---|---|---|---|---|---|---|

Rules:

1. **Rank by "this failure's share × mechanism plausibility ÷ size of change", not by how recent
   the paper is.**
2. **The pre-registered metric may not be AP alone.** Write down which **failure count** this
   technique should reduce. If AP rises at the end and that count did not move, the mechanism was
   not confirmed — you got lucky, or you changed something else.
2.5. ‼️ **Every proposal must state its "premise", and measure that premise's share on THE corpus
   this round will actually run, then and there.** The premise is what the world must be like for
   the technique to hold (e.g. "one-to-many starves the negatives" has the premise that
   `repeat_num × G ≥ nqueries` happens often). **The share is a pure data quantity: no model, no
   GPU, one pass through the loader.**
   - Share < 10% ⇒ that proposal is **tiered down or not queued at all**, however elegant the
     mechanism.
   - The share quoted from elsewhere (a paper, another dataset, last round's corpus) without being
     re-measured on this corpus ⇒ **treated as having no premise**.

   **The cost is measured**: this repo's F1 (one-to-many starving the negatives) was justified with
   "47% of production frames exceed the zero point", while this round trained and evaluated on a
   different corpus where the measurement is **only 4.62%** (G: p50 23 / max 56, zero point at 52).
   An order of magnitude out, and **five arms ran for nothing**. Preventing it needed one pass
   through the loader counting G — equally cheap before or after starting, the only difference
   being that doing it beforehand saves five arms.
   ‼️ **Tests do not catch this**: `tests/test_one_to_one.py` pins that arithmetic entirely
   correctly, because it constructs G itself. **It verifies the formula, not which corpus the
   formula's inputs come from.**
3. **Size of change comes in three grades: no architectural change / local / structural.** At equal
   benefit the no-change option wins, and "no change" must be explicable (same head, same output,
   only the loss target swapped). The user's principle is **architectural uniformity first**:
   whatever can be expressed by one unified architecture should not get a branch for a special
   case. ‼️ **This column is not an independent cost measure; it is one of the three components of
   Stage 3.5's V axis** (V = code availability × size of change + data cost). Do not write two cost
   systems in two places; they will fight.
4. **Write down dependencies.** Example: quality-aware classification plus denoising queries must
   land before switching to one-to-one, or you lose one-to-many's convergence speed without gaining
   a basis for ranking.
5. **`task-driven` proposals are allowed** (driven by Input B alone, with no measured failure behind
   them). But they must be labelled, and must state **which data or which measurement is missing**
   before they can be verified. They do not participate in the ranking above; they queue separately.
6. **Every row must be eligible to be `killed`** — a proposal that cannot state "what measurement
   would kill this" is itself unfit. Read the four already dead (with their revival conditions) via
   `graph.py status`'s `killed` **before listing proposals**, so nobody proposes one again.

---

## Stage 3.5 — verification budget: three axes decide HOW HARD to verify

**It is not "verify / do not verify", it is "verify to which tier".** Score every proposal on three
axes (high / medium / low):

| Axis | The question | How to estimate |
|---|---|---|
| **V, verification cost** | how many GPU hours, how much code, is new data needed? | an existing flag = very low; only a loss target swapped = low; touching the matcher or attention = medium; needing annotation = high |
| **P, prior** | did I expect this to work in the first place? | does the mechanism point directly at a measured failure + has it been reproduced across papers/repos + is it one flag in the reference implementation |
| **U, project value** | what is it worth if it works? Does it move a quantity downstream actually uses? | compute from the **share** of that failure in FINDINGS, never from the improvement quoted in a paper |

‼️ **The V axis is mostly decided by "is there open-source code", not by "how many lines".** With no
code, the largest cost is not writing it — it is that **you must first invent it yourself, and then
spend a training run discovering the invention was wrong**:

| Code availability | V | Notes |
|---|---|---|
| official implementation + its own ablation flag | very low | you are porting a flag, not a design |
| official implementation | low | there is a line-by-line target |
| third-party reproduction only | medium | check its numbers against the paper's before deciding whether to trust it |
| paper only (with complete pseudocode) | high | **the hallucination-risk zone starts here** |
| only a paragraph of prose in a paper | very high | T0 unless U is extremely high |

| Tier | When | How to verify | Label required on any reported number |
|---|---|---|---|
| **T0 do not verify** | U low + V high | do not do it; put it on the "not doing" list with a stated reason | — |
| **T1 trend check** | **P high** (you already expect it to work) | short run / subset, looking only at the trend plus the pre-registered failure count, **no requirement to clear the noise floor** | `[T1 trend]` |
| **T2 single controlled comparison** | P medium + V low-to-medium | re-run the control on today's code, one thing at a time, **must clear the noise floor** | `[T2 controlled]` |
| **T3 ride a large run** | V high but U high, **and a small ablation genuinely cannot answer it** | attach to a full training run that was happening anyway: flag defaults off, one branch with it on | `[T3 large run]` |
| **T4 cheap approximation** | V high + P/U medium | port its **cheap approximation** and use it to price the full version | `[T4 approximation]` |

Seven accompanying rules:

1. ‼️ **The tier is written with the number.** A `[T1 trend]` conclusion may not be cited next week
   as a `[T2 controlled]` one — **a soft number being promoted into a hard conclusion** is the
   easiest way to die on this pipeline (that false noise floor this round came from exactly there).
2. **A cheap check has low power: it can give you a reason to continue, not a reason to rule
   something out.** A high-U proposal that looks bad at T1 gets **tiered up**, not killed; a low-U
   proposal that looks bad at T1 may simply be shelved.
3. ‼️ **An approximation failing cannot refute the original.** T4 must state what the approximation
   dropped, or you will kill a good idea with a bad proxy.
4. **T3 is not free: it uses the one variable slot in that large run.** A large run carries one
   technique (unless machines are running in parallel), so the ordering of T3s is itself a decision.
5. ‼️ **A detailed metric is a tier-DOWN tool, not merely an attribution tool.** (User ruling,
   2026-08-13.) Aggregate metrics (AP/AR) are **slow**, and are confounded together by convergence
   speed, thresholds, NMS and data convention; detailed metrics (the negative-sample share, the
   score distribution, the count of that failure in FINDINGS) **respond immediately** and point
   straight at the mechanism. So **swapping in a fast-responding pre-registered metric often turns a
   T3 large run into a T1 short run** — the main way to save money.

   Example: verifying one-to-one / NMS-free by AP requires running to convergence (expensive); but
   the hard error it fixes is "`repeat_num 5` × `nqueries 256` → p50 255/256 are all positives → the
   classifier never sees a negative → objectness is 0.999 everywhere", and **the negative-sample
   share and the score distribution are visible immediately in a short run**. With the metric
   swapped, a `--repeat_num 2` short run is a fair and sufficient verification.

   ‼️ **The boundary, or this slides into "the detailed metric looks good, declare victory"**:
   **detailed metrics decide "should we continue"; aggregate metrics plus a person's eye decide
   "should we ship".** A confirmed mechanism ≠ better delivery — that is exactly the "faithful but
   off target" cell in debate ⑤'s 2×2.

   ‼️ **Short runs are directionally biased**: they systematically **underestimate changes that slow
   convergence** (one-to-one is one, and H-DETR is a whole paper about it), while favouring changes
   that speed it up (CDN, denoising queries). So when queueing, **let short runs verify what they
   can verify fairly**; a slow-converging change either gets a fast-responding metric (above) or has
   to run fully — do not judge it lost on a short run.
6. ‼️ **Default to orthogonal, small local ablations; do not do whole-architecture comparisons.**
   (User ruling, 2026-08-13.) Verify each change **individually**, **assuming they do not interact**;
   combinations and cross-validation come last, and whether to do them at all is decided by
   **analysis**, not by default. A whole-architecture A/B is expensive and confounded — it moves many
   things at once, and a result from it cannot be attributed. The unit of verification is "one small
   local ablation", not "one large run".
7. ‼️ **T1 also needs a pre-registered pass criterion, written before the run.** "The trend looks
   right" is not a criterion — write it as "at epoch N the pre-registered count drops ≥X% and no
   guardrail metric regresses". Without it, T1 degenerates into reading pictures, and it is exactly
   what the decision to promote to T3 rests on.

### The tier decides which instruments are used (debate needs a budget too)

‼️ **Consistency at the meta level: if I require a verification budget per proposal, debate must
have one too.** Convening all six debates in one round is 15+ agents, so go by tier:

| Tier | Numeric criteria | Cheap checks | Debate | Human blind review |
|---|---|---|---|---|
| **T0** | — | — | ④ (confirm it really should be abandoned) | — |
| **T1** | pre-registered count + guardrail, **no noise-floor requirement** | overfit a single batch | **⑥** | only when the metric or the geometric representation changed |
| **T2** | **clears the noise floor** + three numbers moving together | the full set | ⑥ + ① | when the pre-registered metric and AP move in opposite directions |
| **T3** | as T2 plus guardrails | the full set | ⑥ + **⑤ (must complete before the large run)** + ① | **mandatory** (the last gate) |
| **T4** | pricing only, **never a conclusion** | overfit a single batch | ⑤'s C — did the approximation approximate the mechanism away | — |

Three that are tier-independent: **⑥ is mandatory at every tier** (it is the cheapest, and it
decides the meaning of every number afterwards); **② is mandatory whenever a new ruler is written**;
**③ only when the tiering itself is disputed**.

➜ **The tiering table lands in `stages/exploration/state.json -> tiers`**; the format is in
`lifecycle/exploration/state.json`'s `_comment_tiers`.

---

## Stage 4 — find the paper, and even more the code

‼️ **Read `references/porting.md` before starting** — how to search (three keyword layers · across
fields, strip the domain vocabulary and keep the geometric property), grading evidence (**used by a
top conference ≠ ablated on its own**, which this repo hit twice in the same reference repository),
and which to believe when **the paper's prose says A while its own training script runs B**.

1. **Find the official implementation first**, and set V by Stage 3.5's code-availability table. No
   official code is not merely "a bit more work" — it pushes the proposal into the hallucination-risk
   zone.
2. **Clone it outside the repo** (`~/agent_space/<topic>/ref/` or `third_party/reference/`), record
   `repo@sha`, and **do not turn it into a dependency**. Read the LICENSE before copying.
3. **The most valuable find is the reference implementation's own ablation switch.** If it has a
   flag, you are porting a flag rather than a design.
4. **Read the config diff and the loss/matcher, not the prose. When the paper says X and the code
   does X', believe the code.** Focus on four places: how the loss target is constructed, the
   matcher, how queries are initialised, and **which branches exist only in train** (denoising
   queries and the like, absent at inference).
5. Record the key excerpts and line numbers in the design document; do not leave only a repo name.

---

## Stage 4.5 — the interface comparison table (the only deliverable before writing code)

‼️ **Do not write an implementation against an "idea"; write it against those specific lines of the
reference code.** When an agent invents its own implementation, a missed condition or a half-turned
semantic **raises nothing** — and **the self-correction cycle is one training run** (hours to days).
So hallucination has to be blocked before the code is written, and what blocks it is an **obligation
to cite**, not verification after the fact.

The flow is four steps, not two: **idea → paper → its location in the code → align the interface
item by item → only then write.**

| Concept | What the reference calls it (`repo@sha:file:line`) | What we call it | Semantic difference | **First judgement** | What to do about what is missing |
|---|---|---|---|---|---|

**The "first judgement" column takes irrelevant / undecided / fatal, defaulting to "undecided".**
This table is filled in twice: once by you before porting (so you know which differences must be
handled), and again by role A of debate ⑤ against the diff after porting — **the cells where the two
disagree are what to watch.**

- **Locate before understanding.** "Which functions does this technique live in" must be answered
  before "why does it work"; failing to locate it means you have not yet established what it actually
  changes, and no amount of familiarity with the paper licenses writing code at that point.
- ‼️ **The "semantic difference" column is where the bugs hide.** Typical: the reference's box is
  normalised cxcywh while ours is `center_reg × anchor_size + anchor_center`; the reference's
  "positive" means Hungarian-matched, while ours also includes the copies made by `repeat_num`.
  **Same name, different meaning is more dangerous than absence, because it raises nothing.**
- **Missing items require an explicit decision**: find an equivalent / add a minimal implementation /
  **abandon that part and write down what was abandoned**. The third is commonest — half a technique's
  effect is not that technique's effect, and unwritten it becomes "that technique doesn't work" next
  round.
- **Branches that exist only in train in the reference get their own row** (denoising queries,
  auxiliary heads, EMA, warmup scheduling); these are the easiest to miss, because inference running
  successfully creates the illusion the port is complete.

## Stage 5 — the port

- ‼️ **Every change must point at a line in the reference implementation**, with
  `ref: <repo>@<sha>:<file>:<line>` in a comment or commit message. **What you cannot point at is
  what you invented** — label it `original` separately, and **raise its verification tier
  automatically** (Stage 3.5): it has no target, its only check is the training result, and that
  cycle is long. At the end, mechanically re-check that every hunk carries a citation (this is a
  check, not a debate).
- **One technique, one branch, gated by a flag, defaulting to off.** Then an ablation is one flag
  flip and the control is the same binary. ‼️ **"Branch" here is an `if`, not a git branch — and
  the git one is a separate requirement, not an alternative.** Write the flag in this arm's own
  worktree (`references/experiment-graph.md → §1.5`); the flag is what makes the *ablation*
  controlled, the worktree is what keeps the *record* of two concurrent ports from being one
  record twice.
- ‼️ **The most frequent porting bug is "a config item was added but never passed down", and it
  never raises.** A real case this round: `infer_9dof.py` gained `voxel_size` / `thin_cloud` while
  neither dataset construction site passed them, so weights trained on 145k deduplicated points were
  fed a random sample with 81% fewer occupied cells — silent, no exception, just worse. The
  countermeasure: write a test that **walks every call site** (AST over every construction call,
  asserting the parameter is explicitly passed), rather than testing the function alone.
- **Verify a port by counting, not by shapes.** A matching shape does not mean a matching object set
  (did the denoising queries actually enter the attention mask? did the positive count change?).
  Enumerate independently; do not read `.shape`.
- **New or renamed parameters need a load path from old checkpoints** (this repo:
  `_RETIRED_PARAM_MARKERS` in `utils/io.py`, and it must be a **mapping**, not an exemption).
- Run the repo's tests after changing; if they fail, first establish whether somebody else's
  in-flight refactor is responsible, and do not casually edit their files.
- ‼️ **Wrap up with an acceptance debate (three-way alignment); passing the tests is not "the port is
  done".** Tests can prove the parameters were passed down; they cannot prove "we actually got their
  trick", nor "that trick was ever addressed to our requirement". See debate ⑤ below and
  `references/debate-roles.md`.
- **The single most useful cheap check: overfit a single batch.** With the mechanism wired up, one
  batch should reach near-zero loss; failing to means the gradient does not flow or the targets are
  constructed wrongly — a few minutes to ask, instead of waiting for a large run.

## Stage 6 — verification and ablation

- **The control must be re-run on today's code**; reading a number out of yesterday's log is not
  allowed.
- **Change one thing at a time.** Warm starting is fine, but say that it is a warm start.
- **Report the pre-registered metric, AP, and that failure's count — all three together**, with
  Stage 3.5's tier label. Only three moving the same way counts as the mechanism being confirmed.
- **The criterion must be an independent ruler + a built-in control + a control group.** A criterion
  with a filtering step will filter out the very failure it should detect.
- **Choose control samples by explicit pairing, not by adjacent log lines.** This round `grep -B2`
  picked control frames, and because the log order is tally → saved → rid it picked the previous
  frame and raised a false alarm.
- **Write down what was not run.** Silent truncation (top-N, sampling, no retries) reads as "full
  coverage".

### ‼️ Fixing the implementation-layer traps does not make the design sound — and design-layer traps are invisible on val

The question that comes most naturally once the port is done and the unit tests are green is "so
there are no design-level problems left, right?". The answer is usually no, and the two layers' costs
are completely asymmetric:

| | What can catch it | When it surfaces |
|---|---|---|
| **Implementation layer** | unit tests / overfitting a single batch | minutes |
| **Design layer** | no offline check catches it | **after a full training round** |

‼️ **Unit tests can only prove the wiring is connected** — the configuration propagated, the mask is
computed right, same-named things were not written with different meanings. They cannot prove "this
design holds for this task".

The most expensive class is **the training input and the deployment input are not the same
distribution**, and it has a mechanism that must be stated aloud: **val is sampled from the same
distribution as training, so a distribution mismatch never shows up on val — only on the machine.**
This repo's case: training consumed SfM+MVS reconstructed point clouds (points on every face, dense
and uniform, with reconstruction artifacts), while deployment consumes single-view sensor depth (only
the camera-facing side, with holes, attenuating with distance). For a detector encoding "a key point's
position relative to the query box's eight corners", the MLPs for the four far corners never receive
valid input at deployment — the model learns a prior that does not exist in deployment, and val stays
green throughout.

**Listing risks without explaining "why val cannot detect it" is the same as not explaining** — that
mechanism is the reason anybody agrees to go and check the input distribution before the run finishes.

### ‼️ "Testing the mechanism" and "pricing it" hang off DIFFERENT parents, and only the latter is reported

The same change hung on different parents commonly differs by more than a factor of two in delta —
**especially when it treats the same disease as something already present**.

| Purpose | Which parent | Reported |
|---|---|---|
| **Testing the mechanism** | the one where the mechanism shows most clearly (the configuration with the largest share of that disease) | internal use, **not reported outward** |
| **Pricing** | **the configuration that will actually ship** | ‼️ **report only this** |

**This repo's case**: `--rep_gt` addresses F2, and CDN had already pushed `crossing_frac` from 0.2600
down to 0.1145. Measured against `e1` (no CDN) the delta was more than twice that measured against
`e3` (with CDN), and the shipping configuration will probably include CDN. **Promoting a loss that
will ship alongside CDN using the large delta from e1 is another face of "cross-source comparison
requires one convention"** — the denominators of the two comparisons (how much disease is left to
treat) are simply not the same.

This refines Stage 3's rule 3, "only arms differing by one change are comparable": **differing by one
change is not enough — that change's value also depends on what the parent already contains.**

## Stage 6.5 — human visual acceptance (this gate cannot be delegated to an agent)

‼️ **Numeric verification can become circular: when the criterion and the implementation under test
share an assumption, the numbers move the wrong way and you will believe them.** This project's most
expensive instance was that shape — **AABB**: NMS, GIoU and AP were all axis-aligned, so rotation
errors were numerically invisible and **the scores were even higher**. With the same wrong assumption
sitting inside both the model and the ruler, no amount of ablation can measure it, because both sides
are wrong together.

There is only one way out: **a ruler that shares no assumption with the implementation.** The picture
is that ruler. And — you were right — **large models' understanding of images / video / 3D is
unreliable, so this gate must be a person.** An agent may not look at a rendered screenshot and
declare "it looks right"; the agent's job is **to prepare what the person must look at so it can be
scanned in minutes.**

➜ How to choose frames, the two independent visual channels for blind review (**one channel cannot
detect its own errors**), the discard rate as a metric, and how a person's judgement becomes a count
written back to `findings.json`: `references/human-review.md`.

## The experiment record standard (the run card) — the only thing still standing after several rounds

‼️ **If an experiment's conditions live only in a chat log, it is not an experiment. It is an
anecdote.** Once there are several arms, several rounds, and **several different orderings**, "why is
this number different from that one" can no longer be answered from memory — and that is precisely
what debate ① exists to adjudicate. **A debate with nothing to cite produces opinions.**

The rule: **one card per run, written before the run, results appended only afterwards.**

Four hard rules (in one-line form; **the full argument, the seven mandatory fields, and the measured
lessons from the four ways the record layer breaks are in `references/run-card.md` — read it before
opening an arm**):

1. ‼️ **The delta must be computed, not described.** `check` diffs two cards' configs and asserts the
   result equals `declared_delta`. **More than one key of difference is not a controlled experiment**,
   and no result can be attributed to any one of them.
2. ‼️ **`parent` is not `warm_start_from`.** The first is "who it is read against", the second is
   "where the weights came from". **A run with no parent cannot be attributed.**
3. ‼️ **Comparability fingerprint = hash(data identity + metric script + thresholds), and two cards
   with different ones may not be compared** — `check` refuses outright. This is the machine-executable
   form of "change the metric and the old curves are void" and "cross-source comparison requires one
   convention" — previously remembered, which is to say not enforced.
4. ‼️ **Store the bytes, not only the hash.** A hash only answers "these two differ"; **only a copy
   answers "where"**, and six weeks later the second is what you need. Storage cost should not enter
   this decision; when in doubt, store more.

‼️ **The record layer breaks on its own, and more insidiously than the experiment does** — four ways,
none of which raises, and the most expensive sentence among them: **a guard that alarms because of its
own version drift is a guard people learn to ignore**; while **enumerating the empty set** makes the
guard report the very conclusion it exists to exclude (`sha256("")` is identical for every run, so
`check` says "same code"). **A rubber stamp is worse than no stamp, because a stamp gets believed.**

‼️ **A completely correct test can give completely wrong confidence** — it verifies the formula, not
which corpus the formula's inputs come from. The four failure shapes, the countermeasures, and why the
tests written for these traps also pass vacuously are all in `references/run-card.md`.

**Debates must cite run ids.** Keeping only the conclusion and not the strongest rebuttal means nobody
next round knows what this has already been challenged on.

## Stage 7 — land it in the document (**update**, do not write anew)

‼️ **That chapter already existed as of Stage 3** (plan before implementation — the user asked for this
explicitly). This step **updates it to the current state**; it is not where the writing starts:

| What this step adds to that chapter | What the chapter already has (written in Stage 3 / 3.5) |
|---|---|
| each proposal's **result** and verdict (including tier labels such as `[T1 trend]`) | the proposal table, pre-registered metrics, guardrails, premises and shares |
| the **killed-hypothesis list** (`killed_by` + `revive_if`, the four never interchangeable) | the ranking and its reasoning, tiers T0–T4 |
| **experiments not yet run**, their expected benefit, and **what is blocking them** | the four-state audit table |

**Opening a new section = this round's plan did no work.** If you find while writing that "there is no
cell in the table for this result", Stage 3 missed a column — **go back and add that column**, rather
than appending a narrative paragraph at the end of the document. A narrative cannot be read for "which
proposals are still alive"; a table can.

An agent has no memory across contexts, and only what was written down is still there next round — and
**a structure that can be updated survives a second round better than prose that can be understood.**

## Stage 8 — closing out: writing back, and technical debt (this pipeline's feedback edge)

The first seven steps are linear. **A round must write back when it ends, or the next one starts from
zero.**

‼️ **"Should this round stop" is a judgement, not a point in time** — the criteria are in
**`references/explore-or-stop.md`**: three questions must be passed before stopping (has the disease
changed / is there still an oracle ceiling above the noise floor / is anything "won but unattributed"),
plus **six false "the search is finished"**. The default answer leans toward continuing, because the
commonest cause of "no improvement" is not that the search is finished — it is that **the criterion is
broken**.

The compute-side wrap-up is in **`references/cluster-ops.md`** — ‼️ **do not execute release as a
wrap-up step**: instances and orphaned disks are swept every 15 minutes by the resident
`net.miniclaw.gpujanitor`, with results in `~/.claude/miniclaw/gpu_janitor.json` (check whether
`generated_at` is still moving first — that is its liveness signal; only when it stops do you fall back
to doing it by hand, because **the command that lists instances silently returns empty on the wrong
project**). What is left in your hands is only the two things the janitor cannot see in a cloud API:
**moving the checkpoints off**, and **the run cards landing on disk**.

1. **Write back to `findings.json`**: re-measure that failure with **the same `measure.script`** and
   change `value`; do not add a new field. Whatever the share fell to, write that — it is the only thing
   that can prove the mechanism was confirmed. The human blind-review ratio is also written back, as a
   `source: "human"` entry.
2. **Update P (the priors)**: it worked → raise P for the same family (same paper, same family of idea);
   ported faithfully with no effect → lower P for the family. **P learns; it is not a constant read out
   of a table.**
3. **Writing `status: killed` requires one of the four `killed_by` values**: `share_too_small` /
   `wrong_mechanism (vetoed by ⑤'s C)` / `faithful_but_inert` / `blocked_on_data`. The four revive
   differently, and writing them interchangeably is the same as not writing them.
4. ‼️ **`killed` has an expiry; it is not a permanent grave**: every entry carries a `revive_if` — "what
   change should bring this back". A changed metric, a changed data distribution, or a dependency
   landing can all invalidate an old kill. ("Change the metric and the old curves are void" applies to
   killed entries too.)

### The technical-debt gate

‼️ **This pipeline structurally produces a mess**: one flag per technique, defaulting off, one branch
each — and after a few rounds the dead flags, the two coexisting conventions, and the never-tested flag
combinations pile up. **This is the rules' cost, not an accident**, so walk it explicitly at every
close-out, and when it is worth it call the `agent-refactor` skill to clean up:

- **Dead flags**: flags left behind by techniques tiered T0 or `killed` are pure liability — delete them
  (through the checkpoint's retired-param **mapping**, not an exemption).
- **One concept with two coexisting conventions** (two definitions of a positive, two box
  representations) — ‼️ this is the breeding ground for "same name, different meaning" and AABB-class
  errors, and it has the highest priority.
- **Flag-combination explosion**: of the combinations of n flags, only two or three paths were ever
  tested; the rest are a hallucination incubator.
- Shotgun changes (change one thing, edit three places), and tests getting slower and more brittle.

Two hard constraints:

- ‼️ **A refactor must sit BETWEEN two experiments, and must not change behaviour.** The acceptance
  criterion is the one from the cheap-check catalogue: **with every new flag off, bit-exact with the
  pre-refactor state.** Mixed into a round of experiments, Stage 6's control is void (it violates "change
  one thing at a time").
- **No refactor before this round's conclusions are fully written back**, and none while a large run is
  in flight — otherwise the code that ran and the record of it do not match.
- ‼️ **A merge is a code change between two experiments, so it obeys both rules above** — and
  `graph.py land` is the executor: it refuses while the round is still open, orders the merges,
  attaches each one's re-verification, names the flag combination two winners create, and lists
  the branches that may not be deleted. The
  winning arms land at **close-out, not on winning**: `graph.json → base.commit` is frozen for the
  round, and moving it under an arm still running voids that arm's control by exactly the
  criterion this stage is judged on. Merge them **one at a time, each re-verified with every new
  flag off** — two winners from one round is a flag combination nobody ran, which is this
  section's third bullet arriving by a different door. The losing branches are not tidied up in
  the same pass: a `killed` card's `revive_if` means going back to that code, and `run-card.md`
  rule 4 says what a patch is worth once its commit has been GC'd. `/ara build` is what makes
  them survive; a branch is not a retention policy.

### The definition of a round ending

**The T3 queue advances by one** (it occupies that variable slot), while several T1/T2 may run in
parallel. Default to small local ablations (rule 6); **whether to run the cross terms is decided after
every individual has been verified.** A round ends when **that `findings.json` entry's `value` has been
re-measured**, not when "the code was merged".

### ‼️ Then always call `/conclude` — there are no conclusions in `graph.json`

`graph.json` records **which arm won** (`verdict: won`, `+1.31`, `killed_by: faithful_but_inert`). That
is a **result**, not a conclusion. What actually gets repeated after a round is the sentence "so
one-to-one does / does not help on this corpus", and it carries three things `graph.json` does not have
at all: **which corpus it holds for, what tier it is, and what measurement would overturn it**. The
cards are sealed when the round ends and nobody opens them again six weeks later; that sentence, though,
will be quoted — and quoted about a corpus it was never measured on.

So at close-out, ask once per `closed` card: **what refutable belief did this give us?** If there is one,
`conclude.py add` it, attaching that card and that run as evidence (`--quote` transcribes the line; never
a summary). If there is none, that is fine — an arm winning does not necessarily produce a belief.
**Do not manufacture conclusions to fill in a table.**

Once the conclusions are in, **run `/ara build` once more** — and this round's output then has a shape
somebody else can read: `src` is the input (code + config, which in an architecture search **is**
reproducibility itself), and `evidence` / `logic` / `trace` / `weights` are the output. **No machine
needs to be about to be destroyed**; `/evacuate` merely *forces* the same thing to be finished when one
is.

‼️ The reverse also lives here: **an old conclusion may have just been refuted by this round.**
`conclude.py check` marks whatever rested on it `contested`, and that batch is next round's candidate
list — a candidate list somebody has **already bet on**.

---

## Exit: this skill's own premise must be refutable

‼️ **This pipeline assumes the disease is in the architecture. That assumption must itself be refutable,
or it will perpetuate itself** — queueing another proposal is always easier than admitting you were
working at the wrong layer. On any of the following, stop and go back to the data / the annotation / the
task definition:

- **Two consecutive rounds** in which every proposal ends `killed_by: faithful_but_inert` — ported
  correctly with no effect means the wrong layer was changed;
- the largest-share FINDINGS entry is one where **the labels themselves are ambiguous** (here, 25.3% of
  labels are ambiguous by 74–210 mm in depth, while face precision is only ±12 mm) — no architecture
  suppresses annotation variance;
- the error magnitude is the same order as **sensor or annotation reproducibility** (go and measure the
  difference between two annotations of the same thing);
- the pre-registered metric is already saturated (volume IoU p50 0.882, 81% > 0.8) — **changing the ruler
  pays better than changing the architecture.**

**"Should we exit" is itself worth a ④-style debate**, because the default inertia is certainly to queue
another proposal.

---

## Multi-agent debate: only in six places, and always with an adjudication rule

Debate degenerates most easily into two agents nodding at each other. Four preconditions make it useful:

- ‼️ **Ask first whether to convene: if a question on the checklist can be answered by one command, run
  the command instead.** "Has the noise floor been measured" → run twice at the same weights; "is the
  control on today's code" → look at the git sha. **Debate is for questions where the evidence is
  exhausted or expensive to obtain, not a substitute for a cheap measurement.**
- **The opposing side gets independent input and may not see the proposer's conclusion first** —
  otherwise it will only patch holes around that conclusion. This is the "independent ruler" principle
  applied to agents.
- **Rebuttals must land on a fixed checklist** (one is given for each of the six below). "I don't think
  it works" is not a rebuttal.
- **There must be an adjudication rule**: a structural rebuttal (convention, number of variables, degrees
  of freedom, unmet dependency) **may not** be answered with "but the paper gained 2 points / but our AP
  went up" — **a score is not an answer to a structural objection.** This is the same rule as
  `model-first-then-fit`'s Proponent/Skeptic adjudication; keep them consistent.

The six, ordered by value for money (**the role prompts, each one's rebuttal checklist, the blind-input
protocol and the adjudication record format are all in `references/debate-roles.md` — read it before
convening**; this keeps only "which is worth convening, and why"):

| | Where it is convened | Why it is worth it |
|---|---|---|
| **①** | Stage 6 result interpretation | all three of this round's crashes (the false noise floor, the voided old curves, the cross-source convention) are on its checklist, and one dedicated skeptic would have stopped every one |
| **②** | red-teaming Input A's quantification script | **this round's worst errors were in the measurement code, not in the inference** — attacking the ruler is worth more than attacking the conclusion |
| **③** | Stage 3.5 tiering | the P and U axes are purely subjective, and one person's optimism decides directly where the money goes |
| **④** | Stage 3's proposal table | Proponent + Skeptic; the most frequent hit is "this technique is already in the code" |
| **⑤** | Stage 5 port acceptance | **tests can only verify that the wiring connects, never whether a given difference matters** — the source code will not tell you which is which |
| **⑥** | the moment the pre-registered metric is written down | ② asks "is the ruler implemented correctly", ⑥ asks **"should the ruler have this shape at all"**. In the AABB case the script computed entirely correctly and computed the wrong thing; not one item on ②'s checklist would have stopped it |

Three judgements extracted from those six that must stay in this document:

- ‼️ **In ⑤'s 2×2, "faithful but does not hit our disease" = the proposal is wrong, not the
  implementation.** That cell is the most expensive — the instinct is to go and tune the implementation,
  which takes weeks. Acceptance must be complete **before turning the flag on and before any large run**,
  or it wastes T3's one variable slot. (The other three cells are in `references/debate-roles.md`.)
- ‼️ **Pre-register at least two metrics: one that SHOULD move, plus a guardrail that should NOT.** With
  only one, you cannot distinguish "genuinely better" from "the error was moved somewhere else".
- ‼️ **The four outside ⑥ are answered from a table; do not convene** (precondition 1): **has the noise
  floor been measured** (one that has not may not be a pre-registered metric), **how far from
  saturation** (volume IoU p50 is already 0.882 — further gains have no resolution), **how much of the
  range is manufactured by artificial constants** (part of the 60 mm face-slab's range IS that 60 mm),
  and **direction and units** (`worse: up/down`, millimetres or a ratio).

**Do not convene a debate here**: Stage 1's four-state audit (that is reading code, and what it needs is
an **independent blind re-review**, not a debate — I wrote three existing techniques as ❌ once), Stage 2
(running flags), Stage 4 (finding code, where what is needed is a **parallel multi-route search**: by
paper name / by operator name / by config flag name / by issue discussion).

### Adjudication

**The main agent adjudicates, not the debating sides.** The default verdict **splits by what is under
debate; do not conflate them**:

| What is under debate | An unanswered structural rebuttal → | Why |
|---|---|---|
| **a conclusion / a number** ("this change works") | **does not hold** | a false positive (believing in an improvement that does not exist, then building on it) is far more expensive than one more round of verification |
| **a proposal / whether to try it** | **tier it down or add one measurement**, not kill it | cheap checks have low power (Stage 3.5 rule 2). The one exception: the rebuttal is "this technique is already in the code" — that is a factual error, and it leaves the table |
| **an implementation / was it ported correctly** | **no large run**; do that cheap check first | starting a run with an "undecided" unresolved spends hours to days asking a question answerable in minutes |

**Serial by default.** When starting agents, state how many, in what roles, and what the adjudication
was. **A debate whose adjudication is not written down did not happen** — keeping only the conclusion
and not the strongest rebuttal means nobody next round knows what this has already been challenged on.

➜ The six roles' **prompts, rebuttal checklists, blind-input protocol and six-line adjudication record
format**: `references/debate-roles.md` (copy them directly; a subagent cannot see the conversation, so
the prompt must carry the raw material itself).

---

## This skill's own crash record

- Reported a false noise floor (0.06) and wrote a "+0.30" conclusion on it → retracted. **Measure twice
  first.**
- Took the GT's "axis most along the line of sight" and used it to index the prediction's `size`, while
  27.7% of matched pairs do not even agree which axis that is (a rot6d box has several equivalent
  coordinate frames) → scored a correct box that merely used different labelling as a 100 mm depth error.
  **Never carry one object's labelling convention across to another object.**
- Proposed P0-b "output two faces" and withdrew it myself: 16 outputs expressing 9 degrees of freedom of
  information, 7 redundant. **Count whether the output's degrees of freedom match the information.**
- Claimed `--corner_vis_weights` could decouple depth — wrong: those categories are **sensor
  visibility**, not geometric near/far faces. **Read what a field actually labels before using it as a
  switch.**
- Verified a restore with `diff -q` against the backup — trivially true, i.e. no verification at all.
  **After restoring, run that file's tests.**

## ‼️ Every number in this document expires

The `0.25` noise floor, `0.882` IoU, `154 W`, `25.3%` … these are **measurements under that
configuration at that time, not rules**. The rule is "measure first", not these numbers. **Change the
weights, the frame sampling or the metric and they are all void at once** (Stage 0).

➜ They are collected in `stages/exploration/state.json` (the constants table + current proposal tiers +
**the killed list with revival conditions**), each annotated with when it was measured. **Before citing
one, check that column still holds — or just re-measure.**

## ‼️ This skill currently requires two tools that do not exist

Written down so the next round's agent does not assume they do:

1. **A two-channel blind-review renderer** (required by Stage 6.5): both versions of the same frame,
   with colour and line style as two channels re-randomised independently per frame, position randomised
   too, the mapping stored and never shown, plus a reconciliation script (count only when both channels
   agree, and report the discard rate). The existing `inference/rrd_9dof.py`'s palette **already carries
   semantic meaning** and cannot be reused directly.
2. **A unified measurement convention with paired bootstrap and per-metric noise floors.** The source
   project did this on 2026-08-13 with `scripts/measure_prereg.py`: a fixed
   `{"id","value","unit","n","worse"}` output plus per-frame arrays, `--diff` for a paired bootstrap, and
   **cross-convention comparison refused by default**. ‼️ **MLClaw has no counterpart** —
   `list_runs.py --comparable` compares `mode` + `scope` and does not include **the metric script
   itself**, so two runs whose metric script differs still look comparable to MLClaw.
   `config.json -> fingerprint` is the slot reserved for this, and **filling it in still leaves nothing
   checking it.**

The first is still outstanding, and **without it Stage 6.5 is only a rule on paper.**

## Where the table templates are

The tables in this document are the templates; copy them directly: the **four-state audit table**
(Stage 1), the **proposal table** (Stage 3), the **three axes + tiers + instrument matrix** (Stage 3.5),
the **interface comparison table** (Stage 4.5), and the **default verdict table** (Adjudication).

Two references:

- `references/debate-roles.md` — the six debates' **role prompts, rebuttal checklists, blind-input
  protocol and adjudication record format**;
- `stages/exploration/state.json` — **everything that expires is here**: the constants table, current
  proposal tiers, and the killed list with revival conditions. `graph.py check` reports every card citing
  a stale constant.

Two destinations; do not mix them:

- **`findings.json`** lives under `stages/exploration/` (it has to be re-run and diffed by scripts);
- **everything else goes into the document `config.json -> design_doc` points at**, with no separate
  file — keeping proposals and current state apart means nobody can reconcile them two weeks later. The
  machine-readable half is `graph.json`, written exclusively by `graph.py`.
