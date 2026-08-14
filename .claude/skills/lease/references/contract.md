# Compute Provider Contract

The authority for what a compute provider adapter must implement. One adapter per
provider under `lifecycle/scripts/lease/provider_<name>.py`, plus one machine-type table
`machines_<name>.json`.

Everything else in MLClaw is provider-blind. Run skills already reach a machine with
SSH + rsync + tmux (`lifecycle/references/run-mechanics.md` "Path Mapping (Cross-Machine Execution)", `/train-run` Execution Modes); this
contract only answers **where that machine comes from and when it dies**. Nothing
downstream of `addr` may branch on provider name.

## Where this sits — three layers

```
layer 3  task orchestration   provision -> stage data -> launch -> monitor -> collect -> release
                              lives in the run skills (/train-run Steps 3-5), not here
                              may not know which provider it is on
layer 2  lease lifecycle      lease.py — ledger, money-rule ordering, cross-provider merge
                              may not know what runs on the machine
layer 1  provider adapter     provider_<name>.py — THIS CONTRACT
```

Layer 2 has leaked when its ledger mentions checkpoints, datasets, or epochs. Layer 3
has leaked when it contains `if provider == …`.

Layer 3's variation is real but it is **not provider variation** — it tracks task shape:
one box per unit vs N persistent boxes draining a queue vs multi-unit shards (decided by
IP quota and provisioning cost), copy-per-box vs upload-once-to-object-storage (decided by
data size × reuse count), agent-orchestrated vs autonomous self-driving boxes (decided by
whether anything is around to babysit). None of those choices is answered by knowing the
cloud's name, which is why they must not be pushed down into an adapter.

A static on-prem box is a provider, not an exception. `provider_ssh` reads `addr` from
`resources.json -> servers.<key>` and implements `up`/`down` as **acquire/release of an
exclusivity claim** rather than create/destroy: an `O_EXCL` marker file on the target
machine, holder and expiry recorded inside, released on normal exit and stolen only
explicitly. Nothing is created, so nothing bills — but the three lease guarantees still
hold, which is what lets a run skill treat owned and rented hardware identically.

The claim is what stops a second session, or the same project rsync'd to another host,
from double-booking one GPU. MLClaw has no such interlock today: two conversations can
each scan `runs/*/run.json`, each see nothing running, and each launch. Placing the
marker on the *target* machine rather than in the project tree is deliberate — the
contended resource is the GPU, and it is the only place every contender can see.

If a run skill ever needs to know whether it's on rented hardware, the contract has leaked.

---

## The eight verbs

`history` is the odd one and the reason the count changed: the other seven describe
machines that exist, and it is the only one that can speak about a machine that does
not. See `lifecycle/references/fleet.md` "The two questions, and why one list cannot answer both".

