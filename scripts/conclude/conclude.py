#!/usr/bin/env python3
"""conclude.py -- what is now believed, on what evidence, and what would overturn it.

MLClaw records what HAPPENED in exhaustive detail: a run record, a graph card, a
census, an audit. Nothing recorded what is BELIEVED -- and the belief is the only
thing anybody repeats six weeks later, out loud, with none of the three
qualifiers that made it true. "We tried multi-frame fusion, it didn't help" is a
sentence about a corpus, a tier and a noise floor, none of which survive it.

Borrowed from ARA (arXiv:2604.24658) `logic/claims.md`. Three things are taken:

  1. `Evidence basis` and `Interpretation` are SEPARATE FIELDS. Fused, a
     conclusion reads as if its mechanism had been measured, and the next round
     designs against a mechanism nobody tested.
  2. `Falsification criteria` is MANDATORY. A belief with no falsifier is a
     preference; `check` refuses it.
  3. Conclusions depend on conclusions, so refuting one has to MOVE the others.

One thing is not taken, and one is added.

NOT taken: ARA's word. `claim` in MLClaw already means the opposite of this --
`/ask-human` and `/discover` use it for 「somebody said so, nothing confirmed
it」. Calling the evidenced object a claim would collide with the vocabulary at
exactly the point where the difference matters.

ADDED: `unverifiable`. ARA's statuses assume the evidence stays put. MLClaw
retires datasets, deletes checkpoints, and loses snapshots, so `supported` and
`refuted` do not cover the case that actually happens -- the corpus was retired
and nobody can check any more. That is not a weak `supported` and it is
emphatically not a `refuted`; it is the third fact, the same one `census.py`
keeps apart as `unreachable` and `/repro` reports as its own verdict.

‼️ `status` and `tier` are COMPUTED, never written. `check` derives them from
what the evidence resolves to right now and REPORTS where the stored value
disagrees -- it never repairs, for the same reason `graph.py check` does not: a
record that silently corrects itself cannot be audited, and the drift is the
finding.

Verbs:
  new         create knowledge/conclusions.json for a project
  add         record a conclusion (statement + falsifier + scope required)
  evidence    attach one grounded evidence item
  set         edit a declared field
  refute      record the measurement that overturned it
  supersede   point at the conclusion that replaced it
  check       the invariant sweep -- reports, never repairs; exit 1 on critical
  status      one-screen summary, for conversation start
  render      the artifact: self-contained markdown a human can read
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc,  # noqa: E402
                      quotes_the_number, read_json, refuse)
from _vocab import PROVENANCE, TIERS  # noqa: E402


# Five, and the one ARA does not have is `unverifiable`. See the module
# docstring -- it is the state that actually occurs, and folding it into either
# neighbour is a record-integrity bug: `supported` claims a check nobody can
# run, `refuted` claims a measurement nobody took.
STATUSES = ("supported", "contested", "refuted", "unverifiable", "superseded")

# A settled conclusion is APPENDED to, not edited. What was believed at the time
# is what explains the runs launched at the time; rewriting it destroys the only
# account of why the money was spent.
SETTLED = ("refuted", "superseded")

# `external` is deliberately in this list and deliberately cannot resolve. A
# paper, a colleague's slide, a vendor's number -- citing one is legitimate and
# hiding it is not. It resolves to `claim`, MLClaw's existing word for exactly
# this, and a conclusion resting on nothing else is a claim rather than a
# conclusion no matter how many external refs it stacks up.
EVIDENCE_KINDS = ("run", "node", "baseline", "finding", "audit", "census",
                  "snapshot", "handoff", "external")

# Same ladder as `graph.py`, same reason. T4 is absent on purpose: it is an
# approximation priced for the original, so it does not rank against the
# others -- it is flagged, not weighed.
TIER_POWER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}

SEVERITY = ("critical", "major", "minor")

REL = os.path.join("knowledge", "conclusions.json")


# ------------------------------------------------------------------ resolving
#
# Four states, not two, and the split is the whole reliability of `status`.
# `gone` (looked, not there) and `unreadable` (found it, could not parse) are
# different facts, and `claim` (an external ref, unresolvable by construction)
# is a third that must not be reported as either. Same discipline as
# `census.py`, one level up.

def _split_ref(ref):
    """`a/b.json#N07` -> ("a/b.json", "N07"). No fragment -> (ref, None)."""
    if "#" in ref:
        head, frag = ref.split("#", 1)
        return head, frag
    return ref, None


def _snapshot_path(project, ref):
    """`datasets/boxes@260731` -> the snapshot dir, or None if not that shape."""
    m = re.match(r"^datasets/([^/@]+)@([^/@]+)$", ref)
    if not m:
        return None
    return os.path.join(project, "datasets", m.group(1), "snapshots", m.group(2))


def _probe_json(path):
    """-> (record, state). state is `ok` | `gone` | `unreadable`. Never exits.

    ‼️ Deliberately NOT `_records.read_json`, whose contract is the opposite one
    and is right for its case: a script whose OWN record is corrupt should exit 2
    so the caller falls back and does the work by hand.

    An EVIDENCE file is not this script's record. Its corruption is a finding
    ABOUT a conclusion, and routing it through the shared helper aborts the whole
    sweep -- one unparseable `graph.json` and not a single conclusion gets
    checked, which is a guard reporting nothing at the moment it has something to
    say. The distinction is whose record it is, not which exception was raised.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), "ok"
    except FileNotFoundError:
        return None, "gone"
    except (OSError, ValueError):
        return None, "unreadable"


