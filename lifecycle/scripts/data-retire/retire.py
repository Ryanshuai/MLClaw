#!/usr/bin/env python3
"""retire.py — delete data, against evidence, and outlive what it deleted.

Last box on the data line and the only irreversible one on it. A checkpoint
deleted by mistake costs a retrain; a capture deleted by mistake is gone —
260731 cannot be re-shot. So this follows `retention.py` plan -> apply, with the
bar raised in three places.

  plan   ranks every unit by what would SURVIVE the deletion, excludes the ones
         that would not survive it and names why, writes a plan with a confirm
         token. Deletes nothing, ever.
  apply  re-probes the target location, refuses on any drift from the plan,
         writes the record BEFORE the first rm, then deletes only paths a
         census actually listed.
  log    read back every retire record. A deletion log nobody can read is a
         deletion log that did not happen.

THE ONE NOTHING ELSE SEES: a unit that a frozen snapshot still names. Deleting
it does not break the citation — `datasets/boxes@260731` goes on resolving, the
manifest goes on listing the unit, and every run that cited it goes on reading
as reproducible while it no longer is. That failure is silent by construction,
which is why it is a per-unit exclusion here and why waiving it writes the loss
back into the snapshot rather than leaving the citation to lie.

THE CONTAINMENT RULE, and it is the reason this can own `rm -rf` at all: a path
is deletable only if a census listing enumerated it. Never a path assembled from
config and hoped about. `locations[].root` is a string in a JSON file somebody
edits; a unit id is something a scan came back with. Joining the two and
trusting the result is how a typo in `root` becomes `rm -rf /`.

THE LOG LIVES ONE LEVEL ABOVE WHAT IT DELETES — here, on a different machine
entirely: `{PROJECT}/datasets/<id>/retire/` is git-tracked and the bytes are on
a capture box. A deletion that can take its own record with it is a deletion
nobody can audit afterwards.

Exit codes per CLAUDE.md "Script Integration": 0 ok; 1 = the script worked and
the answer is no; 2 = the script broke, do it by hand. Every exit 1 here guards
an irreversible action — redoing it by hand is not a fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402
from _dataset_paths import dataset_dir, latest_census  # noqa: E402

OK = "__MLCLAW_RETIRE_OK__"

# Why a unit was kept out of the plan. Each is computed, never asked about.
REASONS = {
    "cited_by_snapshot": "a frozen snapshot still names this unit; the citation "
                         "would go on resolving after the bytes are gone",
    "below_min_copies": "deleting this copy drops a source layer under "
                        "replication.min_source_copies",
    "unarchived": "this unit has never reached the authority — this copy is the "
                  "only one there has ever been",
    "survivor_less_complete": "the copies that would survive have no completeness "
                              "marker while this one does; deleting it destroys "
                              "the only version anything claims finished",
    "not_at_target": "the census did not list this unit at the target location, "
                     "so there is nothing here to delete",
}

# Waivable, and each by restating the measured count. `cited_by_snapshot` is on
# this list on purpose: refusing outright would send the user to `rm` outside the
# tool, where no record is written at all. Waiving it makes `apply` stamp the
# loss into the snapshot, so the citation still resolves AND says the data is
# gone. The concession is recorded, never made invisible.
WAIVABLE = ("cited_by_snapshot", "below_min_copies", "unarchived",
            "survivor_less_complete")


def snapshots(d) -> list[dict]:
    sdir = os.path.join(d, "snapshots")
    if not os.path.isdir(sdir):
        return []
    out = []
    for sid in sorted(os.listdir(sdir)):
        rec = read_json(os.path.join(sdir, sid, "snapshot.json"), required=False)
        if rec:
            rec["_dir"] = os.path.join(sdir, sid)
            out.append(rec)
    return out


def snapshot_units(snap) -> set[str]:
    """Units a snapshot names. Read from the manifest, which is the frozen set;
    `snapshot.json` carries only counts."""
    path = os.path.join(snap["_dir"], snap.get("manifest") or "manifest.jsonl")
    units = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "unit" in rec:
                    units.add(rec["unit"])
    except FileNotFoundError:
        # A snapshot whose manifest is missing cannot be checked against, and a
        # snapshot that cannot be checked against must not read as "names
        # nothing". Signalled by the caller as an unusable snapshot.
        return None
    return units


def safe_unit_path(root: str, unit: str) -> str | None:
    """Join a location root and a census-listed unit id, or None if the result
    escapes the root. `..`, absolute unit ids and symlinked units all land here.
    This is the last guard before an `rm -rf`, so it is deliberately blunt."""
    root = os.path.expanduser(root).rstrip("/")
    if not root or root == "/" or unit.startswith("/") or not unit.strip():
        return None
    joined = os.path.normpath(os.path.join(root, unit))
    if joined == root or not joined.startswith(root + "/"):
        return None
    return joined


def token_of(plan: dict) -> str:
    """A digest over exactly what will be deleted. Editing the plan by hand to
    add a unit invalidates the token, so `apply` cannot be talked into deleting
    something `plan` never ranked."""
    payload = json.dumps({"dataset": plan["dataset"], "at": plan["at"],
                          "root": plan["root"],
                          "units": sorted(u["unit"] for u in plan["delete"])},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

def cmd_plan(a) -> None:
    project = os.path.expanduser(a.project)
    d = dataset_dir(project, a.dataset)
    cfg = read_json(os.path.join(d, "dataset.json"))
    census = latest_census(d)

    if census is None:
        refuse(f"no census for {a.dataset!r}",
               why="deletion is ranked by what survives it, and nothing has "
                   "looked at what exists",
               fix="census.py scan")
    # A partial census undercounts copies, which sounds like the safe direction
    # and is not: the location that did not answer may be the very survivor the
    # plan is counting on, and "2 copies remain" would then be a guess about a
    # machine nobody could reach. CLAUDE.md "Never report data you could not
    # look at" — a lower bound is not an inventory, and it is certainly not a
    # basis for rm.
    if not census.get("complete"):
        refuse(f"census {census['census_id']} is PARTIAL "
               f"({', '.join(census.get('unreachable') or [])} did not answer)",
               why="every copy count in it is a lower bound; the machine that "
                   "did not answer may be the survivor this plan assumes",
               fix="fix reach and re-scan — this refusal has no override")

    locs = {l["key"]: l for l in cfg.get("locations") or []}
    if a.at not in locs:
        broke(f"no location {a.at!r} in {a.dataset}", known=sorted(locs))
    target = locs[a.at]
    seen_at = {l["key"]: l for l in census.get("locations") or []}
    if not (seen_at.get(a.at) or {}).get("reachable"):
        refuse(f"location {a.at!r} was not reachable in census "
               f"{census['census_id']}",
               why="the plan can only delete paths that census listing "
                   "enumerated, and there is no listing for this machine")

    # --- which snapshots still name units. A snapshot whose manifest cannot be
    # read is not a snapshot that names nothing.
    unusable, cited = [], {}
    for snap in snapshots(d):
        units = snapshot_units(snap)
        if units is None:
            unusable.append(snap["snapshot_id"])
            continue
        for u in units:
            cited.setdefault(u, []).append(snap["snapshot_id"])
    if unusable and not a.allow_unreadable_snapshots:
        refuse(f"{len(unusable)} snapshot(s) have no readable manifest: "
               f"{', '.join(unusable)}",
               why="a snapshot that cannot be read cannot be checked against, "
                   "and 'could not check' must not be filed as 'names nothing'",
               fix="repair the manifest, or --allow-unreadable-snapshots to "
                   "proceed knowing these were never consulted")

    units = census.get("units") or {}
    min_copies = int((cfg.get("replication") or {}).get("min_source_copies") or 2)
    source_layers = {l["label"] for l in cfg.get("layers") or []
                     if l.get("kind") in ("source", "human_locked")}

    if a.units_from:
        with open(os.path.expanduser(a.units_from), encoding="utf-8") as fh:
            wanted = [ln.strip() for ln in fh if ln.strip()]
    elif a.unit:
        wanted = list(a.unit)
    else:
        wanted = sorted(units)

    unknown = [u for u in wanted if u not in units]
    if unknown:
        refuse(f"{len(unknown)} unit(s) are not in census {census['census_id']}",
               units=unknown[:10],
               why="only a path a census listed may be deleted — see the "
                   "containment rule")

    delete, excluded = [], []
    for uid in wanted:
        rec = units[uid]
        at = rec.get("at") or []
        why = []

        if a.at not in at:
            excluded.append({"unit": uid, "reasons": ["not_at_target"]})
            continue
        if uid in cited:
            why.append("cited_by_snapshot")
        if census.get("verdicts", {}).get("unarchived") and \
                uid in census["verdicts"]["unarchived"]:
            why.append("unarchived")

        # Copy arithmetic, per source layer this location actually holds.
        thin = []
        for label, holders in (rec.get("layers") or {}).items():
            if label not in source_layers or a.at not in holders:
                continue
            if len(holders) - 1 < min_copies:
                thin.append({"layer": label, "copies_now": len(holders),
                             "copies_after": len(holders) - 1,
                             "min": min_copies})
        if thin:
            why.append("below_min_copies")

        # The survivor must be at least as finished as the copy going away. The
        # census records WHERE the completeness marker was found, so this is a
        # set difference and not a guess.
        done_at = rec.get("done_at")
        if isinstance(done_at, list) and a.at in done_at and \
                not [k for k in done_at if k != a.at]:
            why.append("survivor_less_complete")

        row = {"unit": uid, "at": at,
               "layers_here": sorted(l for l, h in (rec.get("layers") or {}).items()
                                     if a.at in h),
               "completeness": rec.get("completeness")}
        if thin:
            row["thin_layers"] = thin
        if uid in cited:
            row["snapshots"] = cited[uid]

        # `reasons` keeps check order — cited first, which is how it should be
        # read. `waived` is sorted, so it matches the plan's top-level `waived`
        # and so two plans over the same units diff cleanly.
        waived = sorted(r for r in why if r in a.waive)
        blocked = [r for r in why if r not in a.waive]
        if blocked:
            excluded.append(dict(row, reasons=blocked))
        else:
            delete.append(dict(row, waived=waived) if waived else row)

    tally = {}
    for e in excluded:
        for r in e["reasons"]:
            tally[r] = tally.get(r, 0) + 1

    if not delete:
        refuse(f"0 of {len(wanted)} unit(s) are safe to retire at {a.at!r}",
               excluded_because={k: {"count": v, "means": REASONS[k]}
                                 for k, v in sorted(tally.items())},
               why="this is an answer, not a failure — nothing here can be "
                   "deleted without losing something",
               fix="--waive <reason> restates which risk is being accepted; "
                   f"waivable: {', '.join(WAIVABLE)}")

    # Said out loud rather than left in the per-unit rows. With no completeness
    # marker declared every unit is `unverifiable` and the survivor check can
    # never fire, so copy count is the only thing standing between this plan and
    # deleting a half-captured unit's last finished-looking copy.
    warnings = []
    n_unver = sum(1 for r in delete if r.get("completeness") == "unverifiable")
    if n_unver:
        warnings.append(f"{n_unver} unit(s) in this plan have no completeness "
                        f"signal at all — `completeness.marker` is not declared "
                        f"for this dataset, so nothing can say they finished")
    if unusable and a.allow_unreadable_snapshots:
        warnings.append(f"{len(unusable)} snapshot(s) were never consulted: "
                        f"{', '.join(unusable)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    plan = {
        "retire_id": f"retire_{stamp}",
        "project": project,
        "dataset": a.dataset,
        "at": a.at,
        "via": target.get("via"),
        "server": target.get("server"),
        "root": target["root"],
        "role": target.get("role"),
        "from_census": census["census_id"],
        "census_scanned_at": census.get("scanned_at"),
        "min_source_copies": min_copies,
        "planned_at": now_utc(),
        "because": a.because,
        "waived": sorted(a.waive),
        "warnings": warnings,
        "unconsulted_snapshots": unusable,
        "delete": delete,
        "excluded": excluded,
        "excluded_because": {k: {"count": v, "means": REASONS[k]}
                             for k, v in sorted(tally.items())},
        "counts": {"considered": len(wanted), "delete": len(delete),
                   "excluded": len(excluded)},
    }
    plan["confirm_token"] = token_of(plan)

    path = os.path.expanduser(a.plan) if a.plan else os.path.join(
        d, "retire", f"{plan['retire_id']}_plan.json")
    atomic_write_json(path, plan)
    plan["_path"] = path
    emit(plan)


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def run_remote(plan, script: str, timeout=600):
    if plan.get("via") == "server":
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               plan["server"], "sh", "-s"]
    else:
        cmd = ["sh", "-s"]
    try:
        return subprocess.run(cmd, input=script, capture_output=True,
                              text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        broke(f"could not reach {plan.get('at')}: {type(e).__name__}: {e}")


def reprobe(plan, paths) -> dict:
    """Ask the target which of these paths are there right now.

    The plan was made against a census that is at least seconds and possibly
    weeks old. Re-stating existence before deleting is retention.py's drift
    check; here the drift that matters is a unit that VANISHED (somebody already
    cleaned up, and a path that is gone must not be reported as deleted by us).
    """
    lines = []
    for p in paths:
        lines.append('[ -e %s ] && echo "1 %s" || echo "0 %s"'
                     % (shlex.quote(p), p, p))
    lines.append("echo " + OK)
    r = run_remote(plan, "\n".join(lines) + "\n")
    if r.returncode != 0 or OK not in r.stdout:
        broke("the existence re-check did not complete; nothing was deleted",
              stderr=(r.stderr or "").strip()[-400:])
    present = {}
    for line in r.stdout.splitlines():
        if line == OK or " " not in line:
            continue
        flag, path = line.split(" ", 1)
        present[path] = flag == "1"
    return present


def cmd_apply(a) -> None:
    plan = read_json(os.path.expanduser(a.plan))
    project = os.path.expanduser(plan["project"])
    d = dataset_dir(project, plan["dataset"])

    if a.confirm != plan.get("confirm_token"):
        refuse("confirmation token does not match this plan",
               why="the token digests exactly the unit list that was ranked; a "
                   "mismatch means the plan changed after it was ranked, or the "
                   "token came from a different plan",
               fix="pass the `confirm_token` from the plan file itself")
    if token_of(plan) != plan.get("confirm_token"):
        refuse("the plan's own token does not match its contents",
               why="the delete list was edited after planning, so these units "
                   "were never put through the exclusion checks")

    # --- resolve every path through the containment guard BEFORE touching
    # anything. One bad join aborts the whole apply rather than deleting the
    # rest and reporting a partial success.
    targets, bad = [], []
    for row in plan["delete"]:
        resolved = safe_unit_path(plan["root"], row["unit"])
        if resolved is None:
            bad.append(row["unit"])
        else:
            targets.append({"unit": row["unit"], "path": resolved})
    if bad:
        refuse(f"{len(bad)} unit path(s) escape the location root; nothing was "
               f"deleted", units=bad, root=plan["root"],
               why="a deletable path is one a census listed under this root — "
                   "never one assembled from config and hoped about")

    present = reprobe(plan, [t["path"] for t in targets])
    gone = [t["unit"] for t in targets if not present.get(t["path"])]
    if gone and not a.allow_already_gone:
        refuse(f"{len(gone)} planned unit(s) are already absent at "
               f"{plan['at']!r}", units=gone[:10],
               why="the target drifted from the plan; recording these as "
                   "deleted by this operation would put a deletion in the log "
                   "that this tool did not perform",
               fix="re-plan against a fresh census, or --allow-already-gone to "
                   "record them as already_absent rather than deleted")

    live = [t for t in targets if present.get(t["path"])]

    # --- the record goes down FIRST. A crash between here and the last rm must
    # leave a record saying what was being deleted, not silence. This is the
    # "one level above what it deletes" rule at its most literal: the record is
    # in the project, on this machine; the bytes are on that one.
    rec = {
        "retire_id": plan["retire_id"],
        "dataset": plan["dataset"],
        "at": plan["at"], "root": plan["root"], "via": plan.get("via"),
        "server": plan.get("server"),
        "from_census": plan["from_census"],
        "because": plan.get("because"),
        "waived": plan.get("waived") or [],
        "unconsulted_snapshots": plan.get("unconsulted_snapshots") or [],
        "started_at": now_utc(),
        "status": "in_progress",
        "planned": len(plan["delete"]),
        "already_absent": gone,
        "attempted": [t["unit"] for t in live],
        "deleted": [], "failed": [],
        "plan": plan,
    }
    rpath = os.path.join(d, "retire", f"{plan['retire_id']}.json")
    atomic_write_json(rpath, rec)

    # --- delete. `rm -rf` on a path that passed the containment guard and was
    # confirmed present a moment ago.
    lines = []
    for t in live:
        q = shlex.quote(t["path"])
        lines.append('rm -rf %s && echo "1 %s" || echo "0 %s"' % (q, t["unit"], t["unit"]))
    lines.append("echo " + OK)
    r = run_remote(plan, "\n".join(lines) + "\n", timeout=3600)

    done, failed = [], []
    for line in r.stdout.splitlines():
        if line == OK or " " not in line:
            continue
        flag, unit = line.split(" ", 1)
        (done if flag == "1" else failed).append(unit)
    unreported = [t["unit"] for t in live if t["unit"] not in done and t["unit"] not in failed]

    rec.update({
        "finished_at": now_utc(),
        "deleted": done, "failed": failed,
        # A unit the shell never reported on is neither deleted nor kept as far
        # as this record can tell, and saying so is the whole point.
        "unreported": unreported,
        "status": "complete" if not failed and not unreported else "partial",
        "sentinel_seen": OK in r.stdout,
    })
    atomic_write_json(rpath, rec)

    # --- a citation that resolves to nothing must at least say so. Stamped into
    # the snapshot itself, because that is the record a reader reaches for a
    # year later, and it is the only place that can tell them.
    stamped = []
    for row in plan["delete"]:
        if row["unit"] not in done:
            continue
        for sid in row.get("snapshots") or []:
            spath = os.path.join(d, "snapshots", sid, "snapshot.json")
            snap = read_json(spath, required=False)
            if snap is None:
                continue
            entry = next((e for e in snap.setdefault("data_retired", [])
                          if e.get("retire_id") == plan["retire_id"]), None)
            if entry is None:
                entry = {"retire_id": plan["retire_id"], "at": plan["at"],
                         "retired_at": rec["finished_at"], "units": [],
                         "note": "these units no longer exist at this location; "
                                 "the citation still resolves, the bytes do not"}
                snap["data_retired"].append(entry)
            if row["unit"] not in entry["units"]:
                entry["units"].append(row["unit"])
            atomic_write_json(spath, snap)
            if sid not in stamped:
                stamped.append(sid)

    out = {"retire_id": plan["retire_id"], "record": rpath,
           "deleted": len(done), "failed": failed, "unreported": unreported,
           "already_absent": gone, "snapshots_stamped": stamped,
           "status": rec["status"]}
    emit(out)
    if failed or unreported:
        sys.exit(1)


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #

def cmd_log(a) -> None:
    project = os.path.expanduser(a.project)
    root = os.path.join(project, "datasets")
    if not os.path.isdir(root):
        return emit({"retirements": [], "note": "no datasets in this project"})
    names = [a.dataset] if a.dataset else sorted(
        x for x in os.listdir(root)
        if os.path.isfile(os.path.join(root, x, "dataset.json")))

    out = []
    for ds in names:
        rdir = os.path.join(root, ds, "retire")
        if not os.path.isdir(rdir):
            continue
        for f in sorted(os.listdir(rdir)):
            if f.endswith("_plan.json") or not f.endswith(".json"):
                continue
            rec = read_json(os.path.join(rdir, f), required=False)
            if not rec:
                continue
            out.append({k: rec.get(k) for k in
                        ("retire_id", "dataset", "at", "root", "status",
                         "finished_at", "because", "waived")} |
                       {"deleted": len(rec.get("deleted") or []),
                        "failed": len(rec.get("failed") or []),
                        "unreported": len(rec.get("unreported") or [])})
    out.sort(key=lambda r: r.get("finished_at") or "")
    emit({"retirements": out, "total_units_deleted": sum(r["deleted"] for r in out)})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("plan", help="rank by what survives; delete nothing")
    s.add_argument("--project", required=True)
    s.add_argument("--dataset", required=True)
    s.add_argument("--at", required=True, help="location key to free space on")
    s.add_argument("--unit", action="append", default=[])
    s.add_argument("--units-from", default=None)
    s.add_argument("--waive", action="append", default=[], choices=WAIVABLE,
                   help="accept one named risk; repeatable")
    s.add_argument("--allow-unreadable-snapshots", action="store_true")
    s.add_argument("--because", default=None, help="why this space is being freed")
    s.add_argument("--plan", default=None, help="where to write the plan")
    s.set_defaults(fn=cmd_plan)

    ap = sub.add_parser("apply", help="delete what the plan ranked, and record it")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--confirm", required=True,
                    help="the confirm_token from the plan file")
    ap.add_argument("--allow-already-gone", action="store_true")
    ap.set_defaults(fn=cmd_apply)

    lg = sub.add_parser("log", help="read back every retirement")
    lg.add_argument("--project", required=True)
    lg.add_argument("--dataset", default=None)
    lg.set_defaults(fn=cmd_log)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