| Verb | Signature | Must hold |
|---|---|---|
| `renew` | `(instance_id, ttl_s) -> new_expiry` | Extends the dead-man switch, provider-side and on-box. Load-bearing, not a convenience: Money rule 3 requires every hold to expire, and a 30-hour training run under a 4-hour TTL is killed by its own lease. Called by the orchestration layer's monitor step, which is the only thing that knows the job is still alive. Must fail — not silently succeed — when the hold is already gone. |
| `capacity` | `(requirements) -> [{region, machine_type, avail, price_hr, binding_limit, label}]` | **`machine_type` must round-trip into `up --machine-type`** — L3's whole flow is show-the-table, user picks a row, lease that row; a `machine_type` holding a display string breaks it. Put display text in `label`. Returns **which limit binds**, not just a count. `avail: 0` with `binding_limit: "vpc.ipv4.public=3"` is actionable; `avail: 0` alone is not. Never inferred from a failed `up`. **One row per purchasing pool** — on-demand and interruptible have different stock and different prices, and collapsing them hides the row a search usually wants (see "Interruptible capacity"). |
| `up` | `(n, machine_type, tags, ttl_s) -> [instance_id]` | **Lease written before the API call** (see Money rule 1). Tags applied at create, never in a second call. `ttl_s` sets the dead-man switch (Money rule 3). Partial success returns what exists — n=4 yielding 2 is a normal outcome, not an error. **Not complete until the instance is reachable** — see "Created is not usable" below. |
| `addr` | `(instance_id) -> reach://…` | Resolved live on **every** call. Never cached, never written into a config file. A stop/start cycle changes it on most providers. |
| `state` | `(instance_id) -> pending\|running\|stopping\|gone\|failed` | Normalized enum only. The adapter owns the mapping, including the ones that cost money — see below. |
| `down` | `(instance_id) -> ok` | Idempotent. Already-gone is **success**, not an error. Must leave **no residual billing** (Money rule 2). |
| `sweep` | `(tag_prefix) -> {units: [{instance_id, tag, age_s, price_hr, expired}], scope: {complete, checked, unreached}}` | Echo the **`tag`** back — layer 2 reconciles on the token it issued, never on an `instance_id` whose format you own. Do **not** self-declare `provider`; layer 2 injects it, so a copy-pasted adapter cannot misroute a later `release`. Finds orphans from the **provider side only** — must work with `leases.json` deleted, corrupt, or written by a different machine. **Reports its own scope**: `complete: false` whenever any corner went unread, with `unreached` naming which. A bare list is not accepted — see "Scope completeness" below. |
| `history` | `(tag_prefix \| instance_id, window_s) -> {events: [{instance_id, tag, action, at, actor, outcome}], scope: {...}, supported}` | The **past tense**, and the only verb that can distinguish "released" from "never looked there". Keyed on `instance_id`, never on a name — names are mutable and reusable, and a name-keyed join reports a live box as released (fleet.md "Group by id, never by name"). Only **completed** events count: a create that ended in error left nothing behind, and counting it invents machines. A provider with no lifecycle log returns `supported: false` — an honest unanswerable, never an inference from `sweep`'s silence. |

## Normalized enums

**`state`** — five values. Two mappings are load-bearing:

- `stopped` (AWS, GCP) maps to **`running`**, not `gone`. Compute billing stops; disk
  billing does not. Reporting it as `gone` teaches the agent the money stopped.
- A provider that deletes the boot disk on instance delete (Nebius) and one that
  retains it (AWS EBS, unless `DeleteOnTermination`) both map to `gone` **only after
  the disk is also released**. If the adapter can't guarantee that, it returns
  `stopping` and `down` is not yet complete.

**`reach`** — the transport, not an address:

| Form | When |
|---|---|
| `ssh://<user>@<ip>:<port>` | public IP available |
| `ssh://<user>@<tailnet-name>` | mesh reachable (no public IP consumed — see Money rule note) |
| `ssm://<instance-id>` | AWS SSM Session Manager; no inbound port, no public IP, no EIP quota |

Returning a mesh or SSM form where the provider supports it is preferred: on AWS the
public-IPv4 quota is a *separate* limit from vCPU/GPU quota and binds fleets at
surprisingly small numbers. Choosing a reach form that consumes no IP removes that
constraint instead of negotiating with it.

## Scope completeness

An adapter whose resources live under a tree — tenants, projects, regions, subscriptions —
**walks the tree and reports what it reached**. Two rules, both non-negotiable, both
learned by getting a confident wrong answer:

1. **Never conclude absence from an unscoped list.** A credential's configured default
   points at one corner. Asked bare, the API answers about that corner and returns empty,
   which reads as "you have nothing" and means "wrong place". Enumerate, then ask each.
2. **Follow pagination on every list.** One page is not proof of completeness, including
   on the lists that obviously fit.