def resolve(project, ev):
    """-> (state, detail). state is `ok` | `gone` | `unreadable` | `claim`."""
    ref = (ev.get("ref") or "").strip()
    if ev.get("kind") == "external":
        return "claim", "external reference -- nothing in this project confirms it"
    if not ref:
        return "gone", "no ref"

    snap = _snapshot_path(project, ref)
    path = snap if snap else os.path.join(project, _split_ref(ref)[0])
    if not os.path.exists(path):
        return "gone", f"{ref} does not resolve -- nothing at {path}"

    # A directory reference (a run dir, a snapshot dir) resolves by existing.
    if os.path.isdir(path):
        # …except a run dir, whose record is the thing being cited.
        rj = os.path.join(path, "run.json")
        if ev.get("kind") == "run" and not os.path.exists(rj):
            return "gone", f"{ref} has no run.json -- the directory is not a record"
        return "ok", ""

    rec, st = _probe_json(path)
    if st == "unreadable":
        return "unreadable", (f"{ref} is there but could not be parsed -- that is "
                              f"not the same fact as absent, and must not be "
                              f"reported as one")

    frag = _split_ref(ref)[1]
    if frag:
        ids = [n.get("id") for n in (rec.get("nodes") or [])] \
            + [n.get("id") for n in (rec.get("findings") or [])] \
            + [n.get("id") for n in (rec.get("conclusions") or [])]
        if frag not in ids:
            return "gone", f"{ref}: {frag} is not in that file (has {sorted(i for i in ids if i)})"
    return "ok", ""


def _retirement_stamp(project, ev):
    """A snapshot that resolves but whose bytes were deleted under a waiver.

    `retire.py apply` appends `data_retired` to `snapshot.json` -- the only place
    a reader a year later can find out. ‼️ Deliberately NOT adjudicated here: a
    stamp says units went from ONE LOCATION, and `/repro`'s
    `survivors_of_retirement` already does the census join that decides whether
    the data survived. Re-implementing that judgement in a second script is how
    two answers to one question start to drift; this reports the stamp and names
    who can rule.
    """
    snap = _snapshot_path(project, (ev.get("ref") or "").strip())
    if not snap:
        return None
    rec = _probe_json(os.path.join(snap, "snapshot.json"))[0] or {}
    retired = rec.get("data_retired") or []
    return len(retired) or None


# ------------------------------------------------------------------ assessing

def _weakest_tier(evidence):
    """-> (tier, has_t4). The WEAKEST, because the tier travels with the number.

    A conclusion resting on one T3 arm and one T1 probe is a T1 conclusion.
    Taking the strongest is how a `[T1 trend]` gets cited next week as a
    controlled result -- CLAUDE.md's 「Never quote a number without the tier it
    was measured at」, applied to the thing the tier is quoted FROM.
    """
    ranked = [e.get("tier") for e in evidence if e.get("tier") in TIER_POWER]
    has_t4 = any(e.get("tier") == "T4" for e in evidence)
    if not ranked:
        return None, has_t4
    return min(ranked, key=lambda t: TIER_POWER[t]), has_t4


