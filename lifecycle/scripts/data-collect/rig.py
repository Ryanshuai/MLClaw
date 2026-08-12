#!/usr/bin/env python3
"""What physically produced this data, and did any of it change without saying so.

Collect is the first box on the data lifecycle and the only one where MLClaw
records something it did not cause. The rig is somebody's hardware; the capture
code is theirs; the bytes land by their own tooling. What MLClaw owns is the
same thing it owns everywhere else — the record — and here the record is the
capture-side counterpart of `code_snapshot.py`. A run snapshots code and
packages because the same code on a different torch produces different numbers.
The same is true of the same procedure on a different rig, and far less visible.

The asymmetry that makes this worth code, generalized from a real hand-held
capture rig:

    a camera that was swapped for a dead one     -> will not connect -> found in seconds
    a camera that was swapped for a working one  -> connects, captures, and its factory
                                                    stereo baseline is different, so every
                                                    measurement downstream is off by a fixed
                                                    ratio, forever, with nothing raising

`on_change: breaks` is the first kind. `on_change: shifts` is the second, and it
is what a tripwire is for.

Two rules that came out of that rig and generalize past cameras:

  The tripwire is not the source of truth. Watch a cheap proxy (a serial
  number); read the expensive truth (the baseline) live, every session. Storing
  the baseline as a config value makes the anchor of every downstream
  measurement drift from the hardware silently. `runtime_only` enforces this.

  A fired tripwire warns; it does not block. `check` exits 1 because it is a
  question being asked. `stamp` never refuses, because the alternative to
  capturing with a changed rig is not capturing at all, and the frames in front
  of an operator cannot be re-shot. It records what fired, into the data.

Exit codes per CLAUDE.md "Script Integration": 1 = worked, the answer is no;
2 = broke, do it by hand.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402

ON_CHANGE = ("breaks", "shifts")
PROBE_TIMEOUT_S = 30


def load_rig(project, rig_id):
    path = os.path.join(os.path.expanduser(project), "rigs", rig_id, "rig.json")
    cfg = read_json(path)
    facts = {k: v for k, v in (cfg.get("facts") or {}).items()
             if not k.startswith("_")}
    if not facts:
        broke(f"{path} declares no facts")
    for name, f in facts.items():
        if f.get("on_change") not in ON_CHANGE:
            broke(f"fact {name!r} has on_change={f.get('on_change')!r}",
                  allowed=list(ON_CHANGE))
        # A runtime_only fact carrying a stored value is the failure this field
        # exists to prevent: the hardware moves on and the config does not, and
        # every measurement anchored to it is quietly wrong afterwards.
        if f.get("runtime_only") and f.get("value") not in (None, ""):
            refuse(f"fact {name!r} is runtime_only but carries a stored value",
                   why="a value read once and stored stops tracking the hardware; "
                       "the anchor of every downstream measurement then drifts "
                       "with nothing raising",
                   fix="clear `value` and keep the probe — it is read live each session")
    return cfg, facts, path


def run_probe(cmd):
    """-> (status, value_or_detail). Never raises: a probe is somebody else's
    command and its failure is a finding, not a crash."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8",
                           timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "timeout", f"probe exceeded {PROBE_TIMEOUT_S}s"
    except OSError as exc:
        return "error", str(exc)
    if p.returncode != 0:
        return "error", (p.stderr or p.stdout or "").strip()[:400] or \
            f"exit {p.returncode}"
    return "ok", p.stdout.strip()


def _assess_row(name, f, probe):
    """One fact's row: state + observed value, against the hardware if a
    probe actually ran."""
    row = {"fact": name, "on_change": f["on_change"],
           "runtime_only": bool(f.get("runtime_only")),
           "recorded": f.get("value"), "evidence": f.get("evidence"),
           "observed": None, "state": None}

    if not f.get("probe"):
        # No probe means no tripwire. For a `shifts` fact that is a stated
        # risk, and it has to read as one: the whole point of `shifts` is
        # that its change is invisible, so an unwatchable one is invisible
        # twice over.
        row["state"] = "unwatchable" if f["on_change"] == "shifts" else "no_probe"
    elif not probe:
        # "Not checked" and "checked, unchanged" are different facts, and
        # collapsing them is the extraction-failure-vs-absence bug from
        # run-mechanics.md "Record integrity".
        row["state"] = "not_checked"
    else:
        status, probe_out = run_probe(f["probe"])
        if status != "ok":
            row["state"] = "probe_failed"
            row["detail"] = probe_out
        elif f.get("runtime_only"):
            # Nothing to compare against by construction; the reading IS
            # the product, and it goes into the stamp.
            row["observed"] = probe_out
            row["state"] = "read"
        elif str(f.get("value")) == probe_out:
            row["observed"] = probe_out
            row["state"] = "match"
        else:
            row["observed"] = probe_out
            row["state"] = "CHANGED"
    return row


def assess(facts, *, probe):
    """Compare every probeable fact against what the hardware says now."""
    return [_assess_row(name, facts[name], probe) for name in sorted(facts)]


