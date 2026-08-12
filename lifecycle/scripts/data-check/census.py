#!/usr/bin/env python3
"""census.py — where the data is, and what state it is in.

The query layer for the data lifecycle, standing in the same relation to a
dataset that `list_runs.py` stands in to runs: the records on disk are the
source of truth, a census is a scan run on demand, and there is no index to
drift. Unlike `list_runs.py`, the truth it scans lives on several machines at
once, so "I could not look" is a possible answer and must never be spelled the
same way as "there is nothing there".

WHAT IT READS: existence, location, and completeness markers. Never content.
That boundary is the whole reason this can be one small script instead of a
pipeline — the moment something here opens a file to judge whether the data
inside it is any good, it has become an evaluation stage. The single exception
is `completeness.partial_marker_field`, and it is permitted only because that
field IS the completion claim rather than a statement about the data.

FOUR VERDICTS. The first two are the familiar ones; the third is the one that
bleeds while nothing reports it.

  GAP           a layer missing at every location that expects it
                -> needs compute
  DRIFT         present somewhere, absent somewhere it is expected
                -> needs sync, toward the authority
  UNREPLICATED  a SOURCE layer with fewer copies than `min_source_copies`
                -> one disk failure from total loss, and usually the machine
                   holding the only copy is the one least able to know it
  UNARCHIVED    the unit exists somewhere but has never reached the authority
                -> still only where it was born. A capture box deleting its
                   oldest day to make room for the next shot cannot answer
                   "was this ever copied off"; this verdict is that answer,
                   and it has to be computed somewhere that can see both ends
  INCOMPLETE    the unit exists but nothing anywhere claims it finished
                -> not a sync problem and not a compute problem; a unit that
                   looks whole and is not is the only one of these five that
                   survives all the way into a trained model

UNREPLICATED is deliberately separate from DRIFT, because collapsing them
loses the only distinction that matters for an irreversible decision. A DERIVED
layer sitting on one machine is an inconvenience: recompute it. A SOURCE layer
sitting on one machine is the data's entire existence. Both read as "one copy
missing" if the verdict is only ever about sync.

UNARCHIVED is separate from UNREPLICATED for the same kind of reason. Copy
count and reach are different questions: a unit sitting on a capture box and
its own local mirror has two copies and has still never entered the pipeline,
while a unit at the authority alone has one copy and is fully in play. Only one
of the two states is fixed by rsync-ing to a bigger disk.

EXIT CODES follow CLAUDE.md "Script Integration": 0 = scanned and every
location answered; 1 = the script worked and the answer is no (a location was
unreachable and `--allow-unreachable` was not given, a snapshot would have
frozen unverified units, the dataset is not declared); 2 = the script broke,
fall back and do the same work by hand. Exit 1 is never a bug to route around.

Verbs:
  scan      --project P --dataset D [--allow-unreachable] [--json]
  show      --project P --dataset D [--census ID] [--units] [--json]
  snapshot  --project P --dataset D --id SID [--layer L] [--at LOC]
            [--units-from FILE] [--allow-incomplete N] [--json]
  resolve   --project P --dataset D --snapshot SID --at LOC [--layer L ...]
            [--allow-missing N] [--out FILE] [--json]
  status    (--project P | --workspace W) [--json]

`snapshot` freezes membership in DATASET space; `resolve` joins that against one
location's root to produce openable paths, which is what a dataloader needs and
what a frozen record must never contain. The split is the point — see `resolve`.

`status` reads records only and never touches the network — that is what makes
it safe for CLAUDE.md "On Conversation Start". `scan` is the one that goes out
and asks four machines, so it is never the thing a session opens with.
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
from _records import (atomic_write_json, now_utc, read_json)  # noqa: E402
from _dataset_paths import DEFAULT_MIN_SOURCE_COPIES, dataset_dir, latest_census_path  # noqa: E402

# A label goes into a shell variable list as `label:marker`, and into a comma
# separated field on the way back. Either character would silently corrupt the
# parse, so they are refused at declaration time rather than mis-parsed later.
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

OK = "__MLCLAW_OK__"            # the probe ran to completion
NOROOT = "__MLCLAW_NOROOT__"    # the machine answered; the root is not there

SOURCE_KINDS = {"source", "human_locked"}   # cannot be recomputed


# ---------------------------------------------------------------- utilities

def die(msg: str, code: int = 1) -> None:
    """A refusal, or a broken script. One line, never a traceback."""
    print(msg, file=sys.stderr)
    sys.exit(code)


def shq(s: str) -> str:
    """Single-quote for /bin/sh. Roots come from a config a human edits."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def rjoin(root: str, *rest: str) -> str:
    """Join a DATA path with '/', never os.path.join.

    The path being built belongs to the machine named in `locations[].root`,
    which is not necessarily this machine: a Windows box resolving a Linux
    server root with os.path.join emits backslashes and produces a path that
    exists nowhere. Same reason run-mechanics.md maps remote paths explicitly.
    """
    out = root.rstrip("/") or root
    for p in rest:
        p = str(p).strip("/")
        if p:
            out += "/" + p
    return out


# ------------------------------------------------------------ config loading

