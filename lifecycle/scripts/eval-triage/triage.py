#!/usr/bin/env python3
"""triage.py — what a bad case actually is, and who owns fixing it.

An aggregate metric says the model got worse. Per-sample records say where. Neither
says WHY, and the why decides everything: the same 40 images ranked at the bottom
of an eval contain three different problems with three different owners, and
routing all of them to "add more data like this" is the one action that makes two
of the three worse.

  rank     read the run's per-sample records, rank worst-first, open a session.
           The pile is CANDIDATES — never "hard examples", see below
  judge    record one verdict on one case, from an agent or a person
  confirm  a second, independent look. This is the only thing that produces
           `verified`; nothing else can, whatever it passes
  route    group the reviewed cases into three piles with three owners, and
           refuse the ones that would do damage
  status   open sessions across this project's eval runs

WHY THE RANKING IS NOT THE ANSWER. Sorting by worst per-sample score surfaces
label errors first, not hard examples: a mislabeled box is a target the model
cannot satisfy, so its loss stays at the top forever. Feed that pile back as
"hard cases to add" and you have amplified the annotation noise that produced it,
which raises those losses further, which selects more of them next round. So this
script never emits a reflow list before review, and `route` refuses to put a
`label_wrong` unit in the hard-example pile at all.

WHY LOOKING IS A CLAIM. Deciding whether a box is wrong or merely hard requires
seeing the image, and both an agent and a person do that by looking. CLAUDE.md
"Never let somebody's word become a checked fact" does not exempt the agent: one
model's judgement is one source. `verified` here requires two judgements from two
DIFFERENT KINDS of source agreeing (agent + human, or anything + gold), so two
agent passes over the same image can never reach it — they are one source sampled
twice, and their agreement measures the model's consistency, not the label.

WHAT THIS IS BLIND TO, stamped into every record it writes: the eval set was cut
from the training distribution, so no ranking over it can contain a sample from a
region the training data never covered. That failure is `/data-drift`'s, and the
two are complements — neither can serve as the other's alarm.

Exit codes per CLAUDE.md "Script Integration": 0 ok; 1 = the script worked and
the answer is no; 2 = the script broke, do it by hand.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402

# What a bad case can be. Free text would make the piles unqueryable and the
# routing unarguable; these four are exhaustive over "the model scored badly on
# this sample" and each has a different owner, which is the whole point.
VERDICTS = ("label_wrong", "sample_hard", "model_wrong", "unclear")

# Who looked. The ordering is authority on disagreement, not trustworthiness in
# general: a person overrules an agent about what is in an image, and a known
# answer overrules both. `verified` needs two DISTINCT kinds regardless of order.
AUTHORITY = {"gold": 3, "human": 2, "agent": 1}

# Where each verdict goes, and the sentence that says why it goes there. The
# third never leaves the model line — it is the one whose owner is not the data
# line at all, and conflating it with the second is how "we need more data"
# becomes the answer to a modelling problem.
OWNERS = {
    "label_wrong": ("/data-label",
                    "a rework round: the annotation is wrong, so fix it at the "
                    "source. Adding more samples like this amplifies the error"),
    "sample_hard": ("the data line (/data-label kind:data_request, or /data-collect)",
                    "the label is right and the sample is genuinely difficult — "
                    "this is the only pile that legitimately becomes more data"),
    "model_wrong": ("stages/training/config.json -> param_injection and the "
                    "training config",
                    "label right, sample ordinary, model still wrong. More data "
                    "does not fix this and never leaves the model line"),
    "unclear": (None,
                "not routable: nobody could tell from looking. Needs a closer "
                "look or a second reviewer, not a destination"),
}

CITE = re.compile(r"^datasets/(?P<ds>[^@/]+)@(?P<snap>[^@/]+)$")

# Said in every record this script writes, because a pile of bad cases reads as
# "the model's problems" and is not: it cannot contain a sample from a region the
# training distribution never covered, since the eval set came from that same
# distribution.
BLIND_TO = ("distribution shift — the eval set was cut from the training "
            "distribution, so no sample from an uncovered region can appear in "
            "this ranking however deep it goes. That is /data-drift's, and "
            "neither skill can serve as the other's alarm")


def eval_run_dir(project, run_id) -> str:
    """The stage is **hardcoded** to `evaluation`, which is correct here and only
    here: /eval-triage operates on evaluation runs by definition.

    Named for that, because `repro.py -> resolve_run_ref` exists specifically to
    *refuse* a bare run_id -- the stage is what decides whether two numbers are
    the same quantity (CLAUDE.md: "Never compare metrics across different mode or
    non-equivalent scope"). Both were called `run_dir`, so a reader who learned
    the name here would write `run_dir(project, "run_003")` somewhere new and
    silently get an evaluation path.
    """
    return os.path.join(os.path.expanduser(project), "stages", "evaluation",
                        "runs", run_id)


def session_path(project, run_id, sid) -> str:
    return os.path.join(eval_run_dir(project, run_id), "triage", sid, "session.json")


# --------------------------------------------------------------------------- #
# reading the per-sample file
# --------------------------------------------------------------------------- #

def load_records(path, fmt, records_at):
    """The per-sample file as a list of dicts.

    A declared file that is not on disk is a refusal, not an empty list. The two
    are different facts — "the eval wrote no per-sample output" and "we could not
    read what it wrote" — and CLAUDE.md "Never record a metric you did not read"
    is the same rule one level up: collapsing them makes an extraction failure
    read as a clean model.
    """
    if not os.path.isfile(path):
        refuse(f"per_sample.path is declared but not on disk: {path}",
               why="declared-and-absent is not the same fact as never-written. "
                   "Either the eval run did not get far enough to write it, or "
                   "the declared path is wrong — both are fixable, and neither "
                   "is 'no bad cases'",
               fix="check the run's output/ dir, then stages/evaluation/"
                   "output.json -> per_sample.path")
    try:
        with open(path, encoding="utf-8") as fh:
            if fmt == "jsonl":
                records = []
                for n, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as exc:
                        broke(f"{path}:{n} is not valid JSON: {exc}")
                    if isinstance(rec, dict):
                        records.append(rec)
                return records
            if fmt == "json":
                blob = json.load(fh)
                if records_at:
                    if not isinstance(blob, dict) or records_at not in blob:
                        broke(f"records_at {records_at!r} is not a key in {path}")
                    blob = blob[records_at]
                if not isinstance(blob, list):
                    broke(f"{path} does not hold an array of records"
                          f"{' at ' + records_at if records_at else ''}")
                return [r for r in blob if isinstance(r, dict)]
            if fmt == "csv":
                return list(csv.DictReader(io.StringIO(fh.read())))
        broke(f"unknown per_sample.format: {fmt!r} (jsonl | json | csv)")
    except OSError as exc:
        broke(f"cannot read {path}: {exc}")


def load_manifest_units(project, cite) -> set | None:
    """The frozen unit ids behind a `datasets/<id>@<snap>` citation, or None.

    None means the question could not be answered — no citation, or the snapshot
    is not in this project. It is deliberately not an empty set: "this unit is
    not in the frozen set" and "we have no frozen set to check against" send
    `route` down different paths, and only the first is a finding.
    """
    m = CITE.match(cite or "")
    if not m:
        return None
    mp = os.path.join(os.path.expanduser(project), "datasets", m.group("ds"),
                      "snapshots", m.group("snap"), "manifest.jsonl")
    if not os.path.isfile(mp):
        return None
    units = set()
    try:
        with open(mp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if isinstance(rec, dict) and "unit" in rec:
                    units.add(rec["unit"])
    except (OSError, json.JSONDecodeError):
        return None
    return units or None


def resolve_unit(raw, how, frozen) -> tuple[str | None, str]:
    """Map an eval record's sample id onto a dataset unit id.

    Returns (resolved_or_None, one of resolved | unresolved | unverifiable).

    `unverifiable` is the third state and the common one: no snapshot was cited,
    so nothing can confirm the mapping either way. It must not read as
    `resolved` — an unroutable finding discovered at routing time is recoverable,
    one discovered by a vendor who received a unit id that means nothing is not.
    """
    if frozen is None:
        return (raw if how == "unit_id" else None), "unverifiable"
    if how == "unit_id":
        return (raw, "resolved") if raw in frozen else (None, "unresolved")
    if how == "basename":
        stem = os.path.splitext(os.path.basename(str(raw)))[0]
        hits = [u for u in frozen
                if u == stem or os.path.basename(u) == stem
                or os.path.splitext(os.path.basename(u))[0] == stem]
        return (hits[0], "resolved") if len(hits) == 1 else (None, "unresolved")
    return None, "unresolved"


# --------------------------------------------------------------------------- #
# provenance — derived, never passed in
# --------------------------------------------------------------------------- #

def settle(case) -> None:
    """Recompute a case's standing verdict and provenance from its judgements.

    Nothing anywhere may set `provenance` directly; that is the point. A verb
    that accepted `--verified` would let one look assert two, which is the exact
    substitution CLAUDE.md "Never let somebody's word become a checked fact"
    exists to stop — and an agent is somebody.

    `verified` requires two judgements from two DIFFERENT KINDS agreeing. Two
    agent passes over the same image are one source sampled twice: their
    agreement measures the model's consistency, not the label, so they stay a
    claim and say why in `caveats`.
    """
    js = case["judgments"]
    case["caveats"] = [c for c in case.get("caveats", [])
                       if not c.startswith(("agent_overruled:", "corroborated_by_",
                                            "overruled:"))]
    if not js:
        case["verdict"], case["provenance"] = None, "unreviewed"
        return

    top = max(AUTHORITY.get(j["by"], 0) for j in js)
    top_group = [j for j in js if AUTHORITY.get(j["by"], 0) == top]
    if len({j["verdict"] for j in top_group}) > 1:
        # Equals disagreeing. There is no tie-break that is not a coin flip, and
        # a coin flip written into the record reads afterwards as a finding.
        case["verdict"], case["provenance"] = None, "disputed"
        case["caveats"].append(
            "disputed: " + "; ".join(f"{j['by']} said {j['verdict']}" for j in top_group))
        return

    verdict = top_group[0]["verdict"]
    agreeing = [j for j in js if j["verdict"] == verdict]
    kinds = {j["by"] for j in agreeing}
    case["verdict"] = verdict
    case["provenance"] = "verified" if len(kinds) >= 2 else "claim"

    overruled = [j for j in js if j["verdict"] != verdict]
    if overruled:
        # The disagreement is kept, not resolved away. Whether the agent's calls
        # can be trusted on this dataset is answerable only by how often a person
        # overruled it, and that is computable only if the overrules survive.
        case["caveats"].append(
            "overruled: " + "; ".join(f"{j['by']} said {j['verdict']}" for j in overruled))
    if case["provenance"] == "claim" and len(agreeing) > 1 and kinds == {"agent"}:
        case["caveats"].append(
            "corroborated_by_agent_only: two passes of one model agreeing is one "
            "source sampled twice, so this is still a claim")


def load_session(project, run_id, sid):
    p = session_path(project, run_id, sid)
    s = read_json(p, required=False)
    if s is None:
        broke(f"no triage session {sid} under {run_id}",
              hint="`status --project <p>` lists them")
    return s, p


def find_case(session, unit):
    for c in session["cases"]:
        if c["unit"] == unit or c.get("resolved_unit") == unit:
            return c
    refuse(f"{unit!r} is not a case in this session",
           why="only ranked candidates can be judged; a unit nobody ranked has "
               "no score behind it and would enter the piles unmeasured",
           cases=len(session["cases"]))


# --------------------------------------------------------------------------- #
# rank
# --------------------------------------------------------------------------- #

def cmd_rank(a) -> None:
    project = os.path.expanduser(a.project)
    rdir = eval_run_dir(project, a.run)
    rec = read_json(os.path.join(rdir, "run.json"))
    if rec.get("status") != "completed":
        refuse(f"{a.run} is {rec.get('status')!r}, not completed",
               why="a partial per-sample file ranks a truncated pass over the "
                   "eval set, and nothing downstream would say so")

    stage = os.path.join(project, "stages", "evaluation")
    output_json = read_json(os.path.join(stage, "output.json"))
    ps = output_json.get("per_sample") or {}
    if not ps.get("path"):
        refuse("stages/evaluation/output.json -> per_sample.path is not declared",
               why="no per-sample records means there is nothing to rank. This is "
                   "a legitimate state, not a broken one — most eval code writes "
                   "such a file and MLClaw never used to ask for it",
               fix="/eval-init Step 1c: find where the eval code writes one "
                   "record per sample, and record the field that scores it")
    score = ps.get("score") or {}
    if not score.get("field") or not score.get("direction"):
        refuse("per_sample.score needs both `field` and `direction`",
               why="`direction` says which end means BAD and is never inferred "
                   "from the field name. Sort the wrong way and the pile is the "
                   "model's best predictions, reviewed as if they were its worst "
                   "— nothing errors and the review reads normally",
               got=score)
    if score["direction"] not in ("min", "max"):
        broke(f"per_sample.score.direction must be min or max, got {score['direction']!r}")
    if not ps.get("unit_key"):
        refuse("per_sample.unit_key is not declared",
               why="a case has to name a sample before it can name a unit, and a "
                   "finding nobody can address is a finding nobody can act on")

    records = load_records(os.path.join(rdir, "output", ps["path"]),
                           ps.get("format") or "jsonl", ps.get("records_at"))
    if not records:
        refuse(f"{ps['path']} holds no records",
               why="an empty per-sample file after a completed run is an "
                   "extraction problem, not a model with no bad cases")

    ukey, sfield = ps["unit_key"], score["field"]
    missing_u = sum(1 for r in records if ukey not in r)
    missing_s = sum(1 for r in records if sfield not in r)
    if missing_u == len(records) or missing_s == len(records):
        refuse(f"the declared fields are not in the records",
               unit_key=ukey, score_field=sfield,
               present=sorted(records[0].keys())[:15],
               fix="/eval-init Step 1c — the declaration is stale or was guessed")

    scored, unscorable = [], 0
    for r in records:
        if ukey not in r or sfield not in r:
            unscorable += 1
            continue
        try:
            v = float(r[sfield])
        except (TypeError, ValueError):
            unscorable += 1
            continue
        scored.append((v, r))
    if not scored:
        refuse(f"no record has a numeric {sfield!r}")
    scored.sort(key=lambda t: t[0], reverse=(score["direction"] == "max"))

    parents = (rec.get("lineage") or {}).get("parents") or []
    cite = next((p for p in parents if CITE.match(p)), None)
    frozen = load_manifest_units(project, cite)

    carry = ps.get("fields") or []
    cases = []
    for n, (v, r) in enumerate(scored[:a.limit], 1):
        raw = r[ukey]
        resolved, how = resolve_unit(raw, ps.get("resolves_to"), frozen)
        cases.append({
            "rank": n, "unit": raw, "resolved_unit": resolved, "resolution": how,
            "score": v,
            "fields": {k: r[k] for k in carry if k in r},
            "judgments": [], "verdict": None, "provenance": "unreviewed",
            "caveats": [],
        })

    sid = a.name or f"triage_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    session = {
        "session_id": sid,
        "project": project,
        "run": f"evaluation/{a.run}",
        "opened_at": now_utc(),
        "closed_at": None,
        "status": "open",
        "mode": rec.get("mode"),
        "scope": rec.get("scope") or {},
        "ranked_by": {"field": sfield, "direction": score["direction"],
                      "worst_first": True},
        "cited_snapshot": cite,
        "unit_resolution": ("against " + cite if frozen else
                            "unverifiable — no frozen snapshot cited by this run"),
        "population": {
            "records_in_file": len(records),
            "ranked": len(cases),
            "unscorable": unscorable,
        },
        # Said here so it survives into anything that quotes the piles.
        "the_ranking_is": "a candidate pile, unreviewed. NOT hard examples — the "
                          "top of a worst-score ranking is disproportionately "
                          "annotation error, because a wrong label is a target "
                          "the model cannot satisfy and its score stays there",
        "lower_bound": True,
        "blind_to": BLIND_TO,
        "cases": cases,
        "routed": None,
    }
    atomic_write_json(session_path(project, a.run, sid), session)
    emit({k: v for k, v in session.items() if k != "cases"} |
         {"cases": cases if a.json else f"{len(cases)} ranked; read the session file",
          "next": "judge each case — an agent looking at the image, then confirm "
                  "with a person. route refuses while anything is unreviewed"})


# --------------------------------------------------------------------------- #
# judge / confirm
# --------------------------------------------------------------------------- #

def cmd_judge(a) -> None:
    session, path = load_session(a.project, a.run, a.session)
    if session["status"] != "open":
        refuse(f"session is {session['status']}", why="reopen it or start a new one")
    case = find_case(session, a.unit)
    if any(j["by"] == a.by for j in case["judgments"]) and not a.again:
        refuse(f"{a.by} has already judged this case",
               why="a second pass by the same kind of source is not a second "
                   "source. Pass --again to record it anyway; it will not move "
                   "the provenance off `claim`",
               existing=case["judgments"])
    case["judgments"].append({"verdict": a.verdict, "by": a.by,
                              "basis": a.basis, "at": now_utc()})
    settle(case)
    atomic_write_json(path, session)
    emit({"unit": case["unit"], "verdict": case["verdict"],
          "provenance": case["provenance"], "caveats": case["caveats"],
          "note": None if case["provenance"] == "verified" else
                  "still a claim — one kind of source has looked. `confirm --by "
                  "human` is what makes it verified"})


def cmd_confirm(a) -> None:
    """The second, independent look. The only thing that produces `verified`."""
    session, path = load_session(a.project, a.run, a.session)
    case = find_case(session, a.unit)
    if not case["judgments"]:
        refuse("nothing to confirm — this case has no verdict yet",
               why="confirmation is a second source agreeing with a first. With "
                   "no first judgement this would just be `judge`, and calling it "
                   "confirmation would put a single look on record as two")
    if a.agree:
        standing = case["verdict"]
        if standing is None:
            refuse("this case is disputed — there is no standing verdict to agree "
                   "with", caveats=case["caveats"],
                   fix="record your own call with --disagree --verdict <v>")
        verdict = standing
    else:
        verdict = a.verdict
    case["judgments"].append({"verdict": verdict, "by": a.by,
                              "basis": a.basis, "at": now_utc(),
                              "confirming": True})
    settle(case)
    atomic_write_json(path, session)
    emit({"unit": case["unit"], "verdict": case["verdict"],
          "provenance": case["provenance"], "caveats": case["caveats"]})


# --------------------------------------------------------------------------- #
# route
# --------------------------------------------------------------------------- #

def cmd_route(a) -> None:
    """Three piles, three owners, and the refusals that keep them apart."""
    session, path = load_session(a.project, a.run, a.session)
    cases = session["cases"]

    unreviewed = [c["unit"] for c in cases if c["provenance"] == "unreviewed"]
    if unreviewed and not a.partial:
        refuse(f"{len(unreviewed)} of {len(cases)} cases are unreviewed",
               why="routing an unreviewed pile is the amplification bug itself: "
                   "the units at the top are disproportionately label errors, and "
                   "sending them onward as hard examples adds more of what should "
                   "have been fixed",
               units=unreviewed[:10],
               fix="judge them, or --partial to route only what has been reviewed")

    disputed = [c["unit"] for c in cases if c["provenance"] == "disputed"]
    if disputed:
        refuse(f"{len(disputed)} case(s) are disputed",
               why="two sources of equal authority disagreed about what is in the "
                   "image. Whichever way it is routed, half the evidence says it "
                   "is the wrong pile",
               units=disputed,
               fix="a third look: `confirm --by gold`, or --by human on an "
                   "agent-vs-agent split")

    piles = {v: [] for v in VERDICTS}
    for c in cases:
        if c["verdict"]:
            piles[c["verdict"]].append(c)

    # Addressability, and only for the piles that leave the model line. A
    # `model_wrong` finding is acted on by editing a config in this repo, so it
    # needs no manifest; the other two name a unit to somebody else.
    unaddressable = {}
    for v in ("label_wrong", "sample_hard"):
        bad = [c["unit"] for c in piles[v] if c["resolution"] != "resolved"]
        if bad:
            unaddressable[v] = bad
    if unaddressable and not a.allow_unaddressable:
        refuse("cases whose sample id does not resolve to a dataset unit",
               why="these findings name something no manifest can look up. Sent "
                   "to a labeling party as-is, the batch comes back reconciled "
                   "against ids that mean nothing on either side",
               unresolved=unaddressable,
               resolution=session.get("unit_resolution"),
               fix="set per_sample.resolves_to in stages/evaluation/output.json, "
                   "or freeze a snapshot so there is a manifest to resolve "
                   "against; --allow-unaddressable routes them as free text")

    routed = {}
    for v in VERDICTS:
        owner, why = OWNERS[v]
        routed[v] = {
            "owner": owner, "why": why, "count": len(piles[v]),
            "units": [c.get("resolved_unit") or c["unit"] for c in piles[v]],
            "claims": sum(1 for c in piles[v] if c["provenance"] == "claim"),
            "verified": sum(1 for c in piles[v] if c["provenance"] == "verified"),
        }

    payload = {
        "session": session["session_id"],
        "run": session["run"],
        "routed": routed,
        # The anti-amplification statement, written down rather than assumed.
        # Someone reading these piles months from now will be looking for a list
        # of data to add, and this is what tells them which list is not it.
        "not_hard_examples": {
            "units": routed["label_wrong"]["units"],
            "why": "these are annotation errors. They must not enter a reflow "
                   "batch as hard examples — that adds more of the noise that "
                   "put them at the top of the ranking",
        },
        "needs_another_look": routed["unclear"]["units"],
        "unreviewed_left": unreviewed,
        "lower_bound": True,
        "blind_to": BLIND_TO,
        "routed_at": now_utc(),
    }
    session["routed"] = {k: v for k, v in payload.items() if k != "routed"} | {"piles": routed}
    if not unreviewed:
        session["status"] = "routed"
        session["closed_at"] = now_utc()
    atomic_write_json(path, session)
    emit(payload)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def cmd_status(a) -> None:
    project = os.path.expanduser(a.project)
    root = os.path.join(project, "stages", "evaluation", "runs")
    rows = []
    for run in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        tdir = os.path.join(root, run, "triage")
        if not os.path.isdir(tdir):
            continue
        for sid in sorted(os.listdir(tdir)):
            s = read_json(os.path.join(tdir, sid, "session.json"), required=False)
            if not s:
                continue
            if a.open_only and s.get("status") != "open":
                continue
            by = {}
            for c in s["cases"]:
                by[c["provenance"]] = by.get(c["provenance"], 0) + 1
            rows.append({"session": s["session_id"], "run": s["run"],
                         "status": s["status"], "opened_at": s["opened_at"],
                         "cases": len(s["cases"]), "by_provenance": by})
    emit({"project": project, "sessions": rows,
          "note": "a session with unreviewed cases is not stalled machinery — it "
                  "is waiting on somebody to look at images" if rows else None})


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rank", help="open a session over a completed eval run")
    r.add_argument("--project", required=True)
    r.add_argument("--run", required=True, help="an evaluation run_id")
    r.add_argument("--limit", type=int, default=40,
                   help="how many candidates to rank (default 40 — this is a "
                        "review by a person, not a batch job)")
    r.add_argument("--name", default=None)
    r.add_argument("--json", action="store_true", help="include cases in stdout")
    r.set_defaults(fn=cmd_rank)

    j = sub.add_parser("judge", help="record one verdict on one case")
    j.add_argument("--project", required=True)
    j.add_argument("--run", required=True)
    j.add_argument("--session", required=True)
    j.add_argument("--unit", required=True)
    j.add_argument("--verdict", required=True, choices=VERDICTS)
    j.add_argument("--by", required=True, choices=tuple(AUTHORITY),
                   help="who looked. There is no flag for the provenance — it is "
                        "derived from how many kinds of source agree")
    j.add_argument("--basis", required=True,
                   help="what the judgement rests on, e.g. 'box covers two "
                        "cartons; GT has one'. A verdict with no basis cannot be "
                        "re-examined by whoever acts on it")
    j.add_argument("--again", action="store_true",
                   help="record a repeat pass by the same kind of source")
    j.set_defaults(fn=cmd_judge)

    c = sub.add_parser("confirm", help="a second, independent look")
    c.add_argument("--project", required=True)
    c.add_argument("--run", required=True)
    c.add_argument("--session", required=True)
    c.add_argument("--unit", required=True)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--agree", action="store_true")
    g.add_argument("--disagree", action="store_true")
    c.add_argument("--verdict", choices=VERDICTS, help="required with --disagree")
    c.add_argument("--by", default="human", choices=tuple(AUTHORITY))
    c.add_argument("--basis", required=True)
    c.set_defaults(fn=cmd_confirm)

    o = sub.add_parser("route", help="three piles, three owners")
    o.add_argument("--project", required=True)
    o.add_argument("--run", required=True)
    o.add_argument("--session", required=True)
    o.add_argument("--partial", action="store_true",
                   help="route what has been reviewed, leaving the rest open")
    o.add_argument("--allow-unaddressable", action="store_true",
                   help="route findings whose unit id resolves to nothing, as "
                        "free text")
    o.set_defaults(fn=cmd_route)

    s = sub.add_parser("status", help="open sessions in this project")
    s.add_argument("--project", required=True)
    s.add_argument("--open-only", action="store_true")
    s.set_defaults(fn=cmd_status)

    a = p.parse_args()
    if a.cmd == "confirm" and a.disagree and not a.verdict:
        broke("--disagree needs --verdict: rejecting a call without saying what "
              "the case actually is leaves it disputed with no way forward")
    if a.cmd == "confirm" and a.agree and a.verdict:
        broke("--agree takes no --verdict; agreement is with the standing one")
    a.fn(a)


if __name__ == "__main__":
    main()