What cannot be reached is named, not dropped. `sweep` carries `scope: {complete, checked,
unreached}`, and layer 2 propagates it: `reap` reporting "no orphans" off a partial sweep
is the failure this exists to prevent, and it is silent, and it costs money for as long as
nobody re-runs it. This is CLAUDE.md "Never report data you could not look at"`
applied to billing — same rule, same three-way distinction between *did not answer*,
*is not there*, and *is genuinely empty*.

An adapter that cannot enumerate its own scope hard-codes `complete: false`. That is a
correct adapter with a stated limit, and it is strictly better than one that implies
completeness it never had.

## Created is not usable

`up` returns when the machine is **reachable**, not when the API accepted the create.

The failure it guards is specific: create succeeds, the instance appears in the list with
a plausible state, and it then dies on its way up — no address, no error, no status
message — because it passed the quota check and could not be *placed*. Everything reads
as a working box until ssh times out several minutes later, by which point a fleet may
have "acquired" a dozen of them and a search has launched trials into nothing.

So the adapter polls to a terminal condition and maps it:

- reachable → return the id
- terminal-without-address → this is `no_capacity`, **and the remains are torn down
  before returning**, because a box that never came up still holds a disk
- still pending at the deadline → `transient`, id returned, lease left open — the one
  case where a half-acquired box must stay visible rather than be quietly abandoned

`state` mirrors it: a machine that was created and then died without becoming reachable
is `failed`, never `running`. `failed` and `running` prescribe opposite next actions and
the provider's own state string routinely cannot tell them apart.

## Interruptible capacity

Where a provider sells the same hardware from a separate interruptible pool, the adapter
exposes it as **its own `capacity` row** with its own `machine_type`, price and stock —
never as a flag on the on-demand row. Two reasons, and the second is the one that matters:

- the price differs, so a collapsed row cannot state cost honestly;
- **the stock is genuinely independent.** Interruptible frequently has cards free at the
  hour when on-demand has none. A caller shown one row concludes "no capacity" and waits,
  when the machine it wanted was available the whole time from the other pool.

Two mechanical requirements the adapter owns:

- **Interruption must stop, not destroy.** The disk has to survive so a checkpointed job
  resumes. An adapter that cannot guarantee this must not offer the pool, because the
  caller's economics (fleet.md "Preemptible is the default for a search") assume it.
- **Interruption policy and recovery policy constrain each other**, and providers reject
  the combinations that would auto-recover a box behind the caller's back. That rejection
  is an API error at create time; classify it as `permission`, never retry past it.

The caller decides whether interruptible is acceptable — it is a property of the *job*,
not of the provider. `fleet.md` states when a search should prefer it and the record rule
that keeps a preempted trial from being read as a refuted hypothesis.

## Shape resolution: requirements → machine type is a lookup, not a translation

MLClaw declares **requirements**, derived from `config.json -> resources`:

```json
{ "gpu_count": 1, "gpu_memory_gb": 80, "host_ram_gb": 200,
  "disk_gb": 300, "arch_min": "sm_90", "arch_max": "sm_90" }
```

Each adapter carries a hand-written table of its own machine-type strings:

```json
{ "gpu-h100-sxm/1gpu-16vcpu-200gb":
    { "gpu": "H100-SXM", "gpu_count": 1, "gpu_memory_gb": 80, "arch": "sm_90",
      "host_ram_gb": 200, "vcpu": 16, "regions": ["eu-north1"], "price_hr": null } }
