#!/usr/bin/env python3
"""lease — the one entry point agents call. Dispatches to provider adapters and owns
everything that must not depend on an agent remembering to do it.

  agent -> lease.py <verb> -> provider_<name>.py

Adapters (`skills/lease/references/contract.md`) translate the seven verbs for
one provider each. They deliberately do NOT own the ledger, so the contract's money
rules cannot live inside them:

  rule 1  the lease row is written BEFORE the create call        <- here
  rule 2  `down` is not done until the instance is verified gone <- here
  rule 3  every hold carries a TTL / dead-man expiry             <- here + adapter
  rule 4  tags are the ledger, leases.json is a fast path        <- here
  rule 5  refuse a job longer than the credential TTL            <- L3 (see SKILL.md)

Putting them here makes them structural. Left to the agent they are advice, and the run
that skips them is the run that leaves a box billing overnight.

What this does NOT do, on purpose: it never picks a machine type and never confirms a spend.
Those are dialogue and belong to L3, with the user in the loop. Bookkeeping and ordering
only, no judgement.

Adapters are discovered by filename — any `provider_<name>.py` beside this file.

Usage
  lease.py capacity [--gpu-count N] [--gpu-memory-gb G] [--arch-min sm_90] [--provider P]
  lease.py up --provider P --machine-type T [--ttl-s N] [--price-hr F]
              [--run ID] [--project NAME]
  lease.py status
  lease.py renew LEASE_ID [--ttl-s N]
  lease.py release LEASE_ID
  lease.py addr LEASE_ID
  lease.py reap [--tag-prefix <prefix>] [--attribute]
  lease.py history [--since-days N]
  lease.py cost [--project NAME] [--tag T] [--since-epoch S]

  lease.py claim --provider P --instance-id ID --purpose "..." [--holder WHO]
                 [--project N] [--run ID] [--session DIR] [--review-days N] [--supersede]
  lease.py disclaim CLAIM_ID [--why "..."]
  lease.py use LEASE_ID --run RUN_ID [--outcome ok|preempted|crashed|abandoned]
  lease.py whose [--instance-id ID | --run ID | --project N | --session S]

leases.json sits beside resources.json; both are located per `_common.resources_path`.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import atomic_write_json  # noqa: E402
from _common import (DEFAULT_TTL_S, TAG_PREFIX, add_shape_args, die, emit,  # noqa: E402
                     fan_out, load_resources, resources_from_workspace_root, shape_flags,
                     sweep_storage_known)

HERE = os.path.dirname(os.path.abspath(__file__))
OPEN_STATES = ("held", "requesting")

# An adapter normally needs a `resources.json -> compute.<name>` entry to count as
# registered. These read an existing block instead, so they need no entry of their own.
# Drops out of here once /resources writes `compute.ssh` when it discovers servers.
SELF_REGISTERING = {"ssh": "servers"}

# Same shape as the line above -- an adapter fact L2 owns. `ssh` is owned hardware:
# every row it returns is `price_hr: 0` by construction, so it cannot hold the thing
# `reap --billing-only` exists to find. Excluding it is not a tidy-up. Sweeping it
# means ssh'ing every registered server, and the automatic call site (CLAUDE.md ->
# "On Conversation Start") runs before the user has said anything -- four ssh
# timeouts is not a greeting, which is the same reason `census.py scan` is not run
# there either. ‼️ A held ssh claim is still a real problem; it is not a MONEY
# problem, and the unfiltered `reap` is what answers for it.
NON_BILLING = {"ssh"}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


# --- ledger -------------------------------------------------------------------

def ledger_path(res_path):
    return os.path.join(os.path.dirname(res_path), "leases.json")


def read_ledger(res_path):
    try:
        with open(ledger_path(res_path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    # `claims` lives in the SAME file as `leases` rather than beside it. Two files
    # would mean two atomic writers for one question -- "what does this ledger know
    # about that box" -- and CLAUDE.md's own record of that mistake is three writers
    # that drifted three ways. One reader, one writer, one fsync.
    data.setdefault("leases", [])
    data.setdefault("claims", [])
    return data


def write_ledger(res_path, data):
    """Atomic: a torn ledger is the one state that hides a billing instance."""
    atomic_write_json(ledger_path(res_path), data, fsync=True)


def open_rows(ledger):
    return [r for r in ledger["leases"] if r["state"] in OPEN_STATES]


# --- registry: what accounts for a machine ------------------------------------

# The four states a swept resource can be in, and the whole reason this section
# exists. `reap` used to have TWO -- orphan or not -- and an orphan list is read as a
# kill list, so everything that was not this ledger's own open lease landed on it.
#
# ‼️ THE TAG PROVES MLCLAW MADE IT, NOT THAT THIS LEDGER DID. `TAG_PREFIX` is a
# property of the TOOL, so two people running MLClaw against one tenant stamp boxes
# that are indistinguishable by prefix, and neither `leases.json` knows about the
# other's. So the default `reap` -- the one wired into conversation start -- reports
# a colleague's live training box as a forgotten one. That is not a hypothetical; it
# is what "no open lease row" means on a shared account.
#
# Ordered by how much they entitle you to do. Only the first is yours.
HOLDS = ("held", "claimed", "attributed", "unaccounted")

# Why nothing named a holder. Four, not one, and they are not interchangeable --
# the same split `census.py` keeps between a machine that did not answer and a
# directory that is genuinely empty.
UNACCOUNTED_WHY = (
    "not_attributed",             # nobody asked -- run with --attribute
    "attribution_unsupported",    # this provider structurally cannot say
    "no_create_event_in_window",  # LOOKED, did not find. Not unowned, not yours
    "attribution_unreached",      # the log did not answer
)

CLAIM_OPEN = "open"


def open_claims(ledger):
    return [c for c in ledger["claims"] if c["state"] == CLAIM_OPEN]


def claim_key(provider, instance_id):
    """Keyed on provider + instance id, never on a name. fleet.md "Group by id, never
    by name": a failed box deleted and its name reused an hour later makes a name-keyed
    claim report the LIVE machine as somebody's released one."""
    return (provider, instance_id)


