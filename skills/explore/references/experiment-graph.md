# The experiment graph: node schema · state machine · four operations · invariants

`SKILL.md` Stage 3's ablation chapter is **a graph**, not a list. This document is the manual
for operating it: how to create nodes, how to take nodes, how to fill results back, and **how to
discover that the graph itself has broken**.

The judgement calls (which dependencies are fake, why parallelism has a hard ceiling) stay in
`SKILL.md`; this file is only **how to do it**.

‼️ **This graph lives in `stages/exploration/graph.json`**, not in the agent's context. A new
context has no memory, so **every operation must land in the file** — and in MLClaw "landing in
the file" has an executor:

```bash
python <mlclaw_root>/scripts/explore/graph.py <verb> --project <PROJECT>
#   add   set   ready   fill   close   reread   check   status
```

The **ADD / TAKE / FILL / CLOSE** of the four sections below correspond to `add`+`set` /
`ready` / `fill` / `close`, and §4's invariants correspond to `check`. In the original version
of this document, the sentence "scan periodically; report breakage rather than repairing it"
had no executor in markdown — `check` is that executor, and it **reports without repairing**: a
graph that repairs itself would conceal the fact that *something wrote an illegal state*, and
every illegal state here reads as a normal one.

‼️ **`check` exits 1 when there is a critical finding. Per CLAUDE.md "Script Integration" that
means the script worked and the answer is no — it is not a broken script, so do not fall back
and work around it by hand.**

---

## 1. Node schema

Every node is a card. **A node missing any field may not enter `ready`.**

| Field | Meaning | What breaks without it |
|---|---|---|
| `id` | queue number, **stable and never reused** (a killed number is not recycled) | cross-references point at the wrong thing |
| `title` | one sentence saying what changes | — |
| `kind` | `measurement` / `port` / `original` / `task-driven` | ‼️ `original` automatically raises the verification tier (Stage 5); the four kinds revive differently |
| `premise` | what the world must be like for this to hold | rule 2.5 loses its handle |
| `premise_share` | measured **on the corpus this round will actually run** | ‼️ quoted from elsewhere = treated as having no premise |
| `primary criterion + predicted direction` | exactly one, and it may not be AP alone | the mechanism cannot be confirmed (rule 2) |
| `guardrail` | at least one, against Goodhart | degradation is invisible |
| `parent` | the mount point for a single-key delta | the delta cannot be attributed |
| `depends_on` | `[id, ...]`, and every edge states **what it blocks**: `"N06"` (blocks launching) or `{"id":"N06","blocks":"reading"}` (blocks only the reading) | **the ready set cannot be computed**. ‼️ And the version that writes an id without a kind computes a ready set that is **correct but too small** — see TAKE |
| `conditional_on` | filled by `close` itself: the `reading` upstreams still unsettled when the verdict landed | a conditional conclusion propagates as an unconditional one |
| `oracle ceiling` | mandatory whenever it can be measured at zero cost (rule 2.6) | you may be running an arm whose ceiling is zero |
| `kill_condition` | what result counts as this being dead | a proposal that cannot state a kill condition is itself unfit (rule 6) |
| `run id` | written on entering `running`, including the code snapshot hash | afterwards there is no way to confirm several arms ran the same code |
| ‼️ `tree` | `{branch, base, head, path}` — **which working tree this arm's code was written in**. Required of the three kinds that write code (`port` / `original` / `task_driven`), never of a `measurement` | see §1.5. Two arms edited in one directory produce two records that are internally consistent, reproducible, and about the same binary — and **nothing else on this list can see it** |
| **the other end** | that run's `run.json -> verifies` must point back at this card and carry `falsified_if` | ‼️ **a one-way pointer reads exactly like a binding**, until somebody follows it. `check` verifies both ends |

---

### 1.5 The tree an arm is written in

**Every other field on this card describes an experiment. This one describes the directory it
was typed into**, and it is here because MLClaw's own layout makes parallel arms share one.

