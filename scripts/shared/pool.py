#!/usr/bin/env python3
"""pool — the slot pool a search holds while it runs. Layer 3.

  /train-tune, /explore    ->  pool.py  ->  lease.py  ->  provider_<name>.py

Read `references/fleet.md` before changing anything here; every rule this file
implements is stated there with what it costs when broken. The short version:

  * a **slot** is one trial's worth of compute, not one machine. An 8-GPU box is eight
    slots for single-GPU trials, and it is provisioned and staged once for all of them.
  * **owned before rented.** Free GPUs are filled first, always.
  * **the loop renews, not the trial.** A trial ending must not release its box; the next
    trial wants it. `heartbeat` is one call for the whole fleet so it cannot be
    half-forgotten.
  * **a preempted trial is not a failed trial.** The pool reports the difference; the
    search must not count an infrastructure outcome as evidence about a hypothesis.

This file holds no provider knowledge and must never gain any. Everything it does goes
through `lease.py`'s verbs, which is what lets an owned 4090 and a rented H100 be the
same row to the search above.

Usage
  pool.py plan      --slots N [shape flags] [--hours H] [--allow-preemptible]
  pool.py open      --session DIR --slots N [shape flags] [--hours H] [--allow-preemptible]
                    [--confirmed-usd-per-hr F]
  pool.py status    --session DIR [--probe]
  pool.py acquire   --session DIR --run RUN_ID
  pool.py release   --session DIR --slot SLOT [--outcome ok|preempted|crashed]
  pool.py heartbeat --session DIR [--ttl-s N]
  pool.py close     --session DIR
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LEASE = os.path.join(HERE, "..", "lease", "lease.py")

# The machine shape is L2's vocabulary and L2 is its author: `_common.SHAPE_ARGS` is
# the argparse table a new dimension gets added to. This layer had a third copy of it
# -- the tuple, the re-serializer, and the four `add_argument` calls -- and the drift
# is silent in the direction that costs money: a dimension added to the table and not
# here is simply not passed on, so the search rents a machine that does not meet the
# constraint and nothing anywhere says so.
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lease"))
from _common import DEFAULT_TTL_S, SHAPE_FLAGS, add_shape_args, shape_flags  # noqa: E402
sys.path.insert(0, HERE)
from _records import atomic_write_json  # noqa: E402

PROBE_TIMEOUT = 10

def emit(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def fail(detail, **extra):
    print(json.dumps({"error": detail, **extra}, indent=2, ensure_ascii=False))
    sys.exit(1)


# --- L2 ------------------------------------------------------------------------

RESOURCES = None   # set once from --resources; L2 otherwise resolves it itself


def lease(*args, timeout=960):
    """One call into layer 2. Returns (ok, payload).

    Never falls back to a provider CLI on failure, and that is deliberate — the fallback
    rule in CLAUDE.md has an exception for a script that *refuses*, and every refusal in
    `lease.py` is a money rule saying no. Doing it by hand there means overriding the
    safeguard, not routing around a bug.
    """
    pre = ["--resources", RESOURCES] if RESOURCES else []
    argv = [sys.executable, "-X", "utf8", LEASE, *pre, *map(str, args)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    out = proc.stdout.strip()
    try:
        payload = json.loads(out) if out else None
    except json.JSONDecodeError:
        return False, {"error": "transient", "detail": out[:300] or proc.stderr[-300:]}
    return proc.returncode == 0, payload


# --- record ---------------------------------------------------------------------

def pool_path(session):
    return os.path.join(session, "pool.json")


def read_pool(session):
    try:
        with open(pool_path(session)) as fh:
            return json.load(fh)
    except FileNotFoundError:
        fail(f"no pool.json in {session}",
             hint="if a fleet was opened and this record is gone, `lease.py reap` finds "
                  "the boxes from the cloud side — it needs no local state at all")
    except json.JSONDecodeError as exc:
        fail(f"pool.json is not valid JSON: {exc}")


def write_pool(session, data):
    """Atomic. A torn pool record is the one state that hides a billing box from the
    only thing that knows its lease id."""
    os.makedirs(session, exist_ok=True)
    atomic_write_json(pool_path(session), data, fsync=True)


# --- planning -------------------------------------------------------------------

def candidates(args):
    ok, data = lease("capacity", *shape_flags(args))
    if not ok:
        fail("capacity failed", detail=data)
    rows = data.get("options") or []
    scope = dict(data.get("scope") or {})
    # An adapter that could not read part of its own scope reports it as a row with no
    # machine_type rather than dropping it (`capacity` returns a plain list, so it has
    # nowhere else to put it). Folding those back into `scope` here is what keeps
    # "nothing available" distinguishable from "nobody looked" one layer up.
    unread = [r for r in rows if not r.get("machine_type")]
    if unread:
        scope["complete"] = False
        scope["unreached"] = (scope.get("unreached") or []) + [
            {"scope": r.get("label"), "why": r.get("binding_limit")} for r in unread]
    if not args.allow_preemptible:
        rows = [r for r in rows if not r.get("preemptible")]
    viable = [r for r in rows if (r.get("avail") or 0) > 0 and r.get("machine_type")]
    return viable, scope, data.get("errors")


def gpus_of(row):
    return max(1, row.get("gpu_count") or 1)


def build_plan(args):
    """Fill `--slots` from the cheapest viable rows, free hardware first.

    Owned hardware is `price_hr == 0` and sorts ahead of everything, per `fleet.md`
    "Owned before rented" — a pool that rented four boxes while the user's own GPU sat
    idle has made an error no later step can detect, because both are the same row by
    the time a run reads them.

    Slots-per-unit is `gpu_count // gpu_count_requested`: renting eight 1-GPU boxes for
    eight single-GPU trials, when one 8-GPU box was cheaper per card and staged once, is
    the standard way a sweep costs triple.
    """
    per_trial = max(1, args.gpu_count or 1)
    viable, scope, errors = candidates(args)
    viable.sort(key=lambda r: (r.get("price_hr") if r.get("price_hr") is not None
                               else float("inf"), -gpus_of(r)))

    picks, remaining = [], args.slots
    for row in viable:
        if remaining <= 0:
            break
        slots_each = max(1, gpus_of(row) // per_trial)
        # `max_single_instance` is what one create can actually be placed into; `avail`
        # is the total across placement domains. Using the total to size a fleet asks
        # for units that individually cannot be placed.
        placeable = min(row.get("avail") or 0,
                        row.get("max_single_instance") or row.get("avail") or 0)
        want_units = min(-(-remaining // slots_each), max(0, placeable // per_trial) or 0)
        if want_units <= 0:
            continue
        picks.append({"machine_type": row["machine_type"], "provider": row.get("provider"),
                      "units": want_units, "slots_each": slots_each,
                      "slots": want_units * slots_each,
                      "price_hr": row.get("price_hr"),
                      "price_status": row.get("price_status"),
                      "price_asof": row.get("price_asof"),
                      "preemptible": bool(row.get("preemptible")),
                      "label": row.get("label"), "arch": row.get("arch"),
                      "arch_status": row.get("arch_status")})
        remaining -= want_units * slots_each

    known = [p for p in picks if p["price_hr"] is not None]
    usd_hr = round(sum((p["price_hr"] or 0) * p["units"] for p in picks), 2)
    est_total = round(usd_hr * args.hours, 2) if args.hours else None
    return {
        "requested_slots": args.slots, "gpu_per_trial": per_trial,
        "picks": picks, "slots_filled": args.slots - max(0, remaining),
        "short_by": max(0, remaining),
        "usd_per_hr": usd_hr,
        "est_hours": args.hours, "est_total_usd": est_total,
        # Cost is stated once, for the whole fleet, before anything is acquired
        # (fleet.md "Cost is reported before it is spent"). Where the number came from
        # a hand-maintained table rather than a billing API it is a claim, and a fleet
        # whose prices are partly unknown must not be quoted as a total.
        "price_confidence": ("claim" if any(p["price_status"] == "claim" for p in known)
                             else "unknown" if not known else "verified"),
        "priced_units": len(known), "unpriced_units": len(picks) - len(known),
        "cost_is_complete": len(known) == len(picks) and bool(picks),
        "any_preemptible": any(p["preemptible"] for p in picks),
        "capacity_scope": scope, "capacity_errors": errors,
        # A plan built from an incomplete capacity read may be short for no real reason.
        "plan_trustworthy": bool(scope.get("complete", True)),
    }


def v_plan(args):
    plan = build_plan(args)
    plan["note"] = []
    if plan["short_by"]:
        plan["note"].append(
            f"can fill {plan['slots_filled']}/{args.slots} slots — the search runs at "
            f"lower concurrency rather than not at all")
    if plan["any_preemptible"]:
        plan["note"].append(
            "includes interruptible capacity: cheaper and usually the only pool with "
            "stock, but a trial can be stopped mid-flight. The search must pull "
            "checkpoints on its monitor cadence rather than at finalize, or a preemption "
            "costs the whole trial and the economics that justified it are gone")
    if not plan["cost_is_complete"]:
        plan["note"].append(
            f"{plan['unpriced_units']} of {len(plan['picks'])} machine types have no "
            f"price in the table — the total below is a LOWER BOUND, not an estimate")
    if not plan["plan_trustworthy"]:
        plan["note"].append(
            "capacity was read incompletely; this plan may understate what is available")
    emit(plan)


# --- open / close ---------------------------------------------------------------

def v_open(args):
    session = args.session
    if os.path.exists(pool_path(session)) and not args.reopen:
        fail(f"{pool_path(session)} already exists",
             hint="close it first, or pass --reopen to add slots to the existing pool")

    plan = build_plan(args)
    if not plan["picks"]:
        fail("nothing viable to open", plan=plan)

    # The spend is confirmed above this line, not here. L2 has no opinion about money and
    # this script is not the user; refusing without an explicit figure is what stops a
    # fleet from being acquired by a loop that never showed anyone a number.
    if plan["usd_per_hr"] > 0 and args.confirmed_usd_per_hr is None:
        fail("this pool bills and no confirmed figure was passed",
             usd_per_hr=plan["usd_per_hr"], est_total_usd=plan["est_total_usd"],
             price_confidence=plan["price_confidence"],
             hint="show the user `pool.py plan` output, then pass "
                  "--confirmed-usd-per-hr with the figure they agreed to")
    if (args.confirmed_usd_per_hr is not None
            and plan["usd_per_hr"] > args.confirmed_usd_per_hr + 0.01):
        fail("capacity moved since the plan was shown — refusing to spend more than was "
             "confirmed", confirmed=args.confirmed_usd_per_hr, now=plan["usd_per_hr"])

    pool = (read_pool(session) if args.reopen and os.path.exists(pool_path(session))
            else {"session": os.path.abspath(session), "opened_at": int(time.time()),
                  "closed_at": None, "shape": {k: getattr(args, k) for k in SHAPE_FLAGS},
                  "allow_preemptible": args.allow_preemptible, "slots": [], "plan": None})
    pool["plan"] = plan
    pool["ttl_s"] = args.ttl_s
    write_pool(session, pool)          # before any `up`, same reason as Money rule 1

    index = len(pool["slots"])
    for pick in plan["picks"]:
        for _ in range(pick["units"]):
            ok, data = lease("up", "--provider", pick["provider"],
                             "--machine-type", pick["machine_type"],
                             "--ttl-s", args.ttl_s, *shape_flags(args),
                             *(["--price-hr", pick["price_hr"]]
                               if pick["price_hr"] is not None else []),
                             *(["--project", args.project] if args.project else []))
            if not ok:
                # Partial success is a normal outcome for a fleet, not an error. Record
                # what failed and keep the slots that came up — a search at three of four
                # slots is a working search.
                pool.setdefault("failures", []).append(
                    {"machine_type": pick["machine_type"], "at": int(time.time()),
                     "error": data})
                write_pool(session, pool)
                continue
            for _slot in range(pick["slots_each"]):
                pool["slots"].append({
                    "slot": f"slot_{index}", "lease_id": data["lease_id"],
                    "provider": pick["provider"], "machine_type": pick["machine_type"],
                    "price_hr": pick["price_hr"] if _slot == 0 else 0,
                    "preemptible": pick["preemptible"],
                    "state": "free", "run": None, "history": [],
                })
                index += 1
            write_pool(session, pool)

    emit({"session": pool["session"], "slots_open": len(pool["slots"]),
          "requested": args.slots, "failures": pool.get("failures"),
          "usd_per_hr": plan["usd_per_hr"],
          "next": "heartbeat once per search iteration, or the TTL kills the fleet"})


def v_close(args):
    """Release everything, verified. Idempotent, and it must work when the search that
    opened the pool is long dead — which is the case that matters, since that session is
    exactly the one that will not run this."""
    session = args.session
    pool = read_pool(session)

    # ‼️ The drain, as a refusal rather than a line of prose.
    #
    # `close` destroys the boxes. While `/train-tune`'s loop was a barrier -- launch a
    # batch, block until ALL of it finished -- no trial could still be running when a
    # stop condition tripped, so nothing had to check. That loop is a pipeline now
    # (trials launch as slots free and are harvested one at a time), which makes
    # "stopped launching" and "nothing is running" two different facts for the first
    # time. Destroying a box under a running trial loses the trial AND is CLAUDE.md ->
    # "Never release a machine you did not evacuate": the disk goes with the lease and
    # there is no `rm` anywhere in the log.
    #
    # The honest exit is `--abandon`, which is a DECISION and is recorded as one. A
    # silent teardown and a deliberate abandonment read identically afterwards, which is
    # the whole reason this cannot be a warning.
    busy = [{"slot": s_["slot"], "run": s_.get("run")}
            for s_ in pool["slots"] if s_["state"] == "busy"]
    if busy and not args.abandon:
        fail("%d slot(s) still have a trial on them -- drain before closing" % len(busy),
             busy=busy,
             fix="let them finish and `pool.py release --slot <s> --outcome ...` each, "
                 "then close. If they must be dropped, `--abandon '<why>'` -- that is a "
                 "decision and lands in the record as one. Closing destroys the disks: "
                 "run /evacuate first if anything on them is worth keeping")
    if busy and args.abandon:
        pool.setdefault("abandoned", []).extend(
            [dict(b, why=args.abandon, at=int(time.time())) for b in busy])

    released, stuck = [], []
    for lease_id in sorted({s["lease_id"] for s in pool["slots"] if s.get("lease_id")}):
        ok, data = lease("release", lease_id)
        (released if ok else stuck).append({"lease_id": lease_id, "detail": data})
    for slot in pool["slots"]:
        slot["state"] = "released" if not any(
            s["lease_id"] == slot["lease_id"] for s in stuck) else "STILL HELD"
    pool["closed_at"] = int(time.time())
    write_pool(session, pool)
    out = {"released": len(released), "stuck": stuck or None,
           "session": pool["session"]}
    if busy and args.abandon:
        out["abandoned"] = busy
        out["‼️"] = ("%d trial(s) were still running and their boxes are gone. They are "
                     "NOT evidence about their hypotheses and must not be recorded as "
                     "refuted -- same rule as a preemption. Name them in the summary."
                     % len(busy))
    if stuck:
        # Never paper over a failed teardown. The lease rows stay open on purpose so the
        # boxes remain visible to `status` and `reap`.
        out["ALERT"] = ("some leases did not verify as gone and MAY STILL BE BILLING — "
                        "run `lease.py status`, then `lease.py reap`")
        emit(out)
        sys.exit(1)
    emit(out)


# --- the loop's verbs ------------------------------------------------------------

def reach_of(slot):
    ok, data = lease("addr", slot["lease_id"])
    return data.get("reach") if ok else None


def probe(reach):
    """Does this slot still answer? The only provider-blind preemption signal there is.

    A preempted machine is `stopped` to its provider, and `stopped` maps to `running`
    because the disk keeps billing — correct for the ledger and useless for the search.
    What the search needs to know is whether the box can take a trial, and that is a
    question about reachability, which every provider answers the same way.
    """
    if not reach or not reach.startswith("ssh://"):
        return False
    target = reach[len("ssh://"):]
    host, _, port = target.partition(":")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={PROBE_TIMEOUT}",
            *(["-p", port] if port else []), host, "true"]
    try:
        return subprocess.run(argv, capture_output=True,
                              timeout=PROBE_TIMEOUT + 5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def v_status(args):
    pool = read_pool(args.session)
    now = int(time.time())
    ok, ledger = lease("status")
    by_lease = {row["lease_id"]: row for row in (ledger or {}).get("held", [])} if ok else {}

    rows, lost = [], 0
    for slot in pool["slots"]:
        row = dict(slot)
        led = by_lease.get(slot["lease_id"])
        row["ttl_remaining_s"] = led.get("ttl_remaining_s") if led else None
        row["accrued_usd"] = led.get("accrued_usd") if led else None
        row["lease_state"] = (led or {}).get("actual_state", "no open lease row")
        if args.probe and slot["state"] != "released":
            row["reachable"] = probe(reach_of(slot))
            if not row["reachable"]:
                # Held, billing, and unusable. For an interruptible slot this is the
                # normal preemption signal; for any slot it means the search must not
                # place a trial here and must not read a trial that was here as a result.
                row["state"] = "preempted" if slot.get("preemptible") else "lost"
                lost += 1
        rows.append(row)
    if args.probe:
        for slot, row in zip(pool["slots"], rows):
            slot["state"] = row["state"]
        write_pool(args.session, pool)

    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    emit({"session": pool["session"], "age_s": now - pool["opened_at"],
          "counts": counts, "unusable": lost, "slots": rows,
          "ledger_scope": (ledger or {}).get("scope"),
          "usd_per_hr": (pool.get("plan") or {}).get("usd_per_hr"),
          "probed": bool(args.probe),
          "note": None if args.probe else
          "not probed — `free` here means the pool has not handed this slot out, not "
          "that the machine answers. Pass --probe before trusting it to place a trial"})


def v_acquire(args):
    """Hand out a free slot. Address resolved **now**, never read from the record."""
    pool = read_pool(args.session)
    slot = next((s for s in pool["slots"] if s["state"] == "free"), None)
    if slot is None:
        fail("no free slot", counts={s["state"]: 1 for s in pool["slots"]},
             hint="wait for a trial to finish, or `pool.py open --reopen` for more")
    reach = reach_of(slot)
    if not reach:
        slot["state"] = "lost"
        write_pool(args.session, pool)
        fail(f"{slot['slot']} could not be resolved to an address; marked lost",
             slot=slot["slot"])
    slot["state"], slot["run"] = "busy", args.run
    slot["history"].append({"run": args.run, "at": int(time.time()), "outcome": None})
    write_pool(args.session, pool)
    emit({"slot": slot["slot"], "reach": reach, "lease_id": slot["lease_id"],
          "preemptible": slot.get("preemptible"), "run": args.run})


# What happened to what was ON the box -- a SECOND AXIS, not a fourth outcome.
# `outcome` answers "is this trial evidence about its hypothesis"; this answers
# "did the work survive". Collapsing them loses the distinction that costs money.
#
# From a real fleet (2026-08-14, ten preempted L40S boxes): four arms were
# preempted WITH weights on disk, reachable only by attaching the volume to
# another platform; three were preempted and probably had nothing, but that could
# not be checked because starting the box back up hit the same tenure wall that
# preempted it. All seven were `--outcome preempted`, and the record could not
# tell them apart.
#
# ‼️ `unverifiable` IS NOT `absent`. "Probably no weights" and "no weights" are
# different facts, and a tenure wall produces the first while reading like the
# second -- the same rule as `census.py` keeping a location that did not answer
# apart from a directory that is genuinely empty, and `/repro` refusing to call an
# unprobed axis `intact`.
ARTIFACTS = {
    "recovered": "pulled off and verified. The only state that permits destroying the box",
    "present_unreachable": "on the disk, not reachable by the normal route — needs another "
                           "path (attach the volume elsewhere) before anything is destroyed",
    "absent": "checked, and there is nothing there",
    "unverifiable": "could not look. NEVER report this as `absent`",
}


def _clearance_verdict(path):
    """-> the verdict in an `/evacuate` record, or a reason it cannot be read.

    Never raises and never guesses: an unreadable clearance is not a passing
    one, and the string it returns lands in the refusal so the operator sees
    which of the two happened.
    """
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError) as e:
        return f"unreadable ({type(e).__name__})"
    return ((rec.get("clearance") or {}).get("verdict")) or "not decided"


def v_release(args):
    """Return a slot to the pool. **Does not release the lease** — the next trial wants
    the box, and tearing down between trials pays provisioning and staging every time.

    `--outcome preempted` is the one that matters upstream: it tells the search that this
    trial's number is not evidence about its hypothesis. fleet.md "A preempted trial is
    not a failed trial" — the search steering away from a good region because the
    provider reclaimed a card is a mistake nothing downstream can detect or undo.
    """
    # An infrastructure outcome means the box may be going away with work on it.
    # That is exactly where a default would turn `unverifiable` into `absent`, so
    # the friction is targeted rather than universal: required here, optional on ok.
    if args.outcome in ("preempted", "crashed") and not args.artifacts:
        fail(f"--outcome {args.outcome} needs --artifacts "
             f"({'|'.join(ARTIFACTS)}) — this is the case where the box may be "
             f"going away with work on it, and where 'probably nothing was there' "
             f"most often gets written down as 'nothing was there'")
    # ‼️ `recovered` is DEFINED as "pulled off and verified", and until now the
    # verification was the operator's word — CLAUDE.md's 「Never let somebody's
    # word become a checked fact」, sitting on top of the one action that cannot
    # be undone. `/evacuate` is what computes it; this requires the record.
    #
    # Only `recovered` needs it. The other three states are honest about not
    # knowing, and demanding paperwork to say "I could not look" would push
    # people toward the state that needs none.
    if args.artifacts == "recovered":
        if not args.clearance:
            fail("--artifacts recovered needs --clearance <evacuation.json> — "
                 "`recovered` means pulled off AND VERIFIED, and without the "
                 "record that is a claim standing where a check belongs. "
                 "`/evacuate` computes one; `unverifiable` is the honest answer "
                 "if nobody ran it")
        verdict = _clearance_verdict(args.clearance)
        if verdict not in ("clear", "clear_size_only"):
            fail(f"clearance is `{verdict}` — this box still has work on it that "
                 f"was not verified off. Releasing the lease destroys the disk. "
                 f"Report `present_unreachable` or `unverifiable`, or finish the "
                 f"evacuation")

    pool = read_pool(args.session)
    slot = next((s for s in pool["slots"] if s["slot"] == args.slot), None)
    if slot is None:
        fail(f"no slot {args.slot} in this pool")
    if slot["history"]:
        slot["history"][-1]["outcome"] = args.outcome
        slot["history"][-1]["artifacts"] = args.artifacts
        slot["history"][-1]["ended_at"] = int(time.time())
    slot["run"] = None
    slot["state"] = "preempted" if args.outcome == "preempted" else "free"
    write_pool(args.session, pool)
    out = {"slot": slot["slot"], "state": slot["state"], "outcome": args.outcome,
           "artifacts": args.artifacts,
           "trial_counts_as_evidence": args.outcome != "preempted",
           "safe_to_destroy_the_box": args.artifacts == "recovered",
           "note": ("infrastructure outcome — re-place and re-run this trial; do NOT "
                    "record it as a refuted hypothesis"
                    if args.outcome == "preempted" else None)}
    if args.artifacts and args.artifacts != "recovered":
        out["‼️"] = (f"artifacts are `{args.artifacts}`: {ARTIFACTS[args.artifacts]}. "
                     f"Releasing this lease destroys the disk. `unverifiable` is not "
                     f"`absent` — say which one when you report this.")
    emit(out)


def v_heartbeat(args):
    """Renew every lease in the fleet, once, from the search loop.

    The loop is the only component that knows the search is still alive, and this is one
    call for the whole pool precisely so it cannot be half-forgotten. Skipping it lets a
    4-hour TTL kill an overnight sweep; the opposite failure — the session ends and the
    TTLs run out — is the safe one, and is why the switch exists at all.
    """
    pool = read_pool(args.session)
    ttl = args.ttl_s or pool.get("ttl_s") or DEFAULT_TTL_S
    renewed, failed = [], []
    for lease_id in sorted({s["lease_id"] for s in pool["slots"]
                            if s.get("lease_id") and s["state"] != "released"}):
        ok, data = lease("renew", lease_id, "--ttl-s", ttl)
        (renewed if ok else failed).append({"lease_id": lease_id, "detail": data})
    pool["last_heartbeat"] = int(time.time())
    write_pool(args.session, pool)
    out = {"renewed": len(renewed), "failed": failed or None, "ttl_s": ttl}
    if failed:
        out["ALERT"] = ("a hold could not be extended — those boxes expire on schedule "
                        "and any trial on them dies with them")
        emit(out)
        sys.exit(1)
    emit(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--resources", help="path to resources.json; else L2 resolves it")
    sub = ap.add_subparsers(dest="verb", required=True)

    def shape(parser):
        add_shape_args(parser)
        parser.add_argument("--allow-preemptible", action="store_true",
                            help="include interruptible capacity — usually right for a "
                                 "search, see fleet.md")
        parser.add_argument("--hours", type=float, help="estimated search duration, for "
                                                        "the cost figure")

    p = sub.add_parser("plan"); p.set_defaults(fn=v_plan); shape(p)
    p.add_argument("--slots", type=int, required=True)

    o = sub.add_parser("open"); o.set_defaults(fn=v_open); shape(o)
    o.add_argument("--session", required=True, help="tune/explore session dir")
    o.add_argument("--slots", type=int, required=True)
    o.add_argument("--ttl-s", type=int, default=DEFAULT_TTL_S)
    o.add_argument("--project")
    o.add_argument("--reopen", action="store_true")
    o.add_argument("--confirmed-usd-per-hr", type=float,
                   help="the figure the user agreed to; required for any pool that bills")

    s = sub.add_parser("status"); s.set_defaults(fn=v_status)
    s.add_argument("--session", required=True)
    s.add_argument("--probe", action="store_true", help="ssh each slot — the only "
                                                        "provider-blind preemption signal")

    a = sub.add_parser("acquire"); a.set_defaults(fn=v_acquire)
    a.add_argument("--session", required=True)
    a.add_argument("--run", required=True)

    r = sub.add_parser("release"); r.set_defaults(fn=v_release)
    r.add_argument("--session", required=True)
    r.add_argument("--slot", required=True)
    r.add_argument("--outcome", default="ok", choices=("ok", "preempted", "crashed"))
    r.add_argument("--artifacts", choices=sorted(ARTIFACTS),
                   help="what happened to what was ON the box. Required when the outcome "
                        "is preempted or crashed. `unverifiable` is never `absent`")
    r.add_argument("--clearance",
                   help="path to an `/evacuate` evacuation.json. REQUIRED with "
                        "--artifacts recovered: that word means verified, and the "
                        "record is what makes it one rather than a claim")

    hb = sub.add_parser("heartbeat"); hb.set_defaults(fn=v_heartbeat)
    hb.add_argument("--session", required=True)
    hb.add_argument("--ttl-s", type=int)

    c = sub.add_parser("close"); c.set_defaults(fn=v_close)
    c.add_argument("--session", required=True)
    c.add_argument("--abandon", metavar="WHY",
                   help="close even though slots still hold running trials, dropping "
                        "them. Records what was abandoned and why. Without it, a busy "
                        "slot is a refusal: closing destroys the disk under a live run")

    args = ap.parse_args()
    global RESOURCES
    RESOURCES = args.resources
    args.fn(args)


if __name__ == "__main__":
    main()