def _actionable(fals, scope):
    """graph.py's predicate, same rule: a number, or the metric's own name.

    「if it turns out not to work」 is a tautology and a tautology passes every
    measurement -- which makes it worse than a missing field, because it looks
    filled.
    """
    fals = (fals or "").strip()
    if not fals:
        return False
    if any(c.isdigit() for c in fals):
        return True
    metric = (scope or {}).get("metric") or ""
    words = [w for w in str(metric).replace("/", " ").split() if len(w) > 2]
    return any(w.lower() in fals.lower() for w in words)


# A MEASURED number, not every digit in the sentence. The lookarounds exclude
# anything glued to an identifier -- `AP50`, `boxes@260731`,
# `run_20260712_141530`, `2026-08-14` -- because a metric name, a snapshot id and
# a date are not quantities and demanding a source for them floods the report.
# ‼️ That flooding is the actual risk, not the missed number: CLAUDE.md's cost of
# an unnecessary question applies to an unnecessary finding, and a check whose
# output gets skimmed is worse than no check.
_NUM = re.compile(r"(?<![\w@./-])\d+(?:\.\d+)?%?(?![\w@./-])")


def _numbers(text):
    """Measured numeric literals in prose, for the grounding check."""
    return _NUM.findall(str(text or ""))


def assess(project, rec):
    """-> {id: {"status", "tier", "resolved": {...}, "findings": [(sev, detail)]}}

    Computes what the evidence CURRENTLY supports. Dependency propagation runs
    to a fixed point afterwards, because a conclusion cannot be stronger than
    what it rests on and a chain three deep has to move all the way down.
    """
    out = {}
    by_id = {c.get("id"): c for c in rec.get("conclusions") or []}

    for c in rec.get("conclusions") or []:
        cid = c.get("id") or "?"
        f, evs = [], c.get("evidence") or []
        states = {}

        # -- the belief itself ------------------------------------------------
        if not (c.get("statement") or "").strip():
            f.append(("critical", f"{cid}: no statement -- nothing is being concluded"))
        if not (c.get("falsified_if") or "").strip():
            f.append(("critical",
                      f"{cid}: no `falsified_if`. A belief nothing can overturn is a "
                      f"preference, not a conclusion -- write the criterion or do not "
                      f"record this as one"))
        elif not _actionable(c.get("falsified_if"), c.get("scope")):
            f.append(("major",
                      f"{cid}: `falsified_if` names neither a number nor the scope's "
                      f"metric -- an independent reader cannot execute it, and a "
                      f"criterion nobody can execute is met by nothing"))
        if not ((c.get("scope") or {}).get("corpus")):
            f.append(("critical",
                      f"{cid}: `scope.corpus` is empty. A conclusion is about a corpus, "
                      f"not about the world -- without it nothing can tell whether this "
                      f"applies to the data in front of you"))
        if c.get("provenance") not in PROVENANCE:
            f.append(("major", f"{cid}: provenance must be one of {list(PROVENANCE)}"))

        # -- the evidence -----------------------------------------------------
        if not evs:
            f.append(("critical", f"{cid}: no evidence. This is an opinion with an id"))
        for i, e in enumerate(evs):
            tag = f"{cid}.evidence[{i}]"
            if e.get("kind") not in EVIDENCE_KINDS:
                f.append(("major", f"{tag}: kind must be one of {list(EVIDENCE_KINDS)}"))
            if not (e.get("quote") or "").strip():
                f.append(("critical",
                          f"{tag}: no «quote». A bare path is not grounding -- the "
                          f"transcribed line is the evidence the source was open"))
            if e.get("tier") is not None and e.get("tier") not in TIERS:
                f.append(("major", f"{tag}: tier must be one of {list(TIERS)}"))
            st, detail = resolve(project, e)
            states[i] = st
            if st == "gone":
                f.append(("critical",
                          f"{tag}: {detail}. The conclusion is now UNVERIFIABLE -- not "
                          f"false. Nobody can check it, which is a third fact and must "
                          f"never be reported as either neighbour"))
            elif st == "unreadable":
                f.append(("critical", f"{tag}: {detail}"))
            n = _retirement_stamp(project, e)
            if n:
                f.append(("major",
                          f"{tag}: cites a snapshot carrying {n} `data_retired` "
                          f"stamp(s) -- the citation resolves, the bytes may not. Only "
                          f"`/repro` can rule (it joins the census against the deletion "
                          f"date); until it does, this conclusion is unverifiable"))

        if evs and all(states.get(i) == "claim" for i in range(len(evs))):
            f.append(("critical",
                      f"{cid}: every evidence item is `external` -- nothing in this "
                      f"project confirms any of it. That makes this a CLAIM in MLClaw's "
                      f"sense, not a conclusion, and it may not be quoted as settled"))

        # -- grounding: a number in the belief must be in a quote --------------
        quotes = " ".join(str(e.get("quote") or "") for e in evs)
        for field in ("statement", "interpretation"):
            for tok in _numbers(c.get(field)):
                if not quotes_the_number(float(tok.rstrip("%")), quotes):
                    f.append(("critical",
                              f"{cid}: the {field} says {tok!r} and no evidence quote "
                              f"contains it. Either the number was written from memory "
                              f"or the source that reports it was never cited"))

        # -- interpretation is not the statement ------------------------------
        interp = (c.get("interpretation") or "").strip()
        if interp and interp == (c.get("statement") or "").strip():
            f.append(("major",
                      f"{cid}: interpretation repeats the statement. The two fields "
                      f"exist to separate what was MEASURED from what is ARGUED on top; "
                      f"filling both with one sentence removes the distinction while "
                      f"looking complete"))

        # -- settled records are appended to, not edited ----------------------
        if c.get("status") in SETTLED and c.get("settled_at"):
            if (c.get("edited_at") or "") > c["settled_at"]:
                f.append(("major",
                          f"{cid}: settled at {c['settled_at']} and edited at "
                          f"{c['edited_at']}. What was believed at the time is what "
                          f"explains the runs launched at the time"))
        if c.get("status") == "refuted" and not c.get("refuted_by"):
            f.append(("critical",
                      f"{cid}: refuted with no `refuted_by`. A refutation names the "
                      f"measurement that did it, or it is an opinion overruling a record"))
        if c.get("status") == "superseded":
            succ = c.get("superseded_by")
            if succ not in by_id:
                f.append(("critical",
                          f"{cid}: superseded by {succ!r}, which is not a conclusion "
                          f"here -- the pointer has to land somewhere"))

        # -- dependencies exist ------------------------------------------------
        for d in c.get("depends_on") or []:
            if d not in by_id:
                f.append(("critical", f"{cid}: depends on {d}, which does not exist"))

        # -- computed status and tier ------------------------------------------
        tier, has_t4 = _weakest_tier(evs)
        if has_t4:
            f.append(("critical",
                      f"{cid}: rests on T4 evidence -- an approximation priced for the "
                      f"original cannot carry a conclusion ABOUT the original. Re-cite "
                      f"at the tier that measured it, or state the conclusion about the "
                      f"approximation"))

        if c.get("status") == "superseded":
            computed = "superseded"
        elif c.get("status") == "refuted" and c.get("refuted_by"):
            computed = "refuted"
        elif any(states.get(i) in ("gone", "unreadable") for i in range(len(evs))) \
                or any(_retirement_stamp(project, e) for e in evs):
            computed = "unverifiable"
        elif not evs or all(states.get(i) == "claim" for i in range(len(evs))):
            computed = "contested"
        elif c.get("disputed_by"):
            computed = "contested"
        else:
            computed = "supported"

        out[cid] = {"status": computed, "tier": tier, "findings": f,
                    "resolved": states}

    # -- propagation, to a fixed point ---------------------------------------
    #
    # A conclusion may not be stronger than what it rests on. Refuting C02 does
    # NOT delete C03 -- it CONTESTS it, and somebody has to look. Deleting the
    # dependent would erase the reason a whole line of runs was launched;
    # leaving it `supported` would let a refuted premise go on being quoted.
    RANK = {"supported": 3, "contested": 2, "unverifiable": 1,
            "refuted": 0, "superseded": 0}
    for _ in range(len(out) + 1):
        moved = False
        for c in rec.get("conclusions") or []:
            cid = c.get("id")
            if cid not in out or out[cid]["status"] in ("refuted", "superseded"):
                continue
            for d in c.get("depends_on") or []:
                if d not in out:
                    continue
                ds = out[d]["status"]
                # A refuted premise contests its dependent; it does not refute it.
                want = "contested" if ds in ("refuted", "contested", "superseded") \
                    else ("unverifiable" if ds == "unverifiable" else None)
                if want and RANK[want] < RANK[out[cid]["status"]]:
                    out[cid]["status"] = want
                    out[cid]["findings"].append(
                        ("major", f"{cid}: {d} is {ds}, so this cannot stand at "
                                  f"`supported` -- it is {want} until somebody looks. "
                                  f"Nothing here deletes it: the belief explains the "
                                  f"runs that were launched under it"))
                    moved = True
        if not moved:
            break

    # -- stored vs computed ---------------------------------------------------
    for c in rec.get("conclusions") or []:
        cid = c.get("id")
        if cid not in out:
            continue
        a = out[cid]
        if c.get("status") and c["status"] != a["status"]:
            a["findings"].append(
                ("critical", f"{cid}: recorded as `{c['status']}` but the evidence now "
                             f"supports `{a['status']}`. `status` is computed -- a "
                             f"stored value that outlived its evidence is exactly the "
                             f"drift this file exists to catch"))
        if a["tier"] and c.get("tier") and c["tier"] != a["tier"]:
            a["findings"].append(
                ("critical", f"{cid}: recorded at tier {c['tier']} but its weakest "
                             f"evidence is {a['tier']}. The tier travels with the "
                             f"number, forever, in every file and every sentence"))
    return out


