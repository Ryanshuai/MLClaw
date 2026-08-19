#!/usr/bin/env python3
"""Where is this dataset on the line, and what is allowed to happen to it next.

    Collect  ->  Label  ->  Curate  ->  Freeze  ->  Retire

Nothing else can answer this, and that is the only reason this script exists.
A dataset's position is a join across four record types living in three
directories — `datasets/<id>/census/`, `datasets/<id>/snapshots/`,
`{PROJECT}/handoffs/`, and `stages/*/runs/` — and no single skill sees all four.
`census.py` deliberately sees only the world's state; `/data-label` sees one
exchange. Neither is wrong; they are just each looking at one box.

This is a router, not a dashboard. It earns its place on three computed facts,
each of which is silent today:

  phase       the join above
  staleness   a snapshot frozen BEFORE inflow that has since been accepted. The
              citation still resolves and still looks authoritative, so "I
              trained on the latest data" is false with nothing raising.
  gates       transitions with preconditions nothing enforces — freezing while a
              handoff is still open, curating off units that are one disk from
              gone, consuming a snapshot whose census never saw every machine.

`history` is the same three facts replayed at every past census, which is what
the board's time axis is drawn from. It exists here rather than in the renderer
for the reason `phase` does: a second implementation of these rules is a second
set of answers, and the one on the wall is the one people believe.

Exit codes per CLAUDE.md "Script Integration": 1 = the script worked and the
answer is no (a gate refused); 2 = the script broke, do it by hand. `phase` and
`history` never exit 1 — reporting a bad position is not a refusal.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (age_days, broke, emit, parse_ts, read_json, refuse)  # noqa: E402
from _dataset_paths import dataset_dir, latest_census  # noqa: E402
from _vocab import HANDOFF_TERMINAL as TERMINAL_HANDOFF  # noqa: E402

# The line, in order. `retire` is present but is never RETURNED as a position:
# retirement is an action on units, not a state a dataset arrives at. A dataset
# that has had forty days deleted off the rig is still `ready` — so what this
# reports for retire is what has happened to it, not where it stands.
PHASES = ("collect", "label", "curate", "freeze", "ready", "retire")
GATES = ("freeze", "curate", "consume")
DEFAULT_STALE_DAYS = 14.0


# --------------------------------------------------------------------------- #
# gathering the four record types
# --------------------------------------------------------------------------- #

def list_datasets(project):
    root = os.path.join(os.path.expanduser(project), "datasets")
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if os.path.isfile(os.path.join(root, d, "dataset.json")))


def _census_scanned_at(ddir, census_id):
    """`scanned_at` of the census a snapshot was frozen from. None when the
    census file is gone — which makes staleness undeterminable, and that is
    reported rather than defaulted to fresh."""
    if not census_id:
        return None
    rec = read_json(os.path.join(ddir, "census", f"{census_id}.json"), required=False)
    return (rec or {}).get("scanned_at")


def snapshots(ddir):
    sdir = os.path.join(ddir, "snapshots")
    if not os.path.isdir(sdir):
        return []
    snaps = []
    for sid in sorted(os.listdir(sdir)):
        snap = read_json(os.path.join(sdir, sid, "snapshot.json"), required=False)
        if snap:
            snaps.append(snap)
    snaps.sort(key=lambda s: s.get("frozen_at") or "")
    return snaps


def handoffs_for(project, dataset):
    """Every handoff naming this dataset. A handoff with `dataset: null` is
    excluded — it feeds no dataset, so it is not this dataset's inflow."""
    root = os.path.join(os.path.expanduser(project), "handoffs")
    if not os.path.isdir(root):
        return []
    handoffs = []
    for hid in sorted(os.listdir(root)):
        rec = read_json(os.path.join(root, hid, "handoff.json"), required=False)
        if rec and rec.get("dataset") == dataset:
            handoffs.append(rec)
    return handoffs


def retirements(ddir):
    """What has been deleted off this dataset, from the records that outlived
    it. Cheap — these are small JSON files in the project, and the bytes they
    describe are on another machine and are gone. Plans are skipped: a plan is
    not a deletion, and counting one would report data as gone that is still
    on the disk."""
    rdir = os.path.join(ddir, "retire")
    if not os.path.isdir(rdir):
        return []
    records = []
    for f in sorted(os.listdir(rdir)):
        if f.endswith("_plan.json") or not f.endswith(".json"):
            continue
        rec = read_json(os.path.join(rdir, f), required=False)
        if rec:
            records.append({"retire_id": rec.get("retire_id"), "at": rec.get("at"),
                        "finished_at": rec.get("finished_at"),
                        "status": rec.get("status"),
                        "units": len(rec.get("deleted") or []),
                        "waived": rec.get("waived") or [],
                        "because": rec.get("because")})
    return records