def load_layout_contract(project: str, dataset: str) -> tuple[dict, str]:
    """Read and validate a dataset's layout contract. Refuses, never guesses.

    Named for what it validates, not for what it loads. `online.py` had a
    `load_dataset(project, dataset) -> (cfg, path)` too -- same name, same
    signature, same return shape, and it checks only that the dataset is
    declared. A reader who learned the name here would carry the layout
    guarantees over to a call that never made them.
    """
    ddir = dataset_dir(project, dataset)
    path = os.path.join(ddir, "dataset.json")
    if not os.path.isfile(path):
        die(f"no dataset declared at {path} — run /data-check to declare it first")
    try:
        cfg = read_json(path)
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON: {e}")

    glob = (cfg.get("identity") or {}).get("unit_glob") or ""
    if not glob:
        die(f"{path}: identity.unit_glob is empty — a unit's shape cannot be guessed")

    layers = [l for l in (cfg.get("layers") or []) if l.get("label")]
    if not layers:
        die(f"{path}: no layers declared — there is nothing to report state about")
    for l in layers:
        if not LABEL_RE.match(l["label"]):
            die(f"{path}: layer label {l['label']!r} must match {LABEL_RE.pattern} "
                f"— a ':' or ',' would corrupt the probe's own output")
        if not l.get("marker"):
            die(f"{path}: layer {l['label']!r} has no marker — existence of what?")
        if l.get("kind") not in ("source", "derived", "human_locked"):
            die(f"{path}: layer {l['label']!r} kind={l.get('kind')!r} must be "
                f"source | derived | human_locked; it decides whether a missing "
                f"copy is a loss or an inconvenience")

    locs = [x for x in (cfg.get("locations") or []) if x.get("key")]
    if not locs:
        die(f"{path}: no locations declared — 'where is the data' has no answer")
    authority = [x for x in locs if x.get("role") == "authority"]
    if len(authority) != 1:
        die(f"{path}: exactly one location must have role=authority, found "
            f"{len(authority)} — DRIFT is resolved *toward* something")
    for x in locs:
        if not x.get("root"):
            die(f"{path}: location {x['key']!r} has no root")
        if x.get("via") == "server" and not x.get("server"):
            die(f"{path}: location {x['key']!r} is via=server but names no server key")

    return cfg, ddir


# ------------------------------------------------------------------- probing

def build_probe(cfg: dict) -> str:
    """The one shell script that answers everything, for one location.

    Generated from the config so adding a layer is a config edit and not a
    second place to keep in sync. It emits one line per unit plus a sentinel:
    the sentinel is what lets the caller tell "ran, found nothing" apart from
    "never ran", which is the distinction `survey.py` in the perception repo
    drops on the floor (an ssh failure yields empty stdout, indistinguishable
    from a machine holding zero scenes).
    """
    ident = cfg["identity"]
    glob = ident["unit_glob"].rstrip("/")
    excl = [p for p in (ident.get("exclude") or []) if p]
    comp = cfg.get("completeness") or {}
    marker = comp.get("marker")
    field = comp.get("partial_marker_field")

    pairs = " ".join(f"{l['label']}:{l['marker']}" for l in cfg["layers"])

    lines = [
        'for d in %s/ ; do' % glob,
        '  [ -d "$d" ] || continue',
        '  u=${d%/}',
    ]
    if excl:
        lines += [
            '  b=${u##*/}',
            '  case "$b" in %s) continue ;; esac' % "|".join(excl),
        ]
    lines += [
        '  fl=""',
        '  for m in %s ; do' % pairs,
        '    lab=${m%%:*}; p=${m#*:}',
        '    [ -e "$u/$p" ] && fl="$fl$lab,"',
        '  done',
        '  dn=-',      # completeness: - none declared, 0 absent, 1 present
        '  fv=""',
    ]
    if marker:
        lines += [
            '  if [ -e "$u/%s" ]; then' % marker,
            '    dn=1',
        ]
        if field:
            # ONE field, by name. Not a content read: this field is the
            # completion claim itself. Its ABSENCE means a clean end (a scene
            # an operator actually finished carries no such key), so an empty
            # match is reported as empty and interpreted upstream — never
            # defaulted to "interrupted" and never to "clean" by this script.
            lines += [
                '    fv=$(grep -o %s "$u/%s" 2>/dev/null | head -n1 || true)'
                % (shq('"%s"[[:space:]]*:[[:space:]]*"[^"]*"' % field), marker),
            ]
        lines += [
            '  else',
            '    dn=0',
            '  fi',
        ]
    lines += [
        r'  printf "%s\t%s\t%s\t%s\n" "$u" "$fl" "$dn" "$fv"',
        'done',
        'echo %s' % OK,
    ]
    body = "\n".join(lines)
    return "cd %s 2>/dev/null || { echo %s; exit 0; }\n%s\n" % (
        shq(cfg["_root_for_probe"]), NOROOT, body)


