---
name: lease
description: >
  Reference for the lease layer (`lease.py`) plus the human's window onto it. Read this when a
  run skill needs a machine it doesn't have — it documents the call sequence an orchestrating
  skill follows to acquire, renew, and release one. Also trigger directly for the user's own
  questions about held resources: "我现在租着什么", "有没有忘关的机器", "把那台关掉",
  "what am I paying for", "kill that box", "any orphaned instances". Acquisition is NOT an
  interactive flow here — a run skill performs it while talking to the user about its own run.
  Not for discovering credentials or servers (use /resources).
---

# /lease — the lease layer

Layer 2 of the three in `references/contract.md` "Where this sits — three layers", which is the
authority for the layering and for everything provider-facing. What matters here: L2 owns the
ledger and the ordering of the money rules, and it is **driven from above, not by a person** —
an orchestrating skill decides a machine is needed and issues lease commands downward. Nobody
manages leases as an activity.

So this file is two things, and neither is a dialogue: **a calling convention for L3**, and a
**read-mostly window** for when the user asks about held resources directly.

`/resources` discovers what you *can* use and owns provider registration in
`resources.json -> compute`. Leases live in `leases.json` beside it.

**A static server is a provider** — `provider_ssh` turns `up`/`down` into acquire/release of an
`O_EXCL` per-GPU claim on the target host (contract, static-box paragraph, for why). Nothing
bills, but the hold is real. Callers never branch on rented-vs-owned.

## For orchestrating skills (L3)

```bash
S=lifecycle/scripts/lease
python $S/lease.py capacity --gpu-count 1 --gpu-memory-gb 80 --arch-min sm_90
python $S/lease.py up --provider <p> --machine-type <s> --ttl-s <n> --price-hr <f> \
                      --run <run_id> --project <name>     # -> lease_id + reach://
python $S/lease.py addr    <lease_id>                     # resolved LIVE, never cached
python $S/lease.py renew   <lease_id> --ttl-s <n>
python $S/lease.py release <lease_id>
```

`up` returns a `reach://` handle; from there proceed exactly as `lifecycle/references/run-mechanics.md` "Path Mapping (Cross-Machine Execution)" and
`/train-run` Execution Modes already specify. Nothing downstream may branch on provider name.

**Re-resolve with `addr` rather than storing what `up` returned.** A stop/start hands the
box a different address and hands the old one to somebody else; with relaxed host-key
checking, ssh then connects to a stranger's machine without complaining.

**Holding several at once is not this layer's job.** `lifecycle/scripts/shared/pool.py`
is the caller above — it fills owned hardware before anything that bills, states the cost
of the whole fleet once before acquiring it, and knows that a preempted trial is not a
refuted one. `lifecycle/references/fleet.md` is its authority; read that, not this file,
before a search opens more than one machine.

**What L3 owns, and L2 deliberately does not:**

| Obligation | Why it can't live in L2 |
|---|---|
| **Confirm the spend before `up`** — show `provider · machine_type · $/hr · est. total · TTL` | Only L3 knows which run, why, and for how long. This is the one moment the number is visible; a lease acquired without it reads as free. |
| **Pick the machine type** from the `capacity` table | Requires the user's judgement on price vs wait vs region. |
| **Reject a shape the env can't use** before `up` | Needs `run.json -> env` / `config.json -> env_snapshot`, which L2 never reads. Rule and rationale: contract, "`arch` is a hard requirement". Lands alongside `/train-run` Step 2's existing env-vs-`env_snapshot` diff. |
| **Refuse a job longer than the credential TTL** (contract Money rule 5) | Needs the estimated duration, which comes from the schedule and prior-run throughput. |
| **`renew` from the monitor step** | Only the thing watching the job knows it is still alive. Skipping it lets a 4-hour TTL kill a 30-hour run. |
| **Decide keep-or-release on failure** | "Keep the box so I can inspect it" is a judgement about debugging value vs burn rate. L2 executes it; it does not make it. |

**The backstop rule for anything added to L3:** prose does not execute when a session dies, so
every money-losing path needs a non-prose fallback. Today: a missed `release` is caught by the
dead-man TTL plus `reap` at conversation start; a missed `renew` kills the job but costs
nothing — it fails safe. Any new L3 step must land on the safe side of that line or bring its
own backstop.

**`cost` answers a different question from `status`.** `status` is what is burning *right now*; `cost` is what a round *has* cost — the one actually asked, repeatedly, across a six-day search, and the one that decides rent-versus-buy.

‼️ **Rows with no `price_hr` are excluded and counted, and the total says it is a LOWER BOUND.** `--price-hr` is optional on `up`, so a ledger routinely holds unpriced rows, and a sum over the priced ones alone reads exactly like the whole bill — the same shape as a census with `complete: false`. A round that rented ten boxes and priced six does not have a cost; it has a floor and four unknowns.

‼️ **Its window starts at `requested_at`, and billing does not.** The ledger's only start stamp is when the lease was asked for; the provider charges from when the box comes up. One machine type measured ~80 minutes to provision, so this over-counts in a direction nobody can see unless the report says so — it does.

## The human's window

Only three things the user asks directly. None is a flow.