def consumers(project, dataset):
    """Runs citing this dataset in lineage.parents, as `datasets/<id>@<sid>`.
    Read from the run tree directly rather than from `dataset.json -> consumers`,
    which is advisory and lags; the citing run's own record is authoritative."""
    root = os.path.join(os.path.expanduser(project), "stages")
    if not os.path.isdir(root):
        return []
    prefix = f"datasets/{dataset}@"
    citers = []
    for stage in sorted(os.listdir(root)):
        rdir = os.path.join(root, stage, "runs")
        if not os.path.isdir(rdir):
            continue
        for run in sorted(os.listdir(rdir)):
            rec = read_json(os.path.join(rdir, run, "run.json"), required=False)
            if not rec:
                continue
            for parent in (rec.get("lineage") or {}).get("parents") or []:
                if isinstance(parent, str) and parent.startswith(prefix):
                    citers.append({"run": f"{stage}/{run}", "cites": parent,
                                "status": rec.get("status")})
    return citers


# --------------------------------------------------------------------------- #
# replaying a past moment
#
# A timeline is only worth drawing if each column shows what was true THEN. The
# records on disk are all current: a snapshot frozen this morning sits in the
# same directory as one from June, and reading them straight would make every
# past column report `ready` the moment anything was ever frozen. So a replay
# filters every record to what existed at that census's `scanned_at`.
#
# The hard half is that "existed" needs a timestamp, and a record whose
# timestamp is missing or naive cannot be placed in time at all. Dropping those
# silently would replay the moment as though the record had never been made;
# including them would invent history. They are reported, and the column is
# marked as a partial replay — run-mechanics.md "Record integrity", applied to
# time instead of to metrics.
# --------------------------------------------------------------------------- #

def _handoff_as_of(h, as_of):
    """What this handoff's status WAS at `as_of`.

    -> ("absent" | "open" | <terminal status>, None), or (None, why) when it
    cannot be placed in time.
    """
    created = parse_ts(h.get("created_at"))
    if created is None:
        return None, "created_at is missing or carries no UTC offset"
    if created > as_of:
        return "absent", None
    st = h.get("status")
    if st not in TERMINAL_HANDOFF:
        return "open", None
    closed = parse_ts(h.get("closed_at"))
    if closed is None:
        return None, f"status is {st!r} but closed_at is missing or carries no offset"
    # Terminal NOW is not terminal THEN, and this is the whole point of a
    # replay: a batch accepted on Friday was still out the Tuesday before, and a
    # column that shows it closed is describing a week that did not happen.
    return (st, None) if closed <= as_of else ("open", None)


def _existing_as_of(entries, key, as_of, kind, id_key):
    """-> (records that existed at `as_of`, records that could not be placed)."""
    kept, unplaceable = [], []
    for it in entries:
        when = parse_ts(it.get(key))
        if when is None:
            unplaceable.append({"kind": kind, "id": it.get(id_key),
                                "reason": f"{key} is missing or carries no UTC offset"})
        elif when <= as_of:
            kept.append(it)
    return kept, unplaceable


# --------------------------------------------------------------------------- #
# the three computed facts
# --------------------------------------------------------------------------- #