def probe(loc: dict, cfg: dict) -> dict:
    """Ask one location what it holds.

    Returns a dict that always distinguishes THREE outcomes, because they are
    three different facts and only one of them means "the data is not there":

      reachable=False           the machine did not answer
      reachable=True, root_missing=True   it answered; that path is not there
      reachable=True, units={...}         it answered, and this is what it has
    """
    cfg = dict(cfg, _root_for_probe=os.path.expanduser(loc["root"]))
    script = build_probe(cfg)
    if loc.get("via") == "server":
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               loc["server"], "sh", "-s"]
    else:
        cmd = ["sh", "-s"]

    try:
        p = subprocess.run(cmd, input=script, capture_output=True,
                           text=True, encoding="utf-8", timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}"}

    out = p.stdout
    # BOTH checks, on purpose. A non-zero code catches ssh refusing; the
    # sentinel catches a shell that died mid-loop with a zero exit. Either
    # alone lets a truncated listing pass as a complete one.
    if p.returncode != 0 or (OK not in out and NOROOT not in out):
        err = (p.stderr or "").strip().splitlines()
        return {"reachable": False,
                "error": f"exit {p.returncode}: {err[-1] if err else 'no sentinel in output'}"}
    if NOROOT in out:
        return {"reachable": True, "root_missing": True, "units": {}}

    units: dict[str, dict] = {}
    for line in out.splitlines():
        if line in (OK, NOROOT) or "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        uid, flags, dn = parts[0], parts[1], parts[2]
        fv = parts[3] if len(parts) > 3 else ""
        units[uid.strip("/")] = {
            "layers": [x for x in flags.split(",") if x],
            "done": {"-": None, "0": False, "1": True}.get(dn),
            "done_field": fv or None,
        }
    return {"reachable": True, "root_missing": False, "units": units}


# ------------------------------------------------------------------- verdicts

def expects(loc: dict, label: str) -> bool:
    """Is this layer supposed to be at this location?

    `has_layers: null` means all of them. A list means only these by design —
    a capture box holding raw frames and nothing else must not report every
    downstream layer as a gap, or the report is all noise and gets ignored,
    which is worse than not having it.
    """
    hl = loc.get("has_layers")
    return label in hl if isinstance(hl, list) else True


def compute(cfg: dict, scans: dict) -> dict:
    """Turn per-location listings into the four verdicts."""
    layers = cfg["layers"]
    by_label = {l["label"]: l for l in layers}
    locs = {x["key"]: x for x in cfg["locations"]}
    live = [k for k, s in scans.items() if s.get("reachable")]
    min_copies = int((cfg.get("replication") or {}).get("min_source_copies")
                    or DEFAULT_MIN_SOURCE_COPIES)
    comp = cfg.get("completeness") or {}
    has_marker = bool(comp.get("marker"))

    unit_ids = sorted({u for k in live for u in scans[k]["units"]})

    # The authority is where every layer belongs and where compute reads from,
    # so "did this unit ever get there" is its own question — see UNARCHIVED in
    # the module docstring. Only askable when the authority actually answered:
    # if it did not, `unarchived` stays empty rather than reporting every unit
    # in the dataset as stranded.
    auth = next((x["key"] for x in cfg["locations"] if x.get("role") == "authority"), None)
    auth_live = auth in live

    units: dict[str, dict] = {}
    gap: dict[str, list] = {l["label"]: [] for l in layers}
    drift: dict[str, list] = {l["label"]: [] for l in layers}
    unreplicated: dict[str, list] = {l["label"]: [] for l in layers}
    unarchived: list[str] = []
    incomplete: list[str] = []
    partial: list[dict] = []

    for uid in unit_ids:
        at = [k for k in live if uid in scans[k]["units"]]
        rec = {"at": at, "layers": {}}
        if auth_live and auth not in at:
            unarchived.append(uid)

        for label in by_label:
            present = [k for k in at if label in scans[k]["units"][uid]["layers"]]
            # Where it is EXPECTED: a location that declares this layer and
            # holds this unit. `working` is excluded from the drift target —
            # a working set is a deliberate subset, so its absences are the
            # normal state and flagging them buries the real ones.
            want = [k for k in at
                    if expects(locs[k], label) and locs[k].get("role") != "working"]
            rec["layers"][label] = present
            if not present and want:
                gap[label].append(uid)
                continue
            if present and set(want) - set(present):
                drift[label].append(uid)
            # `present` must be non-empty: ZERO copies is never under-replication.
            # It is either a GAP (something expects it) or by-design absence (a
            # capture box has no `gt`), and both were already decided above.
            # Without this guard every unit still sitting on its origin machine
            # gets reported as an under-replicated copy of every layer that
            # machine was never supposed to hold.
            if (present and by_label[label].get("kind") in SOURCE_KINDS
                    and len(present) < min_copies):
                unreplicated[label].append(uid)

        if not has_marker:
            rec["completeness"] = "unverifiable"
        else:
            done_at = [k for k in at if scans[k]["units"][uid]["done"]]
            if not done_at:
                rec["completeness"] = "incomplete"
                incomplete.append(uid)
            else:
                fv = next((scans[k]["units"][uid]["done_field"] for k in done_at
                           if scans[k]["units"][uid]["done_field"]), None)
                if fv:
                    rec["completeness"] = "partial"
                    partial.append({"unit": uid, "field": fv})
                else:
                    rec["completeness"] = "complete"
            rec["done_at"] = done_at
        units[uid] = rec

    return {
        "units": units,
        "verdicts": {
            "gap": {k: v for k, v in gap.items() if v},
            "drift": {k: v for k, v in drift.items() if v},
            "unreplicated": {k: v for k, v in unreplicated.items() if v},
            "unarchived": unarchived,
            "incomplete": incomplete,
            "partial": partial,
        },
        "totals": {
            "units": len(unit_ids),
            "gap": sum(len(v) for v in gap.values()),
            "drift": sum(len(v) for v in drift.values()),
            "unreplicated": sum(len(v) for v in unreplicated.values()),
            "unarchived": len(unarchived),
            "incomplete": len(incomplete),
            "partial": len(partial),
            "min_source_copies": min_copies,
            # "not asked" is not "none found" — the authority did not answer, so
            # UNARCHIVED was never computed and must not read as zero.
            "unarchived_checked": auth_live,
        },
    }