`run-mechanics.md → Code snapshot` resolves **one** code path per stage —
`stages/<stage>/code/_source if exists else stages/<stage>/code`, and for
`code_source.source: local` that is a single external directory acting as a soft link.
SKILL.md Stage 3 says to compute the ready set and **open all of it at once**. So two ports get
written into one directory, and `code_snapshot.py` reads that directory at launch.

‼️ **What that produces is not a broken record. It is a clean one about a run that did not
happen.** Arm A's `code_dirty.patch` carries arm B's half-written port. It applies. It
reproduces exactly. `check`'s invariant 13 compares `runtime_params` + `workload` and sees
nothing, because an uncommitted edit to a model file moves neither — so the declared single-key
delta is honoured, the binding resolves both ways, and the number belongs to a binary nobody
described. **That is the rubber-stamp shape** SKILL.md's run-card chapter names: a guard
reporting the very conclusion it exists to exclude.

The isolation is one line of git per arm:

```bash
git worktree add ../arms/N07 -b explore/N07-cdn <round base sha>
```

‼️ **Then launch the arm FROM that worktree, not from the shared directory** — otherwise the
worktree bought nothing. `run-card.md` measured what this looks like when it is got wrong: the
training machines are populated by `rsync --exclude '.git'` and therefore **have no `.git` at
all**, so a snapshot's file list must come from a filesystem walk either way. What the worktree
changes is *which* directory gets walked and rsynced. Point `code_dir` (or the rsync source) at
`../arms/N07`, and record where it was in `tree.path`.

#### Where this is stopped, and where it is only reported

‼️ **`check` reports it correctly and reports it LATE** — by then the GPU hours are spent and the
number cannot be attributed to anything. So the discipline arrives at two earlier points, and
`check` is the backstop for a graph edited around the tool:

| When | What happens |
|---|---|
| `graph.py ready` — **where parallel arms are handed out** | the ready set comes back with a `trees` block: one `git worktree add` line per **unclaimed** code-writing card, already carrying the round's base. Every ready entry also carries `claimed_by` — an unmarked list is what hands the same card to two agents. Plus `base_undeclared` if the base is not frozen, and `parallel_arms` when more than one would be open at once |
| ‼️ `graph.py claim --id N07 --by <who>` — **before a line of its code is written** | allocates the branch, records the claim, prints the `git worktree add`. **This is the real gate**; the two below are backstops |
| `graph.py set --set run_id=…` — the moment an arm launches | refused, at exit 1, when another code-writing arm is already open and this card names no `tree`. It cannot fire on a serial round — one arm at a time reaches it with nothing else open |
| `graph.py check` | `concurrent_arms_one_tree` and the rest of §4 |

‼️ **Why the claim exists at all, when `run_id` was already gated: `run_id` is set at LAUNCH, and
the contamination happens in the hours before it.** Two agents each editing for one card, in one
directory, for an afternoon — and the refusal arrives after both have written into it, at which
point declaring a tree does not unmix the directory. **Taking the work and taking the tree are
one act**, so they are one verb.

It is also the only one of the three that survives the two agents being **dispatched
separately** — no orchestrator handing out worktrees, no shared context, nothing in common but
this file. ‼️ **And its limit belongs in the same breath: two agents that never call `graph.py`
cannot be protected by `graph.py`.** What the claim buys is that the discipline sits at the one
place both of them must pass through to take work, instead of in prose one of them may never
read. A `claim` creates nothing on disk — it prints the command, exactly as every other verb here
executes nothing.

‼️ **One limit that is not the same sentence: `graph.json` is written atomically but is not
locked.** Two `claim` calls landing in the same instant are a read-modify-write race, and the
second write wins — the first claim is simply gone, with no torn file and nothing raised. In
practice claims are minutes apart, which is why this is recorded rather than solved; what would
solve it is an `O_EXCL` lock around the read-modify-write, and it has not been written. **Do not
read "the claim succeeded" as "the claim is exclusive" under real concurrency** — re-read the
card after claiming if two agents genuinely started together.