def assess(project, dataset, stale_days, *, census_id=None):
    """Where this dataset stands. With `census_id`, where it stood at that scan.

    Replay mode is not a second implementation — it is the same rules over a
    time-filtered set of records, which is why it lives here rather than in the
    renderer. A board that worked out its own history would be a second set of
    answers, and the one on the wall is the one people believe.
    """
    ddir = dataset_dir(project, dataset)
    cfg = read_json(os.path.join(ddir, "dataset.json"))
    snaps = snapshots(ddir)
    hos = handoffs_for(project, dataset)
    cons = consumers(project, dataset)
    rets = retirements(ddir)

    if census_id is None:
        census, as_of = latest_census(ddir), None
    else:
        census = read_json(os.path.join(ddir, "census", f"{census_id}.json"),
                           required=False)
        if census is None:
            broke(f"no census {census_id!r} for {dataset!r}")
        as_of = parse_ts(census.get("scanned_at"))
        if as_of is None:
            broke(f"census {census_id} has no placeable scanned_at, so there is "
                  f"no moment to replay")

    layers = {l["label"]: l for l in cfg.get("layers") or []}
    unplaceable = []

    if as_of is None:
        open_handoffs = [h for h in hos if h.get("status") not in TERMINAL_HANDOFF]
        accepted = [h for h in hos if h.get("status") == "accepted"]
    else:
        snaps, u = _existing_as_of(snaps, "frozen_at", as_of, "snapshot", "snapshot_id")
        unplaceable += u
        rets, u = _existing_as_of(rets, "finished_at", as_of, "retirement", "retire_id")
        unplaceable += u
        open_handoffs, accepted = [], []
        for h in hos:
            st, why = _handoff_as_of(h, as_of)
            if st is None:
                unplaceable.append({"kind": "handoff", "id": h.get("handoff_id"),
                                    "reason": why})
            elif st == "open":
                open_handoffs.append(h)
            elif st == "accepted":
                accepted.append(h)

    newest_snap = snaps[-1] if snaps else None
    blockers = []
    if unplaceable:
        blockers.append({
            "blocker": "replay_incomplete", "severity": "warns",
            "detail": f"{len(unplaceable)} record(s) could not be placed in time, "
                      f"so this column is a partial replay",
            "note": "not placed is not absent",
            "records": unplaceable})

    # --- census reach. This one gates everything downstream, because every
    # count below it is a lower bound. CLAUDE.md "Never report data you could
    # not look at": a machine that did not answer and an empty disk are two
    # facts, and only one of them means the data is not there.
    if census is None:
        blockers.append({"blocker": "no_census", "severity": "blocks",
                         "detail": "nothing has looked at this dataset yet",
                         "fix": "census.py scan"})
    else:
        if not census.get("complete"):
            blockers.append({
                "blocker": "census_incomplete", "severity": "blocks",
                "detail": f"{len(census.get('unreachable') or [])} location(s) did not "
                          f"answer: {', '.join(census.get('unreachable') or [])}",
                "note": "every count here is a LOWER BOUND, not an inventory",
                "fix": "fix reach and re-scan, or accept a lower bound knowingly"})
        # Age is measured from now, so it means nothing in a replay: at the
        # moment of a census, that census is zero days old. Reporting it here
        # would put today's staleness on every historical column.
        n = age_days(census.get("scanned_at"))
        if as_of is None and n is not None and n >= stale_days:
            blockers.append({"blocker": "census_stale", "severity": "warns",
                             "detail": f"last scan was {n} days ago",
                             "fix": "census.py scan"})

    totals = (census or {}).get("totals") or {}

    # --- source layers one disk from gone. Deriving from them is the case worth
    # stopping: the compute succeeds, and the thing it was derived from is still
    # unique on one machine that is still one failure from taking it with it.
    if totals.get("unreplicated"):
        blockers.append({
            "blocker": "unreplicated", "severity": "blocks_curate",
            "detail": f"{totals['unreplicated']} source-layer unit(s) under min copies",
            "fix": "replicate before deriving from them"})
    if totals.get("unarchived"):
        blockers.append({
            "blocker": "unarchived", "severity": "warns",
            "detail": f"{totals['unarchived']} unit(s) never left the machine that made them",
            "fix": "copy to the authority — this risk peaks now, not at the end"})
    # "not asked" is not "none found": if the authority did not answer, UNARCHIVED
    # was never computed and a zero here means nothing.
    if census is not None and totals.get("unarchived_checked") is False:
        blockers.append({"blocker": "unarchived_unchecked", "severity": "warns",
                         "detail": "the authority did not answer, so UNARCHIVED was "
                                   "never computed — this is not a clean result"})

    # --- staleness: the one nothing else can see. A snapshot frozen before
    # inflow that has since been accepted still resolves and still reads as
    # authoritative. Nobody is told it predates the newest data.
    stale_against, undetermined = [], []
    if newest_snap:
        # Compare against the census the snapshot FROZE FROM, not against its
        # freeze time. A snapshot's contents come from that scan: freezing at
        # 5pm off a census scanned yesterday cannot include data that landed at
        # noon, however recent the freeze timestamp looks. Using `frozen_at`
        # here reports such a snapshot as current — the exact failure this
        # blocker exists to catch, inverted.
        seen = parse_ts(_census_scanned_at(ddir, newest_snap.get("from_census")))
        for h in accepted:
            got = parse_ts(h.get("closed_at"))
            row = {"handoff": h["handoff_id"], "accepted_at": h.get("closed_at"),
                   "census_scanned_at": newest_snap.get("from_census"),
                   "coverage": (h.get("accepted") or {}).get("coverage")}
            if seen is None or got is None:
                # A timestamp that cannot be compared is not evidence of
                # freshness. Recording this as "not stale" would be the
                # extraction-failure-vs-absence bug from run-mechanics.md
                # "Record integrity", committed by the checker itself.
                undetermined.append(dict(row, reason="a timestamp is missing or "
                                                     "carries no UTC offset"))
            elif not seen > got:
                # Not `got > seen`: equal timestamps mean the scan and the
                # acceptance are indistinguishable at this resolution, and an
                # ambiguous ordering must not read as clean.
                stale_against.append(row)
        if stale_against:
            blockers.append({
                "blocker": "snapshot_stale", "severity": "blocks_consume",
                "detail": f"snapshot {newest_snap['snapshot_id']} froze from census "
                          f"{newest_snap.get('from_census')}, which did not see "
                          f"{len(stale_against)} accepted handoff(s)",
                "note": "the citation still resolves — that is why this is silent",
                "fix": "census.py scan, then snapshot again; or cite it knowingly"})
        if undetermined:
            blockers.append({
                "blocker": "staleness_undetermined", "severity": "blocks_consume",
                "detail": f"could not order {len(undetermined)} handoff(s) against "
                          f"the snapshot's census",
                "note": "not checked is not clean",
                "fix": "repair the timestamps, or cite knowingly"})

    if open_handoffs:
        blockers.append({
            "blocker": "inflow_in_flight", "severity": "blocks_freeze",
            "detail": f"{len(open_handoffs)} handoff(s) still out: " +
                      ", ".join(f"{h['handoff_id']}({h.get('status')})" for h in open_handoffs),
            "fix": "close them, or freeze knowing this set excludes them"})

    # --- phase: first thing on the line that is not done.
    phase, why, nxt, nxt_skill = _phase(cfg, census, totals, layers, open_handoffs,
                                        newest_snap, stale_against + undetermined)

    return {
        "dataset": dataset,
        "phase": phase,
        "why": why,
        "next": nxt,
        "next_skill": nxt_skill,
        # Present only in replay mode, and `complete: false` here means the
        # column is a partial reconstruction of that moment — a different fact
        # from the census's own `complete`, which is about machines that did
        # not answer. Both can be false at once and they do not mean the same
        # thing.
        "replay": None if as_of is None else {
            "as_of": census.get("scanned_at"),
            "complete": not unplaceable,
            "unplaceable": unplaceable,
        },
        "blockers": blockers,
        "census": None if census is None else {
            "census_id": census.get("census_id"),
            "scanned_at": census.get("scanned_at"),
            "age_days": age_days(census.get("scanned_at")),
            "complete": census.get("complete"),
            "counts_are_lower_bound": not census.get("complete"),
            "totals": totals,
        },
        "handoffs": {"open": len(open_handoffs), "accepted": len(accepted),
                     "total": len(hos)},
        "snapshots": [{"snapshot_id": s["snapshot_id"], "frozen_at": s.get("frozen_at"),
                       "cite_as": s.get("cite_as"),
                       "unverified_units": len(s.get("unverified_units") or [])}
                      for s in snaps],
        "stale_against": stale_against,
        "staleness_undetermined": undetermined,
        "consumers": cons,
        # Never a returned phase — see PHASES. What a reader needs here is what
        # has already been deleted, because a count in a census is a count of
        # what is left and says nothing about what used to be there.
        "retire": {
            "is_a_phase": False,
            "note": "retirement is an action on units, not a position on the "
                    "line; a dataset that has had units deleted is still where "
                    "its remaining units put it",
            "retirements": rets,
            "units_deleted": sum(r["units"] for r in rets),
            "waived": sorted({w for r in rets for w in r["waived"]}),
        },
    }