# ---------------------------------------------------------------------- scan

def cmd_scan(a) -> None:
    cfg, ddir = load_layout_contract(a.project, a.dataset)

    scans = {}
    for loc in cfg["locations"]:
        scans[loc["key"]] = probe(loc, cfg)

    unreachable = [k for k, s in scans.items() if not s.get("reachable")]
    root_missing = [k for k, s in scans.items()
                    if s.get("reachable") and s.get("root_missing")]
    result = compute(cfg, scans)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    census = {
        "census_id": f"census_{stamp}",
        "dataset": cfg.get("dataset_id") or a.dataset,
        "project": os.path.expanduser(a.project),
        "scanned_at": now_utc(),
        # The contract this scan was taken UNDER, recorded so that comparing two
        # censuses can tell a change in the DATA from a change in how it was
        # counted. `unit_glob`'s depth decides every unit id, so editing it
        # renames all of them at once — which reads to a differ as every unit
        # being deleted and a different set arriving. Same for the completeness
        # marker: declaring one where there was none flips every unit from
        # `unverifiable` to complete/incomplete without a byte moving.
        "taken_under": {
            "identity": cfg.get("identity"),
            "completeness": cfg.get("completeness"),
            "min_source_copies": (cfg.get("replication") or {}).get("min_source_copies"),
            "layers": [{"label": l["label"], "marker": l.get("marker"),
                        "kind": l.get("kind")} for l in cfg.get("layers") or []],
        },
        # `complete` is the field that stops a partial census from being read as
        # a full one. Any location that did not answer makes every count below
        # a LOWER BOUND, and a snapshot refuses to freeze against it.
        "complete": not unreachable,
        "unreachable": unreachable,
        "root_missing": root_missing,
        "locations": [
            {"key": loc["key"], "role": loc.get("role"), "via": loc.get("via"),
             "server": loc.get("server"), "root": loc["root"],
             "reachable": scans[loc["key"]].get("reachable", False),
             "root_missing": scans[loc["key"]].get("root_missing"),
             "units": len(scans[loc["key"]].get("units") or {}),
             "error": scans[loc["key"]].get("error")}
            for loc in cfg["locations"]
        ],
        **result,
    }
    path = os.path.join(ddir, "census", f"{census['census_id']}.json")
    # fsync: a deletion plan is ranked against this file, and a census that
    # survives the rename but not the power cut would be ranked against a
    # truncated one. ensure_ascii keeps the bytes identical to every census
    # written before this call moved into shared/records.py.
    atomic_write_json(path, census, fsync=True, ensure_ascii=True)
    census["_path"] = path

    if a.json:
        print(json.dumps(census, indent=2))
    else:
        report(cfg, census)

    if unreachable and not a.allow_unreachable:
        die(f"\n{len(unreachable)} location(s) did not answer: "
            f"{', '.join(unreachable)}. The census was written and every count "
            f"in it is a lower bound. Re-run with --allow-unreachable to accept "
            f"that, or fix access first — 'could not look' must not be filed as "
            f"'not there'.")