def classify(row, open_tags, claims_by_key, attribution_supported):
    """One row -> who accounts for it, and on what evidence. THE SINGLE AUTHOR of that
    judgement; `status` and `reap` both read it, so they cannot disagree about whose a
    box is.

    No judgement beyond the join -- this layer is bookkeeping (see the module docstring)
    and never decides that a box may be destroyed. It says which of four buckets the
    evidence puts it in and what the evidence WAS, and L3 decides with the user.

    ‼️ `attributed` is about CREATION, not current use. "She made it on Tuesday" does
    not mean she still needs it, and a box whose owner has moved on looks identical to
    one mid-run. That gap is what `/roll-call` exists to close, and it is why the
    evidence kind travels with the holder forever rather than collapsing to a name.
    """
    key = claim_key(row.get("provider"), row.get("instance_id"))
    if row.get("tag") and row["tag"] in open_tags:
        return {"hold": "held", "holder": None, "holder_evidence": "open_lease_row"}
    claim = claims_by_key.get(key)
    if claim:
        return {"hold": "claimed", "holder": claim.get("holder"),
                # A registration is somebody's WORD. `/ask-human`'s vocabulary, for
                # `/ask-human`'s reason: nothing other than a person confirmed it, so it
                # can never be `verified` and must not read as one.
                "holder_evidence": "claim",
                "claim_id": claim["claim_id"], "purpose": claim.get("purpose"),
                "review_at": claim.get("review_at")}
    status = row.get("operator_status")
    if status == "audit_create" and row.get("operator"):
        return {"hold": "attributed", "holder": row["operator"],
                "holder_evidence": "audit_create"}
    why = ("no_create_event_in_window" if status == "no_create_event_in_window"
           else "attribution_unsupported" if attribution_supported is False
           else "attribution_unreached" if attribution_supported == "unreached"
           else "not_attributed")
    return {"hold": "unaccounted", "holder": None, "holder_evidence": None,
            "unaccounted_why": why}


def attribution_state(payloads):
    """Merge the per-provider attribution envelopes into one tri-state per provider.

    `True` (it answered), `False` (this provider structurally cannot), `"unreached"`
    (it can and did not), `None` (nobody asked). Four, because collapsing the middle
    two is how "the audit log timed out" comes to read as "this box has no owner".
    """
    out = {}
    for name, payload in payloads.items():
        att = (payload or {}).get("attribution")
        if att is None:
            out[name] = None
        elif att.get("supported") is False:
            out[name] = False
        elif att.get("complete"):
            out[name] = True
        else:
            out[name] = "unreached"
    return out


# --- adapters -----------------------------------------------------------------

def providers(res_path):
    installed = sorted(f[len("provider_"):-len(".py")] for f in os.listdir(HERE)
                       if f.startswith("provider_") and f.endswith(".py"))
    res = load_resources(res_path)
    registered = {k for k in (res.get("compute") or {}) if not k.startswith("_")}
    for name, block in SELF_REGISTERING.items():
        if any(not k.startswith("_") for k in (res.get(block) or {})):
            registered.add(name)
    return [p for p in installed if p in registered]


def compute_without_adapter(res_path):
    """Declared in `compute` but naming no installed adapter -> nothing will sweep it.

    `providers()` INTERSECTS, so a mistyped key (`nebius_prod` for `nebius`) does not
    error -- it disappears, and the caller keeps believing the provider is registered.
    That was survivable while `reap` was something a person typed. It is not now that it
    runs unattended at conversation start, where the disappearance reads as "nothing is
    billing" every session, forever, about an account that is.
    """
    installed = {fn[len("provider_"):-len(".py")] for fn in os.listdir(HERE)
                 if fn.startswith("provider_") and fn.endswith(".py")}
    res = load_resources(res_path)
    return sorted(k for k in (res.get("compute") or {})
                  if not k.startswith("_") and k not in installed)


def call(provider, res_path, verb, *extra):
    script = os.path.join(HERE, f"provider_{provider}.py")
    if not os.path.exists(script):
        die("permission", f"no adapter for provider '{provider}' ({script})")
    argv = [sys.executable, "-X", "utf8", script, "--resources", res_path, verb, *map(str, extra)]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    out = proc.stdout.strip()
    try:
        parsed = json.loads(out) if out else None
    except json.JSONDecodeError:
        return False, {"error": "transient",
                       "detail": f"{provider} {verb} returned non-JSON: {out[:200]}",
                       "stderr": proc.stderr[-400:]}
    if proc.returncode != 0:
        if not isinstance(parsed, dict) or "error" not in parsed:
            parsed = {"error": "transient", "detail": proc.stderr[-400:] or "adapter failed"}
        return False, parsed
    return True, parsed


def collect(names, res, verb, *extra):
    """Run one verb across providers and merge. `provider` is injected HERE — adapter
    identity is L2's knowledge, so a copy-pasted adapter that still self-names cannot
    misroute a later `release`.

    Returns `(rows, errors, scope, storage, payloads)`. The scope is the merged answer to
    "did we actually look everywhere", and **a provider that errored is an unreached
    corner** — not just a line in `errors`. Without that, `reap` printed `orphans: []`
    beside a failed adapter and the empty list was the part anyone read.

    `payloads` is each provider's raw envelope, kept because the **attribution** envelope
    must not be merged into `scope` (contract, "Ownership on a shared account"): scope
    answers "did I enumerate every resource", attribution answers "do I know who made
    them", and folding the second into the first makes every audit-log hiccup declare the
    resource count a lower bound when it is not.

    `sweep` and `history` must arrive in the envelope (`_common.sweep_result`); a bare
    list from those verbs is treated as **incomplete**, because an adapter written before
    the envelope existed cannot have checked what it never enumerated. Other verbs
    (`capacity`) legitimately return a list and are merged as complete.

    A `sweep` that reports no `storage` key is treated as an **unreached corner**, not as
    a provider with no storage. The two are one keystroke apart in an adapter and a
    world apart in a bill: "looked, nothing bills" is `storage: []`, and an adapter that
    never looked leaves residual billing unmeasured while `reap` still prints a total.
    Making it `complete: false` is what turns that total into a stated lower bound.
    """
    scoped = verb in ("sweep", "history")
    rows, store, errors, checked, unreached, payloads = [], [], {}, [], [], {}
    results = fan_out(names, lambda n: call(n, res, verb, *extra))
    for name, (ok, data) in zip(names, results):
        if not ok:
            errors[name] = data
            unreached.append({"provider": name, "scope": "*",
                              "why": data.get("error", "transient")})
            continue
        payloads[name] = data if isinstance(data, dict) else None
        if isinstance(data, dict) and "units" in data:
            units, sc = data["units"], data.get("scope") or {}
        elif isinstance(data, (list, type(None))):
            units, sc = (data or []), ({} if not scoped else
                                       {"complete": False, "unreached": [
                                           {"scope": "*", "why": "adapter returned a bare "
                                            "list; scope unknown"}]})
        else:
            # Neither the envelope nor a list. `capacity` is contractually a bare list of
            # rows, and an adapter that wraps it in an object of its own would otherwise
            # be spread key-by-key here and crash on a string — a TypeError from L2 for a
            # mistake in L1, blamed on the wrong file. Refuse it as an unread corner.
            errors[name] = {"error": "transient",
                            "detail": f"{verb} returned {type(data).__name__}, not a list "
                                      f"of rows (keys: {sorted(data)[:6]})"}
            unreached.append({"provider": name, "scope": "*",
                              "why": f"{verb} returned a shape this layer cannot read"})
            continue
        rows += [{"provider": name, **row} for row in units]
        checked += [{"provider": name, "scope": s} for s in (sc.get("checked") or [])]
        unreached += [{"provider": name, **u} for u in (sc.get("unreached") or [])]
        if verb == "sweep":
            if sweep_storage_known(data):
                store += [{"provider": name, **row} for row in (data["storage"] or [])]
            else:
                unreached.append({"provider": name, "scope": "storage",
                                  "why": "adapter does not report storage; residual "
                                         "billing after teardown is unmeasured"})
    return (rows, errors, {"complete": not unreached,
                           "checked": checked, "unreached": unreached}, store, payloads)