‼️ **Exit 1 and not exit 2, and the difference is the whole point.** CLAUDE.md's fallback rule
says exit 2 means the script broke and the skill does the same work **by hand** — which for a
refusal means opening the arm anyway. A safety check whose refusal routes into the fallback rule
is a safety check that fires and is then walked around. Policy refusals here are 1; a malformed
`--set` argument stays 2.

**The record of it is this field, and the record is the half nothing could hold before.**
`branch` (the arm's own, never another card's), `base` (the sha it forked from, which must be
the round's), `head` (what actually ran — compared against the run's snapshot), `path` (where
the worktree is, so somebody else can find it), and `claimed_by` / `claimed_at` (who is in it —
the field that makes `ready` say *taken* instead of handing the card out twice).

#### Giving the tree back  ·  `graph.py land`

`claim` takes a tree and until `land` existed **nothing returned one** — which is the end where a
round quietly loses things, the same shape as `/evacuate`: *doing nothing is the destructive
act*. A branch nobody merged and nobody archived is a round's whole output sitting where the next
`git clean` takes it.

`land` plans and merges nothing — it prints the `git merge` lines the way `claim` prints its
`worktree add`. Three of Stage 8's rules stop being prose and become refusals:

| | |
|---|---|
| **the round is settled first** | refused while any card is open. A merge moves the base under every arm still running, and Stage 6's control is *defined* by the base — landing mid-round voids the arms that have not finished |
| **one at a time, each re-verified** | every merge carries its acceptance criterion: **every new flag off, bit-exact against the pre-merge state.** A merge is a code change between two experiments and takes the refactor gate's own test |
| **two winners is a third arm** | the combination of both flags was run by nobody. ‼️ **Named, not blocked** — it is not a defect, it is the next round's first card, and writing it down is how this stops being a hallucination incubator |

It also prints `keep`: every branch this round produced and **why it may not be deleted** —
including the losing ones, which are the branches most likely to be tidied up and the ones whose
loss is silent. And it says where the next round's `base.commit` comes from: the sha after the
last merge, frozen before anything is claimed, or the next round's arms fork from four different
places.

#### The round's `base`, and why it is frozen

`graph.json → base.commit` is the twin of `corpus`: that one pins what every `premise_share` was
measured on, this one pins **what every arm's delta is measured against**. Stage 6's *"the
control must be re-run on today's code"* has no referent the moment "today" differs per arm —
which is what a winner merged mid-round does to every arm still running.

So: **the winner does not merge when it wins. It merges when the round closes**, and the merge is
governed by Stage 8's technical-debt gate rather than by this file — a merge is a code change
between two experiments, with the same acceptance criterion (*every new flag off, bit-exact with
the pre-merge state*) and the same rule about flag combinations. Two winners merged from one
round is a combination nobody ran.

#### What may not be thrown away

A `killed` card carries `revive_if`, and reviving means going back to that code. The branch is a
convenience; **the evidence is `tree.head` plus the run's snapshot**, and `run-card.md` rule 4 is
blunt about what that is worth on its own — a patch depends on that commit still existing, so
after a force-push, a deleted branch or a GC, `checkout` fails where a tarball would not. For a
round you intend to cite, `/ara build` is what makes the losing arms survive; a branch is not a
retention policy.

---

## 2. State machine

```
⬜ draft ──schema completed──→ 🟨 blocked ──all launch deps closed/killed──→ 🟩 ready
                                                                              │
                                                                   declare run card
                                                                              ↓
  ❌ killed ←──killed_by + revive_if──┐                                  🔵 running
                                      │                                       │
  ✅ closed ←──verdict───────── 🟪 filled ←──results written to the card──────┘
```