def report(cfg: dict, c: dict) -> None:
    """Human-readable. The counts ARE the finding, so they come first."""
    t = c["totals"]
    print(f"{c['dataset']}  ·  {t['units']} units  ·  {c['scanned_at']}"
          f"{'' if c['complete'] else '  ⚠ PARTIAL CENSUS'}")
    print()
    for loc in c["locations"]:
        if not loc["reachable"]:
            state = f"UNREACHABLE — {loc['error']}"
        elif loc["root_missing"]:
            state = "root does not exist on that machine"
        else:
            state = f"{loc['units']} units"
        via = loc["server"] if loc["via"] == "server" else "local"
        print(f"  {loc['key']:<14} {loc['role']:<10} {via:<14} {state}")

    print(f"\n  {'layer':<14}{'kind':<13}" + "".join(f"{l['key'][:11]:<12}" for l in c["locations"]))
    for layer in cfg["layers"]:
        lab = layer["label"]
        cells = []
        for loc in c["locations"]:
            if not loc["reachable"]:
                cells.append(f"{'?':<12}")
            elif not expects(next(x for x in cfg["locations"] if x["key"] == loc["key"]), lab):
                cells.append(f"{'—':<12}")
            else:
                n = sum(1 for u in c["units"].values() if loc["key"] in u["layers"][lab])
                cells.append(f"{n:<12}")
        print(f"  {lab:<14}{layer['kind']:<13}" + "".join(cells))
    print("  (— = not expected here by design;  ? = location did not answer)")

    v = c["verdicts"]
    print("\nverdicts:")
    if not any(v.values()):
        print("  none — every layer is where it should be, every unit claims completion")
    for lab, units in v["gap"].items():
        by = next((l.get("produced_by") for l in cfg["layers"] if l["label"] == lab), None)
        print(f"  GAP           {lab:<12} ×{len(units):<6} missing everywhere"
              f"{f' → {by}' if by else ' → needs compute'}")
    for lab, units in v["drift"].items():
        print(f"  DRIFT         {lab:<12} ×{len(units):<6} present somewhere, "
              f"absent where expected → sync toward authority")
    for lab, units in v["unreplicated"].items():
        kind = next(l["kind"] for l in cfg["layers"] if l["label"] == lab)
        print(f"  UNREPLICATED  {lab:<12} ×{len(units):<6} {kind} layer under "
              f"{t['min_source_copies']} copies → ONE DISK FROM TOTAL LOSS")
    if v.get("unarchived"):
        auth = next(x["key"] for x in cfg["locations"] if x.get("role") == "authority")
        print(f"  UNARCHIVED    {'':<12} ×{len(v['unarchived']):<6} never reached "
              f"{auth} → still only where it was born")
    elif not t.get("unarchived_checked", True):
        print(f"  ⚠ UNARCHIVED not checked — the authority did not answer, so "
              f"'never copied off' was never computed (not the same as none).")
    if v["incomplete"]:
        print(f"  INCOMPLETE    {'':<12} ×{len(v['incomplete']):<6} nothing anywhere "
              f"claims these finished → do not feed downstream")
    if v["partial"]:
        vals = sorted({p['field'] for p in v['partial']})
        print(f"  PARTIAL       {'':<12} ×{len(v['partial']):<6} marker says not a "
              f"clean end: {'; '.join(vals)}")
    if not (cfg.get("completeness") or {}).get("marker"):
        print("  ⚠ no completeness marker declared — every unit is 'unverifiable'. "
              "Directory existence is not completion.")


# ---------------------------------------------------------------------- show

def cmd_show(a) -> None:
    cfg, ddir = load_layout_contract(a.project, a.dataset)
    path = (os.path.join(ddir, "census", f"{a.census}.json") if a.census
            else latest_census_path(ddir))
    if not path or not os.path.isfile(path):
        die(f"no census for {a.dataset} yet — run `scan` first")
    c = read_json(path)
    if a.json:
        print(json.dumps(c, indent=2))
        return
    report(cfg, c)
    if a.units:
        print("\nunits:")
        for uid, u in sorted(c["units"].items()):
            got = ",".join(k for k, v in u["layers"].items() if v) or "-"
            print(f"  {uid:<28} {u.get('completeness', '?'):<13} "
                  f"at[{','.join(u['at'])}]  {got}")


# ------------------------------------------------------------------ snapshot