def storage_orphans(stored, open_tags, live_instance_ids):
    """Volumes that bill with nothing using them.

    Two distinct shapes, and only the first is obvious. A volume with no attachment is
    plainly abandoned. A volume still attached to an instance whose lease is closed is
    the one that hides: the instance row was released, the ledger says so, and the disk
    it declared went on billing because nothing ever asked the storage list about it.

    Deliberately does NOT rank or delete. Ranking a delete is `/data-retire`'s shape and
    it needs evidence this layer does not have — what is ON the volume. See `/lease`
    "Releasing a box does not read it first".
    """
    out = []
    for row in stored:
        attached = row.get("attached_to")
        if attached and attached in live_instance_ids:
            continue
        reason = ("unattached — nothing is using it" if not attached else
                  f"attached to {attached}, which holds no open lease")
        if row.get("tag") and row["tag"] in open_tags:
            continue
        out.append({**row, "orphan_reason": reason})
    return out


def _priced(rows, amount):
    """Sum over the rows that carry a price; count the ones that do not.

    A row with `price_hr: null` is a price nobody wrote down (contract, "Shape
    resolution"), never a free one — folding it in as 0 is how a total reads as
    complete while missing its largest term.

    ‼️ ONE IMPLEMENTATION, TWO QUANTITIES. `usd_hr` asks what this set bills per
    hour; `usd_over` asks what it has cost. The partition is identical and it is a
    CORRECTNESS rule, so it is written once — the same reason `list_runs.py`
    exists instead of a jq snippet everyone retypes. Written twice, it gets fixed
    once.
    """
    known = [amount(r) for r in rows if r.get("price_hr") is not None]
    return round(sum(known), 2), len(rows) - len(known)


def usd_hr(rows):
    """The rate: what this set bills per hour, right now."""
    return _priced(rows, lambda r: r["price_hr"])


def usd_over(rows, hours):
    """The same rule integrated over time: what this set has cost so far.
    `hours` maps a row to the hours it was held."""
    return _priced(rows, lambda r: r["price_hr"] * hours(r))


# --- verbs --------------------------------------------------------------------

def v_capacity(args):
    names = [args.provider] if args.provider else providers(args.res)
    if not names:
        die("permission", "no providers registered — run /resources first",
            hint="resources.json -> compute, or -> servers for owned hardware")
    rows, errors, scope, _, _p = collect(names, args.res, "capacity", *shape_flags(args))
    # viable first, then cheapest — the order L3 should present to the user
    rows.sort(key=lambda r: (-(r.get("avail") or 0),
                             r.get("price_hr") if r.get("price_hr") is not None
                             else float("inf")))
    emit({"options": rows, "scope": scope, "errors": errors or None}, indent=2)


def v_up(args):
    res, ttl = args.res, args.ttl_s or DEFAULT_TTL_S
    lease_id = f"lease_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
    # rule 4: one owner token, carrying the lease id so sweep rows map back to a row.
    tag = f"{TAG_PREFIX}{lease_id}"
    now = int(time.time())

    # rule 1: the row lands before the create call. A create whose response is lost
    # still leaves a trace for `reap` to find.
    ledger = read_ledger(res)
    ledger["leases"].append({
        "lease_id": lease_id, "provider": args.provider, "tag": tag,
        "machine_type": args.machine_type,
        "instance_ids": [], "state": "requesting",
        "project": args.project, "run": args.run,
        "price_hr": args.price_hr, "ttl_s": ttl,
        # Epochs throughout; ISO is derived on read so the two cannot disagree. Note this
        # clock is local, while the adapter enforces expiry with the target's own.
        "requested_at": now, "expires_at": now + ttl,
        "released_at": None, "error": None,
    })
    write_ledger(res, ledger)

    ok, data = call(args.provider, res, "up", "--machine-type", args.machine_type, *shape_flags(args),
                    "--ttl-s", ttl,          # rule 3
                    "--tag", tag,            # rule 4 — unconditional, every provider
                    *(["--run", args.run] if args.run else []),
                    *(["--project", args.project] if args.project else []))

    ledger = read_ledger(res)
    row = next(r for r in ledger["leases"] if r["lease_id"] == lease_id)
    if not ok:
        row["state"], row["error"] = "failed", data
        write_ledger(res, ledger)
        die(data.get("error", "transient"), data.get("detail", "up failed"),
            lease_id=lease_id, binding_limit=data.get("binding_limit"))

    row["instance_ids"] = data if isinstance(data, list) else [data]
    row["state"] = "held"
    write_ledger(res, ledger)

    ok_addr, reach = call(args.provider, res, "addr", row["instance_ids"][0])
    emit({"lease_id": lease_id, "provider": args.provider, "tag": tag,
          "instance_ids": row["instance_ids"], "reach": reach if ok_addr else None,
          "expires_at": row["expires_at"], "ttl_s": ttl,
          "warning": None if ok_addr else
          "held but unreachable — release it or it holds the resource until TTL"}, indent=2)