| Ask | Do |
|---|---|
| "what am I holding / paying for" | `lease.py status` — report per lease: provider, machine_type, age, `$/hr` (`0` for owned hardware), accrued, owning run (**or none — that's the interesting case**), TTL remaining. It also reconciles: a row with no instance and an instance with no row are both surfaced. **Report `compute_usd_per_hr` and `storage_usd_per_hr` separately**, and `residual_storage` alongside — see "Two meters" below. |
| "any forgotten boxes" | `lease.py reap` — cloud-side truth by tag prefix, correct with `leases.json` missing or stale. **Read `complete` before quoting the count**: `orphans: []` off a sweep that reached nothing is byte-identical to "there are none", and `orphans_is_lower_bound` is the only thing separating them. `orphan_storage` is the second list and it is the one nobody asks for. Belongs in CLAUDE.md "On Conversation Start" as a third step, gated on a provider being registered — **not yet wired there**. |
| "did I actually release that one" | `lease.py history [--instance-id ID]` — the **past tense**, and the only verb that can speak about a machine that no longer exists. A box missing from `status` is equally consistent with a scope nobody enumerated; only a lifecycle event proves release. A provider with no log reports `supported: false`, which is an honest unanswerable rather than a silence to read as "gone". |
| "kill that one" | `lease.py release <lease_id>` — it verifies `gone` before closing the row and refuses to close on an unverified teardown. If the owning run is still `running`, say so first: releasing kills the job. If the run `failed` and was kept for inspection, name what would be lost. |

### Two meters, and only one of them stops on its own

Compute is the loud one. It is also the one that ends by itself: the dead-man switch
fires, the instance halts, the large number goes to zero. **Storage is the quiet one.**
It starts when the box is created, it does not stop when the box stops, and on most
providers it survives the box being deleted.

So `sweep` returns two lists and `status` / `reap` report two subtotals. Quoting the
compute figure alone answers "nothing is running" — which is *true*, standing next to a
volume that has billed every hour since. Three ways one gets left behind, all ordinary:
a create that never became reachable and was torn down without its disk; a delete that
deliberately spared the volume in order to rescue data off it; a volume MLClaw never
created at all.

**`price_hr: null` on a storage row means nobody wrote the rate down** — never that it
is free. `total_is_lower_bound` is set whenever a row went unpriced or the sweep was
partial, and it must be said out loud before the number is.

**An adapter that reports no `storage` key at all is treated as not having looked**, so
its sweep is `complete: false`. That is deliberate and it is the difference between
`storage: []` ("looked, nothing bills") and silence. Owned hardware says `[]` and means
it: the disk was bought, so releasing a claim accrues nothing.

**`status` and `reap` scan workspace-wide, not project-scoped.** Cost and exclusivity both cross
project boundaries — a lease held by another project still blocks this one and still bills. That
is a deliberate asymmetry against the project-scoped run scan in CLAUDE.md "On Conversation
Start".

## Adding a provider

Per the contract's "Adding a provider" requirements: one `provider_<name>.py` plus its machine type
table. `lease.py` discovers adapters by filename, and `_common.py` carries the shared JSON /
error / shape conventions so an adapter is not a copy-paste of the last one. If anything *else*
has to change, the contract leaked and the fix belongs in the contract.

Installed today:

| Adapter | Registration | What it is honest about |
|---|---|---|
| **`ssh`** | self-registering off `resources.json -> servers` | owned hardware. `up`/`down` are acquire/release of an `O_EXCL` per-GPU claim; nothing bills, `storage: []`, `history` **unsupported** — the marker is deleted on release, so the past is genuinely unreadable |
| **`nebius`** | `resources.json -> compute.nebius` | rented, federated SSO. Has an audit log, so `history` answers "did I release it". Prices are hand-written **claims** — no per-hour price query exists. `credential_ttl_s: 43200`, so Money rule 5 refuses a long run |
| **`lambda`** | `resources.json -> compute.lambda` | rented, static API key. Prices are read **live**, so they are `verified`. **No audit log → `history` is `supported: false` and "did I release that box" is unanswerable on this provider.** No stopped state; a guest `shutdown -h` stops nothing, so `up` **refuses** without a `dead_man_key_path` rather than renting a box that can never expire |

An adapter being present is not the same as an account existing, which is why the block
is what counts as registration.

**The three disagree in useful ways, and that is the point of having more than one.**
A single adapter makes every provider-shaped assumption invisible: Nebius's hand-written
prices look like how prices work, its audit log looks like something every cloud has, and
the contract's guest-side `shutdown -h` fallback looks universal. Against Lambda all three
are false — price is live, there is no log, and a guest halt is *worse* than no switch
because it removes the last way to reach the box. Read `machines_<name>.json ->
capabilities` before assuming any of it; the `_comment_*` keys there carry the reasoning,
not decoration.

**No adapter pins an infrastructure id** — no tenant, project, subnet or image id, in the
code or the table. Every one is discovered from the credential at call time. That is both
hygiene (this repo is not the never-committed file) and correctness: ids drift, and a
pinned one is a wrong answer that looks like a configured one. `contract_fleet.py` checks it.

Per CLAUDE.md "Script Integration", adapters are an optimization: if one fails, do the same work
inline with the provider's CLI and continue. The exception is `down` — never paper over a failed
teardown, escalate it loudly.

## Safety

- **`status`, `reap`, and `release` must work with zero upstream state.** Killing a billing box
  cannot depend on locating a project, reading `project.json`, or a healthy `leases.json`.
- **Never acquire without an explicit confirmed decision.** L2 has no opinion about spending;
  if no L3 confirmation happened, the `up` should not have been issued.
- **Never write a resolved address into a config file.** `addr` is resolved live per the
  contract; a cached IP after a stop/start points at someone else's machine.
- Credentials are never displayed — provider, profile name, and TTL only.