# ---------------------------------------------------------------------- io

def _path(project):
    return os.path.join(project, REL)


def _load(project):
    rec = read_json(_path(project), required=False)
    if rec is None:
        refuse(f"no conclusions record at {_path(project)}",
               fix="`conclude.py new --project <p>` first")
    return rec


def _save(project, rec):
    atomic_write_json(_path(project), rec)


def _get(rec, cid):
    for c in rec.get("conclusions") or []:
        if c.get("id") == cid:
            return c
    refuse(f"no conclusion {cid}",
           known=[c.get("id") for c in rec.get("conclusions") or []])


def _next_id(rec):
    """Never reused, never renumbered -- a conclusion is cited by id from run
    records, from chat, and from other conclusions."""
    used = [c.get("id", "") for c in rec.get("conclusions") or []]
    n = 0
    for u in used:
        m = re.match(r"^K(\d+)$", str(u))
        if m:
            n = max(n, int(m.group(1)))
    return f"K{n + 1:02d}"


def _log(c, what):
    c.setdefault("log", []).append({"at": now_utc(), "what": what})
    c["edited_at"] = now_utc()


# ------------------------------------------------------------------- verbs

def cmd_new(a):
    path = _path(a.project)
    if os.path.exists(path) and not a.force:
        refuse(f"{path} already exists", fix="pass --force to overwrite")
    tmpl = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "lifecycle", "knowledge", "conclusions.json")
    rec = read_json(tmpl, required=False) or {"conclusions": []}
    rec["project"] = os.path.basename(os.path.abspath(a.project))
    rec["conclusions"] = []
    _save(a.project, rec)
    emit({"ok": True, "path": path})