def v_addr(args):
    """Resolve a held lease to a reachable address, live, on every call.

    Exposed as its own verb because the alternative is a caller writing the address it
    got back from `up` into its own record — which the contract forbids for a reason
    that costs more than a stale field: a stop/start hands the box a different address
    and hands the old one to somebody else, and ssh with relaxed host-key checking
    connects to that stranger without complaining.
    """
    ledger = read_ledger(args.res)
    row = next((r for r in ledger["leases"] if r["lease_id"] == args.lease_id), None)
    if row is None:
        die("permission", f"no lease {args.lease_id} in the ledger")
    if row["state"] not in OPEN_STATES:
        die("permission", f"lease {args.lease_id} is {row['state']}; nothing to reach")
    if not row["instance_ids"]:
        die("transient", f"lease {args.lease_id} holds no instance yet")
    ok, reach = call(row["provider"], args.res, "addr", row["instance_ids"][0])
    if not ok:
        die(reach.get("error", "transient"), f"addr failed: {reach.get('detail')}")
    emit({"lease_id": args.lease_id, "reach": reach,
          "instance_id": row["instance_ids"][0]}, indent=2)


def v_status(args):
    res = args.res
    ledger = read_ledger(res)
    held = open_rows(ledger)

    # One sweep answers most of what a per-lease `state` would, and it is issued anyway
    # for the untracked half. Only leases the sweep does not mention fall through to an
    # individual `state` call — which keeps this correct for a tag-filtered sweep.
    swept, errors, scope, stored, payloads = collect(
        providers(res), res, "sweep", "--tag-prefix", args.tag_prefix,
        *(["--attribute"] if args.attribute else []))
    by_tag = {}
    for row in swept:
        by_tag.setdefault(row.get("tag"), []).append(row)

    now, live, changed = int(time.time()), [], False
    for row in held:
        age = now - row["requested_at"]
        entry = {**row, "requested_iso": iso(row["requested_at"]), "age_s": age,
                 "accrued_usd": round((row.get("price_hr") or 0) * age / 3600, 2),
                 "ttl_remaining_s": max(0, row["expires_at"] - now)}
        found = by_tag.get(row.get("tag"))
        if found:
            entry["actual_state"] = ("expired" if all(f.get("expired") for f in found)
                                     else "running")
        elif not row["instance_ids"]:
            entry["actual_state"] = "never created (row written, create failed or lost)"
        else:
            ok, state = call(row["provider"], res, "state", row["instance_ids"][0])
            entry["actual_state"] = state if ok else f"unknown ({state.get('error')})"
            if ok and state == "gone":
                row["state"], row["released_at"] = "released", now
                changed = True
                entry["note"] = "gone on the provider side — ledger reconciled"
        live.append(entry)
    if changed:
        write_ledger(res, ledger)

    # The other direction: resources the ledger does not know about. Matched on the tag
    # L2 issued, never on an instance id whose format the adapter owns — a multi-unit
    # lease reports one id but many swept rows.
    open_tags = {r.get("tag") for r in open_rows(ledger)}
    claims_by_key = {claim_key(c["provider"], c["instance_id"]): c
                     for c in open_claims(ledger)}
    att = attribution_state(payloads)
    # Not one list any more. `untracked` said only "this ledger did not open it", and a
    # colleague's box and a box forgotten by a dead session of your own produce the same
    # row -- see HOLDS. Each is stamped with which of the four accounts for it and on
    # what evidence, by the same function `reap` uses.
    untracked = [{**r, **classify(r, open_tags, claims_by_key, att.get(r.get("provider")))}
                 for r in swept if r.get("tag") not in open_tags]

    # Storage the ledger cannot know about. `held` prices what was leased; a volume is
    # billed under no lease at all once its instance is gone, so it is a separate line
    # rather than a term folded into the lease total -- see `v_reap`'s two meters.
    live_ids = {r.get("instance_id") for r in swept}
    residual = storage_orphans(stored, open_tags, live_ids)
    compute_usd, compute_unpriced = usd_hr(held)
    storage_usd, storage_unpriced = usd_hr(residual)

    emit({"held": live, "untracked": untracked,
          "untracked_by_hold": {h: sum(1 for r in untracked if r["hold"] == h)
                                for h in HOLDS if any(r["hold"] == h for r in untracked)}
                               or None,
          "attribution": {name: att[name] for name in sorted(att)} if args.attribute
                         else {"asked": False},
          "residual_storage": residual,
          # `held` comes from the ledger and is whole; `untracked` comes from the sweep
          # and is only as complete as the sweep was. Saying which is which is the
          # difference between "nothing else is running" and "nothing else answered".
          "scope": scope,
          "untracked_is_lower_bound": not scope["complete"],
          "errors": errors or None,
          "total_usd_per_hr": round(compute_usd + storage_usd, 2),
          "compute_usd_per_hr": compute_usd,
          "storage_usd_per_hr": storage_usd,
          "unpriced_rows": (compute_unpriced + storage_unpriced) or None,
          "total_is_lower_bound": bool(compute_unpriced or storage_unpriced
                                       or not scope["complete"])},
         indent=2)


def v_renew(args):
    """Called from L3's monitor step — the only thing that knows the job is still alive.
    Without it, rule 3's expiry kills a long run."""
    res = args.res
    ledger = read_ledger(res)
    row = next((r for r in ledger["leases"] if r["lease_id"] == args.lease_id), None)
    if row is None:
        die("permission", f"no lease {args.lease_id} in the ledger")
    if row["state"] != "held":
        die("permission", f"lease {args.lease_id} is {row['state']}, not held")
    ttl = args.ttl_s or row.get("ttl_s") or DEFAULT_TTL_S
    for iid in row["instance_ids"]:
        ok, data = call(row["provider"], res, "renew", iid, "--ttl-s", ttl)
        if not ok:
            die(data.get("error", "transient"),
                f"renew failed for {iid}: {data.get('detail')} — the hold expires at "
                f"{iso(row['expires_at'])}; the job dies with it unless this is fixed",
                lease_id=args.lease_id)
    row["expires_at"], row["ttl_s"] = int(time.time()) + ttl, ttl
    write_ledger(res, ledger)
    emit({"ok": True, "lease_id": args.lease_id,
          "expires_at": row["expires_at"], "ttl_s": ttl}, indent=2)