```

Do **not** invent a portable shape language and translate into it. The details that
would be lost — SXM vs PCIe interconnect, host RAM, local NVMe, compute capability —
are the only ones that decide whether a job runs. Requirements → candidate machine types is a
table lookup; the table is data, is reviewable, and is where each provider's weirdness
is allowed to live.

**Put in the table only what the API will not tell you.** Where a provider serves shape
facts live — gpu count, vCPU, host RAM, GPU memory — read them and leave the table to
carry what it does not: `arch`, a human-readable GPU name, and price. A hand-written copy
of a value the API also serves is a value with two authors, and the stale one is the one
that gets read.

**A hand-maintained `price_hr` is a `claim`, not a measurement**, and rows carry which it
is. Several providers have no per-hour price query at all, so the table is the only source
— which is fine, and stating that it is somebody's note from a console page in April is
what stops a fleet cost estimate from being quoted as a fact. Same status vocabulary as
`/discover` and `/ask-human`, for the same reason. A row with no price at all reports
`price_hr: null`, never `0`: zero is the correct value for owned hardware and must not
also mean "nobody wrote it down".

**`arch` is a hard requirement, not metadata.** A newer card can be strictly unusable:
an `sm_100` (Blackwell) host runs code built for cu128 but dies with "no kernel image
is available" on a torch 2.1.2/cu121 environment — the job launches, burns provisioning
time, and fails at the first kernel. MLClaw already captures torch and CUDA versions in
`run.json -> env` and `config.json -> env_snapshot`, so it can refuse an incompatible
machine type **before `up`**. Any adapter whose machine-type table omits `arch` disables that check.

## What every adapter declares

```json
"capabilities": {
  "credential_ttl_s": 43200,        // 0 = non-expiring static key
  "credential_refreshes": false,     // true for instance-profile / service-account
  "native_ttl": false,               // provider-side auto-destroy exists
  "image_bake": false,               // can snapshot a provisioned box
  "tags": true,                      // required for sweep; false = degraded reaping
  "billing_granularity_s": 1
}
```

`tags: false` is allowed but must be reported at `up` time: reaping then depends on
`leases.json` alone, which is the one failure mode that loses money silently.

## Money rules — non-negotiable, and not inherited from any provider SDK

**1. Write the lease before the create call.** A create that succeeds while its
response is lost is exactly how orphans are born. Prefer a lease row pointing at
nothing over an instance pointing at nobody. Reconcile after: a lease with no matching
instance is swept on next `/lease reap`.

**2. `down` means no residual billing.** Not "stopped", not "terminated but the volume
survives". If the adapter cannot verify the disk is gone, `down` is incomplete and
`state` stays `stopping`.

**3. Every box gets a dead-man switch at provision time.** MLClaw's driver is a
conversational agent — the session can end, be summarized, or the laptop can close.
A shell `trap … EXIT INT TERM` (the usual pattern in fleet scripts) does not exist
here and must not be relied on. Use provider-native TTL where available, otherwise
`shutdown -h +<minutes>` on the box, renewed by the run skill's monitor step. If the
agent disappears, the box expires on its own. This is strictly stronger than a trap.

**4. Tags are the ledger; `leases.json` is a fast path.** Every resource is tagged
`mlclaw-<lease_id>` at create — the token carries the lease id so a swept row maps back to a
ledger row, which id-matching cannot do (one lease may hold many units). Project and run travel
as separate metadata. `sweep` queries the cloud by tag prefix and must
succeed with no local state at all. This is why the lease file is not a violation of
MLClaw's "no separate index" doctrine (`lifecycle/references/run-mechanics.md` "Listing runs (no separate index)") — the cloud API is the
source of truth, the file is a hint list.

Rented boxes genuinely cannot be enumerated by scanning `run.json`: a box outlives its
run when a failure is kept for inspection, and precedes it when provisioning succeeds
but launch never happens. Money-bearing state needs its own record.

**5. Refuse a job longer than the credential TTL.** Federated / SSO / user-token
credentials expire in 1–12h. Training runs routinely exceed that, and an expired
credential means the box cannot be reached, monitored, or destroyed — it is still
billing and now invisible. Compare estimated duration against `credential_ttl_s` at
`up` time and stop unless `credential_refreshes` is true.

## Error classes must be normalized

The agent's next action differs per class, so `"it failed"` is not a usable result:

| Class | Meaning | Next action |
|---|---|---|
| `no_capacity` | provider has no stock now | retry another region/machine type, or wait |
| `quota` | account limit, includes **which** limit | request increase, or shrink the fleet |
| `permission` | credential lacks the right | not fixable by retry; check the target project/tenant |
| `credential_expired` | token TTL elapsed | re-auth, then resume |
| `transient` | API 5xx, throttle | backoff and retry |

`no_capacity` and `quota` are routinely confused by provider error strings and lead to
opposite actions — a wrong classification here wastes either a quota request or an hour
of retries. When an adapter cannot distinguish them, it returns `quota` (the one whose
remedy is not "retry").
