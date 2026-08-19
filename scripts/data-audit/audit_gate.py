#!/usr/bin/env python3
"""Refuse a PRODUCTION run whose data carries an unanswered audit fatal.

THE HOLE THIS CLOSES. `/data-audit` exists to find the wrong label BEFORE a run,
where `/eval-triage` can only find it THROUGH one. It does that -- and then nothing
stops the run. The audit writes `[FATAL] compatibility : class list has 81 entries,
model config says 80`, routes the finding correctly, and the training launches
anyway on data whose category ids the dataloader will silently clamp. The most
expensive waste on the data line, and the missing piece is one check on the
consumer's side.

FIVE STATES, NOT TWO. `/data-audit` Step 7 states the rule this turns on: *"A
skipped check is a recorded field, never an absent one -- an audit missing its
compatibility section reads identically to one that passed it."* So:

  clean          every declared layer came back WARN/INFO or better
  fatal          a layer came back FATAL. Refuse
  never_audited  no audit at all. NOT clean -- nothing looked
  unverifiable   the layer that matters came back SKIP (Step 2 has no consuming
                 code, integrity sampled 500/5240). Nothing looked at that layer,
                 which is the `never_audited` fact per-layer
  stale          the audit judged an older snapshot than the one this run cites.
                 A verdict about different bytes

`unverifiable` and `never_audited` are deliberately NOT collapsed into `fatal`, and
neither is treated as clean: three facts, the same split `census.py` draws between
a machine that did not answer, a path that is not there, and a directory that is
genuinely empty.

RESOLVING WHICH DATA. `input.json -> candidates` entries whose `location` is
`dataset:<id>@<snapshot>` are citations, and they are what a run consumes. When
nothing resolves, the answer is `unresolved` -- not clear. A run reading a raw
directory path is exactly the case an audit cannot have covered.

Exit: 0 cleared · 1 refused (worked, the answer is no) · 2 broke.
"""
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import broke  # noqa: E402

FATAL, SKIP = "FATAL", "SKIP"
OK_VERDICTS = ("WARN", "INFO", "PASS", "OK")
CITATION = re.compile(r"^dataset:(?P<id>[^@\s]+)@(?P<snap>\S+)$")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        broke(f"{path} is not readable JSON: {e}",
              fix="fix the file. An unreadable audit must never read as a clean one.")


def cited_datasets(project, stage):
    """-> [(id, snapshot)] this stage is configured to consume."""
    rec = read_json(os.path.join(project, "stages", stage, "input.json"))
    if not rec:
        return []
    found = []
    cands = (rec.get("candidates") or {}).get("items") or {}
    for entries in cands.values():
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            m = CITATION.match(str(e.get("location") or ""))
            if m:
                found.append((m.group("id"), m.group("snap")))
                continue
            r = e.get("resolve") or {}
            if r.get("dataset") and r.get("snapshot"):
                found.append((str(r["dataset"]), str(r["snapshot"])))
    return sorted(set(found))


def layer_verdicts(audit):
    """The per-layer verdict map, from whichever key holds it.

    `/data-audit` has no script and therefore no template; its Step 7 prints
    `[FATAL] compatibility` and this reads that same vocabulary. An audit with no
    readable verdict map is `unreadable` -- never clean.
    """
    for key in ("layers", "verdicts", "sections"):
        block = audit.get(key)
        if isinstance(block, dict) and block:
            out = {}
            for layer, v in block.items():
                if isinstance(v, dict):
                    v = v.get("verdict")
                if v is not None:
                    out[str(layer)] = str(v).strip().upper()
            if out:
                return out
    return None