def v_release(args):
    res = args.res
    ledger = read_ledger(res)
    row = next((r for r in ledger["leases"] if r["lease_id"] == args.lease_id), None)
    if row is None:
        die("permission", f"no lease {args.lease_id} in the ledger",
            hint="use `reap` for a resource with no lease row")
    if row["state"] == "released":
        emit({"ok": True, "lease_id": args.lease_id, "note": "already released"})
        return
    if not row["instance_ids"]:
        row["state"], row["released_at"] = "released", int(time.time())
        write_ledger(res, ledger)
        emit({"ok": True, "lease_id": args.lease_id, "note": "row had no instance; closed"})
        return

    for iid in row["instance_ids"]:
        ok, data = call(row["provider"], res, "down", iid)
        if not ok:
            die(data.get("error", "transient"),
                f"down failed for {iid} — RESOURCE MAY STILL BE BILLING: "
                f"{data.get('detail')}", lease_id=args.lease_id)

    # rule 2: not done until verified gone. A row closed while the instance survives is
    # exactly how a box becomes invisible.
    unverified = []
    for iid in row["instance_ids"]:
        ok, state = call(row["provider"], res, "state", iid)
        if not ok or state != "gone":
            unverified.append({"instance_id": iid, "state": state if ok else "unknown"})
    if unverified:
        row["error"] = {"unverified": unverified}
        write_ledger(res, ledger)
        die("transient", "down returned ok but state is not `gone` — leaving the lease "
            "row OPEN on purpose so this stays visible", unverified=unverified,
            lease_id=args.lease_id)

    row["state"], row["released_at"], row["error"] = "released", int(time.time()), None
    write_ledger(res, ledger)
    emit({"ok": True, "lease_id": args.lease_id,
          "released": row["instance_ids"], "verified_gone": True}, indent=2)


def v_cost(args):
    """What a round actually cost -- and how much of it could not be priced.

    `status` answers "what am I burning right now"; this answers "what did this
    cost", which is the same question integrated over time and the one that was
    actually asked, repeatedly, across a six-day search. Derived from the ledger,
    never cached: `requested_at`, `released_at` (or now, for a row still open) and
    `price_hr` are already there.

    ‼️ THE WINDOW STARTS AT REQUEST, AND BILLING DOES NOT. The ledger's only start
    stamp is when the lease was asked for; the provider starts charging when the
    instance comes up. On the round this was built for, one machine type took ~80
    minutes to provision -- so this over-counts, per lease, by however long the
    box took to appear. Stated rather than absorbed: a cost report that quietly
    includes provisioning time is wrong in a direction nobody can see.

    ‼️ THE UNPRICED COUNT IS THE POINT, not a footnote. `--price-hr` is optional
    on `up`, so a ledger routinely holds rows with no price -- and a total summed
    over the priced rows alone reads exactly like the whole bill. Same shape as a
    census with `complete: false`: the number is a LOWER BOUND and has to say so,
    or it becomes an inventory. A round that rented ten boxes and priced six does
    not have a cost; it has a floor and four unknowns.
    """
    res = args.res
    ledger = read_ledger(res)
    now = int(time.time())
    rows, priced, unpriced = [], [], []
    since = int(args.since_epoch) if args.since_epoch else None
    for r in ledger["leases"]:
        start = r.get("requested_at")
        if start is None:
            continue
        if since is not None and start < since:
            continue
        if args.project and r.get("project") != args.project:
            continue
        if args.tag and r.get("tag") != args.tag:
            continue
        end = r.get("released_at") or now
        hours = max(0.0, (end - start) / 3600.0)
        item = {"lease_id": r["lease_id"], "machine_type": r.get("machine_type"),
                "tag": r.get("tag"), "run": r.get("run"), "state": r.get("state"),
                "hours": round(hours, 2), "price_hr": r.get("price_hr"),
                "still_open": r.get("released_at") is None}
        if r.get("price_hr") is None:
            unpriced.append(item)
        else:
            item["usd"] = round(r["price_hr"] * hours, 2)
            priced.append(item)
        rows.append(item)

    # Through the shared partition, not a second hand-rolled one: the total and
    # the unpriced count must never be able to disagree with `usd_hr`'s.
    hours_of = {i["lease_id"]: i["hours"] for i in rows}
    total, unpriced_n = usd_over(
        [{"lease_id": i["lease_id"], "price_hr": i["price_hr"]} for i in rows],
        lambda r: hours_of[r["lease_id"]])
    assert unpriced_n == len(unpriced)
    open_rows = [i for i in rows if i["still_open"]]
    payload = {
        "leases": len(rows),
        "priced": len(priced), "unpriced": len(unpriced),
        "gpu_hours_priced": round(sum(i["hours"] for i in priced), 2),
        "gpu_hours_unpriced": round(sum(i["hours"] for i in unpriced), 2),
        "usd": total,
        "complete": not unpriced,
        "window": "requested_at -> released_at (or now). INCLUDES provisioning time, "
                  "which the provider does not bill — see the docstring",
        "still_open": [i["lease_id"] for i in open_rows],
        "rows": sorted(rows, key=lambda i: -(i.get("usd") or 0)),
    }
    if unpriced:
        payload["‼️"] = (
            f"${total} covers {len(priced)} of {len(rows)} leases. "
            f"{len(unpriced)} carry no `price_hr` and are NOT in the total — this is a "
            f"LOWER BOUND, not the bill. They account for "
            f"{payload['gpu_hours_unpriced']} unpriced machine-hours. Quote it as a floor, "
            f"or re-run `up` with `--price-hr` next time.")
    if open_rows:
        payload["note"] = (f"{len(open_rows)} lease(s) still open and still accruing; their "
                           f"hours are counted to now, not to a release.")
    emit(payload, indent=2)