def _phase(cfg, census, totals, layers, open_handoffs, newest_snap, stale_against):
    """First box on the line that is not satisfied. Order matters: it is the
    line's order, so the answer is always the earliest unfinished thing rather
    than the most alarming one.

    -> (phase, why, next_command, next_skill). Both `next` fields, because they
    answer different questions: the command is what to type, the skill is what
    `/data` composes. CLAUDE.md "Status" says composition goes through the Skill
    Dependency Graph and nothing else, so a phase that could only name a command
    would be a phase `/data` cannot route.
    """
    if census is None:
        return ("collect", "no census yet — nothing has looked",
                "census.py scan", "/data-check")

    if totals.get("units", 0) == 0:
        # Nothing captured, or the glob's depth is wrong — and the second is
        # silent, which is why this routes to the skill that owns the layout
        # contract rather than assuming the data is simply not there yet.
        return ("collect", "the census found no units",
                "check identity.unit_glob depth, then census.py scan", "/data-check")

    if totals.get("incomplete") or totals.get("partial"):
        n = (totals.get("incomplete") or 0) + (totals.get("partial") or 0)
        return ("collect", f"{n} unit(s) have nothing claiming they finished",
                "let capture finish, or fix completeness.marker and re-scan",
                "/data-check")

    if open_handoffs:
        return ("label", f"{len(open_handoffs)} handoff(s) still out",
                "handoff.py status --open-only", "/data-label")

    # A GAP routes by the layer's own `produced_by`, which is where /data-check
    # already sends people. Whether a missing layer is Label work or Curate work
    # is a property of that layer, not something to re-derive here.
    gaps = (census.get("verdicts") or {}).get("gap") or {}
    if gaps:
        label = sorted(gaps)[0]
        by = (layers.get(label) or {}).get("produced_by")
        if isinstance(by, str) and by.startswith("handoff"):
            return ("label", f"layer {label!r} is missing everywhere",
                    f"/data-label ({by})", "/data-label")
        if isinstance(by, str) and by.startswith("run:"):
            # The layer is made by an MLClaw stage, so that stage's run skill is
            # the composer's next step — not /data-curate, which records
            # derivations rather than producing layers in place.
            return ("curate", f"layer {label!r} is missing everywhere",
                    by, f"/{by.split(':', 1)[1]}-run")
        return ("curate", f"layer {label!r} is missing everywhere",
                by or "unknown — layers[].produced_by is not set for this layer",
                "/data-curate")

    if newest_snap is None:
        return ("freeze", "everything present and complete, nothing frozen yet",
                "census.py snapshot", "/data-freeze")

    if stale_against:
        return ("freeze",
                f"newest snapshot predates {len(stale_against)} accepted handoff(s)",
                "census.py scan, then census.py snapshot", "/data-freeze")

    # No skill: the data line is done with it, and what happens next belongs to
    # the model lifecycle. Naming one here would make /data compose across the
    # boundary the two lifecycles deliberately meet at rather than cross.
    return ("ready", f"{newest_snap['snapshot_id']} is current",
            f"cite {newest_snap.get('cite_as')} in run.json -> lineage.parents", None)


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #

def census_ids(ddir):
    """Every census this dataset has, oldest first. The ids carry a sortable
    timestamp, which is why this is a filename sort and not a read of each one."""
    cdir = os.path.join(ddir, "census")
    if not os.path.isdir(cdir):
        return []
    return sorted(f[:-5] for f in os.listdir(cdir)
                  if f.startswith("census_") and f.endswith(".json"))


def cmd_history(a):
    """One replayed assessment per census — the timeline the board draws.

    Each row is `assess(census_id=...)`, so the history and the current view come
    out of the same rules. What this verb does NOT do is decide what a dataset
    looked like on a day it was not scanned: censuses are irregular and the gaps
    are real. Filling them in is the renderer's carry-forward, and it has to say
    so, because "we last looked eleven days ago" is the single most important
    thing a timeline can tell you and interpolation is exactly what hides it.
    """
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    names = [a.dataset] if a.dataset else list_datasets(project)

    rows, axis = [], set()
    for d in names:
        ddir = dataset_dir(project, d)
        ids = census_ids(ddir)
        if a.last:
            ids = ids[-int(a.last):]
        timeline = []
        for cid in ids:
            st = assess(project, d, a.stale_days, census_id=cid)
            c = st["census"] or {}
            axis.add(c.get("scanned_at"))
            timeline.append({
                "census_id": cid,
                "scanned_at": c.get("scanned_at"),
                "phase": st["phase"], "why": st["why"],
                "next": st["next"], "next_skill": st["next_skill"],
                "census_complete": c.get("complete"),
                "counts_are_lower_bound": c.get("counts_are_lower_bound"),
                "totals": c.get("totals") or {},
                "handoffs": st["handoffs"],
                "snapshots": [s["snapshot_id"] for s in st["snapshots"]],
                "stale_against": len(st["stale_against"]),
                "staleness_undetermined": len(st["staleness_undetermined"]),
                "units_deleted": st["retire"]["units_deleted"],
                "blockers": [{k: b.get(k) for k in ("blocker", "severity", "detail", "fix")}
                             for b in st["blockers"]],
                "replay": st["replay"],
            })
        rows.append({"dataset": d, "timeline": timeline,
                    "first_seen": timeline[0]["scanned_at"] if timeline else None})

    emit({
        "project": project,
        # The shared x axis. Datasets are scanned on their own schedules, so the
        # union is the only axis on which two rows can be compared at all — and
        # a dataset with no census at a given column has not been interpolated,
        # it simply has nothing there.
        "axis": sorted(t for t in axis if t),
        "datasets": rows,
        "stale_threshold_days": a.stale_days,
    })


