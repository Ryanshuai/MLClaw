#!/usr/bin/env python3
"""The ledger two stages iterate against, when one produces what the other cannot read.

The gap this fills is the one path every skill points at and none of them walks:
the training code wants COCO, the labels on disk are YOLO txt, `/train-init`
writes `match: "mismatch"` and stops. `/data-curate` records a conversion and
executes nothing, so the most common thing a user actually does has a diagnosis
and no action.

**Most of the time nothing is "communicated".** Both ends are code — a dataloader
and a converter — driven by one agent in one session, and the exchange is this
file being appended to. The two-party shape is not because there are two people;
it is because one person iterating alone forgets what round two ruled out, and
round five re-tries it. That failure is the reason this record exists.

Three things it keeps that no existing record does:

    findings   the unit is a FINDING, not a round. A round raises findings; the
               finding outlives it, because what the other end answers is the
               finding.
    responses  what the other end DID about it. `handoff.py receive` reconciles
               ARTIFACTS against a frozen manifest — a batch can come back 100%
               complete with every raised issue untouched. "The delivery is
               complete" and "the finding was addressed" are different facts.
    refuted    what has been ruled OUT, with the rounds that ruled it out. Every
               summary drops this half, and dropping it is how the loop fails to
               converge.

Ends of the seam are `dataset` | `consumer` | `contract` — never people and never
job titles. The same engineer routinely holds both ends and the record is needed
exactly as much then; ownership follows the defect, the way `/repro` attributes
to an axis and `/eval-triage` to a verdict kind. `--to <who>` on the handoff that
carries a finding out is an ADDRESS, for chasing.

**It executes nothing.** The transform is the user's code through the ordinary run
machinery, which is `/data-curate`'s boundary and it stands: a converter built
into MLClaw would be the first violation of zero code invasion. What is new here
is only that the loop is recorded.

Exit codes per CLAUDE.md "Script Integration": 1 = worked, the answer is no;
2 = broke, do it by hand.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (age_days, atomic_write_json, broke, emit, now_utc,  # noqa: E402
                      read_json, refuse, id_stamp)

# The two ends of the seam plus the thing that defines it. A fixed vocabulary
# because the whole design turns on these NOT being people: a role register would
# rot (the same person holds both ends; a vendor is external this quarter and
# internal the next) while a defect's side stays stable.
ENDS = ("dataset", "consumer", "contract")

# What the answering end did. `cannot_fix` and `disagree` are terminal FOR THE
# RESPONDER and not for the finding — they hand it back, they do not close it.
ACTIONS = ("fixed", "partially_fixed", "cannot_fix", "disagree", "needs_from_other_side")

# `blocked` = the other end tried and cannot. `reversed` = the answer is that the
# OTHER end must move. Most designs collapse both into "still open", and they are
# the two states that decide whether this campaign can still succeed.
OPEN_STATES = ("open", "blocked", "reversed")
FINDING_STATES = OPEN_STATES + ("addressed", "refuted", "superseded")

# `degraded_to_rework` is not a failure: the loop established that no converter
# can fix this, so the fix moved upstream — the same destination as
# `/eval-triage`'s `label_wrong`, reached before a training run rather than
# through one.
VERDICTS = ("adapted", "degraded_to_rework", "data_unusable", "unverifiable")
ATTRIBUTIONS = ENDS  # converter defects are the dataset end's; see cmd_close

PROBE_RESULTS = ("pass", "fail", "not_run")
AUDIT_RESULTS = ("clean", "dirty", "not_run")


def sessions_dir(project):
    return os.path.join(os.path.expanduser(project), "adaptation")


def session_path(project, sid):
    return os.path.join(sessions_dir(project), sid, "session.json")


def load(project, sid):
    return read_json(session_path(os.path.expanduser(project), sid))


def save(project, sid, rec):
    atomic_write_json(session_path(os.path.expanduser(project), sid), rec)


def require_open(rec, sid):
    if rec.get("status") != "open":
        refuse(f"{sid} is {rec.get('status')}, not open",
               closed_at=rec.get("closed_at"),
               hint="open a successor session rather than appending to a closed "
                    "one — a campaign that was concluded and then edited reads "
                    "as though the conclusion covered the edit")


def find(rec, n):
    for f in rec.get("findings", []):
        if f.get("n") == n:
            return f
    refuse(f"no finding n={n} in this session",
           have=[f.get("n") for f in rec.get("findings", [])])


def contract_from(project, stage):
    """-> (items, path). The consuming code's requirement, read where it lives.

    `{stage}/input.json -> items` is already declared as "what the code needs
    (schema, survives moving machines)" — a property of the CODE, so two datasets
    feeding one trainer share it. Copying it into `dataset.json -> consumers`
    instead would make one copy per dataset, and copies drift.
    """
    path = os.path.join(os.path.expanduser(project), "stages", stage, "input.json")
    cfg = read_json(path, required=False)
    if cfg is None:
        return None, path
    session_items = cfg.get("items")
    return (session_items if isinstance(session_items, dict) else None), path


def cmd_open(a):
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")

    # A derivation from an unfrozen parent records what happened to be in that
    # directory that day — the untraceable dataset the whole data line exists to
    # prevent, and the same refusal `/data-curate` makes.
    if not a.snapshot:
        refuse("no frozen snapshot given",
               why="an adaptation derives a new dataset from a parent, and a "
                   "parent that is still moving means the result cannot say what "
                   "it was made of",
               fix=f"/data-freeze the parent first, then pass --snapshot <id>")

    contract, cpath = contract_from(project, a.consumer_stage)
    if not contract:
        # Without the consumer's requirement there is no oracle and no diff —
        # every round would be judged by whoever was looking, which is the
        # self-verification this loop exists to replace.
        refuse("the consuming stage declares no contract",
               looked_at=cpath,
               why="`input.json -> items` is what every round gets judged "
                   "against. Empty means nothing can say whether a round passed, "
                   "and `match: \"mismatch\"` stays a bare judgement instead of "
                   "becoming a diff",
               fix="fill items with what the code needs (format, field names, "
                   "value ranges, num_classes) — /train-init's analysis has it")

    os.makedirs(sessions_dir(project), exist_ok=True)
    base = id_stamp()
    for suffix in [""] + [f"_{i}" for i in range(2, 100)]:
        sid = f"adapt_{base}{suffix}"
        if not os.path.exists(session_path(project, sid)):
            break
    else:
        broke("could not allocate a unique session_id")

    rec = {
        "session_id": sid,
        "project": os.path.basename(project.rstrip("/\\")),
        "opened_at": now_utc(),
        "closed_at": None,
        "status": "open",
        "target": {
            "dataset": a.dataset,
            "snapshot": a.snapshot,
            "consumer_stage": a.consumer_stage,
            "consumer_config": cpath,
        },
        # Copied, not read live — the same reason a repro session copies `mode`
        # and `scope`. A contract edited mid-campaign silently invalidates every
        # earlier round's verdict while the record reads as one continuous loop.
        "contract_at_open": contract,
        "measure_via": {
            "dataloader_probe": {"entry": a.probe_entry, "batches": a.probe_batches},
            "audit": {"fatal_checks": a.fatal or [], "advisory_checks": []},
        },
        "declared_clean": {
            "dataloader_probe": "passes",
            "audit": "no fatal findings",
            "notes": a.clean_notes,
        },
        "declared_clean_at_open": {
            "dataloader_probe": "passes",
            "audit": "no fatal findings",
            "notes": a.clean_notes,
        },
        "findings": [],
        "rounds": [],
        "confirmed": [],
        "refuted": [],
        "verdict": None,
        "attributed_to": None,
        "caveats": [],
    }
    save(project, sid, rec)
    emit({"session_id": sid,
          "target": rec["target"],
          "contract_items": sorted(contract.keys()),
          "note": "the contract is COPIED here; editing the stage's input.json "
                  "later will not change what these rounds were judged against",
          "next": f"adapt.py round --project {a.project} --id {sid} --run <stage/run_id> ..."})


def cmd_raise(a):
    rec = load(a.project, a.id)
    require_open(rec, a.id)
    if a.against not in ENDS:
        broke(f"unknown end {a.against!r}", allowed=list(ENDS))

    n = max([f.get("n", 0) for f in rec["findings"]] + [0]) + 1
    f = {
        "n": n,
        "raised_at": now_utc(),
        "raised_by": a.by,
        "raised_against": a.against,
        "what": a.what,
        # What a reader can re-check without asking anyone: the probe's stderr,
        # an audit finding id, unit ids. A finding whose evidence is somebody's
        # recollection is a claim and the record says so.
        "evidence": a.evidence,
        "state": "open",
        "responses": [],
        "reverses": a.reverses,
        "superseded_by": None,
        "round": len(rec["rounds"]) or None,
    }
    if not a.evidence:
        f["caveat"] = ("no evidence recorded — this finding is a CLAIM and "
                       "cannot be cited as an established one")
    rec["findings"].append(f)
    save(a.project, a.id, rec)
    emit({"session_id": a.id, "finding": n, "against": a.against,
          "state": "open",
          "evidence_recorded": bool(a.evidence),
          "open_findings": len([x for x in rec["findings"] if x["state"] in OPEN_STATES])})


def cmd_respond(a):
    rec = load(a.project, a.id)
    require_open(rec, a.id)
    f = find(rec, a.n)
    if a.action not in ACTIONS:
        broke(f"unknown action {a.action!r}", allowed=list(ACTIONS))
    if f["state"] == "superseded":
        refuse(f"finding {a.n} was superseded",
               by=f.get("superseded_by"),
               hint="respond to the finding that replaced it")

    # Appended, never edited away, including responses later overruled: whether
    # one end's calls hold up is answerable only from how often the other end
    # disagreed. `/eval-triage` keeps overruled judgements for the same reason.
    f["responses"].append({
        "by": a.by,
        "at": now_utc(),
        "action": a.action,
        "detail": a.detail,
        "basis": a.basis,
    })

    payload = {"session_id": a.id, "finding": a.n, "action": a.action}
    if a.action == "fixed":
        f["state"] = "addressed"
    elif a.action == "partially_fixed":
        f["state"] = "open"
        payload["note"] = "still OPEN — a partial fix is not a fix"
    elif a.action in ("cannot_fix", "disagree"):
        # Terminal for the responder, not for the finding. Closing it here would
        # record "we stopped discussing it" as "it was resolved".
        f["state"] = "blocked"
        payload["note"] = ("finding is BLOCKED, not closed — the responder handed it "
                       "back. It stays open in status until the other end moves "
                       "or the session is degraded")
    elif a.action == "needs_from_other_side":
        # The reversal does NOT flip this finding's direction. A direction field
        # that mutated in place would rewrite history so the issue reads as
        # having always been the other end's.
        other = _swap(f["raised_against"], a.now_against)
        n2 = max([x.get("n", 0) for x in rec["findings"]] + [0]) + 1
        rec["findings"].append({
            "n": n2,
            "raised_at": now_utc(),
            "raised_by": a.by,
            "raised_against": other,
            "what": a.detail or f"blocked by finding {a.n}: {f['what']}",
            "evidence": a.basis,
            "state": "open",
            "responses": [],
            "reverses": a.n,
            "superseded_by": None,
            "round": len(rec["rounds"]) or None,
        })
        f["state"] = "reversed"
        payload["opened_finding"] = n2
        payload["now_against"] = other
        payload["note"] = ("finding {} is REVERSED and a NEW finding {} was opened "
                       "against `{}` — one schema, flowing both ways".format(a.n, n2, other))

    save(a.project, a.id, rec)
    payload["open_findings"] = len([x for x in rec["findings"] if x["state"] in OPEN_STATES])
    emit(payload)


def _swap(current, explicit):
    if explicit:
        if explicit not in ENDS:
            broke(f"unknown end {explicit!r}", allowed=list(ENDS))
        return explicit
    return "consumer" if current == "dataset" else "dataset"


def cmd_round(a):
    rec = load(a.project, a.id)
    require_open(rec, a.id)
    if a.probe not in PROBE_RESULTS:
        broke(f"unknown probe result {a.probe!r}", allowed=list(PROBE_RESULTS))
    if a.audit not in AUDIT_RESULTS:
        broke(f"unknown audit result {a.audit!r}", allowed=list(AUDIT_RESULTS))

    # The second layer is not optional politeness. A converter emitting all-zero
    # boxes loads perfectly, and a category id the dataloader silently clamps is
    # not a crash — a loop that stops at "it ran" certifies exactly those.
    if a.probe == "pass" and a.audit == "not_run":
        refuse("a round cannot be recorded as passing on the probe alone",
               why="the dataloader accepting the data is the FATAL layer only. "
                   "Values out of range, category ids outside num_classes and a "
                   "distribution that makes no sense all load without error",
               fix="run /data-audit and pass --audit clean|dirty, or record this "
                   "round with --probe pass --audit not_run and accept that it "
                   "does not count as clean")

    n = len(rec["rounds"]) + 1
    rec["rounds"].append({
        "n": n,
        "at": now_utc(),
        # An ORDINARY run in stages/<stage>/runs/ — the transform is the user's
        # code, so the script that produced this dataset is re-runnable rather
        # than remembered.
        "run": a.run,
        "changed": a.changed,
        "probe_result": a.probe,
        "audit_result": a.audit,
        "notes": a.notes,
    })
    clean = a.probe == "pass" and a.audit == "clean"
    save(a.project, a.id, rec)
    emit({"session_id": a.id, "round": n, "run": a.run,
          "probe": a.probe, "audit": a.audit,
          "meets_declared_clean": clean,
          "open_findings": len([x for x in rec["findings"] if x["state"] in OPEN_STATES]),
          "next": (f"adapt.py close --project {a.project} --id {a.id} --verdict adapted"
                   if clean else
                   f"adapt.py raise --project {a.project} --id {a.id} --against ... --what ...")})


def cmd_distill(a):
    """Promote a standing conclusion out of the rounds that established it."""
    rec = load(a.project, a.id)
    require_open(rec, a.id)
    if not a.cites:
        refuse("a conclusion must cite the rounds that established it",
               why="the distillation is the only part of this record safe to read "
                   "alone, so every line in it has to be traceable back to "
                   "evidence that was not compressed away",
               fix="--cites 2 --cites 3")
    bad = [c for c in a.cites if c < 1 or c > len(rec["rounds"])]
    if bad:
        refuse("cited rounds do not exist", cited=bad, rounds=len(rec["rounds"]))

    bucket = "confirmed" if a.kind == "confirmed" else "refuted"
    rec[bucket].append({"says": a.says, "cites_rounds": sorted(set(a.cites)),
                        "at": now_utc()})
    save(a.project, a.id, rec)
    emit({"session_id": a.id, bucket: len(rec[bucket]), "says": a.says,
          "note": ("refuted entries are what stop round five re-trying what round "
                   "two eliminated — a distillation carrying only what worked is "
                   "a record that has forgotten the expensive half")})


def cmd_relax(a):
    """Widen what counts as done, mid-campaign, and stamp it as a caveat."""
    rec = load(a.project, a.id)
    require_open(rec, a.id)
    if not rec["rounds"]:
        # Before any round has run there is no result to tune the bar to, so
        # this is still just declaring it.
        rec["declared_clean"]["notes"] = a.notes
        rec["declared_clean_at_open"]["notes"] = a.notes
        save(a.project, a.id, rec)
        emit({"session_id": a.id, "declared_clean": rec["declared_clean"],
              "note": "no rounds yet — recorded as the original declaration"})
        return

    before = dict(rec["declared_clean"])
    rec["declared_clean"]["audit"] = a.audit or rec["declared_clean"]["audit"]
    rec["declared_clean"]["notes"] = a.notes
    rec["caveats"].append({
        "kind": "declared_clean_widened",
        "at": now_utc(),
        "after_rounds": len(rec["rounds"]),
        "from": before,
        "to": dict(rec["declared_clean"]),
        "why": a.why,
        # At round six, under a deadline, the definition of clean is under
        # pressure from whoever has to meet it. A criterion chosen once the
        # results are in is a test written to its own answer, so it travels with
        # the verdict rather than replacing the original silently.
        "note": "the bar moved AFTER results were in; every conclusion below "
                "carries this",
    })
    save(a.project, a.id, rec)
    emit({"session_id": a.id, "declared_clean": rec["declared_clean"],
          "caveat_recorded": True, "after_rounds": len(rec["rounds"])})


def cmd_close(a):
    rec = load(a.project, a.id)
    require_open(rec, a.id)
    if a.verdict not in VERDICTS:
        broke(f"unknown verdict {a.verdict!r}", allowed=list(VERDICTS))

    probed = [r for r in rec["rounds"] if r["probe_result"] != "not_run"]
    open_f = [f for f in rec["findings"] if f["state"] in OPEN_STATES]

    if a.verdict == "adapted":
        if not probed:
            # A probe that fails open turns every unchecked round into a pass.
            refuse("cannot close as `adapted`: the probe never ran",
                   rounds=len(rec["rounds"]),
                   why="`adapted` asserts the consuming code accepted the data. "
                       "No round ran the probe, so nothing did",
                   fix="close as `unverifiable`, which is the honest verdict when "
                       "the oracle could not speak")
        last = rec["rounds"][-1]
        if not (last["probe_result"] == "pass" and last["audit_result"] == "clean"):
            refuse("the last round does not meet `declared_clean`",
                   probe=last["probe_result"], audit=last["audit_result"],
                   declared=rec["declared_clean"],
                   fix="record a passing round, or close with the verdict that "
                       "matches what happened")
        if open_f:
            refuse("cannot close as `adapted` with findings still open",
                   open_findings=[{"n": f["n"], "state": f["state"],
                                   "against": f["raised_against"]} for f in open_f],
                   why="a blocked or reversed finding is the campaign's unfinished "
                       "business. Closing over it files somebody's unanswered "
                       "question as resolved",
                   fix="respond to them, or close as `degraded_to_rework`")
    elif not a.attributed_to:
        # The question these loops re-litigate every time is "are we fixing the
        # converter or the data". It is cheap to record and expensive to
        # re-derive.
        refuse(f"`{a.verdict}` requires --attributed-to",
               allowed=list(ATTRIBUTIONS),
               why="where the defect actually was is the one conclusion the next "
                   "round of this argument should not have to start without")

    if a.attributed_to and a.attributed_to not in ATTRIBUTIONS:
        broke(f"unknown attribution {a.attributed_to!r}", allowed=list(ATTRIBUTIONS))

    for r in rec["rounds"]:
        if r["probe_result"] == "not_run":
            rec["caveats"].append({"kind": "probe_not_run", "round": r["n"],
                                   "at": now_utc()})
    rec.update(status="closed", closed_at=now_utc(), verdict=a.verdict,
               attributed_to=a.attributed_to)
    save(a.project, a.id, rec)

    payload = {"session_id": a.id, "verdict": a.verdict,
           "attributed_to": a.attributed_to,
           "rounds": len(rec["rounds"]),
           "confirmed": len(rec["confirmed"]), "refuted": len(rec["refuted"]),
           "caveats": len(rec["caveats"])}
    if a.verdict == "degraded_to_rework":
        # The oracle changed — from "the dataloader accepts it" to "the party
        # that owns the data agrees" — and two oracles are never one session.
        payload["next"] = ("the oracle changed, so this campaign ENDS here. Open a "
                       "/data-label rework handoff for the unresolved findings; a "
                       "successor adaptation session cites this one")
        # Every non-terminal state, not just `blocked`. A finding still `open`
        # when the campaign degrades is exactly the work the rework round has to
        # carry, and listing only the handed-back ones would drop it silently.
        payload["unresolved_findings"] = [{"n": f["n"], "state": f["state"],
                                       "against": f["raised_against"]}
                                      for f in rec["findings"]
                                      if f["state"] in OPEN_STATES]
    emit(payload)


def cmd_status(a):
    """Records only, no network — safe to run at conversation start."""
    root = sessions_dir(a.project)
    rows, errors = [], []
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d, "session.json")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8") as fh:
                    r = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                errors.append({"path": p, "error": str(exc)})
                continue
            is_open = r.get("status") == "open"
            if a.open_only and not is_open:
                continue
            age = age_days(r.get("opened_at"))
            findings = r.get("findings") or []
            blocked = [f["n"] for f in findings if f.get("state") == "blocked"]
            reversed_ = [f["n"] for f in findings if f.get("state") == "reversed"]
            rows.append({
                "session_id": r.get("session_id"),
                "status": r.get("status"),
                "dataset": (r.get("target") or {}).get("dataset"),
                "consumer_stage": (r.get("target") or {}).get("consumer_stage"),
                "rounds": len(r.get("rounds") or []),
                "open_findings": [f["n"] for f in findings
                                  if f.get("state") in OPEN_STATES],
                "blocked": blocked,
                "reversed": reversed_,
                "age_days": age,
                "stale": bool(is_open and age is not None and age >= a.stale_days),
                "verdict": r.get("verdict"),
                "path": p,
            })
    emit({"adaptations": rows,
          "open": sum(1 for r in rows if r["status"] == "open"),
          "stale": sum(1 for r in rows if r["stale"]),
          # Blocked is the entry that gets worse by being missed: the other end
          # already said it cannot move, so nothing further happens on its own.
          "blocked_anywhere": sum(len(r["blocked"]) for r in rows),
          "stale_threshold_days": a.stale_days,
          "errors": errors})


def cmd_show(a):
    emit(load(a.project, a.id))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="start a campaign against one consumer's contract")
    o.add_argument("--project", required=True)
    o.add_argument("--dataset", required=True)
    o.add_argument("--snapshot", default=None, help="frozen parent; refused without one")
    o.add_argument("--consumer-stage", required=True,
                   help="the stage whose code sets the contract, e.g. training")
    o.add_argument("--probe-entry", default=None,
                   help="how the consuming dataloader gets exercised")
    o.add_argument("--probe-batches", type=int, default=1)
    o.add_argument("--fatal", action="append", default=None,
                   help="audit check that must be clean; repeatable")
    o.add_argument("--clean-notes", default=None)
    o.set_defaults(fn=cmd_open)

    r = sub.add_parser("raise", help="file a finding against one end of the seam")
    r.add_argument("--project", required=True)
    r.add_argument("--id", required=True)
    r.add_argument("--against", required=True, choices=ENDS)
    r.add_argument("--what", required=True)
    r.add_argument("--evidence", default=None,
                   help="what a reader can re-check without asking anyone")
    r.add_argument("--by", default=None, choices=ENDS)
    r.add_argument("--reverses", type=int, default=None)
    r.set_defaults(fn=cmd_raise)

    rp = sub.add_parser("respond", help="what the answering end did about it")
    rp.add_argument("--project", required=True)
    rp.add_argument("--id", required=True)
    rp.add_argument("--n", type=int, required=True)
    rp.add_argument("--action", required=True, choices=ACTIONS)
    rp.add_argument("--detail", default=None)
    rp.add_argument("--basis", default=None)
    rp.add_argument("--by", default=None, choices=ENDS)
    rp.add_argument("--now-against", default=None, choices=ENDS,
                    help="for needs_from_other_side: which end must move")
    rp.set_defaults(fn=cmd_respond)

    rd = sub.add_parser("round", help="record one attempt and what the oracle said")
    rd.add_argument("--project", required=True)
    rd.add_argument("--id", required=True)
    rd.add_argument("--run", default=None, help="stage/run_id that did the transform")
    rd.add_argument("--changed", default=None)
    rd.add_argument("--probe", required=True, choices=PROBE_RESULTS)
    rd.add_argument("--audit", required=True, choices=AUDIT_RESULTS)
    rd.add_argument("--notes", default=None)
    rd.set_defaults(fn=cmd_round)

    d = sub.add_parser("distill", help="promote a standing conclusion, citing rounds")
    d.add_argument("--project", required=True)
    d.add_argument("--id", required=True)
    d.add_argument("--kind", required=True, choices=("confirmed", "refuted"))
    d.add_argument("--says", required=True)
    d.add_argument("--cites", action="append", type=int, default=None)
    d.set_defaults(fn=cmd_distill)

    rx = sub.add_parser("relax", help="widen declared_clean; stamps a caveat")
    rx.add_argument("--project", required=True)
    rx.add_argument("--id", required=True)
    rx.add_argument("--audit", default=None)
    rx.add_argument("--notes", default=None)
    rx.add_argument("--why", required=True)
    rx.set_defaults(fn=cmd_relax)

    c = sub.add_parser("close", help="verdict + where the defect was")
    c.add_argument("--project", required=True)
    c.add_argument("--id", required=True)
    c.add_argument("--verdict", required=True, choices=VERDICTS)
    c.add_argument("--attributed-to", default=None, choices=ATTRIBUTIONS)
    c.set_defaults(fn=cmd_close)

    st = sub.add_parser("status", help="what is open and how long; no network")
    st.add_argument("--project", required=True)
    st.add_argument("--open-only", action="store_true")
    st.add_argument("--stale-days", type=float, default=7.0)
    st.set_defaults(fn=cmd_status)

    sh = sub.add_parser("show", help="print one session")
    sh.add_argument("--project", required=True)
    sh.add_argument("--id", required=True)
    sh.set_defaults(fn=cmd_show)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