def v_reap(args):
    """Cloud-side truth. Must work with leases.json missing or stale — that is the case
    that matters, since the session that created the box is the one that died.

    Two meters, reported apart. Compute is the loud one and it is the one that stops on
    its own: a dead-man switch expires, an instance halts, the large number goes to zero.
    Storage is the quiet one — it starts when the box is created, it does not stop when
    the box stops, and it survives the instance being deleted. A reap that counted only
    instances answered "nothing is running", which is true, next to a volume that has
    billed every hour since.
    """
    res = args.res
    ledger = read_ledger(res)
    open_tags = {r.get("tag") for r in open_rows(ledger)}
    names = providers(res)
    skipped = sorted(n for n in names if n in NON_BILLING) if args.billing_only else []
    names = [n for n in names if n not in skipped]
    rows, errors, scope, stored, payloads = collect(
        names, res, "sweep", "--tag-prefix", args.tag_prefix,
        *(["--attribute"] if args.attribute else []))

    claims_by_key = {claim_key(c["provider"], c["instance_id"]): c
                     for c in open_claims(ledger)}
    att = attribution_state(payloads)

    # FOUR buckets, and only `orphans` is a kill list. The old two-way split put
    # everything that was not this ledger's own open lease into `orphans`, which on a
    # shared tenant means a colleague's live box -- see HOLDS above for why the tag
    # cannot carry that weight.
    orphans, claimed, others, unaccounted = [], [], [], []
    for row in rows:
        who = classify(row, open_tags, claims_by_key, att.get(row.get("provider")))
        entry = {**row, **who}
        if who["hold"] == "held":
            if row.get("expired"):
                # Ours, expired, still there. The one case the dead-man switch was
                # supposed to close and did not.
                orphans.append({**entry, "orphan_reason": "expired"})
            continue
        if who["hold"] == "claimed":
            claimed.append({**entry, "review_due": bool(
                who.get("review_at") and who["review_at"] <= int(time.time()))})
        elif who["hold"] == "attributed":
            others.append(entry)
        else:
            unaccounted.append(entry)

    # ‼️ THE KILL LIST AND THE MONEY METER ARE DIFFERENT QUESTIONS, and splitting the
    # first must never shrink the second. Everything swept here bills the account —
    # a colleague's box costs exactly what a forgotten one costs — so the meter runs
    # over all four buckets while only `orphans` is proposed for teardown. Summing the
    # meter over `orphans` alone was a `$0.00/hr` printed beside four running boxes,
    # which is the one failure fleet.md says this verb exists to not have:
    # "the whole point of `reap` is to be trusted when it says zero".
    billing = orphans + claimed + others + unaccounted
    live_ids = {r.get("instance_id") for r in rows} - {b.get("instance_id") for b in billing}
    orphan_storage = storage_orphans(stored, open_tags, live_ids)
    compute_usd, compute_unpriced = usd_hr(billing)
    storage_usd, storage_unpriced = usd_hr(orphan_storage)

    # `orphans: []` is the whole product of this verb, and it is exactly the shape a
    # sweep that reached nothing also produces. `complete` is what separates "there are
    # no forgotten boxes" from "nobody looked" -- state it before quoting the count.
    emit({"orphans": orphans,
          # ‼️ THE THREE LISTS BELOW ARE NOT A KILL LIST, and splitting them out is the
          # whole point of this verb having been rewritten. Reported beside `orphans`
          # rather than inside it, each with the evidence that put it there.
          "claimed": claimed or None,
          "attributed_to_others": others or None,
          "unaccounted": unaccounted or None,
          # What the money figures below actually cover: every swept row that no open
          # lease of ours accounts for, whosever it is. Stated because the four lists
          # above invite the reading that the total is only about `orphans`.
          "billing_rows": len(billing),
          # Whether anything even ASKED who made these. Without it a caller reading
          # `attributed_to_others: null` cannot tell "everything here is ours" from
          # "nobody ran the join", and the second one is the default.
          "attribution": {name: att[name] for name in sorted(att)} if args.attribute
                         else {"asked": False},
          "orphan_storage": orphan_storage,
          # Named, never silent. `complete` stays the answer to "did every provider we
          # swept answer"; this is the different fact that we chose not to sweep one, so
          # `orphans: []` here means "nothing RENTED is orphaned", not "nothing is held".
          "skipped_non_billing": skipped or None,
          # A provider you think you registered and nothing sweeps. Reported beside the
          # count rather than folded into it: this is not an unreached corner, it is a
          # corner nobody named correctly, and only the resources file can fix it.
          "compute_without_adapter": compute_without_adapter(res) or None,
          "complete": scope["complete"],
          "orphans_is_lower_bound": not scope["complete"],
          "scope": scope,
          "leases_without_instance": [r["lease_id"] for r in open_rows(ledger)
                                      if not r["instance_ids"]],
          "errors": errors or None,
          "total_usd_per_hr": round(compute_usd + storage_usd, 2),
          "compute_usd_per_hr": compute_usd,
          "storage_usd_per_hr": storage_usd,
          # An unpriced row is a term missing from the total, not a free one. Saying how
          # many keeps the figure from reading as an exhaustive bill.
          "unpriced_rows": (compute_unpriced + storage_unpriced) or None,
          "total_is_lower_bound": bool(compute_unpriced or storage_unpriced
                                       or not scope["complete"])},
         indent=2)


def v_claim(args):
    """Register what a machine is FOR, when no lease of ours opened it.

    The gap this fills: `up` stamps `mlclaw_run` at create and that label is never
    written again. A pool box drains twelve trials under the first one's name; a box
    opened from a console or by another tool carries no label at all; and a colleague's
    box on a shared tenant carries the same `TAG_PREFIX` yours does. In all three
    the sweep produces a row nothing accounts for, and an unaccounted row on an orphan
    list is a box somebody kills.

    ‼️ A CLAIM IS SOMEBODY'S WORD AND STAYS ONE. It is recorded with `/ask-human`'s
    vocabulary and can never become `verified` here, because nothing other than a person
    said it -- see CLAUDE.md "Never let somebody's word become a checked fact". The only
    independent evidence about a box is the provider's own lifecycle log, it is a
    separate field (`operator`), and it answers **who created it**, which is a different
    question from what it is for now.

    Claiming moves nothing and starts nothing. It is a sentence in the ledger.
    """
    res = args.res
    ledger = read_ledger(res)
    key = claim_key(args.provider, args.instance_id)

    held = next((r for r in open_rows(ledger)
                 if r["provider"] == args.provider
                 and args.instance_id in (r.get("instance_ids") or [])), None)
    if held:
        # `held` already answers this question with better evidence than a claim, and
        # writing one on top would give "what holds this box" two authors that nothing
        # reconciles. Amend the lease instead -- that is what `use` is for.
        die("permission",
            f"{args.instance_id} is held under lease {held['lease_id']}; that is stronger "
            f"evidence than a claim. Record the work with `use {held['lease_id']} --run ...`",
            lease_id=held["lease_id"])

    existing = next((c for c in open_claims(ledger)
                     if claim_key(c["provider"], c["instance_id"]) == key), None)
    if existing and not args.supersede:
        # Refusing rather than overwriting: silently replacing somebody's "this is mine"
        # is the failure this whole verb exists to stop, one layer in.
        die("permission",
            f"{args.instance_id} is already claimed by {existing.get('holder') or 'someone'} "
            f"({existing['claim_id']}): {existing.get('purpose')}. Pass --supersede to "
            f"replace it, and say why in --note",
            claim_id=existing["claim_id"], holder=existing.get("holder"))

    now = int(time.time())
    if existing:
        existing["state"], existing["closed_at"] = "superseded", now
        existing["closed_why"] = args.note or "superseded"

    claim = {
        "claim_id": f"claim_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}",
        "provider": args.provider, "instance_id": args.instance_id,
        "holder": args.holder, "purpose": args.purpose,
        "project": args.project, "run": args.run, "session": args.session,
        # Fixed and not a parameter. See the docstring: there is no --status flag on
        # purpose, so no call site can launder a claim into a verified fact.
        "evidence": "claim",
        "state": CLAIM_OPEN, "claimed_at": now,
        # When somebody should be asked again whether this is still true. A claim with
        # no review date is a claim that ages into furniture.
        "review_at": now + args.review_days * 86400 if args.review_days else None,
        "supersedes": existing["claim_id"] if existing else None,
        "note": args.note, "closed_at": None, "closed_why": None,
    }
    ledger["claims"].append(claim)
    write_ledger(res, ledger)
    emit({"ok": True, **claim,
          "claimed_iso": iso(now),
          "review_iso": iso(claim["review_at"]) if claim["review_at"] else None,
          "note_to_caller": "recorded as a CLAIM — nobody verified it, and nothing was "
                            "started, moved or reserved on the machine"}, indent=2)