def cmd_phase(a):
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    names = [a.dataset] if a.dataset else list_datasets(project)
    if not names:
        return emit({"datasets": [], "note": "no datasets declared in this project"})
    statuses = [assess(project, d, a.stale_days) for d in names]
    emit({"line": " -> ".join(PHASES[:-2] + ("ready",)),
          "datasets": statuses,
          "blocked": sum(1 for d in statuses
                         if any(b["severity"] == "blocks" for b in d["blockers"])),
          "stale_threshold_days": a.stale_days})


# Which blockers stop which transition. A blocker that stops everything has
# severity `blocks`; the rest name their one transition, because a gate that
# fires on everything trains people to pass the override.
STOPS = {
    "freeze":  ("blocks", "blocks_freeze"),
    "curate":  ("blocks", "blocks_curate"),
    "consume": ("blocks", "blocks_consume"),
}


def cmd_gate(a):
    if a.to not in GATES:
        broke(f"unknown gate {a.to!r}", allowed=list(GATES))
    project = os.path.expanduser(a.project)
    st = assess(project, a.dataset, a.stale_days)
    severities = STOPS[a.to]
    hits = [b for b in st["blockers"] if b["severity"] in severities]

    if hits and not a.acknowledge:
        refuse(f"{len(hits)} precondition(s) block {a.to} for {a.dataset}",
               phase=st["phase"], blockers=hits,
               fix=f"--acknowledge {len(hits)} to proceed anyway, naming what "
                   f"is being accepted in the same breath")
    if hits and int(a.acknowledge) != len(hits):
        refuse(f"--acknowledge {a.acknowledge} does not match the {len(hits)} "
               f"precondition(s) measured now",
               why="the count binds acceptance to what this assessment found, "
                   "not to a number remembered from a previous one",
               blockers=hits)
    emit({"dataset": a.dataset, "gate": a.to, "phase": st["phase"],
          "passed": True, "acknowledged": hits,
          "warnings": [b for b in st["blockers"] if b["severity"] == "warns"]})


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("phase", help="where each dataset is, and what is next")
    s.add_argument("--project", required=True)
    s.add_argument("--dataset", default=None)
    s.add_argument("--stale-days", type=float, default=DEFAULT_STALE_DAYS)
    s.set_defaults(fn=cmd_phase)

    h = sub.add_parser("history", help="one replayed assessment per census")
    h.add_argument("--project", required=True)
    h.add_argument("--dataset", default=None)
    h.add_argument("--last", type=int, default=None,
                   help="only the N most recent censuses per dataset")
    h.add_argument("--stale-days", type=float, default=DEFAULT_STALE_DAYS)
    h.set_defaults(fn=cmd_history)

    g = sub.add_parser("gate", help="refuse a transition whose preconditions fail")
    g.add_argument("--project", required=True)
    g.add_argument("--dataset", required=True)
    g.add_argument("--to", required=True, choices=GATES)
    g.add_argument("--acknowledge", default=None,
                   help="restate the blocker count to proceed anyway")
    g.add_argument("--stale-days", type=float, default=DEFAULT_STALE_DAYS)
    g.set_defaults(fn=cmd_gate)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