def cmd_snapshot(a) -> None:
    """Freeze WHICH units, so a run can cite what it actually consumed.

    A dataset grows. A run trained on what it was that afternoon, and a
    citation that cannot say which afternoon is not lineage — which is why a
    consuming run cites `datasets/<did>@<sid>` and never the dataset id alone.

    WHAT THIS DOES NOT DO, stated because the neighbouring machinery does:
    it records no content hashes. `/data-label` hashes every item because its
    manifest is the only authority for what a third party owes back; here the
    question is which units were in the set, and hashing a multi-terabyte tree
    to answer it would trade a real answer for one nobody would ever wait for.
    So a snapshot pins membership, not bytes.
    """
    cfg, ddir = load_layout_contract(a.project, a.dataset)
    cpath = latest_census_path(ddir)
    if not cpath:
        die(f"no census for {a.dataset} — snapshot freezes what a scan saw, "
            f"so run `scan` first")
    c = read_json(cpath)

    if not c.get("complete"):
        die(f"census {c['census_id']} is PARTIAL ({', '.join(c['unreachable'])} did "
            f"not answer) — freezing against it would pin a set that was never "
            f"fully seen. Re-scan with every location reachable.")

    units = c["units"]
    if a.units_from:
        with open(a.units_from) as f:
            wanted = [ln.strip() for ln in f if ln.strip()]
        missing = [u for u in wanted if u not in units]
        if missing:
            die(f"{len(missing)} unit(s) in {a.units_from} are not in the census: "
                f"{', '.join(missing[:5])}{' …' if len(missing) > 5 else ''}")
        sel = wanted
    else:
        sel = sorted(units)
        if a.layer:
            if a.layer not in {l["label"] for l in cfg["layers"]}:
                die(f"no layer {a.layer!r} declared in this dataset")
            sel = [u for u in sel if units[u]["layers"].get(a.layer)]
        if a.at:
            sel = [u for u in sel if a.at in units[u]["layers"].get(a.layer, [])] \
                if a.layer else [u for u in sel if a.at in units[u]["at"]]
    if not sel:
        die("the selection is empty — nothing to freeze")

    bad = [u for u in sel if units[u].get("completeness") in ("incomplete", "partial")]
    if bad and a.allow_incomplete != len(bad):
        die(f"{len(bad)} of {len(sel)} units are not verified complete "
            f"({', '.join(bad[:5])}{' …' if len(bad) > 5 else ''}). A unit that "
            f"looks whole and is not is the one defect that survives into the "
            f"model. Pass --allow-incomplete {len(bad)} to freeze them anyway — "
            f"the count binds the acceptance to what was measured now, not to a "
            f"number remembered from a previous scan.")
    unverifiable = [u for u in sel if units[u].get("completeness") == "unverifiable"]

    sdir = os.path.join(ddir, "snapshots", a.id)
    if os.path.exists(sdir):
        die(f"snapshot {a.id} already exists — an identity is never reused; a "
            f"frozen set that changed under a citation is worse than no citation")

    manifest = os.path.join(sdir, "manifest.jsonl")
    os.makedirs(sdir, exist_ok=True)
    tmp = manifest + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps({"_manifest": {
            "count": len(sel), "frozen_at": now_utc(),
            "census_id": c["census_id"], "pins": "membership, not bytes"}}) + "\n")
        for u in sel:
            f.write(json.dumps({"unit": u,
                                "layers": units[u]["layers"],
                                "at": units[u]["at"],
                                "completeness": units[u].get("completeness")}) + "\n")
    os.replace(tmp, manifest)

    snap = {
        "snapshot_id": a.id,
        "cite_as": f"datasets/{cfg.get('dataset_id') or a.dataset}@{a.id}",
        "dataset": cfg.get("dataset_id") or a.dataset,
        "project": os.path.expanduser(a.project),
        "frozen_at": now_utc(),
        "from_census": c["census_id"],
        "selection": {"layer": a.layer, "at": a.at,
                      "units_from": a.units_from, "count": len(sel)},
        "manifest": "manifest.jsonl",
        # Both survive into every downstream description of this set. A frozen
        # set carrying unverified units that reads as merely "frozen" becomes a
        # clean dataset in every record that follows it.
        "unverified_units": bad,
        "unverifiable_units": unverifiable,
        "layer_coverage": {
            l["label"]: sum(1 for u in sel if units[u]["layers"].get(l["label"]))
            for l in cfg["layers"]
        },
    }
    atomic_write_json(os.path.join(sdir, "snapshot.json"), snap,
                      fsync=True, ensure_ascii=True)

    if a.json:
        print(json.dumps(snap, indent=2))
    else:
        print(f"froze {len(sel)} units as {snap['cite_as']}")
        print(f"  from census {c['census_id']}  ·  pins membership, not bytes")
        for lab, n in snap["layer_coverage"].items():
            print(f"  {lab:<14} {n}/{len(sel)}")
        if bad:
            print(f"  ⚠ {len(bad)} unverified-complete units are IN this snapshot")
        if unverifiable:
            print(f"  ⚠ {len(unverifiable)} units have no completeness signal at all")
        print(f"\n  cite in run.json -> lineage.parents as: {snap['cite_as']}")


# ------------------------------------------------------------------- resolve

