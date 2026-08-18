# Fleet mechanics — many machines, one search

Loaded on demand. Read this before holding **more than one machine at a time**:
`/train-tune` with `max_concurrent > 1`, the exploration stage when it lands, or any
sweep that rents rather than borrows. A single box needs none of this — `/lease` plus
`run-mechanics.md` "Path Mapping (Cross-Machine Execution)" already covers it.

Contract statements in this file are cited by checks in `contracts/` as
`fleet.md ->` followed by the section heading in double quotes.

Layering is the compute provider contract's (`.claude/skills/lease/references/contract.md`
"Where this sits — three layers"). This file is **layer 3 and only layer 3**: what a
caller does with N leases once L2 can hand them out one at a time. Nothing here may
mention a provider by name, and the moment something does, it belongs in an adapter.

```
   /train-tune, /explore-*        decide WHAT to run          ← the search
        │  slots, not machines
   pool.py                        decide WHERE it runs        ← this file
        │  one lease per unit
   lease.py                       ledger + money rules        ← L2
        │  seven verbs + history
   provider_<name>.py             one cloud, or one owned box ← L1
```

## Preemption has two axes, not one

`--outcome` answers **is this trial evidence about its hypothesis**. It does not answer
**did the work survive**, and on a real fleet those came apart. Of ten preempted L40S
boxes in one round: four held weights reachable only by attaching the volume to another
platform, and three probably held nothing — but that could not be checked, because
starting the box back up hit the same tenure wall that preempted it. All seven were
`--outcome preempted`, and the record could not tell them apart.

So `pool.py release` carries a second field, required whenever the outcome is
`preempted` or `crashed` — the cases where the box may be going away with work on it:

| `--artifacts` | |
|---|---|
| `recovered` | pulled off and verified. **The only state that permits destroying the box** |
| `present_unreachable` | on the disk, not reachable by the normal route — needs another path first |
| `absent` | checked, and there is nothing there |
| `unverifiable` | **could not look.** Never report this as `absent` |

‼️ **`unverifiable` is not `absent`.** A tenure wall produces the first while it reads
like the second — the same distinction `census.py` keeps between a location that did not
answer and a directory that is genuinely empty, and the one `/repro` keeps between an
unprobed axis and an intact one. Friction is deliberately targeted rather than universal:
an `ok` release needs nothing, because universal friction is the kind that gets routed
around.

## What a fleet is, and what it is not

A fleet here is **one search holding several machines at once**. It is not a cluster
manager, not a scheduler, and deliberately not a queue service: there is no daemon, no
control plane, and nothing runs between sessions. The whole of it is a record of which
slots this search holds and a set of leases whose TTLs outlive the loop that renews them.

That modesty is the design. A scheduler would need to survive the agent, and MLClaw's
driver is a conversation that can end mid-sweep — so anything whose correctness depends
on the orchestrator still being alive is already wrong. What survives instead is the
same thing that survives a single run: the **dead-man switch on each box** and a
**ledger the next session can read**. A fleet is N of those, not a new kind of thing.

### Slot, not machine

The unit the search asks for is a **slot**: one trial's worth of compute. A slot is not
a machine and the difference is load-bearing in both directions.

- An 8-GPU box is **eight slots** for single-GPU trials. Renting eight 1-GPU boxes to
  run eight trials, when one 8-GPU box was cheaper per card and provisioned once, is the
  most common way a sweep costs triple what it should.
- A slot is not always a whole GPU's worth of *time*: a box held across twelve
  sequential trials is one lease and one data staging, not twelve. **Provisioning and
  staging are paid per box, not per trial** — which is why the pool holds boxes and
  drains trials through them, rather than acquiring per trial.

So the pool's job is the mapping `N trials → M boxes → one lease each`, and `M` is
chosen from the shape of the trial, not from `N`.

### Owned before rented

Fill from `resources.json -> servers` before anything that bills. Not a cost
optimization — a **record** one: an owned GPU held under a `provider_ssh` claim and a
rented H100 held under a cloud lease are the same row to everything downstream, so the
only place the distinction can be made is at fill time, and the only sane default is
free-first. A pool that opened four cloud boxes while the user's own 4090 sat idle has
not made an error any later step can detect.

## The two questions, and why one list cannot answer both

| Question | Answered by | Verb |
|---|---|---|
| What exists **right now**? What is burning money? | resource lists, walked across the whole scope | `sweep` |
| Did *that* box get released? What happened to it? | the provider's **lifecycle / audit log** | `history` |

The second is the one that gets answered wrongly, and it is worth being blunt about why:
**absence from a list is not evidence of deletion.** A box missing from `instance list`
is equally consistent with a project you did not enumerate, a region you did not ask, a
credential that reached less far than you assumed, or a rename. Every one of those reads
as "it's gone" and exactly one of them is.