def v_disclaim(args):
    """Close a claim. ‼️ THIS DOES NOT TOUCH THE MACHINE.

    Named `disclaim` and not `release` for that reason: the box goes on running and goes
    on billing. All that changes is that the ledger stops saying somebody needs it — so
    the next sweep files it under `unaccounted`, which is the honest state and still not
    a licence to destroy it.
    """
    ledger = read_ledger(args.res)
    claim = next((c for c in ledger["claims"] if c["claim_id"] == args.claim_id), None)
    if claim is None:
        die("permission", f"no claim {args.claim_id} in the ledger")
    if claim["state"] != CLAIM_OPEN:
        emit({"ok": True, "claim_id": args.claim_id, "note": f"already {claim['state']}"})
        return
    claim["state"], claim["closed_at"] = "closed", int(time.time())
    claim["closed_why"] = args.why
    write_ledger(args.res, ledger)
    emit({"ok": True, "claim_id": args.claim_id, "instance_id": claim["instance_id"],
          "note_to_caller": "the claim is closed; THE MACHINE IS UNTOUCHED and still "
                            "bills. Releasing it is `release`, and `/evacuate` comes "
                            "first"}, indent=2)


def v_use(args):
    """Record that a run used this lease. Appended, never replaced.

    `up --run` stamps ONE run at create and a pooled box drains many: twelve trials
    through one lease record the first trial's name forever, and after the search's
    session dies `pool.json` -- the only thing that knew the other eleven -- is a file
    in a directory nobody opens. So "which machines did that search use" was answerable
    only from a live session, which is exactly when nobody asks.

    Called by `pool.py release` per trial. Cheap and local; no provider call.
    """
    ledger = read_ledger(args.res)
    row = next((r for r in ledger["leases"] if r["lease_id"] == args.lease_id), None)
    if row is None:
        die("permission", f"no lease {args.lease_id} in the ledger")
    now = int(time.time())
    entry = {"run": args.run, "outcome": args.outcome,
             "from": args.from_epoch or None, "to": args.to_epoch or now,
             "session": args.session, "recorded_at": now}
    row.setdefault("used_by", []).append(entry)
    write_ledger(args.res, ledger)
    emit({"ok": True, "lease_id": args.lease_id, "used_by": len(row["used_by"]),
          "recorded": entry}, indent=2)


def v_whose(args):
    """The two-way join, read from the ledger alone. **No network, ever.**

    Both directions of the question the registry exists for:

      --instance-id  -> what accounts for that box: lease rows, claims, and every run
                        that ran on it
      --run/--project/--session -> which machines that work used

    Local-only is the load-bearing part, twice over. It is what makes this safe to call
    at conversation start, and it is what keeps the second direction answerable **after
    every box is gone** -- which is when somebody reconstructing a round actually asks.
    A verb that had to reach the provider would answer "which machines did that search
    use" with silence for every search that finished.
    """
    ledger = read_ledger(args.res)
    leases, claims = ledger["leases"], ledger["claims"]

    def uses(row):
        return row.get("used_by") or []

    if args.instance_id:
        leases = [r for r in leases if args.instance_id in (r.get("instance_ids") or [])]
        claims = [c for c in claims if c["instance_id"] == args.instance_id]
    else:
        def hit(row):
            if args.run:
                return row.get("run") == args.run or any(
                    u.get("run") == args.run for u in uses(row))
            if args.project:
                return row.get("project") == args.project
            if args.session:
                return row.get("session") == args.session or any(
                    u.get("session") == args.session for u in uses(row))
            return True
        leases = [r for r in leases if hit(r)]
        claims = [c for c in claims
                  if (args.run and c.get("run") == args.run)
                  or (args.project and c.get("project") == args.project)
                  or (args.session and c.get("session") == args.session)
                  or not (args.run or args.project or args.session)]

    emit({"filter": {k: getattr(args, k) for k in
                     ("instance_id", "run", "project", "session") if getattr(args, k)}
                    or {"all": True},
          "leases": [{"lease_id": r["lease_id"], "provider": r["provider"],
                      "state": r["state"], "instance_ids": r["instance_ids"],
                      "machine_type": r.get("machine_type"),
                      "project": r.get("project"), "run_at_create": r.get("run"),
                      "requested_iso": iso(r["requested_at"]),
                      "released_iso": iso(r["released_at"]) if r.get("released_at") else None,
                      "used_by": uses(r)} for r in leases],
          "claims": [{k: c[k] for k in ("claim_id", "provider", "instance_id", "holder",
                                        "purpose", "project", "run", "session",
                                        "evidence", "state", "review_at")}
                     for c in claims],
          # ‼️ The ledger is the only thing read here, so silence means "this ledger has
          # no record", never "that machine was not used". A box opened outside MLClaw
          # and never claimed produces exactly this empty answer.
          "scope": {"source": "leases.json only", "network": False,
                    "complete_for": "what this ledger was told",
                    "blind_to": "anything opened elsewhere and never claimed"}},
         indent=2)


