#!/usr/bin/env python3
"""Refuse a PRODUCTION launch while `provenance.json` still calls a value a guess.

THE HOLE THIS CLOSES. `provenance.json` is the one file in the repo whose job is to
say which values were *read* and which were *inferred* -- and both its own template
("Nothing here is required for a run to launch") and `/train-init` ("Not read by
`/train-run`") declare that nothing consumes it. So the honest record exists, is
written carefully, and has no reader. A production run launched on a guessed
`primary_metric` produces a `run.json` that states a metric as fact; `/conclude`
then cites that number, `baseline_delta` subtracts it, and every one of them is
correct about a value nobody ever read. Nothing raises at any step.

DEBUG IS EXACTLY WHERE A GUESS BELONGS, so debug always clears. The gate is the
boundary between "I am finding out whether this runs" and "this number is going
into the record."

THE FOUR STATUSES ARE NOT ONE THING (`provenance.json -> _comment_unresolved`):

  blocking    the config is EMPTY and the flow already stopped -- reaching a launch
              with one of these means something skipped Step 7
  guessed     a value IS filled in and was inferred, not read -- the dangerous one,
              because the config looks complete
  unverified  taken from a README or the author's word. CLAUDE.md: never let
              somebody's word become a checked fact. In a production record it
              becomes one
  absent      confirmed not to exist (no done signal is emitted at all). A
              CONCLUSION, not a gap -- it must never block, and treating it as one
              is how a correct record gets edited to make a gate pass

WHY IT WAIVES INSTEAD OF ONLY REFUSING. This runs unattended, and CLAUDE.md's
"File the question; do not block on it" applies: a hard refusal at 02:00 is a
deadlock, not a safeguard. `--waive KEY` clears one entry AND returns a stamp for
`run.json`, on `retire.py --waive cited_by_snapshot`'s shape -- the loss is
recorded rather than avoided, so the run says of itself that it trained on a guess.
A waiver nobody can see is just a flag.

Exit: 0 cleared · 1 refused (the script worked and the answer is no) · 2 broke.
"""
import argparse
import json
import os
import sys

BLOCK_STATUSES = ("blocking", "guessed", "unverified")
CLEAR_STATUSES = ("absent",)
KNOWN = BLOCK_STATUSES + CLEAR_STATUSES


def broke(msg, fix=None):
    out = {"ok": False, "error": msg}
    if fix:
        out["fix"] = fix
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    sys.exit(2)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("verb", choices=["check"])
    p.add_argument("--project", required=True)
    p.add_argument("--stage", default="training")
    p.add_argument("--mode", required=True, choices=["debug", "production"])
    p.add_argument("--waive", action="append", default=[],
                   help="a dotted `key` from unresolved[] to launch anyway; stamped into run.json")
    a = p.parse_args(argv)

    path = os.path.join(os.path.expanduser(a.project), "stages", a.stage, "provenance.json")
    if not os.path.isfile(path):
        # Absent is not clear and not refused: it is a third fact, and the stage
        # may predate the sidecar. Say which one it is rather than picking.
        json.dump({"cleared": a.mode == "debug", "mode": a.mode,
                   "provenance": "missing", "path": path,
                   "‼️": ("no `provenance.json` for this stage. That is not the same as "
                          "'nothing was guessed' -- it means nothing recorded what was. "
                          "A production launch on an unrecorded provenance is the case this "
                          "gate cannot rule on; run `/train-init` Step 7 or waive explicitly.")},
                  sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0 if a.mode == "debug" else 1

    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception as e:
        broke(f"{path} is not readable JSON: {e}",
              fix="fix the file; do NOT fall back and launch -- an unreadable provenance "
                  "is less informative than a missing one, and this gate must not guess")

    unresolved = rec.get("unresolved") or []
    if not isinstance(unresolved, list):
        broke("`unresolved` is not a list")

    waived, buckets, unknown = [], {s: [] for s in BLOCK_STATUSES}, []
    absent = []
    for e in unresolved:
        if not isinstance(e, dict):
            unknown.append({"entry": e, "why": "not an object"})
            continue
        key, status = e.get("key"), e.get("status")
        item = {"key": key, "status": status, "why": e.get("why")}
        if status not in KNOWN:
            # An unenumerated status must never fall through as clear.
            unknown.append(item)
        elif status in CLEAR_STATUSES:
            absent.append(item)
        elif key in a.waive:
            waived.append(item)
        else:
            buckets[status].append(item)

    offenders = [i for s in BLOCK_STATUSES for i in buckets[s]] + unknown
    cleared = a.mode == "debug" or not offenders

    out = {
        "cleared": cleared,
        "mode": a.mode,
        "source_mode": rec.get("source_mode") or None,
        "blocking": buckets["blocking"],
        "guessed": buckets["guessed"],
        "unverified": buckets["unverified"],
        "unknown_status": unknown,
        "absent_not_a_gap": len(absent),
        "waived": waived,
    }
    if waived:
        out["stamp"] = {"provenance_waived": [
            {"key": i["key"], "status": i["status"], "why": i["why"]} for i in waived]}
        out["‼️_stamp"] = ("write `stamp` into this run's `run.json` before launching. A waiver "
                           "that is not in the record is a flag, and the number this run "
                           "produces will be cited by `/conclude` as if it were read.")
    if a.mode == "debug" and offenders:
        out["note"] = (f"{len(offenders)} unresolved entr(ies) -- cleared because this is debug, "
                       "which is exactly where a guess belongs. The same launch in production "
                       "will refuse.")
    if not cleared:
        out["‼️"] = ("PRODUCTION refused. Two routes, and only these two: settle the entry in "
                     "`provenance.json` (that is `/train-init` Step 7), or `--waive <key>` and "
                     "write the returned stamp into `run.json`. Unattended, do neither silently: "
                     "`ask.py open --to <who> --asked \"is <key> right?\" --why \"production run "
                     "blocked\"` and carry on with what does not depend on it.")
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0 if cleared else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # a gate that crashes must not read as a pass
        broke(f"{type(e).__name__}: {e}")
