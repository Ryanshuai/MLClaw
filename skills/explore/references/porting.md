# Porting: what to search for · what to move · what changed · did it work

The operating manual for Stages 4 / 4.5 / 5. The central section is **§5: there are two kinds
of deviation — adaptation and breakage — and afterwards they look identical.**

---

## 1. Before searching: audit your own code

‼️ **The most common search result is "it is already in the repo, behind a flag that defaults
to off".** This repo has stepped on it twice:

| Assumed missing | Actually |
|---|---|
| one-to-one / NMS-free | `--repeat_num` set to 0 or 1 IS pure one-to-one, and the author's comment says so |
| query content from the encoder | `--q_content random_add` is exactly that; the default `random` has it off |

**Going to the literature without auditing the code is going to port something you already
have.** argparse is the structural blind spot for this — grepping the feature's name finds
nothing; you have to grep the flag names and the defaults.

---

## 2. How to search: three layers of keywords, and across fields go by **geometric property**, not domain vocabulary

| Layer | What to search | Example |
|---|---|---|
| **Canonical name** | what the technique is formally called in the literature | `mixed query selection`, `contrastive denoising` |
| **Code symbols** | the flag or variable names in a reference implementation — often more accurate than the paper's name | `learnt_init_query`, `label_noise_ratio`, `embed_init_tgt` |
| **Mechanism description** | for when you do not know what it is called | "predicted boxes should not overlap each other" |

‼️ **Across fields, strip the domain vocabulary and keep only the geometric property.** The
thing we wanted — "tightly packed boxes must not overlap" — lives in the literature under **2D
crowd pedestrian detection** (`crowd` / `occlusion`) and **densely packed remote sensing**
(`densely packed`). Searching `carton` / `logistics` / `palletizing` **finds nothing**;
`densely packed` + `repulsion` hits on the first try.

**Record explicitly what the search did NOT find**: for this repo's RepGT entry, there is **no
direct precedent** on the 3D detection side — that is not "we did not search hard enough", it is
a conclusion, and it moves the entry from `port` to `cross-domain port`, which raises the
verification tier.

### ‼️ When nothing turns up, ask first: **does this constraint even hold under 2D projection?**

Searching the whole detection literature and finding nothing is often not because the idea is
useless, but because **mainstream detection is 2D and the constraint is false in 2D**. This
repo has hit the same shape twice:

| Constraint | In 2D | In 3D metric space |
|---|---|---|
| boxes must not overlap | **overlapping projections = occlusion**, legal and common ⇒ nobody writes that loss | **overlap = physical interpenetration**, impossible ⇒ the constraint holds |
| objects of the same type have the same size | ‼️ **different under perspective** ⇒ this prior is **false** in 2D | **exactly identical** ⇒ the clustering prior holds |

The second has strong corroboration: **SKU-110K** (CVPR 2019), dense retail shelves, averaging
**147 nearly identical objects per image**, records exactly the failure this repo records ("when
goods are identically sized, one detection box may span a large region, significantly reducing
recall") — **and it does not use the "same type ⇒ same size" prior**, because in 2D it cannot.

⇒ **Once you judge that it holds, go find precedent in other fields**: 3D reconstruction, robot
grasping, scene synthesis, trajectory optimisation. Those fields work in metric space, and
physical constraints are their daily business.
‼️ **This also changes the classification**: what you found is "a mechanism from a neighbouring
field", not "a precedent on the same task" ⇒ per Stage 5, the self-invented portion gets its
verification tier **raised by one**.

**Once you have the official implementation, clone it locally** (this repo:
`~/agent_space/detr_refs/`). Reading the code beats reading the paper — reasons in §3.

---

## 2.5 ‼️ Having found a precedent, ask first: **is its hard problem my hard problem?**

However strong the evidence, if the difficulty **that paper is solving** does not exist here,
what can be borrowed is the **form**, not the **techniques**.

This repo's case: the SKU pointer found two strong precedents (ReID / face recognition, and
ProtoNet / few-shot). They genuinely are the same problem family as ours (**open-set: the test
class set ≠ the training class set**, and both solve it by learning a metric rather than a
classifier). But:

| Precedent | Its hard problem | Ours |
|---|---|---|
| ReID | a gallery of many thousands ⇒ re-ranking, hard negative mining | **k ≤ 6** |
| ProtoNet | the prototype must be **estimated** from K samples ⇒ all kinds of calibration | **ground truth handed over by the catalogue** |

⇒ Take only the **form** (pointer = `W` computed from the input on the fly · CE for training,
retrieval for inference · Euclidean rather than cosine), and **none of the techniques** — they
solve problems we do not have, and importing them is pure overhead.

‼️ **This judgement has a second payoff: it predicts the outcome.** The part both fields agree
is hard is easy for us ⇒ **there may be very little to learn on this task at all** ⇒ that arm's
precondition check (is the hit rate already high) is promoted from "measure it while we're
here" to "**this may drop to T0 on the spot**". **A precedent's difficulty is a free prior on
the effect.**

‼️ **The unit of this judgement is the ARM, not the project — one precedent, two arms, opposite
conclusions.** The table above is about this repo's **SKU-level pointer** (gallery = catalogue,
k ≤ 6). The **instance-level ReID** arm of the **same project** has a gallery of **5909 IDs /
223k samples** (larger than MSMT17) — **its hard problem is exactly ReID's, and those techniques
are the ones to borrow.** ⇒ Change arm, and this judgement must be **made again**; last time's
conclusion cannot be inherited.

