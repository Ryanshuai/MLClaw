#!/usr/bin/env python3
"""Send artifacts to a party MLClaw does not control, and verify what comes back.

Every other run skill closes its own loop: it starts the process, watches it, and
reads the result. The completion signal is evidence MLClaw produced itself. Here
the loop is closed by somebody else, and the completion signal is a *claim* —
"it's labeled" / "done, see the link". This script's only job is to turn that claim
back into evidence, and there is exactly one way to do it: compare what came back
against a manifest frozen at send time. There is no external authority to ask.
`lease.py reap` can consult the cloud API; nothing here can consult the vendor.

Consequences that shape the command set:

  send     freezes the manifest. Rigor here is load-bearing in a way it is not
           anywhere else in MLClaw, because the manifest is the *only* record of
           what the returned data is supposed to cover.
  receive  computes a reconciliation. It never marks the handoff complete —
           computing is not accepting, the same split as `retention.py`
           plan -> apply (CLAUDE.md "Script Integration": a refusal is an answer).
  close    is the accept/reject step, and it refuses to accept a partial return
           unless the caller restates the coverage it is accepting.

Exit codes follow CLAUDE.md "Script Integration": 1 = the script worked and the
answer is no (do not "fall back" and do it by hand — that is overriding a check).
2 = the script broke; fall back to doing the work manually.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (age_days, atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402

# Fixed vocabulary, deliberately short. A handoff's kind decides what a reviewer
# looks for on return, so an open string would make that undecidable downstream.
KINDS = ("annotation", "data_request", "review", "human_eval", "delivery", "other")

# How a returned file is matched back to a manifest entry. Vendors rename; a
# labeling job returns 000123.json for the 000123.jpg you sent. The strategy is
# recorded in the reconciliation because it changes what "matched" means.
MATCH_BY = ("path", "stem", "name")

HASH_ALGOS = ("sha256", "size")

TERMINAL = ("accepted", "rejected", "cancelled")


  # atomic: a half-written record is worse than none


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

def iter_files(root, include=None, exclude=None):
    """Relative paths under root, sorted. Sorted so a manifest is reproducible and
    two manifests of the same tree diff cleanly.

    Always forward slashes, whatever the OS separator is. A manifest item id is
    not a path on this machine — it is the identifier a third party is sent, works
    against for three weeks, and returns a listing under, and `receive` computes
    completeness by comparing those strings. Let the separator follow the host and
    a batch enumerated on Windows comes back from a vendor on Linux matching
    nothing: every item reported missing, and the count is the record that
    downstream inherits. The manifest is frozen at send time precisely so it can
    outlive the machine that wrote it; an OS-dependent key defeats that.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            rel = rel.replace(os.sep, "/")
            if include and not any(fnmatch.fnmatch(rel, p) for p in include):
                continue
            if exclude and any(fnmatch.fnmatch(rel, p) for p in exclude):
                continue
            out.append(rel)
    return out


def hash_file(path, algo):
    if algo == "size":
        return str(os.path.getsize(path))
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root, items, algo):
    records = []
    for rel in items:
        full = os.path.join(root, rel)
        records.append({"item": rel, "hash": hash_file(full, algo),
                        "bytes": os.path.getsize(full)})
    return records


def write_manifest(path, records, algo):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_manifest": {"hash_algo": algo, "count": len(records),
                                           "frozen_at": now_utc()}},
                            ensure_ascii=False) + "\n")
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_manifest(path):
    """-> (header, [record]). JSONL because a manifest of 100k images should stream."""
    header, records = {}, []
    try:
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    broke(f"{path}:{n} is not valid JSON: {exc}")
                if "_manifest" in obj:
                    header = obj["_manifest"]
                else:
                    records.append(obj)
    except FileNotFoundError:
        broke(f"manifest not found: {path}")
    return header, records


def key_of(rel, match_by):
    if match_by == "path":
        return rel
    base = os.path.basename(rel)
    return os.path.splitext(base)[0] if match_by == "stem" else base


# --------------------------------------------------------------------------- #
# locating handoffs
# --------------------------------------------------------------------------- #

def handoff_dir(project, handoff_id):
    return os.path.join(os.path.expanduser(project), "handoffs", handoff_id)