def cmd_add(a):
    rec = _load(a.project)
    cid = _next_id(rec)
    c = {
        "id": cid,
        "statement": a.statement,
        "scope": {"corpus": a.corpus, "mode": a.mode, "metric": a.metric},
        "evidence": [],
        "interpretation": a.interpretation,
        "falsified_if": a.falsified_if,
        "depends_on": [d.strip() for d in (a.depends_on or "").split(",") if d.strip()],
        "status": None,
        "tier": None,
        "provenance": a.provenance,
        "recorded_at": now_utc(),
        "edited_at": now_utc(),
        "settled_at": None,
        "superseded_by": None,
        "refuted_by": None,
        "disputed_by": None,
        "log": [{"at": now_utc(), "what": f"recorded ({a.provenance})"}],
    }
    rec.setdefault("conclusions", []).append(c)
    _save(a.project, rec)
    emit({"ok": True, "id": cid,
          "next": "attach evidence -- `status` and `tier` stay null until `check` "
                  "computes them, and a conclusion with no evidence is an opinion "
                  "with an id"})


def cmd_evidence(a):
    rec = _load(a.project)
    c = _get(rec, a.id)
    if c.get("status") in SETTLED:
        refuse(f"{a.id} is {c['status']} -- settled conclusions are appended to via "
               f"`log`, not re-evidenced",
               fix="record a new conclusion that supersedes it")
    ev = {"kind": a.kind, "ref": a.ref, "quote": a.quote, "tier": a.tier}
    st, detail = resolve(a.project, ev)
    c.setdefault("evidence", []).append(ev)
    _log(c, f"evidence + {a.kind}:{a.ref}")
    _save(a.project, rec)
    emit({"ok": True, "id": a.id, "resolves": st, "detail": detail,
          "n_evidence": len(c["evidence"])})


