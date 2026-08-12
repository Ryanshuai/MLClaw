#!/usr/bin/env python3
"""online.py — a dated reading of the live input stream.

The production side of a drift comparison. `/data-freeze` already pins the
reference side: `datasets/<id>@<snap>` is exactly which units trained the model,
citable and reproducible. Every drift tool in existence fakes that half with a
CSV somebody exported; here it is real, and this is the half that was missing.

  declare  the online contract, once: where live inputs land, how a window
           expands into places to look, and how a production id maps onto a
           dataset unit id. Written into `dataset.json -> online`
  sample   take one reading. Uniform, always. Writes a dated observation
  status   readings on record. No network

WHY THE POLICY IS FIXED. Sampling serves two purposes with opposite policies:
drift needs a uniform draw or it measures the filter instead of the world;
retraining wants a biased one — low-confidence, rule-flagged, complained-about —
or it spends the annotation budget on easy frames. One mechanism cannot do both,
and using a biased sample as a drift window is a silent wrong answer. So this
script has no policy flag. The biased pull is `/data-collect`, which brings bytes
and cites a reading from here as its DENOMINATOR: without one, 500 hard frames
came out of an unknown number and the bias is not computable.

WHY IT DECLARES INSTEAD OF ASSUMING. Nothing here knows that production data is
date-partitioned, that a unit is a directory, or that ids look like anything in
particular. Those are facts about somebody's business, and a script that assumed
them would work at one company. The window->locations expansion is declared; the
unit identity is REUSED from `dataset.json -> identity`, so the online and
offline sides count the same thing; and `--units-from` is a first-class input for
every layout neither covers — your tooling lists it, this records it.

WHAT IT CANNOT DO, said plainly: a reading can never be retaken. A census
re-scanned next month gives a different answer and both are true of their date.
A window re-read next month gives nothing — the traffic rolled off, and the model
that was answering has been replaced.

Exit codes per CLAUDE.md -> "Script Integration": 0 ok; 1 = the script worked and
the answer is no; 2 = the script broke, do it by hand.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402
from _dataset_paths import dataset_dir  # noqa: E402

# How a time window becomes places to look. Three, and the third is why this
# stays abstract: whatever a project's layout is, its own tooling can enumerate
# it and this records the result rather than reimplementing it.
PARTITIONS = ("strftime", "flat", "external")

# Sentinels for the listing, same discipline as census.py: both a non-zero exit
# and a missing sentinel are checked, because either alone lets a shell that died
# mid-loop pass as a complete listing.
OK = "__MLCLAW_OK__"
NOPREFIX = "__MLCLAW_NOPREFIX__"


def load_declared_dataset(project, dataset):
    """Read a dataset's declaration -- **existence only, no layout validation.**

    `census.py -> load_layout_contract` shares this signature and return shape
    and additionally refuses an empty `unit_glob`, a layer without a marker, and
    a label that would corrupt its own output. This one refuses none of that: a
    reading only needs the unit identity both sides count by. The names are
    different so a caller cannot inherit guarantees it did not get.
    """
    p = os.path.join(dataset_dir(project, dataset), "dataset.json")
    cfg = read_json(p, required=False)
    if cfg is None:
        refuse(f"{dataset} is not declared",
               why="a reading is compared against this dataset's frozen "
                   "snapshot, and its unit identity is what both sides count",
               fix="/data-check declare")
    return cfg, p


def parse_instant(s: str, *, what: str) -> datetime:
    """An ISO timestamp that MUST carry a UTC offset.

    A timestamp without one is not an instant, it is an instant in an unnamed
    zone. Two readings written in different zones cannot be ordered, and a trend
    line over them is arithmetic on incomparable numbers — the same mistake
    `/data` reports as `staleness_undetermined` one domain over.
    """
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        broke(f"--{what} is not an ISO timestamp: {s!r}",
              example="2026-08-01T00:00:00+08:00")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        refuse(f"--{what} has no UTC offset: {s!r}",
               why="a bare local timestamp cannot be ordered against a reading "
                   "taken in another zone, and every trend over these records "
                   "depends on ordering them",
               fix="write it as 2026-08-01T00:00:00+08:00, or …Z")
    return dt


# --------------------------------------------------------------------------- #
# declare
# --------------------------------------------------------------------------- #

def cmd_declare(a) -> None:
    """Write the online contract into `dataset.json -> online`.

    Beside `identity`, `layers` and `locations` rather than in a file of its own,
    because it is the same kind of thing: a statement about what this dataset IS,
    read later by a generic script. `identity` is deliberately NOT restated here —
    the online and offline sides must count the same unit or the comparison is
    between two different questions.
    """
    cfg, path = load_declared_dataset(a.project, a.dataset)
    if a.partition not in PARTITIONS:
        broke(f"--partition must be one of {PARTITIONS}", got=a.partition)
    if a.partition == "strftime" and not a.pattern:
        broke("--partition strftime needs --pattern, e.g. 'inputs/%Y/%m/%d'")
    if a.partition == "external" and a.pattern:
        broke("--partition external takes no --pattern: the enumeration comes "
              "from --units-from, so nothing here expands a window")

    existing = cfg.get("online")
    if existing and not a.replace:
        refuse(f"{a.dataset} already declares an online contract",
               why="readings already on record were taken under the old one, and "
                   "silently replacing it makes them describe something else — "
                   "the reason every reading records its `taken_under`",
               existing=existing, fix="--replace to change it deliberately")

    cfg["online"] = {
        "resource": a.resource,
        "kind": a.kind,
        "partition": a.partition,
        "pattern": a.pattern,
        # How a production-side id becomes a dataset unit id. Same problem as
        # /eval-triage's `resolves_to`, and the same three answers: it already is
        # one, it is a filename of one, or nobody has worked it out — in which
        # case say so rather than let the mismatch surface at comparison time.
        "resolves_to": a.resolves_to,
        "unit_depth": a.unit_depth,
        "declared_at": now_utc(),
        "note": a.note,
    }
    atomic_write_json(path, cfg)
    emit({"dataset": a.dataset, "online": cfg["online"],
          "identity_reused_from": cfg.get("identity", {}),
          "next": "sample --from <iso> --to <iso>"})


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #

def expand(online: dict, frm: datetime, to: datetime) -> list[str]:
    """The window as a list of places to look. Declared, never assumed."""
    if online["partition"] == "flat":
        return [online.get("pattern") or ""]
    if online["partition"] == "external":
        return []
    patterns, day = [], frm.astimezone(timezone.utc).date()
    end = to.astimezone(timezone.utc).date()
    while day <= end:
        patterns.append(day.strftime(online["pattern"]))
        day += timedelta(days=1)
        if len(patterns) > 1000:
            broke("the window expands to over 1000 prefixes",
                  why="almost certainly a pattern or a window that is not what "
                      "was meant; a reading is a sample, not a full scan")
    return patterns


def list_prefix(online: dict, prefix: str, resources: str | None,
                project: str) -> dict:
    """One prefix, three outcomes kept apart.

      reachable=False                   it did not answer
      reachable=True, missing=True      it answered; that prefix is not there
      reachable=True, ids=[...]         it answered, and this is what it holds

    Only the third means the window was quiet. Collapsing the first into an
    empty listing is how a logging outage reads as a quiet day — CLAUDE.md ->
    "Never silently": never report data you could not look at.
    """
    kind = online["kind"]
    depth = int(online.get("unit_depth") or 1)

    if kind == "s3":
        uri = prefix if prefix.startswith("s3://") else \
            f"{online['resource'].rstrip('/')}/{prefix}"
        p = subprocess.run(["aws", "s3", "ls", uri.rstrip("/") + "/"],
                           capture_output=True, text=True, encoding="utf-8", timeout=600)
        if p.returncode != 0:
            err = (p.stderr or "").strip().splitlines()
            low = " ".join(err).lower()
            # A bucket that exists and holds nothing under this prefix exits 0
            # with empty stdout, so a non-zero code here is a real failure to
            # look — except the one AWS spells as an error rather than emptiness.
            if "not found" in low or "nosuchbucket" in low:
                return {"reachable": True, "missing": True, "ids": []}
            return {"reachable": False,
                    "error": f"exit {p.returncode}: {err[-1] if err else 'aws s3 ls failed'}"}
        ids = []
        for line in p.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            ids.append(parts[-1].rstrip("/"))
        return {"reachable": True, "missing": False, "ids": ids}

    # server and local share one shell script; the only difference is whether it
    # runs through ssh. `find -mindepth -maxdepth` is what makes `unit_depth`
    # declarable rather than hardcoded to "one level down".
    script = (
        f'cd {shlex.quote(prefix)} 2>/dev/null || {{ echo {NOPREFIX}; exit 0; }}\n'
        f'find . -mindepth {depth} -maxdepth {depth} 2>/dev/null '
        f'| sed "s|^\\./||"\n'
        f'echo {OK}\n'
    )
    if kind == "server":
        rpath = resources or os.environ.get("MLCLAW_RESOURCES") or \
            os.path.join(os.path.dirname(os.path.expanduser(project)), "resources.json")
        servers = (read_json(rpath, required=False) or {}).get("servers") or {}
        entry = servers.get(online["resource"])
        if not entry:
            broke(f"no server {online['resource']!r} in {rpath}",
                  known=[k for k in servers if not k.startswith("_")],
                  hint="run /resources to register it")
        host = entry.get("host") or entry.get("alias")
        user = entry.get("username")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               f"{user}@{host}" if user else host, "sh", "-s"]
    else:
        cmd = ["sh", "-s"]

    try:
        p = subprocess.run(cmd, input=script, capture_output=True,
                           text=True, encoding="utf-8", timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}"}
    stdout = p.stdout
    if p.returncode != 0 or (OK not in stdout and NOPREFIX not in stdout):
        err = (p.stderr or "").strip().splitlines()
        return {"reachable": False,
                "error": f"exit {p.returncode}: {err[-1] if err else 'no sentinel'}"}
    if NOPREFIX in stdout:
        return {"reachable": True, "missing": True, "ids": []}
    ids = [l.strip() for l in stdout.splitlines() if l.strip() and l.strip() != OK]
    return {"reachable": True, "missing": False, "ids": ids}


# --------------------------------------------------------------------------- #
# the draw
# --------------------------------------------------------------------------- #

def draw(ids: list[str], n: int, seed: str) -> list[str]:
    """A uniform draw that can be re-derived from the record.

    `sha256(seed:id)` ascending, not a shuffle: stable under insertion, so a
    unit's membership does not change because some other unit turned up. A draw
    nobody can reproduce makes the whole reading unverifiable — the record would
    assert a sample that no one could check came from this window.
    """
    keyed = sorted(ids, key=lambda i: hashlib.sha256(
        f"{seed}:{i}".encode()).hexdigest())
    return sorted(keyed[:n])


def digest(ids: list[str]) -> str:
    h = hashlib.sha256()
    for i in sorted(ids):
        h.update(i.encode())
        h.update(b"\n")
    return h.hexdigest()[:32]


# --------------------------------------------------------------------------- #
# sample
# --------------------------------------------------------------------------- #

def _enumerate_units(a, online, prefixes, project):
    """-> (ids, paths, unreachable, missing). Either read from an external
    tool's own enumeration (--units-from) or list every expanded prefix.

    First-class, not a fallback: whatever a project's layout is, its own
    tooling can enumerate it; this records that rather than reimplementing
    it. The trade is stated in the record: nothing here looked, so
    reachability is somebody else's claim.
    """
    ids, paths, unreachable, missing = [], {}, [], []

    if a.units_from:
        try:
            with open(a.units_from, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    uid, _, p = line.partition("\t")
                    ids.append(uid)
                    if p:
                        paths[uid] = p
        except OSError as e:
            broke(f"cannot read --units-from: {e}")
        if not ids:
            refuse(f"{a.units_from} lists no units",
                   why="an empty enumeration from an external tool is not a "
                       "quiet window — nothing here can tell those apart, which "
                       "is exactly why it is not assumed")
        return ids, paths, unreachable, missing

    if online["partition"] == "external":
        broke("this dataset declares partition `external`, so a reading "
              "needs --units-from: nothing here expands the window")
    for pref in prefixes:
        r = list_prefix(online, pref, a.resources, project)
        if not r["reachable"]:
            unreachable.append({"prefix": pref, "error": r.get("error")})
            continue
        if r.get("missing"):
            missing.append(pref)
            continue
        for uid in r["ids"]:
            ids.append(uid)
            paths[uid] = f"{pref.rstrip('/')}/{uid}"
    return ids, paths, unreachable, missing


def cmd_sample(a) -> None:
    project = os.path.expanduser(a.project)
    cfg, _ = load_declared_dataset(project, a.dataset)
    online = cfg.get("online")
    if not online:
        refuse(f"{a.dataset} has no online contract",
               why="nothing says where this dataset's live counterpart arrives, "
                   "and guessing a production layout is how a reading ends up "
                   "describing a directory nobody serves from",
               fix="declare --resource <r> --kind server|s3|local --partition ...")

    frm = parse_instant(a.frm, what="from")
    to = parse_instant(a.to, what="to")
    if to <= frm:
        broke("--to must be after --from")
    if to > datetime.now(timezone.utc):
        refuse("--to is in the future",
               why="an open window is a moving target: re-read an hour later it "
                   "covers more, so the enumeration and the draw are not "
                   "reproducible and the record asserts something unrepeatable",
               now=now_utc())

    prefixes = expand(online, frm, to)
    ids, paths, unreachable, missing = _enumerate_units(a, online, prefixes, project)
    ids = sorted(set(ids))
    complete = not unreachable
    if not ids and complete:
        # A genuinely quiet window is a real reading and worth recording; it is
        # the one case where zero means zero.
        pass

    seed = a.seed or f"{a.frm}|{a.to}"
    selected = draw(ids, a.n, seed) if ids else []

    population = a.population
    if population is not None and population < len(ids):
        refuse("--population is smaller than what the listing enumerated",
               why="the declared total cannot be below the units actually seen; "
                   "one of the two is measuring something else",
               population=population, enumerated=len(ids))
    known = population is not None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    rec = {
        "window_id": f"window_{ts}",
        "project": project,
        "dataset": a.dataset,
        "taken_at": now_utc(),
        # Which contract this reading was taken under, so a later comparison can
        # tell a change in production from a change in how we counted it. Same
        # reason `census.py` records it.
        "taken_under": online,
        "identity": cfg.get("identity", {}),
        "window": {"from": frm.isoformat(), "to": to.isoformat()},
        "source": {"resource": online["resource"], "kind": online["kind"],
                   "pattern": online.get("pattern"),
                   "expanded": prefixes if not a.units_from else [],
                   "enumerated_by": "external tool (--units-from)"
                                    if a.units_from else "this script"},
        "policy": "uniform",
        "draw": {"n_requested": a.n, "n_selected": len(selected),
                 "seed": seed, "enumeration_digest": digest(ids) if ids else None},
        "enumerated": len(ids),
        "population": population,
        "population_basis": "declared" if known else "enumeration_only",
        "population_source": a.population_source if known else None,
        "sample_rate": (len(selected) / population) if known and population else None,
        "rates_are": "exact" if known else "lower bound",
        "complete": complete,
        "unreachable": unreachable,
        "missing_prefixes": missing,
        "units": selected,
        "paths": {u: paths[u] for u in selected if u in paths},
        "scored_by": a.scored_by,
        "retain_until": a.retain_until,
        "overlaps": overlaps(project, a.dataset, frm, to),
        "note": a.note,
    }
    out_path = os.path.join(dataset_dir(project, a.dataset), "online", f"{rec['window_id']}.json")
    atomic_write_json(out_path, rec)

    summary = {k: rec[k] for k in
              ("window_id", "dataset", "window", "policy", "enumerated",
               "population", "population_basis", "rates_are", "complete")}
    summary["selected"] = len(selected)
    summary["record"] = out_path
    if not complete:
        summary["warning"] = (
            f"{len(unreachable)} prefix(es) did not answer, so `enumerated` is a "
            f"LOWER BOUND and this reading must not be compared — a drift verdict "
            f"against a window with a missing day is a verdict about the outage")
    if not known:
        summary["note_population"] = (
            "population is unknown, so every rate off this reading is a lower "
            "bound and must be said as one. Request logging is itself sampled "
            "and rotated; the listing sees what reached the store, not what "
            "happened")
    if rec["overlaps"]:
        summary["note_overlaps"] = (
            f"{len(rec['overlaps'])} earlier reading(s) cover part of this "
            f"window; they double-count in any trend line")
    summary["next"] = ("a drift comparison against a frozen snapshot; "
                      "/data-collect --cite-window to pull a biased sample with "
                      "this reading as its denominator")
    emit(summary)
    if not complete:
        sys.exit(1)


def overlaps(project, dataset, frm, to) -> list[dict]:
    """Earlier readings whose interval intersects this one. A warning, never a
    refusal: two readings of the same afternoon are a legitimate thing to take."""
    d = os.path.join(dataset_dir(project, dataset), "online")
    if not os.path.isdir(d):
        return []
    hits = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".json"):
            continue
        r = read_json(os.path.join(d, f), required=False) or {}
        w = r.get("window") or {}
        try:
            a0 = datetime.fromisoformat(w["from"])
            a1 = datetime.fromisoformat(w["to"])
        except (KeyError, TypeError, ValueError):
            continue
        if a0 < to and frm < a1:
            hits.append({"window_id": r.get("window_id"), "from": w["from"],
                         "to": w["to"]})
    return hits


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def cmd_status(a) -> None:
    """Readings on record. No network — that is what makes this safe to run on
    every conversation start, the same split `census.py status` follows."""
    project = os.path.expanduser(a.project)
    root = os.path.join(project, "datasets")
    rows = []
    for ds in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if a.dataset and ds != a.dataset:
            continue
        d = os.path.join(root, ds, "online")
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json"):
                continue
            r = read_json(os.path.join(d, f), required=False) or {}
            rows.append({
                "window_id": r.get("window_id"), "dataset": ds,
                "window": r.get("window"), "taken_at": r.get("taken_at"),
                "enumerated": r.get("enumerated"),
                "selected": (r.get("draw") or {}).get("n_selected"),
                "population_basis": r.get("population_basis"),
                "complete": r.get("complete"),
            })
    emit({"project": project, "readings": rows,
          "note": "a reading can never be retaken — the traffic has rolled off "
                  "and the model that answered has been replaced" if rows else None})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("declare", help="the online contract, once")
    d.add_argument("--project", required=True)
    d.add_argument("--dataset", required=True)
    d.add_argument("--resource", required=True,
                   help="a resources.json -> servers key, an s3:// base, or a "
                        "local path root. Named, never spelled out: the record "
                        "is committable and the address must not leak through it")
    d.add_argument("--kind", required=True, choices=("server", "s3", "local"))
    d.add_argument("--partition", required=True, choices=PARTITIONS,
                   help="how a window becomes places to look. `external` means "
                        "your own tooling enumerates it and this records that")
    d.add_argument("--pattern", default=None,
                   help="strftime path pattern, e.g. 'inputs/%%Y/%%m/%%d'")
    d.add_argument("--resolves-to", default=None,
                   choices=("unit_id", "basename", None),
                   help="how a production id becomes a dataset unit id. Omit "
                        "when nobody has worked it out — that is honest and "
                        "makes the mismatch visible now rather than later")
    d.add_argument("--unit-depth", type=int, default=1,
                   help="how many levels below a prefix a unit sits")
    d.add_argument("--note", default=None)
    d.add_argument("--replace", action="store_true")
    d.set_defaults(fn=cmd_declare)

    s = sub.add_parser("sample", help="one uniform reading of one closed window")
    s.add_argument("--project", required=True)
    s.add_argument("--dataset", required=True)
    s.add_argument("--from", dest="frm", required=True,
                   help="ISO instant WITH a UTC offset")
    s.add_argument("--to", required=True,
                   help="ISO instant WITH a UTC offset; must be in the past")
    s.add_argument("--n", type=int, default=500,
                   help="how many units to draw (default 500)")
    s.add_argument("--seed", default=None,
                   help="defaults to the window itself, so the same window "
                        "draws the same units")
    s.add_argument("--units-from", default=None,
                   help="a file of unit ids, optionally `id<TAB>path`. "
                        "First-class: your tooling lists it, this records it")
    s.add_argument("--population", type=int, default=None,
                   help="how many inputs production actually handled, if "
                        "something that counts can say. Omit when nothing can — "
                        "then every rate off this reading is a lower bound")
    s.add_argument("--population-source", default=None,
                   help="where the population number came from")
    s.add_argument("--scored-by", default=None,
                   help="the artifact that was serving. A path for now: a "
                        "checkpoint has no citable identity yet")
    s.add_argument("--retain-until", default=None,
                   help="when these units must be deleted, if a limit applies")
    s.add_argument("--resources", default=None)
    s.add_argument("--note", default=None)
    s.set_defaults(fn=cmd_sample)

    t = sub.add_parser("status", help="readings on record; no network")
    t.add_argument("--project", required=True)
    t.add_argument("--dataset", default=None)
    t.set_defaults(fn=cmd_status)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
