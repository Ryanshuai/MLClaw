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
python $S/lease.py renew   <lease_id> --ttl-s <n>
python $S/lease.py release <lease_id>
```

`up` returns a `reach://` handle; from there proceed exactly as CLAUDE.md "Path Mapping" and
`/train-run` Execution Modes already specify. Nothing downstream may branch on provider name.

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

## The human's window

Only three things the user asks directly. None is a flow.

| Ask | Do |
|---|---|
| "what am I holding / paying for" | `lease.py status` — report per lease: provider, machine_type, age, `$/hr` (`0` for owned hardware), accrued, owning run (**or none — that's the interesting case**), TTL remaining. It also reconciles: a row with no instance and an instance with no row are both surfaced. |
| "any forgotten boxes" | `lease.py reap` — cloud-side truth by tag prefix, correct with `leases.json` missing or stale. Belongs in CLAUDE.md "On Conversation Start" as a third step, gated on a provider being registered — **not yet wired there**. |
| "kill that one" | `lease.py release <lease_id>` — it verifies `gone` before closing the row and refuses to close on an unverified teardown. If the owning run is still `running`, say so first: releasing kills the job. If the run `failed` and was kept for inspection, name what would be lost. |

**`status` and `reap` scan workspace-wide, not project-scoped.** Cost and exclusivity both cross
project boundaries — a lease held by another project still blocks this one and still bills. That
is a deliberate asymmetry against the project-scoped run scan in CLAUDE.md "On Conversation
Start".

## Adding a provider

Per the contract's "Adding a provider" requirements: one `provider_<name>.py` plus its machine type
table. `lease.py` discovers adapters by filename, and `_common.py` carries the shared JSON /
error / shape conventions so an adapter is not a copy-paste of the last one. If anything *else*
has to change, the contract leaked and the fix belongs in the contract.

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