def record_path(project, handoff_id):
    return os.path.join(handoff_dir(project, handoff_id), "handoff.json")


def scan(root):
    """Every handoff.json at or under root. No index file — run-mechanics.md
    "Listing runs (no separate index)" applies for the same reasons: an index
    drifts after an rsync, a manual delete, or a schema change, and none of that
    needs handling when there is nothing to drift."""
    root = os.path.expanduser(root)
    found, errors = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "code", "runs")]
        if "handoff.json" in filenames:
            path = os.path.join(dirpath, "handoff.json")
            try:
                with open(path, encoding="utf-8") as fh:
                    rec = json.load(fh)
                rec["_path"] = path
                found.append(rec)
            except (OSError, json.JSONDecodeError) as exc:
                # One malformed record must not kill the scan — same rule as list_runs.py.
                errors.append({"path": path, "error": str(exc)})
    found.sort(key=lambda r: r.get("created_at") or "")
    return found, errors


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #

def cmd_send(args):
    project = os.path.expanduser(args.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")

    source_root = os.path.expanduser(args.source)
    if not os.path.isdir(source_root):
        broke(f"source is not a directory: {source_root}")

    if args.kind not in KINDS:
        broke(f"unknown kind {args.kind!r}", allowed=list(KINDS))
    if args.hash not in HASH_ALGOS:
        broke(f"unknown hash {args.hash!r}", allowed=list(HASH_ALGOS))

    # A missing spec is allowed but never silent. The annotation guideline (or
    # review criteria, or acceptance spec) the other party worked from is part of
    # the returned artifact's identity: two batches labeled under different
    # guidelines are different distributions, and mixing them raises nothing —
    # the same failure shape as the preprocessing mismatch in run-mechanics.md
    # "Preprocessing contract (cross-stage)". So the absence has to be typed out.
    if not args.spec and not args.no_spec:
        refuse("no --spec given",
               why="the spec the other party works from is part of the returned "
                   "data's identity; batches under different specs are different "
                   "distributions and nothing downstream will notice",
               fix="pass --spec <file|dir>, or --no-spec to record its absence deliberately")

    rework_of, carried = None, None
    if args.rework:
        base = read_json(record_path(project, args.rework))
        if base.get("status") not in ("returned", "rejected"):
            refuse(f"{args.rework} is {base.get('status')!r}; rework needs a reconciled round",
                   hint="run `receive` first, then `close --reject`")
        rework_of = args.rework
        carried = _deficit_items(project, base)
        if not carried:
            refuse(f"{args.rework} has no outstanding items to rework",
                   coverage=base.get("latest", {}).get("coverage"))

    items = carried if carried is not None else iter_files(
        source_root, args.include or None, args.exclude or None)
    if not items:
        refuse("nothing to send: no files matched under source",
               source=source_root, include=args.include, exclude=args.exclude)

    hid, hdir = _make_dir(project, args.id)

    try:
        records = build_manifest(source_root, items, args.hash)
    except OSError as exc:
        shutil.rmtree(hdir, ignore_errors=True)
        broke(f"could not hash source files: {exc}")
    write_manifest(os.path.join(hdir, "manifest.jsonl"), records, args.hash)

    spec = {"path": None, "hash": None, "version": args.spec_version}
    if args.spec:
        src = os.path.expanduser(args.spec)
        if not os.path.exists(src):
            shutil.rmtree(hdir, ignore_errors=True)
            broke(f"spec not found: {src}")
        dst = os.path.join(hdir, "spec", os.path.basename(src.rstrip("/")))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            spec["hash"] = None  # a directory has no single hash; the copy is the record
        else:
            shutil.copy2(src, dst)
            spec["hash"] = hash_file(dst, "sha256")
        spec["path"] = os.path.relpath(dst, hdir)

    total_bytes = sum(r["bytes"] for r in records)
    rec = {
        "handoff_id": hid,
        "project": os.path.basename(project.rstrip("/")),
        "stage": args.stage,
        # The join key to the data lifecycle. Without it a handoff and the
        # dataset it feeds are two records nothing can relate, and "is inflow
        # still in flight for this dataset" becomes unanswerable.
        "dataset": args.dataset,
        "kind": args.kind,
        "status": "sent",
        "round": 1 if not rework_of else _round_of(project, rework_of) + 1,
        "rework_of": rework_of,
        "to": args.to,
        "channel": args.channel,
        "channel_ref": args.channel_ref,
        "description": args.description,
        "spec": spec,
        "sent": {
            "source_root": source_root,
            "count": len(records),
            "bytes": total_bytes,
            "hash_algo": args.hash,
            "manifest": "manifest.jsonl",
            "include": args.include or [],
            "exclude": args.exclude or [],
        },
        "due_at": args.due,
        "latest": None,
        "rounds": [],
        "lineage": {"parents": args.parent or [], "consumed_by": []},
        "created_at": now_utc(),
        "returned_at": None,
        "closed_at": None,
        "outcome": None,
    }
    atomic_write_json(os.path.join(hdir, "handoff.json"), rec)

    emit({"handoff_id": hid, "dir": hdir, "count": len(records),
          "bytes": total_bytes, "hash_algo": args.hash,
          "spec_recorded": bool(args.spec), "round": rec["round"],
          "next": f"handoff.py receive --project {args.project} --id {hid} --returned <dir>"})


def _make_dir(project, explicit):
    """A handoff_id identifies exactly one handoff — the run_id uniqueness rule in
    run-mechanics.md "Record integrity", for the same reason: one-second
    resolution plus exist_ok=True lets two sends share a directory, and the second
    write destroys the first record."""
    if explicit:
        hdir = handoff_dir(project, explicit)
        try:
            os.makedirs(hdir, exist_ok=False)
        except FileExistsError:
            refuse(f"handoff {explicit} already exists", dir=hdir)
        return explicit, hdir
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for suffix in [""] + [f"_{i}" for i in range(2, 100)]:
        hid = f"handoff_{stamp}{suffix}"
        try:
            os.makedirs(handoff_dir(project, hid), exist_ok=False)
            return hid, handoff_dir(project, hid)
        except FileExistsError:
            continue
    broke("could not allocate a unique handoff_id")


def _round_of(project, handoff_id):
    return read_json(record_path(project, handoff_id)).get("round", 1)


def _deficit_items(project, base):
    """Items from a prior handoff that never came back, as source-relative paths.
    Read from the stored reconciliation, not recomputed — the deficit is what the
    reconciliation said it was at the time it was accepted or rejected."""
    latest = base.get("latest") or {}
    path = latest.get("reconciliation")
    if not path:
        return []
    full = os.path.join(handoff_dir(project, base["handoff_id"]), path)
    rec = read_json(full)
    return [m["item"] for m in rec.get("missing", [])]


# --------------------------------------------------------------------------- #
# receive — computes, never accepts
# --------------------------------------------------------------------------- #

def cmd_receive(args):
    project = os.path.expanduser(args.project)
    rpath = record_path(project, args.id)
    rec = read_json(rpath)
    hdir = handoff_dir(project, args.id)

    if rec.get("status") in TERMINAL:
        refuse(f"{args.id} is already {rec['status']}",
               hint="a closed handoff is a record; send a rework round instead "
                    "(`send --rework`) rather than re-reconciling this one")
    if args.match_by not in MATCH_BY:
        broke(f"unknown --match-by {args.match_by!r}", allowed=list(MATCH_BY))

    returned_root = os.path.expanduser(args.returned)
    if not os.path.isdir(returned_root):
        broke(f"returned is not a directory: {returned_root}")

    header, sent = read_manifest(os.path.join(hdir, "manifest.jsonl"))
    algo = header.get("hash_algo", rec.get("sent", {}).get("hash_algo", "sha256"))

    # Index the manifest by match key. Two sent items can collapse to one key
    # (a/1.jpg and b/1.jpg under --match-by stem); that makes the whole match
    # ambiguous and must be reported, not resolved by picking one.
    sent_by_key = {}
    for entry in sent:
        sent_by_key.setdefault(key_of(entry["item"], args.match_by), []).append(entry)

    returned_files = iter_files(returned_root, args.include or None, args.exclude or None)
    ret_by_key = {}
    for rel in returned_files:
        ret_by_key.setdefault(key_of(rel, args.match_by), []).append(rel)

    matched, ambiguous, missing, unexpected = [], [], [], []
    for k, entries in sent_by_key.items():
        got = ret_by_key.get(k)
        if not got:
            missing.extend({"item": e["item"], "key": k} for e in entries)
            continue
        if len(entries) > 1 or len(got) > 1:
            ambiguous.append({"key": k, "sent": [e["item"] for e in entries], "returned": got})
            continue
        matched.append({"item": entries[0]["item"], "key": k, "returned": got[0]})
    for k, got in ret_by_key.items():
        if k not in sent_by_key:
            unexpected.extend({"returned": g, "key": k} for g in got)

    # Did the local source change while the batch was out? A label set is bound to
    # the bytes it was produced from; if 000123.jpg was re-exported last Tuesday,
    # the returned label describes an image that no longer exists here. Nothing
    # downstream can detect this — the filename still matches.
    if args.skip_drift_check:
        # "not checked" and "checked, none found" are different facts. Recording
        # both as an empty list is the extraction-failure-vs-absence bug from
        # run-mechanics.md "Record integrity", one domain over.
        drift = None
    else:
        drift = _source_drift(rec["sent"]["source_root"], sent, algo)

    count_sent = len(sent)
    coverage = round(len(matched) / count_sent, 4) if count_sent else 0.0

    rnd = rec.get("round", 1)
    rel_out = os.path.join("rounds", f"round_{rnd}", "reconciliation.json")
    recon = {
        "handoff_id": args.id,
        "round": rnd,
        "reconciled_at": now_utc(),
        "returned_root": returned_root,
        "match_by": args.match_by,
        "hash_algo": algo,
        "counts": {
            "sent": count_sent,
            "matched": len(matched),
            "missing": len(missing),
            "unexpected": len(unexpected),
            "ambiguous_keys": len(ambiguous),
        },
        "coverage": coverage,
        "complete": len(missing) == 0 and len(ambiguous) == 0,
        "source_drift_checked": not args.skip_drift_check,
        "source_drift": drift,
        "missing": missing,
        "unexpected": unexpected,
        "ambiguous": ambiguous,
        "matched": matched if args.record_matched else [],
        "_matched_omitted": not args.record_matched,
    }
    atomic_write_json(os.path.join(hdir, rel_out), recon)

    rec["status"] = "returned"
    rec["returned_at"] = now_utc()
    rec["latest"] = {"round": rnd, "reconciliation": rel_out, "coverage": coverage,
                     "complete": recon["complete"],
                     "source_drift_checked": recon["source_drift_checked"],
                     "drift_count": None if drift is None else len(drift)}
    rec["rounds"] = [r for r in rec.get("rounds", []) if r.get("round") != rnd] + [rec["latest"]]
    atomic_write_json(rpath, rec)

    emit({"handoff_id": args.id, "round": rnd, "coverage": coverage,
          "counts": recon["counts"],
          "source_drift": "not_checked" if drift is None else len(drift),
          "complete": recon["complete"],
          "reconciliation": os.path.join(hdir, rel_out),
          "status": "returned",
          "note": "reconciled, NOT accepted — `close --accept` is a separate step"})


def _source_drift(source_root, sent, algo):
    out = []
    for entry in sent:
        full = os.path.join(source_root, entry["item"])
        if not os.path.exists(full):
            out.append({"item": entry["item"], "drift": "source_gone"})
            continue
        try:
            now = hash_file(full, algo)
        except OSError as exc:
            out.append({"item": entry["item"], "drift": "unreadable", "detail": str(exc)})
            continue
        if now != entry["hash"]:
            out.append({"item": entry["item"], "drift": "changed",
                        "at_send": entry["hash"], "now": now})
    return out


# --------------------------------------------------------------------------- #
# close — the accept/reject step
# --------------------------------------------------------------------------- #

def cmd_close(args):
    project = os.path.expanduser(args.project)
    rpath = record_path(project, args.id)
    rec = read_json(rpath)

    if rec.get("status") in TERMINAL:
        refuse(f"{args.id} is already {rec['status']}", closed_at=rec.get("closed_at"))

    if args.cancel:
        rec.update(status="cancelled", closed_at=now_utc(),
                   outcome=args.outcome or "cancelled before return")
        atomic_write_json(rpath, rec)
        return emit({"handoff_id": args.id, "status": "cancelled"})

    latest = rec.get("latest")
    if not latest:
        refuse(f"{args.id} has no reconciliation; nothing to accept or reject",
               status=rec.get("status"),
               fix="run `receive --returned <dir>` first")

    if args.reject:
        rec.update(status="rejected", closed_at=now_utc(),
                   outcome=args.outcome or f"rejected at coverage {latest['coverage']}")
        atomic_write_json(rpath, rec)
        return emit({"handoff_id": args.id, "status": "rejected",
                     "next": f"handoff.py send --rework {args.id} ... "
                             f"(carries the {1 - latest['coverage']:.0%} deficit forward)"})

    # Accepting. Two refusals, and neither is "are you sure" — each requires the
    # caller to restate a number the record already knows, so that accepting a
    # degraded batch is a thing somebody typed rather than a thing they clicked.
    if not latest["complete"]:
        if not args.accept_partial:
            refuse(f"coverage is {latest['coverage']}, not complete",
                   why="a partial return that closes as 'accepted' becomes a "
                       "full-coverage artifact in every downstream record",
                   fix=f"--accept-partial {latest['coverage']} to accept it as partial")
        if abs(float(args.accept_partial) - latest["coverage"]) > 1e-6:
            refuse(f"--accept-partial {args.accept_partial} does not match the "
                   f"computed coverage {latest['coverage']}",
                   why="the value is restated so acceptance is bound to the number "
                       "that was actually measured, not to one remembered from a "
                       "previous round")
    if latest.get("drift_count"):
        if not args.accept_drift:
            n = latest["drift_count"]
            refuse(f"{n} source file{'s' if n != 1 else ''} changed since send",
                   why="the returned work describes bytes that are no longer on "
                       "disk here; the pairing is broken and nothing downstream "
                       "can see it",
                   fix="--accept-drift to accept anyway, or re-send the drifted items")
    if not latest.get("source_drift_checked") and not args.accept_unchecked_drift:
        refuse("source drift was never checked for this round",
               why="'not checked' is not 'clean' — accepting here records an "
                   "unverified pairing as a verified one",
               fix="re-run `receive` without --skip-drift-check, or pass "
                   "--accept-unchecked-drift")

    rec.update(status="accepted", closed_at=now_utc(),
               outcome=args.outcome or f"accepted at coverage {latest['coverage']}")
    rec["accepted"] = {
        "coverage": latest["coverage"],
        "complete": latest["complete"],
        "partial": not latest["complete"],
        "source_drift_checked": latest.get("source_drift_checked"),
        "drift_count": latest.get("drift_count"),
        "round": latest["round"],
        "reconciliation": latest["reconciliation"],
        "location": args.location,
    }
    atomic_write_json(rpath, rec)
    emit({"handoff_id": args.id, "status": "accepted",
          "coverage": latest["coverage"], "partial": not latest["complete"],
          "cite_as": f"handoffs/{args.id}",
          "note": "downstream runs that consume this must list it in "
                  "run.json -> lineage.parents"})


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def cmd_status(args):
    records, errors = scan(args.project or args.workspace)
    rows = []
    for rec in records:
        if args.id and rec.get("handoff_id") != args.id:
            continue
        status = rec.get("status")
        open_ = status not in TERMINAL
        if args.open_only and not open_:
            continue
        age = age_days(rec.get("created_at"))
        due = rec.get("due_at")
        overdue = bool(due and open_ and due < now_utc())
        rows.append({
            "handoff_id": rec.get("handoff_id"),
            "project": rec.get("project"),
            "stage": rec.get("stage"),
            "dataset": rec.get("dataset"),
            "kind": rec.get("kind"),
            "status": status,
            "round": rec.get("round"),
            "to": rec.get("to"),
            "channel": rec.get("channel"),
            "count": (rec.get("sent") or {}).get("count"),
            "age_days": age,
            "due_at": due,
            "overdue": overdue,
            "stale": bool(open_ and age is not None and age >= args.stale_days),
            "coverage": (rec.get("latest") or {}).get("coverage"),
            "path": rec.get("_path"),
        })
    emit({"handoffs": rows,
          "open": sum(1 for r in rows if r["status"] not in TERMINAL),
          "stale": sum(1 for r in rows if r["stale"]),
          "overdue": sum(1 for r in rows if r["overdue"]),
          "stale_threshold_days": args.stale_days,
          "errors": errors})


def cmd_show(args):
    emit(read_json(record_path(os.path.expanduser(args.project), args.id)))


# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="freeze a manifest and open a handoff")
    s.add_argument("--project", required=True)
    s.add_argument("--source", required=True, help="directory whose contents go out")
    s.add_argument("--kind", required=True, choices=KINDS)
    # A key in resources.json -> outsourcing for anyone recurring, a plain name for a
    # one-off. Deliberately not validated here: this script never reads resources.json, so
    # contact details stay in the never-committed file and out of the git-tracked record.
    s.add_argument("--to", required=True, dest="to",
                   help="outsourcing key from resources.json, or a plain name for a one-off")
    s.add_argument("--stage", default=None, help="which stage this serves, if any")
    s.add_argument("--dataset", default=None,
                   help="dataset_id this feeds; lets /data see inflow in flight")
    s.add_argument("--channel", default="manual", help="how the bytes travel")
    s.add_argument("--channel-ref", default=None, help="link / ticket / thread")
    s.add_argument("--spec", default=None, help="guideline or acceptance criteria to snapshot")
    s.add_argument("--no-spec", action="store_true", help="record the absence deliberately")
    s.add_argument("--spec-version", default=None)
    s.add_argument("--description", default=None)
    s.add_argument("--due", default=None, help="ISO timestamp with offset")
    s.add_argument("--hash", default="sha256", choices=HASH_ALGOS)
    s.add_argument("--include", action="append")
    s.add_argument("--exclude", action="append")
    s.add_argument("--parent", action="append", help="lineage parent, e.g. training/run_...")
    s.add_argument("--rework", default=None, help="carry a prior handoff's deficit forward")
    s.add_argument("--id", default=None)
    s.set_defaults(fn=cmd_send)

    r = sub.add_parser("receive", help="reconcile a returned directory against the manifest")
    r.add_argument("--project", required=True)
    r.add_argument("--id", required=True)
    r.add_argument("--returned", required=True)
    r.add_argument("--match-by", default="stem", choices=MATCH_BY)
    r.add_argument("--include", action="append")
    r.add_argument("--exclude", action="append")
    r.add_argument("--skip-drift-check", action="store_true",
                   help="records source_drift as not-checked; it will block accept")
    r.add_argument("--record-matched", action="store_true",
                   help="write the full matched list (large for big batches)")
    r.set_defaults(fn=cmd_receive)

    c = sub.add_parser("close", help="accept / reject / cancel")
    c.add_argument("--project", required=True)
    c.add_argument("--id", required=True)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true")
    g.add_argument("--reject", action="store_true")
    g.add_argument("--cancel", action="store_true")
    c.add_argument("--accept-partial", default=None,
                   help="restate the measured coverage to accept an incomplete return")
    c.add_argument("--accept-drift", action="store_true")
    c.add_argument("--accept-unchecked-drift", action="store_true")
    c.add_argument("--location", default=None, help="where the accepted data now lives")
    c.add_argument("--outcome", default=None)
    c.set_defaults(fn=cmd_close)

    st = sub.add_parser("status", help="what is out, how long, what is overdue")
    st.add_argument("--project", default=None)
    st.add_argument("--workspace", default=None, help="scan every project under here")
    st.add_argument("--id", default=None)
    st.add_argument("--open-only", action="store_true")
    st.add_argument("--stale-days", type=float, default=14.0)
    st.set_defaults(fn=cmd_status)

    sh = sub.add_parser("show", help="print one handoff record")
    sh.add_argument("--project", required=True)
    sh.add_argument("--id", required=True)
    sh.set_defaults(fn=cmd_show)

    args = p.parse_args()
    if args.cmd == "status" and not (args.project or args.workspace):
        p.error("status needs --project or --workspace")
    args.fn(args)


if __name__ == "__main__":
    main()
