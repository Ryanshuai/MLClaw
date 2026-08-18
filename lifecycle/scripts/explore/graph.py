#!/usr/bin/env python3
"""The experiment graph — the four operations, and the scan that says it broke.

`/explore` is a search whose unit is a PROPOSAL, not a trial: one card per thing
worth trying, verified by an ordinary run, adjudicated once. The design came from
the `arch-transplant` skill, where the graph lived in a markdown design doc and
its own invariant section said "scan periodically, report don't repair". Nothing
scanned. `check` is that scanner, and it is the reason this file exists at all —
every other verb here is bookkeeping around it.

Four operations, from that skill's `references/experiment-graph.md`:

    add     a one-line idea becomes a card. Incomplete cards CANNOT reach `ready`
    ready   the ready set = dependencies all settled. Empty + non-empty queue is
            a DEADLOCK and says which of the two kinds
    fill    results onto the card -- and enumerate what that result may have
            invalidated ELSEWHERE. Filling without propagating is the most
            expensive failure on this line: dead premises stay on the queue and
            the next person runs against them
    close   a verdict, or one of four deaths each with its own revival shape

Two things it will not do:

**It executes nothing.** An arm is an ordinary run in `stages/<target_stage>/runs/`,
cited here by `run_id`. Same boundary `/data-curate` draws for a conversion and
`adaptation` draws for a converter -- a search that runs its own trials is a
second run machinery, and it drifts from the first.

**It repairs nothing.** `check` reports. A graph that fixes itself hides the fact
that something wrote an illegal state, and the illegal states here are exactly
the ones that read as normal: a `killed` card with no `revive_if` looks like a
decision, and is a proposal the next round will re-propose.

Exit codes per CLAUDE.md "Script Integration": 1 = worked, the answer is no;
2 = broke, do it by hand.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc,  # noqa: E402
                      read_json, refuse)

# Seven, and the load-bearing split is `filled` vs `closed`: 有结果 != 有结论.
# A card whose numbers are in but whose verdict is not is `filled`. Collapsing
# the two propagates an unexplained number downstream as a finding -- the
# recorded instance had a mechanism criterion pass literally while the verdict
# was "the mechanism was verified and it is not worth anything", and that verdict
# needed a DIFFERENT card's measurement before it could be reached.
STATES = ("draft", "blocked", "ready", "running", "filled", "closed", "killed")

# Settled = may be someone else's dependency. `killed` counts: "stop waiting on
# this one" is as much an answer as a verdict.
SETTLED = ("closed", "killed")

# What a card proposes. The kind decides which stage its arm runs in and which
# fields it needs -- a measurement has no single-key delta and therefore no
# parent, while a port without one cannot attribute its delta to anything.
KINDS = ("measurement", "port", "original", "task_driven")

# Four deaths. Their revive_if are differently shaped, which is why the kind is
# recorded rather than a free-text reason: written interchangeably they are the
# same as not written.
#   unfaithful_port is NOT a death -- it returns to `running`. It is in this
#   vocabulary so that "the port was wrong" cannot be recorded as "the idea was".
KILLED_BY = {
    "share_too_small": "the fault is real, rare here. Revive on a new corpus or operating condition.",
    "faithful_but_inert": "ported correctly, changed nothing. Revive on a changed metric or upstream.",
    "wrong_mechanism": "the proposal itself was wrong. Revive by measuring the effect DIRECTLY.",
    "unfaithful_port": "NOT a death -- go back and fix the port. Returns to `running`.",
}

# T4 is pricing only. An approximation failing never refutes the original, so a
# T4 card may not be closed with a verdict of `lost` -- see cmd_close.
TIERS = ("T0", "T1", "T2", "T3", "T4")

VERDICTS = ("won", "lost", "downgraded")

# Required before `ready`. Per kind, because a measurement card genuinely has no
# parent -- demanding one would teach the reader to write a fake value, which is
# worse than the missing field.
REQUIRED_COMMON = ("title", "kind", "criterion", "guardrail", "depends_on",
                   "oracle_ceiling", "kill_condition")
REQUIRED_BY_KIND = {
    "measurement": (),
    "port": ("premise", "premise_share", "parent"),
    "original": ("premise", "premise_share", "parent"),
    "task_driven": ("premise", "premise_share"),
}


def _paths(project, session=None):
    base = os.path.join(project, "stages", "exploration")
    if session:
        base = os.path.join(base, "sessions", session)
    return {
        "graph": os.path.join(base, "graph.json"),
        "state": os.path.join(project, "stages", "exploration", "state.json"),
        "baseline": os.path.join(project, "stages", "exploration", "baseline.json"),
        "config": os.path.join(project, "stages", "exploration", "config.json"),
    }


def _load(path, what):
    rec = read_json(path, required=False)
    if rec is None:
        refuse(f"no {what} at {path}",
               fix="run `/explore` init first -- a graph needs a declared corpus "
                   "before any card's premise share means anything")
    return rec


def _node(graph, nid):
    for n in graph.get("nodes", []):
        if n.get("id") == nid:
            return n
    refuse(f"no card {nid}", known=[n.get("id") for n in graph.get("nodes", [])])


def _next_id(graph):
    """Queue numbers are stable and NEVER reused -- a killed card's number stays
    spent, because cross-references would otherwise point at a different
    proposal. So this is max+1 over every card ever, not a gap filler."""
    used = [n.get("id", "") for n in graph.get("nodes", [])]
    nums = [int(i[1:]) for i in used if i.startswith("N") and i[1:].isdigit()]
    return "N%02d" % ((max(nums) + 1) if nums else 1)


def _missing_fields(node):
    req = list(REQUIRED_COMMON) + list(REQUIRED_BY_KIND.get(node.get("kind"), ()))
    out = []
    for f in req:
        v = node.get(f)
        if v is None or v == "" or (f == "guardrail" and v == []):
            out.append(f)
    return out


def _share_scope(node, corpus):
    """The scope guard, and the single most expensive rule on this line.

    A `premise_share` measured on another corpus is not weak evidence, it is
    evidence about a different question -- the recorded instance predicted 47%
    where this corpus measured 4.62%, an order of magnitude, with five arms
    already queued on it. Same rule as CLAUDE.md "Never compare metrics across
    different `mode` or non-equivalent `scope`", one level down: here it decides
    whether a proposal may exist at all, not just whether two numbers may be
    subtracted.

    -> a complaint string, or None.
    """
    share = node.get("premise_share")
    if not isinstance(share, dict):
        return None
    on = share.get("measured_on") or {}
    if not on.get("dataset_id"):
        return "premise_share has no `measured_on` -- a share quoted from nowhere is no share"
    if (on.get("dataset_id") != corpus.get("dataset_id")
            or on.get("snapshot") != corpus.get("snapshot")):
        return ("premise_share measured on %s@%s, this graph's corpus is %s@%s -- "
                "a share from another corpus is treated as absent (Stage 3 rule 2.5)"
                % (on.get("dataset_id"), on.get("snapshot"),
                   corpus.get("dataset_id"), corpus.get("snapshot")))
    return None


# ---------------------------------------------------------------- ADD

def cmd_add(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    nid = _next_id(graph)
    node = {
        "id": nid,
        "title": a.title,
        "kind": a.kind,
        "state": "draft",
        "premise": a.premise,
        "premise_share": None,
        "criterion": a.criterion,
        "guardrail": a.guardrail or [],
        "parent": a.parent,
        "depends_on": a.depends_on or [],
        "oracle_ceiling": None,
        "kill_condition": a.kill_condition,
        "tier": None,
        "axes": {"V": None, "P": None, "U": None, "code_availability": None},
        "run_id": None,
        "result": None,
        "verdict": None,
        "killed_by": None,
        "revive_if": None,
        "history": [{"at": now_utc(), "to": "draft", "note": "added"}],
    }
    graph.setdefault("nodes", []).append(node)
    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    emit({"id": nid, "state": "draft", "missing": _missing_fields(node),
          "note": "a draft cannot be opened as an arm. Complete it with `set`, "
                  "then `ready` will include it."})


def cmd_set(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    node = _node(graph, a.id)
    if node["state"] in SETTLED:
        refuse(f"card {a.id} is {node['state']} -- settled cards are not edited",
               fix="a conclusion that changed is a NEW card citing this one; "
                   "rewriting a settled card destroys what the next round needs")
    for kv in a.set or []:
        if "=" not in kv:
            broke(f"--set wants field=value, got {kv!r}")
        k, v = kv.split("=", 1)
        try:
            node[k] = json.loads(v)
        except json.JSONDecodeError:
            node[k] = v
    missing = _missing_fields(node)
    corpus = graph.get("corpus") or {}
    scope = _share_scope(node, corpus)
    if scope:
        missing.append("premise_share (" + scope + ")")
    prev = node["state"]
    if not missing and node["state"] == "draft":
        node["state"] = "blocked"
        node["history"].append({"at": now_utc(), "to": "blocked",
                                "note": "schema complete"})
    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    emit({"id": a.id, "state": node["state"], "was": prev, "missing": missing})


# ---------------------------------------------------------------- READY

def cmd_ready(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    nodes = graph.get("nodes", [])
    by_id = {n["id"]: n for n in nodes}
    corpus = graph.get("corpus") or {}

    ready, blocked = [], []
    for n in nodes:
        if n["state"] in SETTLED or n["state"] in ("running", "filled"):
            continue
        missing = _missing_fields(n)
        scope = _share_scope(n, corpus)
        unmet = [d for d in n.get("depends_on") or []
                 if d not in by_id or by_id[d]["state"] not in SETTLED]
        if missing or scope:
            blocked.append({"id": n["id"], "why": "incomplete card",
                            "missing": missing, "scope": scope})
        elif unmet:
            blocked.append({"id": n["id"], "why": "dependencies unsettled",
                            "waiting_on": unmet})
        else:
            ready.append({"id": n["id"], "title": n["title"], "kind": n["kind"],
                          "tier": n.get("tier"),
                          "offline": n.get("kind") == "measurement"})

    out = {"ready": ready, "blocked": blocked,
           "running": [n["id"] for n in nodes if n["state"] == "running"],
           "filled": [n["id"] for n in nodes if n["state"] == "filled"]}

    # Deadlock: two kinds, and they want opposite responses. Reporting "nothing
    # to do" for either is how a round stalls while looking finished.
    if not ready and blocked:
        cyc = _cycle(by_id)
        if cyc:
            out["deadlock"] = {"kind": "cycle", "detail": cyc,
                               "fix": "the graph is wrong -- break the cycle"}
        else:
            waits = {}
            for b in blocked:
                for w in b.get("waiting_on", []):
                    waits[w] = waits.get(w, 0) + 1
            top = sorted(waits.items(), key=lambda kv: -kv[1])[:3]
            out["deadlock"] = {
                "kind": "single_blocker" if top else "all_incomplete",
                "detail": top,
                "fix": "that blocker IS the work -- promote it. Do not open an arm "
                       "outside the ready set because there is nothing else to do"}

    # Capacity is not this file's. `pool.py` holds slots; over-parallelising is
    # a structural zero rather than a slowdown -- see references/cluster-ops.md.
    if out["filled"]:
        out["note"] = ("%d card(s) filled but not adjudicated. Results with no verdict "
                       "block a stop decision and are worth more than a new arm."
                       % len(out["filled"]))
    emit(out)


def _cycle(by_id):
    seen, stack = set(), []

    def walk(nid):
        if nid in stack:
            return stack[stack.index(nid):] + [nid]
        if nid in seen or nid not in by_id:
            return None
        seen.add(nid)
        stack.append(nid)
        for d in by_id[nid].get("depends_on") or []:
            c = walk(d)
            if c:
                return c
        stack.pop()
        return None

    for nid in by_id:
        c = walk(nid)
        if c:
            return c
    return None


# ---------------------------------------------------------------- FILL

def cmd_fill(a):
    """Results onto the card -- and enumerate what else this may have voided.

    ‼️ FILL IS NOT WRITING ONE CELL. The source skill calls it a propagation and
    names it the most expensive failure on the line: a result that invalidates
    another card's premise, and nobody swept for it, so the queue keeps that arm
    and somebody runs it. This verb ENUMERATES the candidates -- cards that
    depend on this one, cards whose premise or tier rationale names it, constants
    sourced from this run. It does not judge them. Judging is the agent's, and
    the list is what makes the judgement possible.
    """
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    node = _node(graph, a.id)
    if node["state"] != "running":
        refuse(f"card {a.id} is {node['state']}, not running",
               fix="a result belongs to a declared arm. `set state=running` with a "
                   "`run_id` first, so the number has a run behind it")
    if not node.get("run_id"):
        refuse(f"card {a.id} is running with no run_id",
               fix="a result with no run cannot be re-checked by anyone later")
    try:
        result = json.loads(a.result)
    except json.JSONDecodeError as e:
        broke(f"--result is not JSON: {e}")
    if not a.tier:
        refuse("every result carries its tier",
               fix="a [T1 trend] conclusion cited next week as [T2 controlled] is "
                   "how a soft number becomes a hard one -- the single most common "
                   "death on this line. Pass --tier")
    if a.tier not in TIERS:
        broke(f"--tier must be one of {TIERS}")

    node["result"] = result
    node["tier"] = a.tier
    node["state"] = "filled"
    node["history"].append({"at": now_utc(), "to": "filled", "run": node["run_id"],
                            "tier": a.tier})
    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)

    emit({"id": a.id, "state": "filled", "tier": a.tier,
          "must_review": _propagation(graph, node, p),
          "note": "filled, NOT adjudicated. `close` needs a verdict, and a verdict "
                  "often waits on another card -- that is why the two are separate."})


def _propagation(graph, node, p):
    """The three sweep questions, as candidate lists. Textual and structural
    reference only -- whether a premise is actually void is a judgement."""
    nid = node["id"]
    hits = {"depends_on_this": [], "names_this": [], "constants_from_this_run": []}
    for n in graph.get("nodes", []):
        if n["id"] == nid:
            continue
        if nid in (n.get("depends_on") or []) or n.get("parent") == nid:
            hits["depends_on_this"].append(n["id"])
            continue
        blob = json.dumps({k: n.get(k) for k in
                           ("premise", "premise_share", "criterion", "kill_condition")},
                          ensure_ascii=False)
        if nid in blob:
            hits["names_this"].append(n["id"])
    state = read_json(p["state"], required=False) or {}
    for c in state.get("constants", []):
        if c.get("source_run") and c.get("source_run") == node.get("run_id"):
            hits["constants_from_this_run"].append(c.get("name"))
    hits["questions"] = [
        "did this result void another card's PREMISE?",
        "did it void another card's ORDERING rationale (its tier)?",
        "did it void a CONSTANT in state.json? If so update it and say in that "
        "file's header which foundation constant this round overturned.",
    ]
    return hits


# ---------------------------------------------------------------- CLOSE

def cmd_close(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    node = _node(graph, a.id)
    if node["state"] != "filled":
        refuse(f"card {a.id} is {node['state']}, not filled",
               fix="a verdict needs a result. Nothing may be adjudicated on a card "
                   "that never ran -- except a T0, which is closed at `set` time")
    if bool(a.verdict) == bool(a.killed_by):
        broke("pass exactly one of --verdict / --killed-by")

    if a.killed_by:
        if a.killed_by not in KILLED_BY:
            broke(f"--killed-by must be one of {sorted(KILLED_BY)}")
        if a.killed_by == "unfaithful_port":
            # Not a death. Recorded as a distinct kind precisely so that "the
            # port was wrong" can never be filed as "the idea was wrong".
            node["state"] = "running"
            node["result"] = None
            node["history"].append({"at": now_utc(), "to": "running",
                                    "note": "unfaithful port -- back to fixing, not killed"})
            graph["updated_at"] = now_utc()
            atomic_write_json(p["graph"], graph)
            emit({"id": a.id, "state": "running",
                  "note": "unfaithful_port is not a death. The card returns to running; "
                          "fix the port and fill again."})
            return
        if not a.revive_if:
            refuse("a kill needs --revive-if",
                   fix="the four deaths have differently shaped revival conditions; "
                       "one written without it is a proposal the next round re-proposes. "
                       "Note: a card revives the moment the effect is measured DIRECTLY, "
                       "whether or not this proxy condition ever fires")
        if node.get("tier") == "T4":
            refuse("a T4 approximation cannot kill the original",
                   fix="T4 prices, it does not refute -- say what the approximation "
                       "dropped, then close it as `downgraded` or promote the original")
        node["state"] = "killed"
        node["killed_by"] = a.killed_by
        node["revive_if"] = a.revive_if
        node["history"].append({"at": now_utc(), "to": "killed",
                                "killed_by": a.killed_by, "revive_if": a.revive_if})
    else:
        if a.verdict not in VERDICTS:
            broke(f"--verdict must be one of {VERDICTS}")
        if a.verdict == "lost" and node.get("tier") == "T4":
            refuse("a T4 approximation cannot refute the original", fix="close as `downgraded`")
        node["state"] = "closed"
        node["verdict"] = a.verdict
        node["history"].append({"at": now_utc(), "to": "closed", "verdict": a.verdict})

    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    emit({"id": a.id, "state": node["state"],
          "unblocked": [n["id"] for n in graph["nodes"]
                        if a.id in (n.get("depends_on") or [])
                        and n["state"] not in SETTLED]})


# ---------------------------------------------------------------- CHECK

def cmd_check(a):
    """The seven invariants, plus the two MLClaw adds. Reports; repairs nothing.

    Cited by contracts/contract_explore.py. Every finding names the card and what
    the breakage means -- a check whose output does not say which side to change
    is a check people learn to ignore.
    """
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    nodes = graph.get("nodes", [])
    by_id = {n["id"]: n for n in nodes}
    corpus = graph.get("corpus") or {}
    state = read_json(p["state"], required=False) or {}
    baseline = read_json(p["baseline"], required=False) or {}
    findings = []

    def flag(sev, inv, nid, detail):
        findings.append({"severity": sev, "invariant": inv, "card": nid, "detail": detail})

    for n in nodes:
        nid, st = n["id"], n.get("state")

        # 1. every settled card has a run id or a measurement source
        if st in SETTLED and not n.get("run_id") and n.get("tier") != "T0":
            flag("critical", "settled_without_source", nid,
                 "a conclusion with no run behind it -- nobody can re-check it")

        # 2. a running card's dependencies are all settled
        if st in ("running", "filled"):
            unmet = [d for d in n.get("depends_on") or []
                     if d not in by_id or by_id[d]["state"] not in SETTLED]
            if unmet:
                flag("critical", "gate_bypassed", nid,
                     f"opened with unsettled dependencies {unmet}")

        # 3. every kill has a typed cause and a revival condition
        if st == "killed":
            if n.get("killed_by") not in KILLED_BY:
                flag("critical", "untyped_kill", nid,
                     "the four deaths revive differently; untyped is unwritten")
            if not n.get("revive_if"):
                flag("critical", "kill_without_revive", nid,
                     "the next round will re-propose this")

        # 5. a ready card's schema is complete
        if st == "ready":
            miss = _missing_fields(n)
            if miss:
                flag("critical", "ready_but_incomplete", nid,
                     f"would open an arm nobody can adjudicate; missing {miss}")

        # 8. MLClaw add -- the scope guard. A share from another corpus is absent.
        sc = _share_scope(n, corpus)
        if sc and st not in ("draft",):
            flag("critical", "premise_share_out_of_scope", nid, sc)

        # 9. MLClaw add -- every number carries its tier
        if n.get("result") is not None and not n.get("tier"):
            flag("major", "result_without_tier", nid,
                 "an untiered number gets promoted; that is how a false floor entered once")

        # 6. filled cards need somebody to adjudicate them
        if st == "filled":
            flag("minor", "awaiting_verdict", nid,
                 "a result with no conclusion; blocks any stop decision")

    # 4. no two running cards share (parent, delta)
    seen = {}
    for n in nodes:
        if n.get("state") != "running":
            continue
        key = (n.get("parent"), json.dumps(n.get("delta"), sort_keys=True))
        if key in seen:
            flag("major", "duplicate_arm", n["id"],
                 f"same parent and delta as {seen[key]} -- duplicated work, or one was forgotten")
        seen[key] = n["id"]

    # 7. cited constants still hold
    declared = corpus.get("declared_at")
    for c in state.get("constants", []):
        if declared and c.get("measured_at") and c["measured_at"] < declared:
            flag("major", "stale_constant", None,
                 f"constant {c.get('name')!r} measured {c.get('measured_at')}, "
                 f"before this corpus was declared {declared} -- state.json's own "
                 f"header voids it")

    # The floor gates the WORDING of every result, not any arm.
    if baseline.get("value") is None:
        hard = [n["id"] for n in nodes
                if n.get("tier") in ("T2", "T3") and n.get("result") is not None]
        if hard:
            flag("critical", "hard_result_without_noise_floor", None,
                 f"{hard} report at T2/T3 with no measured floor. Without one, "
                 f"'no significant improvement' is UNDECIDABLE, not negative -- "
                 f"those results are [T1 trend] at best")

    order = {"critical": 0, "major": 1, "minor": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    payload = {"cards": len(nodes), "findings": findings,
               "counts": {s: sum(1 for f in findings if f["severity"] == s)
                          for s in ("critical", "major", "minor")}}
    if any(f["severity"] == "critical" for f in findings):
        refuse("the graph has critical findings -- do not open another arm", **payload)
    emit(payload)


# ---------------------------------------------------------------- STATUS

def cmd_status(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    baseline = read_json(p["baseline"], required=False) or {}
    nodes = graph.get("nodes", [])
    counts = {s: sum(1 for n in nodes if n.get("state") == s) for s in STATES}
    emit({
        "corpus": graph.get("corpus"),
        "noise_floor": baseline.get("value"),
        "counts": counts,
        "killed": [{"id": n["id"], "killed_by": n.get("killed_by"),
                    "revive_if": n.get("revive_if")}
                   for n in nodes if n.get("state") == "killed"],
        "awaiting_verdict": [n["id"] for n in nodes if n.get("state") == "filled"],
        "updated_at": graph.get("updated_at"),
    })


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--project", required=True)
        p.add_argument("--session")

    a = sub.add_parser("add", help="a one-line idea becomes a card")
    common(a)
    a.add_argument("--title", required=True)
    a.add_argument("--kind", required=True, choices=KINDS)
    a.add_argument("--premise")
    a.add_argument("--criterion")
    a.add_argument("--guardrail", action="append")
    a.add_argument("--parent")
    a.add_argument("--depends-on", action="append", dest="depends_on")
    a.add_argument("--kill-condition", dest="kill_condition")
    a.set_defaults(fn=cmd_add)

    s = sub.add_parser("set", help="complete or amend a card")
    common(s)
    s.add_argument("--id", required=True)
    s.add_argument("--set", action="append", metavar="FIELD=VALUE")
    s.set_defaults(fn=cmd_set)

    r = sub.add_parser("ready", help="the ready set, and deadlocks")
    common(r)
    r.set_defaults(fn=cmd_ready)

    f = sub.add_parser("fill", help="results onto a card + what to re-examine")
    common(f)
    f.add_argument("--id", required=True)
    f.add_argument("--result", required=True, help="JSON")
    f.add_argument("--tier", required=True, choices=TIERS)
    f.set_defaults(fn=cmd_fill)

    c = sub.add_parser("close", help="a verdict, or one of four deaths")
    common(c)
    c.add_argument("--id", required=True)
    c.add_argument("--verdict", choices=VERDICTS)
    c.add_argument("--killed-by", dest="killed_by", choices=sorted(KILLED_BY))
    c.add_argument("--revive-if", dest="revive_if")
    c.set_defaults(fn=cmd_close)

    k = sub.add_parser("check", help="the invariants; reports, never repairs")
    common(k)
    k.set_defaults(fn=cmd_check)

    t = sub.add_parser("status", help="one-screen summary")
    common(t)
    t.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