| Colour | State | Meaning |
|---|---|---|
| ⬜ | `draft` | a one-line idea and nothing else |
| 🟨 | `blocked` | card complete, dependencies unmet |
| 🟩 | `ready` | **may open an arm** |
| 🔵 | `running` | the run card is declared |
| 🟪 | `filled` | results on the card, **verdict not yet reached** |
| ✅ | `closed` | conclusion reached (won / lost / downgraded); may be another card's dependency. ‼️ A ✅ carrying `conditional_on` is a **conditional** closure, and citing it means citing the condition with it |
| ❌ | `killed` | with `killed_by` + `revive_if`, and **may also be a dependency** ("stop waiting on this one") |

‼️ **`filled` and `closed` must stay apart**, because *having a result is not having a
conclusion*: an arm's meaning often depends on another arm. This repo's instance: `e1`'s
mechanism criterion **passed literally** (the negatives really did come back), while the verdict
was **"the mechanism checked out and that mechanism is worthless"** — and that verdict could not
be reached until F1's share had been measured. Using 🟪 as if it were ✅ propagates a number
awaiting explanation as though it were a conclusion.

**Illegal transitions** (report on sight; do not repair them yourself):
- 🟨 skipped, straight to 🟩: somebody bypassed the premise gate;
- 🔵 while a **launch** dependency is neither ✅ nor ❌: the gate was bypassed (an unsettled
  `reading` dependency is normal — that is exactly what it is for);
- ✅ with no `run id`: a conclusion with no provenance;
- ❌ with no `revive_if`: the four kinds of death revive differently, and writing them
  interchangeably is the same as not writing them.

---

## 3. The four operations

### ADD — create a node  ·  `graph.py add` then `graph.py set`

