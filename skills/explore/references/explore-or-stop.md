# Keep exploring, or stop? — judging convergence after several rounds

The question to answer once a few arms have run: **where is there still something to dig up,
and where has the search been exhausted.** This is a judgement, not a procedure, so what this
file holds is the **criteria** and **the false signals most likely to fool you**.

‼️ **The default answer leans toward "keep going"**, because on this pipeline the most common
cause of "no improvement" is not that the search is finished — it is that **the criterion is
broken** (§3). Stopping requires actively passing the three questions in §2.

---

## 1. Signals that you must continue (any one of them forbids stopping)

| Signal | Why it is worth more than opening a new arm |
|---|---|
| ‼️ **Won, but unattributed** | Something clearly won, but its **pre-registered primary criterion did not move**, or the mechanism was never confirmed. The cheapest next step is to **explain it**, not to open another arm. In this repo: CDN won by +35 AP50 while the fault it was proposed to fix (contrastive hard negatives) **is still measurably there** ⇒ it probably did not win by that mechanism, and `e3b` is what separates the two |
| **A guardrail moved and nobody explained it** | A guardrail's entire value is the moment it fires. Fired and unexplained = it was never worth setting |
| **🟪 piling up** | Results in, verdicts not reached. The queue keeps running while nobody knows what the numbers already in hand mean |
| **The primary criterion and AP move in opposite directions** | It means we do not understand what is happening. The next measurement taken in that state carries more information than any new arm |
| **Items with oracle ceiling > noise floor still untouched** | Zero cost, and they may kill several arms outright |

---

## 2. The three questions that must be passed before stopping (only "no" three times means the search is genuinely done)

### Question one: **is the largest failure count the same one as when this round started?**

- **No** ⇒ **the fault changed. Go back to Stage 1 and re-audit; do not keep digging inside
  this round.** This is the case most often misread as "the search is finished": the original
  proposals do not address the current fault, so none of them has any effect, and it reads as
  "this avenue is exhausted" when in fact **you took the wrong turning**.
- Yes ⇒ go to question two.

### Question two: **is there anything left on the table whose oracle ceiling exceeds the noise floor?**

- **Yes** ⇒ you may not stop. A ceiling above the floor means there is still measurable room.
- No ⇒ go to question three. **Note that this question requires the oracle to have actually
  been measured**; "I don't think there's much room left" does not count.

### Question three: **is anything "won but unattributed", or 🟪 with no verdict?**

- Yes ⇒ you may not stop (§1).
- No ⇒ **you may stop.**

---

## 3. ‼️ False "the search is finished" — this repo has stepped on every one

| False signal | What is actually true | How to see through it |
|---|---|---|
| "Several arms showed no improvement" | **The noise floor was never measured** ⇒ "no significant improvement" is **undecidable**, not negative | Look for `[T1 trend]` in the wording of the reported number. Present = you are not yet in a position to say "no improvement" |
| "The metric will not move" | **The criterion selects itself** — a filtering step removed the very failures it should detect; or an empty initial value makes the criterion trivially true | Does the criterion contain a filter, a threshold, an initial value? Write the criterion its own test case: one it *should* fire on |
| "We tried that mechanism, it doesn't work" | What was tried was the **cheap approximation**. An approximation failing **cannot refute the original** | Is the card marked as an approximation |
| "No better than the last version" | **The metric changed, or the data changed ⇒ the old curve is void.** You are comparing two different rulers | Do the run card's data hash and metric-script hash match |
| "That fault is not serious for us" | **The share was quoted from another corpus.** In this repo: predicted 47%, measured on this corpus **4.62%** — an order of magnitude | Was that share measured on **the corpus this round will actually run** |
| "All tests are green, so nothing is wrong" | Tests pin the **arithmetic**. They cannot pin **which corpus the input distribution came from** | Does the test construct its own input? If so ⇒ it knows nothing about the corpus |

---

## 4. Stopping ≠ finishing. Stopping is **switching**

The correct output of a round that stops is not "there is nothing left to do" but three things
(Stage 8):

1. **Re-weigh FINDINGS** — did the fault change? What are the shares now? The next round's
   ordering comes from this.
2. **Update P (the priors)** — which priors did the data correct this round. ‼️ Including
   **the ones that won**: a win whose mechanism was never confirmed should *lower* the prior,
   not raise it.
3. **Fill in every `revive_if`** — every ❌ must be wakeable by some future measurement.
   ‼️ The four kinds of death have completely different revival conditions, and writing them
   interchangeably is the same as not writing them (see CLOSE in `experiment-graph.md`).

**Only with those three done has the round actually stopped** — otherwise the next round
starts from zero and re-proposes what was already killed.

---

## 5. One reminder in the other direction: do not explore forever either

The above leans toward "keep going", but there are two hard exit conditions, and hitting either
means you must stop:

- ‼️ **The fault is not in the architecture.** If the largest failure count points at a
  **data / annotation / capture gap**, then the most elegant architectural change is still
  only fitting a wrong target. This repo has one ready-made: the depth label is ambiguous by
  74–210 mm on **25.3%** of boxes, and that is **information that is not in there** rather
  than a training problem — no architecture breaks through it.
- **Marginal return < the cost of one measurement.** Once each new arm's expected delta is
  already smaller than the noise floor, the right move is **to go measure the noise floor or
  change corpus**, not to open another arm.