def cmd_resolve(a) -> None:
    """Turn a frozen snapshot into openable paths, for the side that trains.

    A snapshot pins MEMBERSHIP in dataset space: `260725/s003`, plus which
    location KEYS hold which layers. That is the right thing to freeze and the
    wrong thing to hand a dataloader, which needs a path it can open on the
    machine it is running on. Resolving it needs a three-way join — manifest ×
    `locations[].root` × `layers[].marker` — and only `dataset.json` holds two
    of the three, which the training side does not read. So without this verb a
    manifest is a set of unit ids that cannot open a single file, and every
    consumer reimplements the join, differently.

    THIS IS A DERIVED VIEW AND NEVER PART OF THE FROZEN RECORD. `dataset.json`
    is machine-independent on purpose — `locations` names the machines and
    everything else describes the data, so moving a disk edits one block. A
    resolved path embeds one machine's root, and writing it into `snapshots/`
    would put a machine into the one record that is supposed to survive
    machines. Nothing is lost by keeping it out: snapshot + dataset.json +
    `--at` regenerate it byte for byte, which is exactly why the consuming run
    cites `cite_as` and not this file.

    WHAT IT DOES NOT DO: convert, copy, shard, or hash. A "standard structure
    for training" can mean two things and only one of them is free. Standardising
    the *manifest* costs nothing and asks nothing of the user's code.
    Standardising the *bytes* — one shard format for every dataset — would make
    freezing cost terabytes, break "a snapshot pins membership, not bytes", and
    STILL need the dataloader changed, which is the one thing MLClaw does not
    ask for. When the bytes genuinely must change, that is a curate run
    consuming this snapshot and producing a new dataset, separately frozen and
    separately citable — reproducible, instead of a conversion nobody recorded.

    PATHS WERE TRUE AS OF A CENSUS, and the header says which one and how long
    ago. This verb stats nothing: re-walking a multi-terabyte tree to confirm
    what a scan already established is the cost `scan` exists to pay once. A
    resolve against a three-week-old census is a set of paths that were real
    three weeks ago, and the consumer is told so rather than left to assume.
    """
    cfg, ddir = load_layout_contract(a.project, a.dataset)

    sdir = os.path.join(ddir, "snapshots", a.snapshot)
    spath = os.path.join(sdir, "snapshot.json")
    mpath = os.path.join(sdir, "manifest.jsonl")
    if not os.path.isfile(spath) or not os.path.isfile(mpath):
        die(f"no snapshot {a.snapshot!r} at {sdir} — freeze one with `snapshot` "
            f"first; resolving is a view over a frozen set, not a substitute for one")
    snap = read_json(spath)

    locs = {l.get("key"): l for l in (cfg.get("locations") or []) if l.get("key")}
    loc = locs.get(a.at)
    if not loc:
        die(f"no location {a.at!r} declared in this dataset — declared: "
            f"{', '.join(sorted(locs)) or '(none)'}")
    if not loc.get("root"):
        die(f"location {a.at!r} has an empty root — there is nothing to join a "
            f"unit path onto")
    # dataset.json's own words for this role: "written to, never read from for
    # compute". Training off the backup is how a restore test never happens and
    # how the copy that was supposed to be untouched acquires mtimes.
    if loc.get("role") == "backup":
        die(f"location {a.at!r} is role `backup` — written to, never read from "
            f"for compute. Resolve against the authority or a working copy.")

    markers = {l["label"]: l["marker"] for l in cfg["layers"]}
    want = list(a.layer or []) or [l["label"] for l in cfg["layers"]]
    for lab in want:
        if lab not in markers:
            die(f"no layer {lab!r} declared in this dataset — declared: "
                f"{', '.join(markers)}")
    # `has_layers` is a design statement, not an observation: this location is
    # not supposed to carry that layer. Reporting it as N missing units would
    # describe a wrong request as missing data.
    has = loc.get("has_layers")
    if has is not None:
        wrong = [l for l in want if l not in has]
        if wrong:
            die(f"location {a.at!r} is declared to hold only "
                f"{', '.join(has) or '(nothing)'} — {', '.join(wrong)} is not "
                f"missing there, it was never supposed to be there")

    rows, missing, head = [], [], {}
    with open(mpath) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            rec = json.loads(ln)
            if "_manifest" in rec:
                head = rec["_manifest"]
                continue
            gone = [l for l in want if a.at not in (rec.get("layers") or {}).get(l, [])]
            if gone:
                missing.append((rec["unit"], gone))
                continue
            rows.append({
                "unit": rec["unit"],
                "paths": {l: rjoin(loc["root"], rec["unit"], markers[l])
                          for l in want},
                # Carried through, not recomputed. A unit frozen as unverified
                # stays unverified in every downstream description of it —
                # otherwise the resolve is where that fact quietly disappears.
                "completeness": rec.get("completeness"),
            })

    if missing and a.allow_missing != len(missing):
        ex = ", ".join(f"{u} ({'+'.join(g)})" for u, g in missing[:5])
        die(f"{len(missing)} of {len(rows) + len(missing)} units do not have "
            f"{', '.join(want)} at {a.at!r}: {ex}{' …' if len(missing) > 5 else ''}. "
            f"Emitting the rest would hand training a set that reads as the "
            f"whole snapshot. Pass --allow-missing {len(missing)} to resolve the "
            f"{len(rows)} that are there — the count binds the acceptance to "
            f"what is measured now, not to a number remembered from before.")
    if not rows:
        die(f"nothing resolvable — no unit in {snap.get('cite_as', a.snapshot)} "
            f"has {', '.join(want)} at {a.at!r}")

    census_id = head.get("census_id") or snap.get("from_census")
    age = None
    try:
        then = datetime.fromisoformat(snap["frozen_at"])
        age = (datetime.now(timezone.utc) - then).days
    except (ValueError, KeyError, TypeError):
        age = None                        # unparseable, not zero

    header = {
        "_resolved": {
            "cite_as": snap.get("cite_as"),
            "dataset": snap.get("dataset") or a.dataset,
            "snapshot": a.snapshot,
            "at": a.at,
            "root": loc["root"],
            # `server` means these paths live on another machine: the run either
            # executes there or maps them — run-mechanics.md "Path Mapping (Cross-Machine Execution)".
            "reachable": ("local" if loc.get("via") == "local"
                          else f"server:{loc.get('server')}"),
            "layers": want,
            "count": len(rows),
            "excluded_missing": len(missing),
            "unverified_units": len(snap.get("unverified_units") or []),
            "unverifiable_units": len(snap.get("unverifiable_units") or []),
            "paths_true_as_of": census_id,
            "frozen_at": snap.get("frozen_at"),
            "snapshot_age_days": age,
            "resolved_at": now_utc(),
            "derived": "regenerate from the snapshot; never a frozen record. "
                       "Cite `cite_as` in run.json -> lineage.parents, not this file.",
        }
    }

    out = [json.dumps(header)] + [json.dumps(r) for r in rows]
    if a.out:
        dest = os.path.abspath(os.path.expanduser(a.out))
        if dest.startswith(os.path.abspath(sdir) + os.sep):
            die(f"refusing to write into {sdir} — a resolved path names one "
                f"machine, and the snapshot is machine-independent on purpose. "
                f"Write it beside the run that consumes it.")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, dest)

    if a.json:
        print(json.dumps(header["_resolved"], indent=2))
    elif a.out:
        h = header["_resolved"]
        print(f"resolved {h['count']} units of {h['cite_as']} at {a.at} "
              f"({h['reachable']}) -> {a.out}")
        print(f"  layers: {', '.join(want)}  ·  root {loc['root']}")
        print(f"  frozen {'?' if age is None else f'{age}d'} ago from census "
              f"{census_id or '?'} — paths were true then, nothing was stat'd now")
        if missing:
            print(f"  ⚠ {len(missing)} units excluded: layer absent at {a.at}")
        if h["unverified_units"] or h["unverifiable_units"]:
            print(f"  ⚠ snapshot carries {h['unverified_units']} unverified and "
                  f"{h['unverifiable_units']} unverifiable-complete units")
        print(f"\n  cite in run.json -> lineage.parents as: {h['cite_as']}")
    else:
        print("\n".join(out))