What a user throws in is usually **one sentence** ("has anyone done cross attention between
SKUs?"). The agent's job is to complete it into a card:

1. **Audit your own code first** (Stage 1) — ‼️ the most common outcome is "it is already in the
   repo, behind a flag that defaults to off". This repo hit it twice: one-to-one
   (`--repeat_num`) and query content source (`--q_content`).
   **Going to the literature without auditing the code is going to port something you already
   have.**
2. Fill `premise` and `premise_share` — the share is **pure data measurement**, usually one pass
   over the loader; do not skip it.
3. Fill `oracle ceiling` — anything measurable at zero cost must be measured first (rule 2.6).
4. Choose `parent`: **testing the mechanism** and **pricing it** may hang off different parents.
   Write both, and report only the latter.
5. Choose `depends_on` and insert it into the graph.

A new node defaults to ⬜, and becomes 🟨 or 🟩 once complete. **Do not start a run during the
card-completion stage.**

### TAKE — take the ready set  ·  `graph.py ready`

```
ready = { n | n's card is complete and premise_share is on this corpus
              and all of n's **launch** dependencies ∈ {✅, ❌} }     ← reading edges do not participate
```

### ‼️ An edge states what it blocks

`depends_on` used to be a list of bare ids, and a bare id can express only one thing: **wait**.
So every real dependency automatically became serial — including the ones that were never about
ordering. The case we hit: N07 (fusion) needed N06's **σ**, one value, two flags written in the
same source file; the graph assigned it N06's **verdict**, four and a half hours. All that could
be done at the time was to delete the edge, and deleting it removed the real half too: with σ
uncalibrated, an N07 loss says nothing about whose fault it was.

| `blocks` | Blocks `ready` | Blocks `close` | When to use |
|---|---|---|---|
| `launch` (the default for a bare id) | ✅ | ✅ | premise share unmeasured · the parent checkpoint does not exist · the code cannot be written |
| `reading` | ❌ | ❌ | **attribution**: it can run, but the result cannot be read on its own |

**The test: do I need its conclusion, or only one value / one artifact / one line of code from
it?** Only the former is `launch`.
‼️ **Needing one parameter is not a dependency. It is a parameter.**

‼️ **A bare id still means `launch`, and that is deliberate.** The other default would silently
unlock every graph already written. The prompt lives in `ready` instead — every `blocked` entry
carries an `ask` field, asked at the moment that edge actually starts costing money: "do you
need the verdict, or a value?" ‼️ The two mislabellings differ in visibility by an order of
magnitude: mislabelling as `launch` costs wall clock and **is invisible** (the queue looks
entirely normal, only slower), while mislabelling as `reading` costs one arm's machine time and
`conditional_on` shouts about it. **So the default answer leans toward parallelism.**

‼️ `_cycle` walks `launch` edges only. **Two cards `reading` each other is legal** — "run both
together, adjudicate together" looks exactly like that — and walking every edge would report
this new form's healthiest use as a cycle that must be broken.

‼️ **⬜🟨🟩 are computed, never stored** (`graph.py -> _derive_state`). That line used to read
`n.state == 🟩`, which reads as if a stored 🟩 existed first and dependencies were filtered
after — but 🟩 appears because **some other card** closed, so any stored copy may be stale the
moment it is written. `status` read the label while `ready` computed live, and the same set of
cards produced opposite answers (`blocked: 3` / `ready: [N01,N02,N03]`) — with `status` being
the one a person looks at. Both verbs now go through one function. The value stored in `state`
is a convenience for outside readers and **is not a criterion**.

🔵🟪✅❌ are the other way round — those are **actions taken**, they cannot be computed, and the
record itself is the declaration. Among them 🔵 is recognised by `run_id` rather than by the
label: **a card with a run_id and no result yet counts as running**, even if somebody forgot
`set state=running`. A missing label is precisely the shape that gets a second arm opened.

Then filter by capacity:

1. **Offline nodes (0 GPU) come first and are not capacity-limited** — they are always in the
   ready set, and all of them open in parallel.
2. GPU nodes: `parallelism ≤ stable capacity`. ‼️ The excess is not "a bit slower", it is
   **structurally zero output**, and it churns the arms that already hold slots (`SKILL.md`'s
   section says why).
   ‼️ **`ready` does no capacity filtering; it computes dependencies only.** Capacity belongs to
   `pool.py`, one layer up — `<mlclaw_root>/references/fleet.md`. Keeping the two apart is
   correct: a dependency is a property of the graph, capacity is a property of this moment.
3. Arms to be compared against each other **must be on the same GPU model**; to change it,
   change the whole group.

‼️ **An empty ready set with a non-empty queue = deadlock.** Two causes, handled differently:
- **A dependency cycle** → the graph is written wrongly; find the cycle and break it;
- **Everything blocked on one predecessor** → that predecessor is the only thing worth doing
  right now. **Move it to the front**, and do not open an arm outside the ready set because
  there is "nothing to do".

### FILL — write results back  ·  `graph.py fill`  ‼️ This is not filling in a cell; it is a **propagation through the graph**

`graph.py fill` writes the result onto the card (🔵 → 🟪) and returns a `must_review` list:
whatever depends on this card, whatever cites it in prose, and the constants that came from this
run. ‼️ **That is a candidate list, not a verdict** — the script can enumerate references, but
judging whether a premise really is void is yours. Walk the list and ask three questions:

| Question | Instance in this repo |
|---|---|
| Has any other node's **premise** been overturned by this result? | F1's share measured at 4.62% (predicted 47%) ⇒ the premises of **five arms — e1/e1b/e1c/e2/e6 — failed at once** |
| Has any other node's **ordering rationale** been overturned? | H100 measurably can write checkpoints ⇒ "three arms do not fit under current capacity" is void, and becomes "2.3× more expensive but it fits" |
| Has a **constant** been overturned? | Then update `stages/exploration/state.json -> constants` (along with `measured_at`), stating which foundational constant this update overturned — `graph.py check` reports every card citing a stale constant |

**A fill with no propagation is the most expensive failure on this pipeline**: it leaves already-
void premises hanging in the queue, and the next person runs against them. Only after the run
does anyone discover the premise had been gone all along.

‼️ This is also why `fill` enumerates rather than decides: a verb that decides for you which
premises are void is guessing, and a verb that returns nothing is the markdown version itself.

### CLOSE — adjudicate  ·  `graph.py close`

🟪 → ✅ or ❌. The verdict must state **which kind**, because the four have completely different
`revive_if`:

| `killed_by` | Meaning | Shape of `revive_if` |
|---|---|---|
| `share_too_small` | the fault is real but rare | re-propose on a new corpus or operating condition |
| `faithful_but_inert` | ported correctly, changed nothing | a changed convention or a changed upstream |
| `wrong_mechanism` | the proposal itself was wrong | measure the effect DIRECTLY (**no need to wait for the proxy condition**) |
| `unfaithful_port` | **not a death — go back and fix it** | — |

‼️ **Two hard rules:**

1. **A rebuttal of a proposal may only lower its tier or demand more measurement; it may not
   kill it** (the adjudication table in `debate-roles.md`). The one exception is a factual error
   such as "this technique is already in the code".
2. ‼️ **Never kill a proposal with a number measured on a different corpus.** This is the other
   face of "using another corpus's number to justify one" — rule 2.5 treats that as having no
   premise, so it equally cannot serve as grounds for a verdict. This repo did it once: DINO's
   +0.5 on COCO used to kill `--q_content`, withdrawn the same day.

`revive_if` states a **proxy condition**. ‼️ **When the effect can be measured directly, there is
no need to wait for the proxy condition** — this repo's CDN revival went exactly that way:
neither of the two `revive_if` conditions originally written was ever met, and what revived it
was a direct measurement.

#### ‼️ `conditional_on` — when a verdict runs ahead of its foundation

If a `reading` upstream is still unsettled when a verdict is issued, `close` **does not refuse**.
It stamps: those upstreams are written into `conditional_on`, and the output states that this
conclusion "may not be cited without its condition".

**Why it does not refuse.** Refusing only shifts the same wait one column to the right — into a
pile of 🟪 nobody dares adjudicate, with the GPU hours still being spent, and §4's "🟪 piling up
= a pile of results nobody has explained" is the name of that state. What must be prevented was
never "a conclusion reached too early"; it is **a conclusion travelling apart from its
condition**, which is CLAUDE.md's "Never silently" rule about re-reading a status.

**Closing the loop is three steps, none of them automatic:**

1. `close` the downstream → the card gets `conditional_on: [N06]`, and `check` reports
   `verdict_is_conditional` (minor);
2. `close` the upstream → the output carries `re_read: [N07]`, naming whose verdict hangs on it;
3. the condition lands and nobody re-reviews → `check` escalates to
   `condition_resolved_unreviewed` (major).

4. somebody re-reads it → `graph.py reread --id N07 --condition N06 --note "<what it
   showed>"`, which retires that one condition and writes the note, the upstream's verdict
   and the timestamp into the card's history.

‼️ **Nothing clears `conditional_on` automatically** — a field that clears itself is the same
as not having the field. But it had no way to be cleared *at all* before `reread` existed:
`set` refuses every settled card and a card carrying this field is settled by construction, so
step 3's `major` would have stood for the rest of the round on a verdict somebody had already
re-read and had no way to say so about. **A permanently red finding is how a checker becomes
the thing people route around** — the reason §3.5 reports a dispute as `major` rather than
`critical`, one turn further on.

‼️ `reread` retires a condition; it never revises a verdict. If re-reading changed the answer,
that is `dispute` — the losing card **keeps** its verdict and gains `superseded_by`, because a
conclusion that was overturned and one that never existed are different information. `--note`
is required for the same reason `dispute --note` is: the verdict does not say *why*, and why is
the only part the next round can re-check.

What if the condition really does not hold: **say so and stop the arm.** An arm overturned by
its condition costs its own machine time; an arm not opened because it was waiting for a
condition costs the whole round's wall clock — the first is countable and the second is not.

---

## 3.5 DISPUTE / RESOLVE — when two records disagree  ·  `graph.py dispute` / `resolve`

The original version of this document had no such section, because it assumed verdicts always
happen **in order**: one card filled, adjudicated, next card. In reality N05's result overturns a
conclusion N01 closed three weeks ago — and until now there were exactly two things that could be
done, **both of which destroy the record**: rewrite N01 (the very thing the next round has to
read), or pretend not to have seen it.

Borrowed from ARA's (arXiv:2604.24658) Contradiction trigger, where the rule *is* the mechanism:

1. **Neither side may be edited.** N01's `verdict` stays exactly as it was — a conclusion that
   was overturned and a conclusion that never existed are completely different information for
   the next round.
2. Both sides gain a `disputed_by: [Dxx]`, and one record pointing at both lands in `disputes[]`.
3. **Stop.** Adjudicating is a person's job. `check` reports it as `major`, escalating to
   `critical` only when **another card depends on the disputed one** (that arm would be standing
   on disputed ground). Reporting it as major is deliberate: one unrelated dispute should not
   block the whole graph, or `check` becomes the thing everybody routes around.

‼️ **A lower-tier card may not contradict a higher-tier one, and `dispute` refuses outright.**
This is `SKILL.md` Stage 3.5 rule 2 applied to conflicts: a cheap check has low power — it can
give you a reason to continue, not a reason to rule something out. **Most apparent
"contradictions" between a short run and a controlled one are not disagreements at all, they are
incomparable** — and adjudicating incomparability as disagreement is exactly how a good result
gets thrown away by a cheap probe. A T4 approximation may contradict nothing.

Three resolutions: `upheld` (the challenger is right — the losing card **keeps its verdict** and
gains a `superseded_by`, marked rather than rewritten) · `rejected` (the challenger is wrong, and
the losing card is untouched by a single character) · `not_comparable` (they never conflicted).
`--note` is mandatory: the resolution itself does not say **why**, and the why is the only part
the next round can re-examine.

---

## 4. The graph's invariants (scan periodically; report breakage rather than repairing it)

`SKILL.md` has a section on "the record layer breaks on its own, and more insidiously than the
experiment does". The graph is the same:

| Invariant | What its breaking means |
|---|---|
| Every ✅/❌ has a `run id` or a measurement provenance | a conclusion with no provenance |
| Every 🔵's **launch** dependencies are all ✅/❌ | the gate was bypassed. ‼️ An unsettled `reading` dependency **does not count** — that is its normal use |
| ‼️ **Every edge's `blocks` is readable** | MLClaw's addition. An unreadable edge is blocked as `launch`, so the symptom is a card that is never ready **plus a reason nobody can state**. `set` refuses outright, and `check` reports major as a backstop |
| ‼️ **Every ✅/❌ carrying `conditional_on` has its condition either still open (minor: cite it with the condition) or landed and reviewed by somebody (landed and unreviewed = major)** | MLClaw's addition. This is the bookkeeping end of "running first is not reading first": the time parallelism bought is paid for by a batch of conclusions temporarily standing on something unsettled. **Nothing clears `conditional_on` by itself** — which is exactly why it exists |
| Every ❌ has a `revive_if` and is tagged with one of the four kinds | a revival condition written this way is the same as none |
| No two 🔵 share "same parent + same delta" | duplicated work, or somebody forgot another one is running. ‼️ Recognised by `run_id`, not the label — iterating on `state == running` used to **miss precisely the one shape that genuinely opens a duplicate arm**: run_id set, label never flipped |
| Every 🟩 has a complete schema | it would open an arm that cannot be adjudicated |
| ‼️ **Every 🟪 has somebody responsible for pushing it to ✅** | 🟪 piling up = a pile of results nobody has explained, while the queue keeps running |
| ‼️ Every cited **constant** still holds in `state.json` | that file opens by saying "change the weights, the frame sampling or the metric and this entire file is void". `check` compares `measured_at` against `corpus.declared_at` |
| ‼️ **Every card's `premise_share.measured_on` equals this graph's `corpus`** | MLClaw's addition. A share quoted from elsewhere is treated as **having no premise** — the other face of CLOSE's "never kill a proposal with another corpus's number", on the justification side |
| ‼️ **No two arms were open at once in one working tree** (`concurrent_arms_one_tree`) | MLClaw's addition, and the axis the rest of this table is blind to — see §1.5. ‼️ **Severity does not reward silence**: two arms naming one branch and two arms naming none are the same finding at the same level, because the floor's four states were forced by exactly the opposite arrangement — the honest form cost two criticals and an invented reference cost one major. What clears it is two DISTINCT branches, which is also the only thing that fixes the round. Overlap is computed from `run.json → started_at` + `duration_s`, and when the record cannot say, that is **unknown** and reported as such — never as "did not overlap" |
| ‼️ **Every code-writing arm has a `tree`, and its `base` is the round's** (`arm_tree_unrecorded` / `arm_base_drift` / `round_base_undeclared`) | MLClaw's addition. `arm_base_drift` is `premise_share`'s scope guard one axis over, with the same verdict: an arm off another base is not a weak comparison, it shares **no control** with the round. And with `base.commit` null the axis is simply unmeasured — reported at round level, once, as absence rather than as a pass |
| ‼️ **The card's tree and the run's snapshot agree** (`arm_tree_disagrees_with_run`) | MLClaw's addition, and the two ends here have **different authors**: the card is written from intent before the arm opens, the snapshot is read off a disk at launch. When they disagree the disk is right and the card describes a run that did not happen |
| ‼️ **Every result carries a tier** | MLClaw's addition. A number with no tier gets promoted into a hard conclusion, which is exactly where a false noise floor comes from |
| **With no noise floor, there are no T2/T3 results** | MLClaw's addition. With no floor, "no significant improvement" is **undecidable**, not negative |
| ‼️ **When the floor is `external`, there are no T3 results** | MLClaw's addition. An external floor supports T2 — not supporting that would be a door opening onto a wall; it does not support T3, because T3 is the last gate before a large run and the tier at which blind human review is mandatory. A soft number being promoted into a hard conclusion is the whole reason the tier ladder exists |
| ‼️ **The noise floor itself**: `measured_on` equals this graph's corpus · with `origin: mlclaw`, `runs` has at least two entries with matching `mode`/`scope` · with `origin: external`, `unchecked` is mandatory · `measured_at` is no earlier than the corpus declaration | MLClaw's addition. These fields **had no reader at all** (while `_comment_runs` claimed check would read run.json). So `_share_scope` would **kill a card** over an out-of-corpus share while an out-of-corpus **floor** was silently accepted and went on governing the wording of every result in the round — and the floor is the more expensive side. ‼️ An out-of-scope or expired floor is treated as **not measured** (its own `retires_on` says `dataset_snapshot`), and every T2/T3 falls back to `[T1 trend]` |
| ‼️ **The floor has four states, not "pass / fail"**: `verified` / `claim` / `unverifiable` / `not_measured` | MLClaw's addition, and the four states were forced by a real gap: `runs` used to be the only door, and somebody taking over an existing project **has no** MLClaw run to cite (the machine was released, the checkpoint is on a stopped disk, that pipeline never wrote run.json at all) — while `SKILL.md -> where you come in` says in its own heading that *users essentially never start from scratch*. The measured ranking of the three ways to write it was **inverted**: writing the number honestly with `runs: []` → two criticals, `check` refuses outright; pretending there is none → critical; **inventing two unresolvable ids → one major, and the floor takes effect as normal**. The record layer was paying a bonus to the least honest route. `origin: external` is now that front door (`claim`, the same word `conclude.py` gives an external reference), and **being unable to read a run record** remains a third fact (major, not void — "Never report data you could not look at"), except that it **no longer gets further than the front door**: both support T2 and neither supports T3 |

These must be **re-checked every round**, not once: the `measured_at` column beside the numbers
in `state.json` exists precisely so this scan can be done. All of it is executed by
`graph.py check` — the corresponding contracts are in `contracts/contract_explore.py`, and each
check class's docstring cites the section number in this file.
