#!/usr/bin/env python3
"""lease — the one entry point agents call. Dispatches to provider adapters and owns
everything that must not depend on an agent remembering to do it.

  agent -> lease.py <verb> -> provider_<name>.py

Adapters (`.claude/skills/lease/references/contract.md`) translate the seven verbs for
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
  lease.py reap [--tag-prefix mlclaw-]

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
from _common import (DEFAULT_TTL_S, TAG_PREFIX, add_shape_args, die, emit,  # noqa: E402
                     fan_out, load_resources, resources_from_workspace_root, shape_flags)

HERE = os.path.dirname(os.path.abspath(__file__))
OPEN_STATES = ("held", "requesting")

# An adapter normally needs a `resources.json -> compute.<name>` entry to count as
# registered. These read an existing block instead, so they need no entry of their own.
# Drops out of here once /resources writes `compute.ssh` when it discovers servers.
SELF_REGISTERING = {"ssh": "servers"}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


# --- ledger -------------------------------------------------------------------

def ledger_path(res_path):
    return os.path.join(os.path.dirname(res_path), "leases.json")


def read_ledger(res_path):
    try:
        with open(ledger_path(res_path)) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"leases": []}


def write_ledger(res_path, data):
    """Atomic: a torn ledger is the one state that hides a billing instance."""
    path = ledger_path(res_path)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def open_rows(ledger):
    return [r for r in ledger["leases"] if r["state"] in OPEN_STATES]


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

    Returns `(rows, errors, scope)`. The scope is the merged answer to "did we actually
    look everywhere", and **a provider that errored is an unreached corner** — not just
    a line in `errors`. Without that, `reap` printed `orphans: []` beside a failed
    adapter and the empty list was the part anyone read.

    `sweep` and `history` must arrive in the envelope (`_common.sweep_result`); a bare
    list from those verbs is treated as **incomplete**, because an adapter written before
    the envelope existed cannot have checked what it never enumerated. Other verbs
    (`capacity`) legitimately return a list and are merged as complete.
    """
    scoped = verb in ("sweep", "history")
    rows, errors, checked, unreached = [], {}, [], []
    results = fan_out(names, lambda n: call(n, res, verb, *extra))
    for name, (ok, data) in zip(names, results):
        if not ok:
            errors[name] = data
            unreached.append({"provider": name, "scope": "*",
                              "why": data.get("error", "transient")})
            continue
        if isinstance(data, dict) and "units" in data:
            units, sc = data["units"], data.get("scope") or {}
        else:
            units, sc = (data or []), ({} if not scoped else
                                       {"complete": False, "unreached": [
                                           {"scope": "*", "why": "adapter returned a bare "
                                            "list; scope unknown"}]})
        rows += [{"provider": name, **row} for row in units]
        checked += [{"provider": name, "scope": s} for s in (sc.get("checked") or [])]
        unreached += [{"provider": name, **u} for u in (sc.get("unreached") or [])]
    return rows, errors, {"complete": not unreached,
                          "checked": checked, "unreached": unreached}


# --- verbs --------------------------------------------------------------------

def v_capacity(args):
    names = [args.provider] if args.provider else providers(args.res)
    if not names:
        die("permission", "no providers registered — run /resources first",
            hint="resources.json -> compute, or -> servers for owned hardware")
    rows, errors, scope = collect(names, args.res, "capacity", *shape_flags(args))
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
    swept, errors, scope = collect(providers(res), res, "sweep", "--tag-prefix", args.tag_prefix)
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
    untracked = [r for r in swept if r.get("tag") not in open_tags]
    emit({"held": live, "untracked": untracked,
          # `held` comes from the ledger and is whole; `untracked` comes from the sweep
          # and is only as complete as the sweep was. Saying which is which is the
          # difference between "nothing else is running" and "nothing else answered".
          "scope": scope,
          "untracked_is_lower_bound": not scope["complete"],
          "errors": errors or None,
          "total_usd_per_hr": round(sum(r.get("price_hr") or 0 for r in held), 2)},
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


def v_reap(args):
    """Cloud-side truth. Must work with leases.json missing or stale — that is the case
    that matters, since the session that created the box is the one that died."""
    res = args.res
    ledger = read_ledger(res)
    open_tags = {r.get("tag") for r in open_rows(ledger)}
    rows, errors, scope = collect(providers(res), res, "sweep", "--tag-prefix", args.tag_prefix)

    orphans = []
    for row in rows:
        reason = ("expired" if row.get("expired") else
                  "no open lease row" if row.get("tag") not in open_tags else None)
        if reason:
            orphans.append({**row, "orphan_reason": reason})

    # `orphans: []` is the whole product of this verb, and it is exactly the shape a
    # sweep that reached nothing also produces. `complete` is what separates "there are
    # no forgotten boxes" from "nobody looked" -- state it before quoting the count.
    emit({"orphans": orphans,
          "complete": scope["complete"],
          "orphans_is_lower_bound": not scope["complete"],
          "scope": scope,
          "leases_without_instance": [r["lease_id"] for r in open_rows(ledger)
                                      if not r["instance_ids"]],
          "errors": errors or None,
          "total_usd_per_hr": round(sum(o.get("price_hr") or 0 for o in orphans), 2)},
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

    n = sub.add_parser("renew"); n.set_defaults(fn=v_renew)
    n.add_argument("lease_id")
    n.add_argument("--ttl-s", type=int, help="new expiry from now; default reuses the lease's")

    r = sub.add_parser("release"); r.set_defaults(fn=v_release)
    r.add_argument("lease_id")

    p = sub.add_parser("reap"); p.set_defaults(fn=v_reap)
    p.add_argument("--tag-prefix", default=TAG_PREFIX)

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
