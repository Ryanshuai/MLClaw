#!/usr/bin/env python3
"""curate.py — what a derived dataset is actually made of.

Third box on the data line, and the only one that is run-shaped: convert,
split, dedup, relabel, sample. Everything else on the line describes a state
that can be re-observed by scanning again. A derivation cannot be re-observed —
once `boxes_v2` exists on disk it looks exactly like data somebody captured, and
the fact that it is a 30% sample of `boxes` with the blurry scenes dropped lives
nowhere except in whoever's memory.

So this script does not transform anything. It owns the record:

  plan      declare the derivation BEFORE the compute, and refuse the ones that
            should not happen — deriving off data that is one disk from gone,
            writing the output inside its own input, reusing a dataset id
  register  after the run, write the derived dataset's layout contract with
            `derived_from` filled in — verified against the RUN'S OWN RECORD,
            not against what the operator says it did
  trace     walk the chain back to captured roots. A record nothing reads is a
            record nobody maintains

WHY IT REUSES `run.json` INSTEAD OF INVENTING A RUN. A curate job has inputs,
outputs, code, params and a reproduction story — that is a run, and MLClaw
already has code snapshots, env capture, param injection and lineage for one.
Curate is the cheap box precisely because it needs no new primitives. This
script therefore executes nothing: the user runs their transform through the
ordinary run machinery, and `register` checks that run's record.

CLAIM VERSUS VERIFIED, same vocabulary as `/ask-human`. `provenance: "run"` means
a completed run cited these parents in its own `lineage.parents`. `"claimed"`
means somebody said so — recorded, because a degraded honest record beats a
refusal that pushes the work outside the tool, but never spelled the same way.

Exit codes per CLAUDE.md "Script Integration": 0 ok; 1 = the script worked and
the answer is no; 2 = the script broke, do it by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402
from _dataset_paths import dataset_dir  # noqa: E402

# What a derivation can be. Free text would make `op` unqueryable and the list
# unarguable; these five cover every transform that has come up, and adding a
# sixth should be a decision somebody makes on purpose.
OPS = ("convert", "split", "dedup", "relabel", "sample", "merge")

# The two ways a derivation can be known. Never collapse them — see the module
# docstring. `run` is checked against a record; `claimed` is somebody's word.
PROVENANCE = ("run", "claimed")

CITE = re.compile(r"^datasets/(?P<ds>[^@/]+)@(?P<snap>[^@/]+)$")

# The half of a layout contract that describes the DATA rather than the
# machines. `--like` copies exactly this and nothing else: a derived dataset
# usually has the same unit shape and the same layers as its parent, and never
# has the same locations, because it is somewhere new by construction.
INHERITED = ("identity", "layers", "completeness", "replication")


def parse_cite(spec: str) -> tuple[str, str]:
    """`datasets/<id>@<snapshot>` -> (id, snapshot).

    A bare dataset id is refused rather than defaulted to its newest snapshot.
    A dataset grows; "derived from boxes" names no particular afternoon, and a
    parent edge that cannot say which one is not lineage — the same rule the
    consuming side follows in `run.json -> lineage.parents`.
    """
    m = CITE.match(spec)
    if not m:
        broke(f"--from must be datasets/<id>@<snapshot>, got {spec!r}",
              why="a bare dataset id names no particular version of a growing "
                  "set, so it cannot be a parent edge")
    return m.group("ds"), m.group("snap")


def under(child: str, parent: str) -> bool:
    """Is `child` at or inside `parent`? Both realpath'd, so a symlink into a
    source tree cannot smuggle an in-place write past the containment check."""
    c = os.path.realpath(os.path.expanduser(child))
    p = os.path.realpath(os.path.expanduser(parent))
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #

def run_gate(project, dataset, acknowledge) -> dict | None:
    """Ask `/data`'s gate whether this dataset may be curated off.

    Shelling out rather than reimplementing: the gate's rules live in phase.py
    and a second copy here would drift, at which point two parts of MLClaw
    disagree about whether it is safe to derive from data that exists on one
    disk. Returns the refusal payload, or None when it passed.
    """
    gate = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "phase.py")
    if not os.path.isfile(gate):
        return {"refused": "phase.py is not present, so the curate gate could "
                           "not be evaluated", "unchecked": True}
    cmd = [sys.executable, gate, "gate", "--project", project,
           "--dataset", dataset, "--to", "curate"]
    if acknowledge:
        cmd += ["--acknowledge", str(acknowledge)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if p.returncode == 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        # The gate broke rather than refused. "Not checked" is not "clean":
        # returning None here would let a curate plan through on the strength
        # of a crash.
        return {"refused": "the curate gate could not be evaluated",
                "unchecked": True, "stderr": (p.stderr or "").strip()[-400:]}


def cmd_plan(a) -> None:
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")

    parents, sources = [], []
    for spec in a.from_:
        ds, snap = parse_cite(spec)
        sdir = os.path.join(dataset_dir(project, ds), "snapshots", snap)
        if not os.path.isfile(os.path.join(sdir, "snapshot.json")):
            refuse(f"no snapshot {snap!r} for dataset {ds!r}",
                   why="a derivation cites a frozen set; freezing is what makes "
                       "the parent edge mean something a year from now",
                   fix=f"/data-freeze, then cite datasets/{ds}@<snapshot_id>")
        snap_rec = read_json(os.path.join(sdir, "snapshot.json"))
        cfg = read_json(os.path.join(dataset_dir(project, ds), "dataset.json"))
        parents.append(spec)
        sources.append({"dataset": ds, "snapshot": snap,
                        "units": (snap_rec.get("selection") or {}).get("count"),
                        "unverified_units": len(snap_rec.get("unverified_units") or []),
                        "locations": cfg.get("locations") or []})

    # --- identity is never reused. Same rule as a snapshot id: a dataset that
    # changed under an existing name makes every record citing it describe
    # something that no longer exists in that shape.
    out_cfg = os.path.join(dataset_dir(project, a.to), "dataset.json")
    if os.path.exists(out_cfg):
        refuse(f"dataset {a.to!r} already exists",
               path=out_cfg,
               why="a derived dataset is a new identity, not a new version of "
                   "an old one — runs already cite the old id")

    # --- the output must not land inside its own input. In-place transforms are
    # the one curate failure with no undo: the parent's frozen snapshots keep
    # naming units whose bytes have been rewritten underneath them, and every
    # citation still resolves. Checked both directions — a source root inside
    # the output root is the same accident wearing different clothes.
    into = os.path.realpath(os.path.expanduser(a.into))
    for s in sources:
        for loc in s["locations"]:
            root = loc.get("root")
            if not root:
                continue
            if under(into, root) or under(root, into):
                refuse(f"--into {a.into} overlaps location {loc['key']!r} of "
                       f"source dataset {s['dataset']!r} ({root})",
                       why="curate produces a NEW dataset; writing inside the "
                           "input rewrites bytes that frozen snapshots still "
                           "name, and every one of those citations goes on "
                           "resolving",
                       fix="pick an output root outside every source location")

    # --- the gate. One call per distinct source dataset.
    gates = {}
    for ds in sorted({s["dataset"] for s in sources}):
        hit = run_gate(project, ds, a.acknowledge)
        if hit is not None:
            refuse(f"the curate gate refused for source dataset {ds!r}",
                   gate=hit,
                   fix="fix the blocker, or pass --acknowledge <n> naming what "
                       "is being accepted in the same breath")
        gates[ds] = "passed"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    plan = {
        "plan_id": f"plan_{stamp}",
        "project": project,
        "produces": a.to,
        "op": a.op,
        "parents": parents,
        "sources": [{k: v for k, v in s.items() if k != "locations"} for s in sources],
        "into": into,
        "at": a.at,
        "like": a.like,
        "note": a.note,
        "gate": gates,
        "acknowledged": a.acknowledge,
        "planned_at": now_utc(),
        # Not a suggestion. `register` refuses a run that does not carry these
        # strings, because a parent edge asserted by the person registering it
        # is the person's word, and the run's own record is evidence.
        "the_run_must_cite": parents,
    }
    path = os.path.join(dataset_dir(project, a.to), "curate", f"{plan['plan_id']}.json")
    atomic_write_json(path, plan)
    plan["_path"] = path
    emit(plan)


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #

def find_run(project, ref) -> tuple[dict, str]:
    """`<stage>/<run_id>` -> (run record, canonical ref)."""
    if "/" not in ref:
        broke(f"--run must be <stage>/<run_id>, got {ref!r}")
    stage, run_id = ref.split("/", 1)
    path = os.path.join(project, "stages", stage, "runs", run_id, "run.json")
    rec = read_json(path, required=False)
    if rec is None:
        refuse(f"no run record at stages/{stage}/runs/{run_id}/run.json",
               why="the run record is the evidence that this derivation "
                   "happened; without it the parent edge is somebody's word",
               fix="point at the real run, or use --claimed --because '<why>' "
                   "to record it honestly as unverified")
    return rec, f"{stage}/{run_id}"


def cmd_register(a) -> None:
    project = os.path.expanduser(a.project)
    plan = read_json(os.path.expanduser(a.plan))
    target = plan["produces"]

    out_cfg = os.path.join(dataset_dir(project, target), "dataset.json")
    if os.path.exists(out_cfg):
        # Retrying a register that went wrong is legitimate; replacing a
        # dataset something has already looked at or cited is not. A census or a
        # snapshot is the evidence that the identity has left this command —
        # after either, re-registering makes existing records describe data that
        # no longer exists in that shape.
        d = dataset_dir(project, target)
        witnessed = [w for w, sub in (("census", "census"), ("snapshot", "snapshots"))
                     if os.path.isdir(os.path.join(d, sub))
                     and os.listdir(os.path.join(d, sub))]
        if witnessed:
            refuse(f"dataset {target!r} already has a {' and a '.join(witnessed)}",
                   path=out_cfg,
                   why="the identity has been observed or cited; replacing its "
                       "contract now makes those records describe something else")
        if not a.re_register:
            refuse(f"dataset {target!r} already has a dataset.json",
                   path=out_cfg,
                   why="registering over it would silently replace one "
                       "derivation record with another",
                   fix="--re-register if the previous attempt was wrong — "
                       "nothing has censused or cited this id yet")

    if a.claimed:
        if not a.because:
            refuse("--claimed requires --because '<why there is no run>'",
                   why="an unverified derivation is a legitimate record; an "
                       "unverified derivation with no reason given is a hole "
                       "nobody can evaluate later")
        prov, run_ref, run_status = "claimed", None, None
    else:
        rec, run_ref = find_run(project, a.run)
        run_status = rec.get("status")
        # --- a run that did not finish did not produce this dataset. The output
        # directory exists either way, which is the whole problem: a crashed
        # conversion leaves a partial tree that reads as a complete dataset in
        # every record that follows. Same shape as CLAUDE.md "Never say a unit
        # is complete because its directory exists", one level up.
        if run_status != "completed":
            refuse(f"run {run_ref} has status {run_status!r}, not 'completed'",
                   why="a partial output tree is indistinguishable from a whole "
                       "one once it is registered as a dataset",
                   fix="finish or rerun it, or register --claimed --because ...")
        cited = [p for p in (rec.get("lineage") or {}).get("parents") or []
                 if isinstance(p, str)]
        missing = [p for p in plan["parents"] if p not in cited]
        if missing:
            refuse(f"run {run_ref} does not cite {len(missing)} of the plan's "
                   f"parents in lineage.parents",
                   missing=missing, run_cites=cited,
                   why="CLAUDE.md 'Never let somebody's word become a checked "
                       "fact' — the run's own record is what makes this edge "
                       "evidence rather than an assertion",
                   fix="tag_lineage.py the run, or register --claimed")
        prov = "run"

    if a.declare:
        cfg = read_json(os.path.expanduser(a.declare))
    else:
        like = a.like or plan.get("like") or parse_cite(plan["parents"][0])[0]
        parent_cfg = read_json(os.path.join(dataset_dir(project, like), "dataset.json"),
                               required=False)
        if parent_cfg is None:
            refuse(f"cannot inherit a layout contract: no dataset.json for "
                   f"{like!r}",
                   fix="--declare <file> with the new dataset's own contract, "
                       "or --like <an existing dataset>")
        cfg = {k: parent_cfg[k] for k in INHERITED if k in parent_cfg}
        cfg["description"] = a.note or plan.get("note")
        # Locations are never inherited: the derived data is somewhere new by
        # construction, and copying the parent's roots would declare the output
        # to be at machines that have never held it.
        cfg["locations"] = [{
            "key": plan.get("at") or "local",
            "role": "authority", "via": "local", "server": None,
            "root": plan["into"], "has_layers": None,
            "note": f"written by {run_ref or 'an unrecorded process'}",
        }]
        cfg["consumers"] = []

    cfg["dataset_id"] = target
    cfg["project"] = project
    cfg["created_at"] = cfg.get("created_at") or now_utc()
    cfg["updated_at"] = now_utc()
    cfg["derived_from"] = {
        "provenance": prov,
        "parents": plan["parents"],
        "op": plan["op"],
        "run": run_ref,
        "run_status": run_status,
        "plan": os.path.relpath(os.path.expanduser(a.plan), project),
        "because": a.because,
        "note": a.note or plan.get("note"),
        "recorded_at": now_utc(),
    }

    atomic_write_json(out_cfg, cfg)
    emit({"registered": target, "path": out_cfg,
          "derived_from": cfg["derived_from"],
          "next": "census.py scan — a registered dataset has no census yet, and "
                  "until it does nothing may freeze or train off it"})


# --------------------------------------------------------------------------- #
# trace
# --------------------------------------------------------------------------- #

def cmd_trace(a) -> None:
    project = os.path.expanduser(a.project)
    chain, seen = [], []

    def walk(dataset, depth):
        if dataset in seen:
            refuse(f"derivation cycle at {dataset!r}", chain=seen + [dataset],
                   why="a dataset cannot be its own ancestor; one of these "
                       "derived_from records is wrong")
        seen.append(dataset)
        cfg = read_json(os.path.join(dataset_dir(project, dataset), "dataset.json"),
                        required=False)
        if cfg is None and depth == 0:
            # At the root the user named something that is not here — bad input,
            # not a gap in the records. Reporting it as an `unknown_link` would
            # dress a typo up as a finding about the data.
            broke(f"no dataset {dataset!r} in this project")
        if cfg is None:
            # Absence of a record and absence of a parent are different facts.
            # Truncating the chain here would report a derived dataset as a
            # captured root — run-mechanics.md "Record integrity".
            chain.append({"dataset": dataset, "depth": depth, "origin": "unknown",
                          "detail": "no dataset.json in this project — the chain "
                                    "continues somewhere this project cannot see"})
            return
        df = cfg.get("derived_from")
        if not df:
            chain.append({"dataset": dataset, "depth": depth, "origin": "captured",
                          "detail": "no derived_from — this is where the data "
                                    "entered the world"})
            return
        chain.append({"dataset": dataset, "depth": depth, "origin": "derived",
                      "provenance": df.get("provenance"), "op": df.get("op"),
                      "run": df.get("run"), "parents": df.get("parents"),
                      "because": df.get("because")})
        for p in df.get("parents") or []:
            m = CITE.match(p)
            walk(m.group("ds") if m else p, depth + 1)

    walk(a.dataset, 0)
    unverified = [c for c in chain if c.get("provenance") == "claimed"]
    unknown = [c for c in chain if c["origin"] == "unknown"]
    emit({
        "dataset": a.dataset,
        "chain": chain,
        "roots": [c["dataset"] for c in chain if c["origin"] == "captured"],
        "claimed_links": [c["dataset"] for c in unverified],
        "unknown_links": [c["dataset"] for c in unknown],
        # Said out loud, because the chain reads as authoritative either way and
        # a reader who is not told will not ask.
        "trustworthy": not unverified and not unknown,
    })


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("plan", help="declare a derivation, and refuse the bad ones")
    s.add_argument("--project", required=True)
    s.add_argument("--from", dest="from_", required=True, action="append",
                   metavar="datasets/<id>@<snapshot>",
                   help="a frozen parent; repeatable for --op merge")
    s.add_argument("--to", required=True, help="the new dataset id")
    s.add_argument("--op", required=True, choices=OPS)
    s.add_argument("--into", required=True, help="output root; must not overlap any source")
    s.add_argument("--at", default=None, help="location key for the output root")
    s.add_argument("--like", default=None,
                   help="dataset whose layout contract the output inherits "
                        "(default: the first parent)")
    s.add_argument("--note", default=None)
    s.add_argument("--acknowledge", default=None,
                   help="restate the gate's blocker count to proceed anyway")
    s.set_defaults(fn=cmd_plan)

    r = sub.add_parser("register", help="write the derived dataset's contract")
    r.add_argument("--project", required=True)
    r.add_argument("--plan", required=True)
    r.add_argument("--run", default=None, metavar="<stage>/<run_id>")
    r.add_argument("--claimed", action="store_true",
                   help="no run record exists; file the edge as unverified")
    r.add_argument("--because", default=None, help="required with --claimed")
    r.add_argument("--like", default=None)
    r.add_argument("--declare", default=None,
                   help="a full dataset.json when the output's shape differs")
    r.add_argument("--note", default=None)
    r.add_argument("--re-register", action="store_true",
                   help="redo a register that went wrong; refused once anything "
                        "has censused or cited the id")
    r.set_defaults(fn=cmd_register)

    t = sub.add_parser("trace", help="walk the derivation chain to captured roots")
    t.add_argument("--project", required=True)
    t.add_argument("--dataset", required=True)
    t.set_defaults(fn=cmd_trace)

    a = p.parse_args()
    if a.cmd == "register" and not a.claimed and not a.run:
        broke("register needs --run <stage>/<run_id>, or --claimed --because ...")
    if a.cmd == "register" and a.claimed and a.run:
        broke("--run and --claimed are mutually exclusive: a derivation is "
              "either checked against a run record or it is not")
    a.fn(a)


if __name__ == "__main__":
    main()