def cmd_set(a):
    rec = _load(a.project)
    c = _get(rec, a.id)
    if a.field in ("status", "tier"):
        refuse(f"`{a.field}` is computed, not written",
               why="a stored confidence that outlived its evidence is the whole "
                   "failure this record exists to catch. Use `refute` / "
                   "`supersede`, or fix the evidence and re-run `check`")
    if a.field not in ("statement", "interpretation", "falsified_if", "corpus",
                       "mode", "metric", "depends_on", "provenance", "disputed_by"):
        refuse(f"unknown field {a.field!r}")
    if c.get("status") in SETTLED:
        refuse(f"{a.id} is {c['status']} -- what was believed at the time is what "
               f"explains the runs launched at the time",
               fix="record a new conclusion and `supersede` this one")
    if a.field in ("corpus", "mode", "metric"):
        c.setdefault("scope", {})[a.field] = a.value
    elif a.field == "depends_on":
        c[a.field] = [d.strip() for d in a.value.split(",") if d.strip()]
    else:
        c[a.field] = a.value
    _log(c, f"set {a.field}")
    _save(a.project, rec)
    emit({"ok": True, "id": a.id, "field": a.field})


def cmd_refute(a):
    rec = _load(a.project)
    c = _get(rec, a.id)
    ev = {"kind": a.kind, "ref": a.by, "quote": a.quote, "tier": a.tier}
    st, detail = resolve(a.project, ev)
    if st in ("gone", "unreadable"):
        refuse(f"the refuting reference does not resolve: {detail}",
               why="a refutation that cannot be opened is an opinion overruling a "
                   "record. `unverifiable` is the honest state here, and it is "
                   "reached by the evidence rotting, not by asserting a result")
    c["refuted_by"] = dict(ev, at=now_utc(), note=a.note)
    c["status"] = "refuted"
    c["settled_at"] = now_utc()
    _log(c, f"refuted by {a.by}")
    _save(a.project, rec)
    emit({"ok": True, "id": a.id, "status": "refuted",
          "note": "dependents are CONTESTED, not deleted -- run `check` to see which"})


def cmd_supersede(a):
    rec = _load(a.project)
    c, succ = _get(rec, a.id), _get(rec, a.by)
    if succ["id"] == c["id"]:
        refuse("a conclusion cannot supersede itself")
    c["superseded_by"] = succ["id"]
    c["status"] = "superseded"
    c["settled_at"] = now_utc()
    _log(c, f"superseded by {succ['id']}: {a.note}")
    _save(a.project, rec)
    emit({"ok": True, "id": a.id, "superseded_by": succ["id"]})