def summarize(rows):
    fired = [r for r in rows if r["state"] == "CHANGED" and r["on_change"] == "shifts"]
    broke_ = [r for r in rows if r["state"] == "CHANGED" and r["on_change"] == "breaks"]
    unwatchable = [r for r in rows if r["state"] == "unwatchable"]
    failed = [r for r in rows if r["state"] == "probe_failed"]
    unchecked = [r for r in rows if r["state"] == "not_checked"]
    return {
        "tripwires_fired": [r["fact"] for r in fired],
        "breaking_changes": [r["fact"] for r in broke_],
        "unwatchable_shifts": [r["fact"] for r in unwatchable],
        "probe_failures": [r["fact"] for r in failed],
        "not_checked": [r["fact"] for r in unchecked],
        "runtime_readings": {r["fact"]: r["observed"]
                             for r in rows if r["state"] == "read"},
    }


def cmd_check(a):
    cfg, facts, _ = load_rig(a.project, a.rig)
    rows = assess(facts, probe=not a.no_probe)
    s = summarize(rows)
    payload = {"rig_id": cfg.get("rig_id") or a.rig, "checked_at": now_utc(),
           "probed": not a.no_probe, "facts": rows, **s}

    if s["tripwires_fired"]:
        # Exit 1: this is a question being asked, and the answer is that
        # something changed which changes what the data means. It does not stop
        # a capture — `stamp` deliberately still writes — but a caller that
        # ignores a non-zero exit here is ignoring the only warning there is.
        payload["verdict"] = "TRIPWIRE"
        payload["why"] = ("a `shifts` fact changed: the capture will work and the "
                      "data will mean something different, with nothing else "
                      "raising anywhere downstream")
        emit(payload)
        sys.exit(1)
    payload["verdict"] = "ok" if not (s["probe_failures"] or s["not_checked"]) else "incomplete"
    emit(payload)


def cmd_stamp(a):
    """Write a per-session reading into the capture tree.

    Never refuses. Frames in front of an operator cannot be re-shot, so the
    alternative to stamping a changed rig is losing the capture — strictly
    worse than recording the change and moving on. What fired travels in the
    stamp, where the census and every downstream reader will find it.
    """
    cfg, facts, _ = load_rig(a.project, a.rig)
    into = os.path.expanduser(a.into)
    if not os.path.isdir(into):
        broke(f"capture root is not a directory: {into}")

    rows = assess(facts, probe=not a.no_probe)
    s = summarize(rows)
    stamp_cfg = cfg.get("stamp") or {}
    rel = os.path.join(stamp_cfg.get("path") or "_rig",
                       stamp_cfg.get("filename") or "rig_stamp.json")
    path = os.path.join(into, rel)
    if os.path.exists(path) and not a.overwrite:
        refuse(f"a stamp already exists at {path}",
               why="a capture tree describing two different rig readings cannot "
                   "say which one produced its data",
               fix="--overwrite only if this session genuinely re-stamped the same rig")

    stamp = {
        "rig_id": cfg.get("rig_id") or a.rig,
        "stamped_at": now_utc(),
        "session": a.session,
        "probed": not a.no_probe,
        "facts": rows,
        **s,
        # Denormalized so a reader of the data tree alone — after an rsync to a
        # machine that never heard of this project — sees the warning without
        # having to re-derive it from the fact rows.
        "verdict": "TRIPWIRE" if s["tripwires_fired"] else "ok",
    }
    atomic_write_json(path, stamp)
    emit({"stamped": path, "rig_id": stamp["rig_id"], "session": a.session,
          "verdict": stamp["verdict"], "tripwires_fired": s["tripwires_fired"],
          "runtime_readings": s["runtime_readings"],
          "note": "stamp never refuses — a capture is not blocked by a changed rig, "
                  "it is recorded as one"})


def cmd_show(a):
    cfg, facts, path = load_rig(a.project, a.rig)
    emit({"path": path, "rig_id": cfg.get("rig_id") or a.rig,
          "facts": len(facts),
          "shifts": [k for k, f in facts.items() if f["on_change"] == "shifts"],
          "unwatchable_shifts": [k for k, f in facts.items()
                                 if f["on_change"] == "shifts" and not f.get("probe")],
          "runtime_only": [k for k, f in facts.items() if f.get("runtime_only")],
          "no_evidence": [k for k, f in facts.items() if not f.get("evidence")]})


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="probe the hardware; exit 1 if a tripwire fired")
    c.add_argument("--project", required=True)
    c.add_argument("--rig", required=True)
    c.add_argument("--no-probe", action="store_true",
                   help="records every fact as not_checked; never reads as unchanged")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("stamp", help="write a session reading into the capture tree")
    s.add_argument("--project", required=True)
    s.add_argument("--rig", required=True)
    s.add_argument("--into", required=True, help="capture root for this session")
    s.add_argument("--session", default=None, help="session label, e.g. the capture date")
    s.add_argument("--no-probe", action="store_true")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(fn=cmd_stamp)

    sh = sub.add_parser("show", help="read back the contract; touches no hardware")
    sh.add_argument("--project", required=True)
    sh.add_argument("--rig", required=True)
    sh.set_defaults(fn=cmd_show)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