This is CLAUDE.md "Never report data you could not look at"` with money attached
instead of data. The failure is identical in shape and worse in consequence: a partial
census under-reports a dataset, a partial sweep under-reports **a running bill**, and
the whole point of `reap` is to be trusted when it says zero.

Hence: **`sweep` reports its own scope, and a scope with an unreached corner is
`complete: false`.** `reap` and `/lease status` must say so before quoting a count, and
a count from an incomplete sweep is a **lower bound** and has to be said as one. An
adapter that cannot enumerate its own scope returns `complete: false` permanently — that
is an honest adapter, not a broken one.

`history` is the only thing that can turn "not in the list" into "released at 14:22".
Where a provider has no lifecycle log, the adapter says so (`history` → `unsupported`)
and the question stays unanswerable — which is a fact worth printing, not a gap to fill
with the list's silence.

### Group by id, never by name

Instance names are mutable and reusable; ids are neither. A failed box deleted and its
name reused an hour later on a fresh one makes a name-keyed history report the **live**
machine as released — the exact inverse of the truth, produced confidently. Any grouping,
join, or reconciliation across time keys on the id, and a rename is recorded as an alias
rather than a second machine.

The same rule is why `sweep` echoes back the **tag** L2 issued rather than matching on
anything the user or the console can edit.

## Placement: capacity is not one number

`capacity` returns rows, and three things about those rows decide whether a search can
run at all.

**Quota and stock are different limits with opposite remedies.** "You may have 32" and
"there are 0 free right now" both surface as a failed create. The first is a support
ticket; the second is a wait, a different machine type, or a different region — and a
ticket for the second one buys nothing, because no amount of approval conjures a
physical GPU. The row must say **which limit binds**, per the provider contract's
`binding_limit`; when the adapter genuinely cannot tell them apart it reports `quota`,
whose remedy is not "retry".

**Availability is reported per placement domain, and a search reads the wrong number by
default.** Providers subdivide a region (fabric, zone, AZ, cell), report free capacity
per subdivision, and apply quota per account across all of them. Reading the first row
under-reports; summing rows over-reports whenever quota binds first. The placeable count
is `min(sum of free across domains, quota)` — and for a single instance it is the
**largest single domain**, since one instance lands in one domain. A multi-node job that
needs a shared interconnect must land in *one* domain and cannot use the sum at all.

**A separate pool with separate stock usually exists, and it is the one a search wants.**
See below.

## Preemptible is the default for a search, and the record rule that makes it safe

Interruptible capacity is cheaper and — this is the part that surprises — **frequently
in stock when on-demand is not**, because it draws on a different pool. A sweep that
refuses it is routinely a sweep that does not run.

It suits search work specifically: trials are short, independent, individually
worthless, and already checkpointed. Losing one costs its elapsed minutes. Losing a
final production training run costs its whole wall clock, which is why this default is
scoped to the search and does not propagate to `/train-run` on its own.

Two mechanical requirements travel with it, and both were learned by hitting them:

- **Preemption must stop the box, not destroy it.** Keeping the disk is what lets the
  trial resume from its last per-epoch checkpoint instead of starting over — which is
  the entire reason the economics work. A provider whose interruption policy deletes the
  volume turns every preemption into a lost trial.
- **The interruption policy and the recovery policy are two settings and they constrain
  each other.** Providers reject the combinations that would silently auto-recover a
  box into a state the caller does not expect; the rejection arrives as an API error at
  create time and is worth reading rather than retrying past.

### A preempted trial is not a failed trial

This is the rule that has to survive contact with the search loop, and it is a **record**
rule, so nothing raises when it breaks.

An HPO loop reads each trial's outcome and updates its beliefs. A trial that was
interrupted by the provider at epoch 3 produces a truncated curve and a bad final
metric — and if that lands in the session as an ordinary result, the search concludes
that **the configuration** was bad. It then steers away from a region of the space for a
reason that has nothing to do with the model, and no later step can recover the mistake,
because the poisoned belief is indistinguishable from a real one.

So:

- A run ended by preemption is recorded with that cause, and is **excluded from the
  search's evidence** rather than counted as a refutation.
- It is re-placed and re-run, not replaced by a different hypothesis. The trial still
  owes an answer.
- `scope` records what it actually completed, per `run-mechanics.md` "Record integrity"
  — a trial that ran 3 of 30 epochs is not comparable to one that ran 30, and the
  existing comparability rules already refuse it once `scope` is honest.

The same shape applies to any infrastructure-caused end: a TTL expiry, a reaped box, a
host that vanished. **Infrastructure outcomes and model outcomes must never merge into
one `failed`.** The loop is entitled to know which of its trials told it something.

## Whose dead-man switch, and what renews it

Each box carries its own expiry (contract Money rule 3) and nothing about a fleet
changes that. What a fleet changes is **who renews it**, and the answer is not the trial.

- A trial finishing must **not** release the box. The next trial wants it, and a pool
  that tears down between trials pays provisioning and staging N times.
- A trial dying must not release it either — the pool decides, and "keep it for
  inspection" is a judgement, per `/lease` L3 obligations.
- The **loop** renews, once per iteration, for every held lease. That is the only
  component that knows the search is still alive. `pool.py heartbeat` is one call for
  the whole fleet precisely so it cannot be half-forgotten.

The failure mode this guards is specific and expensive: TTLs sized for one trial, a
search that runs overnight, and every box in the fleet expiring mid-flight. The opposite
failure — the session ending and TTLs running out — is the **safe** one, and is the
reason the switch exists.

**A pool must be closable with no upstream state.** `pool.py close --session <id>` reads
the pool record; if that is gone, `/lease reap` is the backstop and works from the cloud
side alone. Neither may require the project, the tune session, or a healthy `leases.json`
— the session that opened the fleet is precisely the one that died.

## Cost is reported before it is spent, once, by the caller

L2 has no opinion about money and L1 has no idea what the job is, so the only place a
fleet's cost can be stated is the caller — and it is stated **before `open`**, as one
number for the whole fleet rather than per box. `N slots × $/hr × estimated hours`,
with the estimate from prior runs' throughput where there is one and marked absent where
there is not.

A per-box confirmation is not a substitute: four separate "$2.40/hr, ok?" prompts is how
a sweep gets approved at four times the number anyone held in their head. And a price
that came from a hand-maintained table rather than the provider's own API is a **claim**,
not a verified figure, and says so — the same status vocabulary `/discover` and
`/ask-human` use, for the same reason.

## Traps

Every one of these produced a confident wrong answer, most of them in the session that
produced this file. Ordered by how badly they mislead.

| Trap | What it looks like | Rule |
|---|---|---|
| The credential's default scope is not where the machines are | A bare list returns empty and reads as "you have nothing" | Enumerate the scope tree; never conclude absence from an unscoped list |
| Absence from a list read as proof of release | "I released it" — while it bills in a project nobody enumerated | Only a lifecycle event proves release: `history`, not `sweep` |
| Stopped read as released | Compute halted, storage bills indefinitely, `--running` reports zero | `stopped` maps to `running`; report stopped boxes **with** their disks |
| Ownership inferred from creation time | A colleague's box reported as yours on a shared account | Ownership is the tag L2 issued, or the operator field in the log — never the clock |
| Truncating an identity/scope response | Every later query covers a subset while looking exhaustive | Read the whole response; the tail is where the scope list usually is |
| One page taken for the whole list | Under-report, called "all" | Follow the pagination token on **every** list, including the ones that "obviously" fit |
| Availability read off one placement domain | "No capacity" when a sibling domain is empty, or vice versa | `min(sum across domains, quota)`; largest single domain for one instance |
| Created ≠ usable | Create succeeds, the box appears in the list, then silently dies without an address; it looks alive until ssh times out | `up` is not complete until the box is **reachable**; a create that never became reachable is `no_capacity`, and the remains must be torn down |
| A create error swallowed by output filtering | "failed to return an instance id" — with the real reason discarded upstream | Never filter an adapter's stderr; classify it |
| "Failed to return an id" treated as "nothing was created" | The box exists and bills while the local record is empty and teardown says "nothing to destroy" | A create whose response was lost means **go and look**, never retry blind. This is why the lease row is written first |
| A stale host alias pointing at a recycled address | With relaxed host-key checking, you connect to a stranger's machine and nothing complains | Resolve the address live on every call; never write one into a config |

The last four are `up`-time failures and they share a property worth naming: **the local
record and the provider disagree, and the local record is the one that looks fine.**
Money rule 1 exists for exactly this — a lease row pointing at nothing is recoverable,
an instance pointing at nobody is not.

## What this does not do

Stated so the gaps are gaps and not surprises.

- **No multi-node distributed training.** A slot is one machine. `world_size` spanning
  hosts needs a rendezvous, a shared interconnect domain, and a failure model where one
  dead worker kills the job — none of which the slot abstraction carries. A multi-GPU
  *single-host* job is a normal slot with `gpu_count > 1`.
- **No bin-packing across searches.** The pool is scoped to one session. Two concurrent
  searches contend through the same per-GPU claim every other caller uses, and the loser
  waits.
- **No spot-price bidding or cross-provider arbitrage.** `capacity` reports what each
  provider says; choosing is the caller's, with the user in the loop.
- **Nothing runs between sessions.** No daemon renews a TTL while nobody is watching,
  and that is deliberate: the expiry is what makes an abandoned fleet safe.