def cmd_check(a):
    rec = _load(a.project)
    res = assess(a.project, rec)
    findings = []
    for cid in sorted(res):
        for sev, detail in res[cid]["findings"]:
            findings.append({"severity": sev, "id": cid, "detail": detail})
    # The distribution is itself the signal -- `graph.py`'s rule for the queue,
    # and it lands harder here. A card nobody asked for costs one run; a
    # CONCLUSION nobody asked for is quoted back at the user as their own
    # position. `ai-suggested` never auto-upgrades, so this stays visible until
    # a person actually reads the file.
    provs = [c.get("provenance") for c in rec.get("conclusions") or []]
    if provs and all(p == "ai-suggested" for p in provs):
        findings.append({"severity": "major", "id": "-",
                         "detail": f"all {len(provs)} conclusions are `ai-suggested` -- "
                                   f"nobody has confirmed any of them. A screen of "
                                   f"beliefs the agent proposed to itself reads exactly "
                                   f"like a screen the user agreed to"})
    findings.sort(key=lambda f: SEVERITY.index(f["severity"]))

    # ‼️ REPORTS, NEVER REPAIRS. Same as `graph.py check`: a record that
    # silently corrects itself cannot be audited, and here the drift between
    # stored and computed IS the finding -- repairing it would erase the only
    # evidence that a conclusion outlived its support.
    counts = {s: 0 for s in STATUSES}
    for cid in res:
        counts[res[cid]["status"]] += 1
    n_crit = sum(1 for f in findings if f["severity"] == "critical")
    payload = {"project": a.project, "n": len(res), "by_status": counts,
               "computed": {cid: {"status": res[cid]["status"],
                                  "tier": res[cid]["tier"]} for cid in sorted(res)},
               "findings": findings, "critical": n_crit,
               "repaired": "nothing -- this verb reports"}
    if n_crit and not a.no_fail:
        emit(payload)
        sys.exit(1)
    emit(payload)


def cmd_status(a):
    rec = _load(a.project)
    res = assess(a.project, rec)
    rows = []
    for c in rec.get("conclusions") or []:
        cid = c.get("id")
        st = res.get(cid, {}).get("status")
        if a.open_only and st in ("refuted", "superseded"):
            continue
        rows.append({"id": cid, "status": st, "tier": res.get(cid, {}).get("tier"),
                     "corpus": (c.get("scope") or {}).get("corpus"),
                     "statement": c.get("statement")})
    drifted = [cid for cid in res
               if _get_status(rec, cid) and _get_status(rec, cid) != res[cid]["status"]]
    emit({"project": a.project, "n": len(rows), "conclusions": rows,
          "status_drift": drifted,
          "unverifiable": [cid for cid in res
                           if res[cid]["status"] == "unverifiable"]})


def _get_status(rec, cid):
    for c in rec.get("conclusions") or []:
        if c.get("id") == cid:
            return c.get("status")
    return None