def v_history(args):
    """The past tense. `sweep` says what exists; only this says what *happened*, and the
    gap between them is where "I released it" turns out to be false.

    Merges the ledger's own view with each provider's lifecycle log, because they fail
    in opposite directions: the ledger knows about a create whose response was lost and
    the cloud does not; the cloud knows about a box launched from a console and the
    ledger does not. A provider with no log says so rather than being read as silence.
    """
    res = args.res
    extra = ["--tag-prefix", args.tag_prefix, "--window-s", str(args.window_s)]
    if args.instance_id:
        extra += ["--instance-id", args.instance_id]
    names = providers(res)

    # Its own fan-out rather than `collect`, because this verb needs a second field off
    # each payload (`supported`) and calling `collect` then re-calling for that field
    # would issue every audit query twice -- the slowest call in the layer.
    events, errors, unsupported, checked, unreached = [], {}, [], [], []
    for name, (ok, data) in zip(names, fan_out(names, lambda n: call(n, res, "history", *extra))):
        if not ok:
            errors[name] = data
            unreached.append({"provider": name, "scope": "*",
                              "why": data.get("error", "transient")})
            continue
        events += [{"provider": name, **e} for e in (data.get("events") or [])]
        if data.get("supported") is False:
            unsupported.append({"provider": name, "why": data.get("why")})
        sc = data.get("scope") or {}
        checked += [{"provider": name, "scope": s} for s in (sc.get("checked") or [])]
        unreached += [{"provider": name, **u} for u in (sc.get("unreached") or [])]
    scope = {"complete": not unreached, "checked": checked, "unreached": unreached}

    ledger = read_ledger(res)
    rows = [r for r in ledger["leases"]
            if not args.instance_id or args.instance_id in (r.get("instance_ids") or [])]
    emit({"events": sorted(events, key=lambda e: e.get("at") or ""),
          "ledger": [{"lease_id": r["lease_id"], "provider": r["provider"], "tag": r["tag"],
                      "state": r["state"], "instance_ids": r["instance_ids"],
                      "requested_iso": iso(r["requested_at"]),
                      "released_iso": iso(r["released_at"]) if r.get("released_at") else None}
                     for r in rows],
          "unsupported": unsupported or None,
          "scope": scope, "errors": errors or None}, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--resources", help="path to resources.json (else $MLCLAW_RESOURCES)")
    sub = ap.add_subparsers(dest="verb", required=True)

    c = sub.add_parser("capacity"); c.set_defaults(fn=v_capacity); add_shape_args(c)
    c.add_argument("--provider", help="limit to one provider")

    u = sub.add_parser("up"); u.set_defaults(fn=v_up); add_shape_args(u)
    u.add_argument("--provider", required=True)
    u.add_argument("--machine-type", required=True, help="a `machine_type` value from a capacity row")
    u.add_argument("--ttl-s", type=int, help=f"dead-man expiry, default {DEFAULT_TTL_S}")
    u.add_argument("--price-hr", type=float, help="confirmed price, recorded for accrual")
    u.add_argument("--run")
    u.add_argument("--project")

    a = sub.add_parser("addr"); a.set_defaults(fn=v_addr)
    a.add_argument("lease_id")

    s = sub.add_parser("status"); s.set_defaults(fn=v_status)
    s.add_argument("--tag-prefix", default=TAG_PREFIX)
    s.add_argument("--attribute", action="store_true",
                   help="ask each provider's lifecycle log who created the boxes it "
                        "swept. Off by default: it is the slowest call in the layer. On "
                        "a SHARED account leave it on — without it every untracked row "
                        "is `unaccounted`, which is not the same as unowned")

    n = sub.add_parser("renew"); n.set_defaults(fn=v_renew)
    n.add_argument("lease_id")
    n.add_argument("--ttl-s", type=int, help="new expiry from now; default reuses the lease's")

    r = sub.add_parser("release"); r.set_defaults(fn=v_release)
    c = sub.add_parser("cost", help="what a round cost, and how much could not be priced")
    c.add_argument("--project"); c.add_argument("--tag")
    c.add_argument("--since-epoch", dest="since_epoch",
                   help="only leases started at or after this epoch second")
    c.set_defaults(fn=v_cost)
    r.add_argument("lease_id")

    p = sub.add_parser("reap"); p.set_defaults(fn=v_reap)
    p.add_argument("--tag-prefix", default=TAG_PREFIX)
    p.add_argument("--attribute", action="store_true",
                   help="join the lifecycle log so a colleague's box lands in "
                        "`attributed_to_others` instead of `unaccounted`")
    p.add_argument("--billing-only", action="store_true",
                   help="sweep only providers that can bill (skip owned hardware). For "
                        "the automatic conversation-start check, where the justification "
                        "for going to the network is money accruing right now.")

    cl = sub.add_parser("claim", help="register what a machine is FOR (a claim, never "
                                      "a verified fact); touches nothing on the box")
    cl.set_defaults(fn=v_claim)
    cl.add_argument("--provider", required=True)
    cl.add_argument("--instance-id", required=True,
                    help="the provider's id, never a name — names are reused")
    cl.add_argument("--purpose", required=True,
                    help="what it is for, in a sentence somebody else can act on")
    cl.add_argument("--holder", help="who or what holds it: a person, or a session id")
    cl.add_argument("--project"); cl.add_argument("--run"); cl.add_argument("--session")
    cl.add_argument("--review-days", type=int, default=3,
                    help="when to ask whether this is still true; 0 for never. A claim "
                         "with no review date ages into furniture")
    cl.add_argument("--supersede", action="store_true",
                    help="replace an existing claim on this box; the old one is kept")
    cl.add_argument("--note")

    dc = sub.add_parser("disclaim", help="close a claim. Does NOT touch the machine")
    dc.set_defaults(fn=v_disclaim)
    dc.add_argument("claim_id"); dc.add_argument("--why")

    us = sub.add_parser("use", help="record that a run used this lease; appended")
    us.set_defaults(fn=v_use)
    us.add_argument("lease_id")
    us.add_argument("--run", required=True)
    us.add_argument("--outcome", choices=("ok", "preempted", "crashed", "abandoned"))
    us.add_argument("--session")
    us.add_argument("--from-epoch", dest="from_epoch", type=int)
    us.add_argument("--to-epoch", dest="to_epoch", type=int)

    w = sub.add_parser("whose", help="which work used which machine, both directions. "
                                     "Ledger only, no network")
    w.set_defaults(fn=v_whose)
    w.add_argument("--instance-id"); w.add_argument("--run")
    w.add_argument("--project"); w.add_argument("--session")

    h = sub.add_parser("history"); h.set_defaults(fn=v_history)
    h.add_argument("--tag-prefix", default=TAG_PREFIX)
    h.add_argument("--instance-id", help="one machine's lifecycle, instead of the prefix")
    h.add_argument("--window-s", type=int, default=5 * 86400,
                   help="how far back; a box released outside it looks like it never was")

    args = ap.parse_args()
    args.res = resources_from_workspace_root(args.resources)
    args.fn(args)


if __name__ == "__main__":
    main()