**The failure shape in the other direction**: import a pile of techniques that solve "large
gallery", get no effect, and attribute it to "the implementation was not faithful" — when in
fact it **faithfully solved a problem we do not have** (§6, third row, "mechanism does not
address our fault").

---

## 3. ‼️ Grading evidence: **used by a top conference ≠ ablated**

Whether a technique is worth porting depends on **whether it was ablated on its own**, not on
how many papers it appears in.

| Grade | Meaning | Instance in this repo |
|---|---|---|
| **A · ablated on its own, with numbers** | strongest | DINO Table 4: pure 46.5 → mixed 47.0 |
| **B · ablated, but on a different baseline** | medium — the margin moves with the baseline | DINO's CDN +0.5, stacked on a baseline that already had DN |
| **C · used, but the authors never ablated it** | ‼️ **weak** — it is just an inherited default hyperparameter | RT-DETR's denoising, and `learnt_init_query` — **neither ablated** |
| **D · the paper's prose says A, the code runs B** | ‼️ **that sentence in the paper may not be cited** | V-DETR's paper says content queries come from encoder features, but the official README's training command does not pass `--q_content` — **the published numbers were run with learned slots** |

**Both grade-C encounters were in the same repository**, so: **having found a technique "in use"
in some repo, the next step is to go to its paper and find that ablation cell**; failing to find
it means dropping a grade.

---

## 4. The code is authoritative — and read the **training script**

Priority: **training script / config > a class's default arguments > the paper's prose >
second-hand interpretation.**

`learnt_init_query: False` written in a config, and `embed_init_tgt = True` written in four
configs, are more reliable than any prose in the paper. And **a flag the training script does
not pass is a flag that ran at its default**, which is a step that can directly overturn a
paper's prose (grade D in §3).

---

## 5. ‼️ Two kinds of deviation: **adaptation** and **breakage**, and afterwards they look the same

Porting requires changing things (not changing them would be wrong), and changing them wrongly
raises nothing. Their signatures:

| | **Adaptation** (must change) | **Breakage** (believed to be adaptation) |
|---|---|---|
| What was changed | the **carrier** — geometry, data structures, dimensions | the **mechanism** — while you believed it was the carrier |
| Can you state "what happens if I don't" | yes, and the consequence is concrete and measurable | no, or the reason is "we have nothing corresponding to that here" |
| Afterwards | the reference implementation's mechanism explanation **still holds** | that explanation **quietly stops holding**, and the code runs on |

### The test (beforehand, one sentence)

> **For every deviation, write down why the reference implementation wrote it that way. If you
> cannot write it, you may not change it.**

The original wording from the D6 incident was "**cited the right line, misread what that line
was doing**" — a `ref:` comment being accurate does not make the inference from it accurate.

### Three known breakage shapes (this repo hit all three; none raised)

1. **Same name, different meaning.** focal's `alpha` multiplies **positive** samples; VFL's
   `alpha` multiplies only the **negatives** — one name, opposite roles. Reusing it cut the
   background weight from 0.75 to 0.25, **the opposite direction from the intent**.
2. **Deleting something that "looks useless".** The reference's
   `nn.Embedding(num_classes+1, ..., padding_idx=...)` had its `+1` removed, justified as "that
   is the label-noise slot and we have no label noise".
   ‼️ Misread: that row is the **padding slot**, frozen at zero by `padding_idx`. Remove it and
   one full-pallet sample pads out every other sample in the batch, so that trainable vector
   receives gradients for "you are a box" and "you are background" simultaneously.
3. ‼️ **Symmetry maintained in the wrong space.** The box noise is symmetric in the **ratio**
   (`E[new/old]=1`, which reads as unbiased), while the loss operates on `log(gt/pre)` and log
   is concave ⇒ measured `E[log(gt/pre)] = +0.113` per axis, so every denoising positive was
   asking the size head to **enlarge each axis by 12%, i.e. 1.40× in volume** — landing squarely
   on that round's guardrail metric. **A port will go and measure its own noise design.**
   → The rule: **check that your sampling is unbiased in the space the loss scores in**, and
   write the assertion in that space.

### Two reminders pointing the other way

- ‼️ **When the reference implementation makes a "strange choice", ask what it is defending
  against first.** RepGT uses IoG rather than IoU, which looks like a detail; it is in fact
  defending against "the regressor enlarges the box to inflate the denominator" — **the same
  Goodhart this repo independently constructed**. Copying it verbatim is correct; being clever
  and switching back to IoU re-opens that path.
- ‼️ **A condition in the reference implementation may mean something different under our
  configuration.** RepBox applies only to prediction pairs with *different designated targets*;
  under `--repeat_num 5`, five queries claim the same box and **are supposed to overlap** — not
  grouping by target puts it in a head-on fight with one-to-many. **The same line of code does
  different things under two configurations.**

### A deviation must be switchable off

Every new flag defaults to off, and **when off it is bit-exact with the pre-port state**, with a
test pinning that. The cost (recomputing some quantity, say) buys the **verifiable property**
that switching it off makes no difference.

---

## 6. Did it work: three failures that must be kept apart, because they revive differently

The verdict after a run is **not a two-way "improved / did not improve"**:

| Verdict | Criterion | Next step |
|---|---|---|
| **Unfaithful implementation** | the interface comparison table has an unhandled "fatal" cell | **go back and fix it**, do not kill |
| **Faithful but ineffective** | fidelity passes, the primary criterion did not move | record `killed_by: faithful but ineffective`; hang `revive_if` on a change of convention or upstream |
| **Mechanism does not address our fault** | faithful and effective, but **it treats a disease we do not have** | record `killed_by: wrong mechanism`; `revive_if` is "measure the effect directly" |
| ‼️ **Mechanism confirmed, and that mechanism is worthless** | the primary criterion passes literally, and the result is worse | This is a **premise** problem, not an implementation problem. Go back and check the premise share (rule 2.5) |

The last row is a real case from this repo: `e1`'s declared mechanism criterion **passed** (the
negatives really did come back) while AP50 fell from 7.88 to 5.57 — **not "the mechanism failed
to check out", but "it checked out and was worth nothing"**, because its premise holds on only
4.62% of this corpus.

‼️ **One more to watch for: won but unattributed.** An arm wins big while the fault it was
proposed to fix **is still measurably there** ⇒ it probably did not win by that mechanism. The
cheapest next step is to **separate the two**, not to write "this technique works" into a
conclusion.

---

## 7. This repo's deviation ledger (append on every port)

| Port | Adaptation (had to change) | Breakage (caught in debate) |
|---|---|---|
| VFL / IoU-aware | 2D axis-aligned IoU → `soft_iou9d`; `temp=0` on the target side | `alpha` reuse (D4) |
| CDN | box noise moved into **the box's own coordinate frame**; `assignments` generated directly rather than recomputed; injected **after** topk | size noise symmetric in the ratio (D5); `padding_idx` deleted (D6) |
| CDN's rotation noise | — | **the whole thing was self-invented**, and finally judged "the mechanism does not hold": `rot6d_head` predicts absolutely, so a noised rotation **never appears on the target side at all** |
| RepGT (planned) | IoG's geometry replaced with oriented 3D | pending — the dead zone is a self-invented deviation, **not the form from the literature**, and needs its own ablation |