def cmd_render(a):
    """The artifact. Self-contained markdown -- no scripts, no server, no
    refresh. What a person opens in six weeks, or hands to whoever takes over.

    ‼️ Every conclusion prints its STATUS and its TIER next to the statement,
    and `interpretation` prints under its own heading. Rendering the sentence
    alone is how the qualifiers get lost, which is the failure the whole record
    exists to prevent -- so the renderer may not be more readable than the
    record is honest.
    """
    rec = _load(a.project)
    res = assess(a.project, rec)
    MARK = {"supported": "✅", "contested": "⚖️", "refuted": "❌",
            "unverifiable": "❓", "superseded": "📦"}
    L = [f"# Conclusions — {rec.get('project') or a.project}", ""]
    L += ["> Every conclusion carries a **status** and a **tier**. The `tier` is the",
          "> **weakest** of its evidence, not the strongest. `unverifiable` is not a",
          "> weaker `supported` -- it means nobody can check any more, which has",
          "> nothing to do with `refuted`.", "",
          f"Rendered {now_utc()}. Computed, never hand-written; `conclude.py check` "
          "applies the same judgement.", ""]

    counts = {}
    for cid in res:
        counts[res[cid]["status"]] = counts.get(res[cid]["status"], 0) + 1
    L += ["| " + " | ".join(f"{MARK[s]} {s}" for s in STATUSES) + " |",
          "|" + "---|" * len(STATUSES),
          "| " + " | ".join(str(counts.get(s, 0)) for s in STATUSES) + " |", ""]

    for c in rec.get("conclusions") or []:
        cid = c.get("id")
        a_ = res.get(cid, {})
        st, tier = a_.get("status"), a_.get("tier")
        L += [f"## {cid} {MARK.get(st, '')} `{st}` · tier **{tier or '—'}**", "",
              f"**{c.get('statement')}**", ""]
        sc = c.get("scope") or {}
        L += [f"- **Scope** — corpus `{sc.get('corpus')}`"
              f" · mode `{sc.get('mode')}` · metric `{sc.get('metric')}`",
              f"- **Falsified if** — {c.get('falsified_if')}"]
        if c.get("depends_on"):
            L.append(f"- **Depends on** — {', '.join(c['depends_on'])}")
        if c.get("superseded_by"):
            L.append(f"- **Superseded by** — {c['superseded_by']}")
        if c.get("refuted_by"):
            rb = c["refuted_by"]
            L.append(f"- **Refuted by** — `{rb.get('ref')}` «{rb.get('quote')}»")
        L.append("")
        L += ["### Evidence", "", "| # | kind | ref | tier | resolves | quote |",
              "|---|---|---|---|---|---|"]
        for i, e in enumerate(c.get("evidence") or []):
            r = a_.get("resolved", {}).get(i, "?")
            L.append(f"| {i} | {e.get('kind')} | `{e.get('ref')}` | "
                     f"{e.get('tier') or '—'} | {r} | {e.get('quote')} |")
        L.append("")
        if (c.get("interpretation") or "").strip():
            L += ["### Interpretation — argued, **not measured**", "",
                  c["interpretation"], ""]
        bad = [d for s, d in a_.get("findings", []) if s == "critical"]
        if bad:
            L += ["### ‼️ check did not pass", ""] + [f"- {d}" for d in bad] + [""]

    out = a.out or os.path.join(a.project, "knowledge", "conclusions.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    emit({"ok": True, "path": out, "n": len(res)})


# -------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--project", required=True)
        return sp

    s = common(sub.add_parser("new"))
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_new)

    s = common(sub.add_parser("add"))
    s.add_argument("--statement", required=True)
    s.add_argument("--falsified-if", required=True, dest="falsified_if")
    s.add_argument("--corpus", required=True)
    s.add_argument("--mode", default=None)
    s.add_argument("--metric", default=None)
    s.add_argument("--interpretation", default=None)
    s.add_argument("--depends-on", default="", dest="depends_on")
    s.add_argument("--provenance", default="ai-suggested", choices=PROVENANCE)
    s.set_defaults(func=cmd_add)

    s = common(sub.add_parser("evidence"))
    s.add_argument("--id", required=True)
    s.add_argument("--kind", required=True, choices=EVIDENCE_KINDS)
    s.add_argument("--ref", required=True)
    s.add_argument("--quote", required=True)
    s.add_argument("--tier", default=None, choices=TIERS)
    s.set_defaults(func=cmd_evidence)

    s = common(sub.add_parser("set"))
    s.add_argument("--id", required=True)
    s.add_argument("--field", required=True)
    s.add_argument("--value", required=True)
    s.set_defaults(func=cmd_set)

    s = common(sub.add_parser("refute"))
    s.add_argument("--id", required=True)
    s.add_argument("--by", required=True, help="the ref that measured the refutation")
    s.add_argument("--quote", required=True)
    s.add_argument("--kind", default="run", choices=EVIDENCE_KINDS)
    s.add_argument("--tier", default=None, choices=TIERS)
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_refute)

    s = common(sub.add_parser("supersede"))
    s.add_argument("--id", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_supersede)

    s = common(sub.add_parser("check"))
    s.add_argument("--no-fail", action="store_true",
                   help="report criticals without exiting 1")
    s.set_defaults(func=cmd_check)

    s = common(sub.add_parser("status"))
    s.add_argument("--open-only", action="store_true")
    s.set_defaults(func=cmd_status)

    s = common(sub.add_parser("render"))
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_render)

    a = p.parse_args()
    try:
        a.func(a)
    except SystemExit:
        raise
    except Exception as e:                                   # noqa: BLE001
        broke(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