def judge(project, ds, snap, require_layers):
    d = os.path.join(project, "datasets", ds, "audits")
    audits = sorted(glob.glob(os.path.join(d, "*", "audit.json")))
    if not audits:
        return {"dataset": ds, "snapshot": snap, "state": "never_audited",
                "why": "no audit.json under datasets/%s/audits/. Nothing looked -- "
                       "which is not the same as nothing being wrong." % ds}
    dated = []
    for p in audits:
        rec = read_json(p) or {}
        dated.append((str(rec.get("audited_at") or ""), p, rec))
    dated.sort()
    at, path, rec = dated[-1]
    verdicts = layer_verdicts(rec)
    audit_id = os.path.basename(os.path.dirname(path))
    base = {"dataset": ds, "snapshot": snap, "audit_id": audit_id, "audited_at": at or None}
    if verdicts is None:
        return dict(base, state="unreadable",
                    why="the audit records no per-layer verdict map (`layers`), so it "
                        "cannot be told from one that passed. /data-audit Step 7.")
    judged = str(rec.get("snapshot") or (rec.get("references") or {}).get("snapshot") or "")
    fatal = sorted(k for k, v in verdicts.items() if v == FATAL)
    skipped = sorted(k for k, v in verdicts.items() if v == SKIP)
    missing = sorted(set(require_layers) - set(verdicts))
    if fatal:
        return dict(base, state="fatal", layers=verdicts, fatal_layers=fatal,
                    why="a layer came back FATAL: " + ", ".join(fatal))
    if judged and snap and judged != snap:
        return dict(base, state="stale", layers=verdicts, judged_snapshot=judged,
                    why=f"the audit judged @{judged}; this run cites @{snap}. A verdict "
                        "about different bytes.")
    blind = sorted(set(skipped) | set(missing))
    if blind:
        return dict(base, state="unverifiable", layers=verdicts, blind_layers=blind,
                    why="nothing looked at: " + ", ".join(blind) +
                        ". A SKIP is a recorded field, not a pass.")
    return dict(base, state="clean", layers=verdicts)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("verb", choices=["check"])
    p.add_argument("--project", required=True)
    p.add_argument("--stage", default="training")
    p.add_argument("--mode", required=True, choices=["debug", "production"])
    p.add_argument("--dataset", action="append", default=[],
                   help="<id>@<snapshot>; repeatable. Omit to resolve from input.json -> candidates")
    p.add_argument("--require-layer", action="append", default=["integrity", "compatibility"],
                   help="layers whose absence counts as blind (default: the two fatal ones)")
    p.add_argument("--waive", action="append", default=[],
                   help="a dataset id to launch anyway; stamped into run.json")
    a = p.parse_args(argv)
    project = os.path.expanduser(a.project)

    if a.dataset:
        targets = []
        for d in a.dataset:
            ds, _, snap = d.partition("@")
            targets.append((ds, snap))
        origin = "--dataset"
    else:
        targets = cited_datasets(project, a.stage)
        origin = f"stages/{a.stage}/input.json -> candidates"

    if not targets:
        out = {"cleared": a.mode == "debug", "mode": a.mode, "datasets": "unresolved",
               "resolved_from": origin,
               "‼️": ("no `dataset:<id>@<snapshot>` citation resolves for this stage. That is not "
                      "'no data' -- it means this run reads a path rather than a frozen membership "
                      "set, so no audit can have covered what it will actually open. Cite a "
                      "snapshot (`/data-freeze`) or pass --dataset explicitly.")}
        json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0 if a.mode == "debug" else 1

    rulings = [judge(project, ds, snap, a.require_layer) for ds, snap in targets]
    waived = [r for r in rulings if r["dataset"] in a.waive and r["state"] != "clean"]
    offenders = [r for r in rulings
                 if r["state"] != "clean" and r["dataset"] not in a.waive]
    cleared = a.mode == "debug" or not offenders

    out = {"cleared": cleared, "mode": a.mode, "resolved_from": origin,
           "rulings": rulings,
           "refused_on": [{"dataset": r["dataset"], "state": r["state"], "why": r["why"]}
                          for r in offenders],
           "waived": [{"dataset": r["dataset"], "state": r["state"]} for r in waived]}
    if waived:
        out["stamp"] = {"audit_waived": [
            {"dataset": r["dataset"], "state": r["state"], "audit_id": r.get("audit_id"),
             "why": r["why"]} for r in waived]}
        out["‼️_stamp"] = ("write `stamp` into this run's `run.json` before launching. Every number "
                           "this run produces was measured on data whose audit said this, and "
                           "nothing downstream can learn it any other way.")
    if a.mode == "debug" and offenders:
        out["note"] = (f"{len(offenders)} dataset(s) would refuse in production -- cleared because "
                       "this is debug. Debug on known-bad data is legitimate; recording a number "
                       "from it is not.")
    if not cleared:
        out["‼️"] = ("PRODUCTION refused. Route by state, never generically: `fatal` -> what the "
                     "audit's own suggestion said (a /data-label rework, a /data-curate conversion, "
                     "or /data-freeze for a corrected snapshot -- never fix a label in place). "
                     "`never_audited` / `unverifiable` -> run /data-audit, or the missing layer of "
                     "it. `stale` -> re-audit the snapshot this run actually cites. `--waive <id>` "
                     "launches and stamps it into run.json.")
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0 if cleared else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        broke(f"{type(e).__name__}: {e}")