# -------------------------------------------------------------------- status

def _status_projects(a) -> list[str]:
    """Every project to report on: the workspace's declared ones, or just `a.project`."""
    if a.workspace:
        root = os.path.expanduser(a.workspace)
        return [os.path.join(root, n) for n in sorted(os.listdir(root))
                if os.path.isfile(os.path.join(root, n, "project.json"))]
    return [os.path.expanduser(a.project)]


def _status_row(p: str, ddir: str, name: str) -> dict:
    """One dataset's row: its latest census, read off disk. No network."""
    cpath = latest_census_path(ddir)
    row = {"project": os.path.basename(p), "dataset": name,
           "census": None, "scanned_at": None, "age_days": None,
           "complete": None, "totals": None,
           "snapshots": len(os.listdir(os.path.join(ddir, "snapshots")))
           if os.path.isdir(os.path.join(ddir, "snapshots")) else 0}
    if not cpath:
        return row
    c = read_json(cpath)
    row.update(census=c["census_id"], scanned_at=c["scanned_at"],
               complete=c.get("complete"), totals=c.get("totals"))
    try:
        then = datetime.fromisoformat(c["scanned_at"])
        row["age_days"] = (datetime.now(timezone.utc) - then).days
    except (ValueError, KeyError):
        row["age_days"] = None      # unparseable, not zero
    return row


def _status_rows(projects: list[str]) -> list[dict]:
    rows = []
    for p in projects:
        dsroot = os.path.join(p, "datasets")
        if not os.path.isdir(dsroot):
            continue
        for name in sorted(os.listdir(dsroot)):
            ddir = os.path.join(dsroot, name)
            if os.path.isfile(os.path.join(ddir, "dataset.json")):
                rows.append(_status_row(p, ddir, name))
    return rows


def _print_status_row(r: dict) -> None:
    if not r["census"]:
        print(f"  {r['dataset']:<20} never scanned")
        return
    t, age = r["totals"], r["age_days"]
    flags = [] if r["complete"] else ["PARTIAL"]
    for k in ("gap", "drift", "unreplicated", "unarchived", "incomplete"):
        if t.get(k):
            flags.append(f"{k}={t[k]}")
    print(f"  {r['dataset']:<20} {t['units']:>5} units  "
          f"scanned {'?' if age is None else f'{age}d'} ago  "
          f"{'  '.join(flags) or 'clean'}")


def cmd_status(a) -> None:
    """Records only — no network. Safe for "On Conversation Start"."""
    rows = _status_rows(_status_projects(a))
    if a.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        die("no datasets declared", 1)
    for r in rows:
        _print_status_row(r)


# ----------------------------------------------------------------------- cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, dataset=True):
        p.add_argument("--project", required=dataset)
        if dataset:
            p.add_argument("--dataset", required=True)
        p.add_argument("--json", action="store_true")

    s = sub.add_parser("scan", help="go ask every location what it holds")
    common(s)
    s.add_argument("--allow-unreachable", action="store_true",
                   help="accept a census whose counts are a lower bound")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("show", help="read back a census; touches nothing")
    common(s)
    s.add_argument("--census")
    s.add_argument("--units", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("snapshot", help="freeze which units, for lineage")
    common(s)
    s.add_argument("--id", required=True)
    s.add_argument("--layer")
    s.add_argument("--at")
    s.add_argument("--units-from")
    s.add_argument("--allow-incomplete", type=int, default=-1)
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("resolve", help="frozen membership -> openable paths")
    common(s)
    s.add_argument("--snapshot", required=True)
    # Required, never defaulted to the authority: picking a location silently is
    # how a run trains off a copy nobody meant it to read.
    s.add_argument("--at", required=True, help="which location's root to join onto")
    s.add_argument("--layer", action="append",
                   help="repeatable; default is every declared layer")
    s.add_argument("--allow-missing", type=int, default=-1)
    s.add_argument("--out", help="write JSONL here; default stdout. Never inside "
                                 "the snapshot dir")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("status", help="records only, no network")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--project")
    g.add_argument("--workspace")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    try:
        a.fn(a)
    except SystemExit:
        raise
    except Exception as e:                       # exit 2 = the script broke
        die(f"census.py failed: {type(e).__name__}: {e}", 2)


if __name__ == "__main__":
    main()
