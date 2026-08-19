#!/usr/bin/env python3
"""The experiment graph — the four operations, and the scan that says it broke.

`/explore` is a search whose unit is a PROPOSAL, not a trial: one card per thing
worth trying, verified by an ordinary run, adjudicated once. What it searches over
is what the model IS -- structure, components, network selection -- and that is the
line against `/train-tune`, which searches the operating point of a model already
settled. A parameter can be a card here (「is it a capacity problem」); a card is
never a point in `runtime_params`. The design came from
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

Plus the three that read rather than write, and one that records a disagreement:

    check   the invariants. Reports, never repairs -- see below
    status  one-screen summary of the whole graph
    new     what became a CONCLUSION since a time, and what only became a number.
            The two lists stay apart because "so what new conclusions are there" is the moment that
            most tempts reporting a result as one
    dispute two cards disagree: mark both, revert neither, defer the adjudication
    resolve adjudicate one dispute -- upheld / rejected / not_comparable

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
from _records import (atomic_write_json, broke, digits as _digits,  # noqa: E402
                      emit, now_utc, read_json, refuse)
from _vocab import PROVENANCE, TIERS  # noqa: E402
from compare import scope_key, scopes_equivalent, UNSPECIFIED_SCOPE  # noqa: E402

# Seven, and the load-bearing split is `filled` vs `closed`: having a result is NOT
# having a conclusion.
# A card whose numbers are in but whose verdict is not is `filled`. Collapsing
# the two propagates an unexplained number downstream as a finding -- the
# recorded instance had a mechanism criterion pass literally while the verdict
# was "the mechanism was verified and it is not worth anything", and that verdict
# needed a DIFFERENT card's measurement before it could be reached.
STATES = ("draft", "blocked", "ready", "running", "filled", "closed", "killed")

# Settled = may be someone else's dependency. `killed` counts: "stop waiting on
# this one" is as much an answer as a verdict.
SETTLED = ("closed", "killed")

# ‼️ The seven split two ways, and leaving the split unnamed is what let `status`
# and `ready` answer the same question OPPOSITELY -- the first reading the stored
# label, the second recomputing from the card, neither wrong about its own
# question. Three cards complete with no dependencies reported `blocked: 3` and
# `ready: [N01, N02, N03]` at the same instant, and `status` is the one a person
# reads. Worse in the other direction: nothing ever WROTE `ready`, so an arm whose
# card was never flipped to `running` stayed in the computed ready set and could
# be opened twice.
#
# DERIVED    a function of the card's own fields plus its dependencies. Nobody
#            declares these and nobody should: they change when a DIFFERENT card
#            settles, so a stored copy is stale the moment it is written.
# DECLARED   an act somebody performed -- an arm opened, a result filled, a
#            verdict reached. Not computable from the card, and the record IS the
#            declaration.
#
# `_derive_state` is the single truth for the first three; the stored field is
# written forward as a convenience for external readers, never read back for a
# decision. Same fact, one writing -- the rule `/agent-refactor` calls a DOUBLE PROTOCOL, and
# the reason this file has one state machine rather than three.
DERIVED_STATES = ("draft", "blocked", "ready")
DECLARED_STATES = ("running", "filled", "closed", "killed")

# What a card proposes. The kind decides which stage its arm runs in and which
# fields it needs -- a measurement has no single-key delta and therefore no
# parent, while a port without one cannot attribute its delta to anything.
KINDS = ("measurement", "port", "original", "task_driven")

# The three kinds that WRITE CODE, and therefore the three that need a tree of
# their own. A `measurement` card reads the corpus or a checkpoint that already
# exists; the other three all end in an edit to the training repo, which is why
# concurrency is a question about exactly these and not about the queue at large.
#
# ‼️ This is not a stylistic preference, it is forced by MLClaw's own layout.
# `run-mechanics.md -> Code snapshot` resolves ONE path per stage --
# `stages/<stage>/code/_source if exists else stages/<stage>/code` -- and for
# `code_source.source: local` that path is a single external directory acting as
# a soft link. So arms opened in parallel share a working tree BY CONSTRUCTION,
# while `SKILL.md` Stage 3 says to compute the ready set and open all of it at
# once. The two rules meet in `code_snapshot.py`, which reads that tree at launch.
CODE_KINDS = ("port", "original", "task_driven")

# An arm that has not closed. Compares greater than any ISO timestamp, so an open
# arm overlaps everything that had not already ended when it started.
OPEN_END = "\uffff"

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

VERDICTS = ("won", "lost", "downgraded")

# When two records in this graph disagree. Borrowed from ARA (arXiv:2604.24658)
# -> research-manager "Contradiction trigger", whose rule is the whole point:
# NEITHER RECORD IS OVERWRITTEN. Both get marked, a node is appended that names
# the pair, and it stops there -- adjudication is the researcher's.
#
# MLClaw had no way to say this at all. A new result contradicting a closed
# verdict left exactly two options, and both destroy the record: rewrite the old
# card (losing what the next round needs) or say nothing. `check` can now report
# the third state, which is the true one -- these two disagree and nobody has
# ruled.
DISPUTE_OUTCOMES = {
    "upheld": "the challenger is right; the disputed card no longer stands",
    "rejected": "the challenger is wrong; the disputed card is untouched",
    "not_comparable": "they never conflicted -- different corpus, metric or scope",
}

# Power ordering. T0 verified nothing and T4 is an approximation priced for the
# original, so neither may contradict anything; among the rest, a cheaper check
# CANNOT refute a dearer one -- SKILL.md Stage 3.5 rule 2: a cheap check has low power;
# it can give you a reason to continue, not a reason to rule something out. Most
# "contradictions" between a short run and
# a controlled one are not disagreements at all, and adjudicating one as if it
# were is how a good result gets thrown away by a cheap probe.
TIER_POWER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 0}

# Who put this on the queue. Borrowed from ARA (arXiv:2604.24658) --
# `research-manager/references/event-taxonomy.md` -> "Provenance Assignment".
#
# It is load-bearing HERE specifically because SKILL.md Stage 3 says the queue is
# the queue is maintained BY THE USER, not the agent's notebook: the user drops in a one-line idea, the
# agent completes the card and executes. A card the user demanded and a card the
# agent invented therefore mean different things -- and without this field they
# are the same row. The distribution is itself the signal: a graph that is all
# `ai-suggested` is a graph nobody asked for, and `check` says so.
#
# ‼️ `ai-suggested` NEVER auto-upgrades. Only an explicit `set` moves it, because
# the whole value of the tag is that it cannot be earned by the agent's own
# confidence. Same rule as `/ask-human` refusing to call an answer `verified`
# when nothing but the person confirmed it.

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


# --------------------------------------------------------------- grounding
#
# Borrowed from ARA (arXiv:2604.24658) -> research-manager "Number grounding".
# CLAUDE.md's "Never record a metric you did not read" is a PROHIBITION, and a
# prohibition leaves no trace when it is broken: a value typed from memory and a
# value read off a log are the same JSON. The quote is what makes the difference
# visible, and it is what turns the rule into something `check` can enforce.
#
# Shape:  {"value": 0.0462,
#          "sources": [{"ref": "logs/scan.txt:214",
#                       "quote": "G>=52 frames: 254/5495 (4.62%)",
#                       "kind": "result"}]}
# or:     {"value": null, "pending": "loader scan not run on this corpus yet"}
#
# `[input]` vs `[result]`: a value you SET (cite what defines it) versus a value
# a run PRODUCED (cite what reports it). MLClaw already draws this line one level
# up as `workload` versus `scope`; it is the same distinction, and citing a
# measured outcome to the config meant to produce it is the same error there.
#
# `[derived]` is the third, and it exists because the two above could not express
# the noise floor -- the one number the whole round rests on. A floor is a SPREAD
# between two repeat measurements, so no log anywhere prints it: every honest
# citation of its endpoints failed the digit check below, and the cheapest way to
# pass was to invent `{"quote": "31.09 - 28.16 = 2.93"}` -- which is precisely the
# fabrication the check exists to catch. A record layer whose only passing path is
# the forbidden one has taught its reader to fake the field.
#
# So a derived value names the COMPUTATION as a source of its own:
#   {"ref": "stages/exploration/scripts/seed_floor.py",
#    "command": "python stages/exploration/scripts/seed_floor.py",
#    "quote": "<its stdout, which contains the number>", "kind": "derived"}
# beside the endpoints it consumed, cited `result` as usual.
#
# ‼️ `derived` is NOT a way out of the digit check -- it MOVES it. The derived
# source must still quote the value (a ruler that does not print its answer is
# not a source), and it must carry `command`, because the only thing separating a
# derivation from a number typed from memory is that somebody else can RE-RUN it.
# What `derived` relaxes is the demand that the ENDPOINTS quote the value too:
# citing both logs used to cost two criticals, so the more honest record scored
# worse than the thinner one.
SOURCE_KINDS = ("input", "result", "derived")


def _grounding(label, obj):
    """-> [(severity, detail)] for one {value, sources|pending} block.

    ‼️ This is a FLOOR, not a proof. A quote containing the digits does not show
    the source was open -- but a quote NOT containing them shows it was not, and
    that is the failure mode worth catching: a number written from memory and
    back-cited to a plausible path. `[pending]` beats a guess, so it is not a
    finding; an unverified-but-plausible citation is fabrication and is.
    """
    if not isinstance(obj, dict) or "value" not in obj:
        return []
    v, out = obj.get("value"), []
    srcs = obj.get("sources") or []
    if obj.get("pending"):
        if v is not None:
            out.append(("major", f"{label}: has a value AND a `pending` note -- "
                                 f"decide which is true"))
        return out
    if v is None:
        return out
    if not srcs:
        return [("critical", f"{label}: a number with no source. Write `pending` if you "
                             f"cannot open one -- a bare value is indistinguishable from "
                             f"one recalled and back-cited")]
    # Which sources must ATTEST the value. Default: all of them. With a `derived`
    # source present, the derivation attests and the endpoints are its inputs --
    # they are cited so the computation can be re-checked, not so they can each
    # independently contain an answer that only the computation produces.
    has_derived = any(s.get("kind") == "derived" for s in srcs)
    for i, s in enumerate(srcs):
        tag = f"{label}.sources[{i}]"
        kind = s.get("kind")
        if not s.get("ref"):
            out.append(("critical", f"{tag}: no ref"))
        if kind not in SOURCE_KINDS:
            out.append(("major", f"{tag}: kind must be `input` (a value you set), "
                                 f"`result` (a value a run produced) or `derived` "
                                 f"(a value a stated computation produced)"))
        if kind == "derived" and not s.get("command"):
            out.append(("critical",
                        f"{tag}: a `derived` source needs the «command» that produced "
                        f"the quote. A derivation nobody can re-run is a number typed "
                        f"from memory with a script path beside it"))
        q = s.get("quote")
        if not q:
            out.append(("critical", f"{tag}: no «quote». A bare path is not grounding -- "
                                    f"the transcribed line is the evidence the source "
                                    f"was open"))
        elif ((kind == "derived" or not has_derived)
                and isinstance(v, (int, float)) and not isinstance(v, bool)):
            if _digits(v) not in "".join(c for c in q if c.isdigit()):
                out.append(("critical",
                            f"{tag}: the quote does not contain {v!r}. Either the source "
                            f"was not open when this was written, or the record carries "
                            f"more precision than the source reports"))
    return out


# ----------------------------------------------------------- run binding
#
# ARA (arXiv:2604.24658) -> research-manager "Forensic Binding Checklist":
# experiment -> claim, and the binding must RESOLVE. MLClaw's run record carries
# `hypothesis` as free text that run-mechanics explicitly says tools must not
# require -- so a run states an expectation and nothing ever closes it. `verifies`
# is the optional structured sibling: it does not replace the sentence, it says
# which card the sentence is about and what would falsify it.
#
# Two checks, both from ARA's D2 (Falsifiability Quality):
#   actionable   -- could an independent reader execute this? It must name a
#                   number or the criterion's own metric. "if the method does not
#                   work" is a tautology, and a tautology passes every run.
#   resolves     -- the card names this run and this run names the card. A
#                   one-way pointer looks identical to a binding right up to the
#                   moment somebody follows it.
#
# `[pending]` is a legitimate value, per ARA's rule that an impossible binding is
# written down rather than guessed -- the same reason `unverifiable` exists here
# beside `gone`.
PENDING = "[pending]"


def _run_json(project, target_stage, run_id):
    """-> (record, status). status is `ok` | `absent` | `unreadable`.

    Three states, not two. A run directory that is not here means the arm was
    staged on another machine or has not been created yet; that is NOT the same
    as a broken binding, and reporting it as one would train the reader to ignore
    the finding. Same discipline as `census.py` keeping `gone` apart from
    `unreachable`.
    """
    path = os.path.join(project, "stages", target_stage, "runs", run_id, "run.json")
    if not os.path.exists(path):
        return None, "absent"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), "ok"
    except (OSError, json.JSONDecodeError):
        return None, "unreadable"


# Where a floor came from, and therefore what may be asked of it.
#
# ‼️ `runs` used to be the ONLY door: two `/eval-run` ids, re-read from
# `stages/*/runs/<id>/run.json`. That is right for a project MLClaw has owned
# from the start, and it has no answer at all for the case `SKILL.md ->
# Where you come in` calls the normal one -- "users essentially never start from
# scratch", enter at Stage 6,
# backfill Stage 0. A floor from before the takeover was measured on a box that
# has since been released, from a checkpoint on a powered-down disk, by a
# pipeline that never wrote a `run.json`. There was nowhere to write it down.
#
# What this code did to somebody in that position, measured on it:
#
#   honest    value + sources, `runs: []`               2 criticals, check REFUSES
#   silent    value: null                               critical, every T2/T3 void
#   invented  `runs: [two ids that do not resolve]`     one major, floor USABLE
#
# The record layer paid for the invented one. That -- not the missing feature,
# the INVERTED incentive -- is the defect `origin` removes. An external floor is
# writable, says out loud what nobody here could check, and is worth exactly what
# it is: a `claim`, which is MLClaw's existing word for this. The same concept has
# had a door in two other scripts all along (`conclude.py -> EVIDENCE_KINDS`,
# `compare_baseline.py -> _external_side`); the floor was the one place without one.
FLOOR_ORIGINS = ("mlclaw", "external")

# What each state buys, in words, because the one-screen summary is where the
# sentence gets quoted off and a state name alone does not say what it permits.
FLOOR_GATES = {
    "verified": "T2 and T3",
    "claim": "T2 -- never a T3",
    "unverifiable": "T2 -- never a T3",
    "not_measured": "nothing -- every result is [T1 trend] at best",
}


def _floor_status(void, unchecked):
    """NOT MEASURED outranks could-not-look, which outranks confirmed."""
    return "not_measured" if void else ("unverifiable" if unchecked else "verified")


def _floor(baseline, corpus, project, stages):
    """The noise floor, judged. -> ([(severity, detail)], status).

    `status` is `verified` | `claim` | `unverifiable` | `not_measured`, and four
    is the point. A bool could only say "gates or does not", which is what forced
    an HONEST external floor and an ABSENT one into the same answer:

      verified      this project measured it -- >=2 runs whose records resolve and
                    agree on `mode`, `scope` and corpus.
      claim         `origin: external`. Grounded by `sources` exactly like a
                    measured one (so a number somebody remembered still cannot be
                    written), and it states in `unchecked` what nobody here could
                    confirm. Gates T2. Never carries a T3.
      unverifiable  it claims to be this project's and the run records do not
                    resolve. Gated identically to `claim` and reported as a major
                    -- deliberately NOT better than the honest door, which is the
                    half that stops invented ids from being the cheap path.
      not_measured  null, out-of-corpus, retired, or the runs disagree about what
                    they measured. Every T2/T3 in the round drops to `[T1 trend]`.

    ‼️ An out-of-scope or stale floor is not a WEAK floor, and `unverifiable` is
    not a weak `verified`. `_share_scope` treats a share measured on another
    corpus as ABSENT and kills the card -- the 47%-vs-4.62% rule -- and the floor
    is the more expensive side to be wrong on, being what every later verdict
    rests on: the recorded failure is a floor reported as 0.06 that was really
    0.25, a "+0.30 is real" conclusion built on it, and the lot withdrawn.

    History worth keeping: every field checked below once had NO READER. `runs`
    carried a written promise -- "`graph.py check` re-reads their `run.json`" --
    that no code kept, `measured_on` said "Must equal `graph.json -> corpus`" and
    nothing compared them, and `check` asked the floor exactly two questions: is
    it grounded, and is it null.
    """
    out = []
    if baseline.get("value") is None:
        return out, "not_measured"

    origin = baseline.get("origin") or "mlclaw"
    if origin not in FLOOR_ORIGINS:
        out.append(("major",
                    "`origin` is %r -- it must be `mlclaw` (this project measured it) "
                    "or `external` (measured where MLClaw cannot re-read it). Read as "
                    "`mlclaw`, the stricter of the two" % (origin,)))
        origin = "mlclaw"

    void = False       # NOT MEASURED: the number does not describe this round
    unchecked = False  # nobody could look: not agreement, and not disagreement

    # 1. Which corpus. `_share_scope`'s rule, applied to the floor. It does NOT
    #    relax for `origin: external` -- where a number was measured is precisely
    #    what an outside floor is least able to prove, and a floor from another
    #    corpus is not a floor whoever ran it.
    on = baseline.get("measured_on") or {}
    if not on.get("dataset_id"):
        out.append(("critical", "the floor has no `measured_on` -- a floor is a property "
                                "of (weights x measurement x corpus), so one quoted from "
                                "nowhere gates nothing. Treated as NOT MEASURED"))
        void = True
    elif (on.get("dataset_id") != corpus.get("dataset_id")
            or on.get("snapshot") != corpus.get("snapshot")):
        out.append(("critical",
                    "the floor was measured on %s@%s, this graph's corpus is %s@%s -- "
                    "its own `retires_on` lists `dataset_snapshot`, so this floor is "
                    "RETIRED, not weak. Treated as NOT MEASURED"
                    % (on.get("dataset_id"), on.get("snapshot"),
                       corpus.get("dataset_id"), corpus.get("snapshot"))))
        void = True

    # 2. Staleness, symmetric with the constants scan below.
    declared = corpus.get("declared_at")
    if declared and baseline.get("measured_at") and baseline["measured_at"] < declared:
        out.append(("major",
                    "the floor was measured %s, before this corpus was declared %s"
                    % (baseline["measured_at"], declared)))

    # 3. An external floor cannot be re-read, so what is asked of it is different:
    #    not "do these two runs agree" -- nothing here can see them -- but "does
    #    the record say, in the file, what nobody here confirmed".
    if origin == "external":
        if baseline.get("runs"):
            out.append(("major",
                        "an `external` floor also names `runs` -- two accounts of where "
                        "this number came from and only one can be true. Drop `runs`, or "
                        "set `origin: mlclaw` and let them be re-read"))
        if not str(baseline.get("unchecked") or "").strip():
            out.append(("critical",
                        "an `external` floor must fill `unchecked`: what nobody in this "
                        "project could confirm, and why it cannot simply be re-measured "
                        "here. Without it this is a bare number wearing a floor's name, "
                        "and the door is narrow on purpose. Treated as NOT MEASURED"))
            void = True
        return out, ("not_measured" if void else "claim")

    # 4. The two runs behind it. A spread needs two measurements, and they must
    #    differ in NOTHING but the seed -- two ids that differ in anything else
    #    measure that difference and call it noise.
    runs = baseline.get("runs") or []
    if len(runs) < 2:
        out.append(("critical",
                    "a floor is a SPREAD and %d run(s) cannot produce one. `runs` names "
                    "the repeat measurements it was computed from; without them nobody "
                    "can re-check what was actually varied. ‼️ If this floor was measured "
                    "before MLClaw -- the takeover, which `SKILL.md -> Where you come in` calls "
                    "the normal way in -- do NOT invent two ids: set `origin: external` "
                    "and fill `unchecked`" % len(runs)))
        return out, "not_measured"

    read, unread = [], []
    for rid in runs:
        rec, st = None, "absent"
        for stage in stages:
            rec, st = _run_json(project, stage, rid)
            if st == "ok":
                break
        if st == "ok":
            read.append((rid, rec))
        else:
            unread.append((rid, st))
    if unread:
        # ‼️ Never report data you could not look at. A run whose record is absent
        # and one whose record is corrupt are two facts, and neither is agreement.
        out.append(("major",
                    "could not read the run record for %s (searched %s) -- so `mode` and "
                    "`scope` agreement is UNVERIFIED for this floor, not confirmed. ‼️ If "
                    "these ids were never MLClaw runs, `origin: external` is the honest "
                    "form of the same floor and gates exactly as far"
                    % (", ".join("%s [%s]" % (r, st) for r, st in unread),
                       "/".join(stages))))
        unchecked = True
    if len(read) < 2:
        return out, _floor_status(void, unchecked)

    modes = {r.get("mode") for _, r in read}
    if len(modes) > 1:
        out.append(("critical",
                    "the floor's runs disagree on `mode` (%s). A debug spread and a "
                    "production spread are different quantities that share a name, and "
                    "the difference between them is not noise. Treated as NOT MEASURED"
                    % ", ".join(sorted(str(m) for m in modes))))
        void = True
    keys = {scope_key(r.get("scope")) for _, r in read}
    if keys == {UNSPECIFIED_SCOPE}:
        out.append(("major",
                    "none of the floor's runs recorded a `scope`, so nothing can tell "
                    "whether they measured the same thing. An unrecorded scope is not "
                    "evidence of an equal workload -- it is a gap, and it is why this "
                    "is reported rather than passed"))
        unchecked = True
    elif len(keys) > 1:
        first = read[0]
        for rid, rec in read[1:]:
            if not scopes_equivalent(first[1].get("scope"), rec.get("scope")):
                out.append(("critical",
                            "the floor's runs %s and %s were measured on non-equivalent "
                            "`scope` -- what that spread measures is the scope difference. "
                            "Treated as NOT MEASURED" % (first[0], rid)))
                void = True
                break
    return out, _floor_status(void, unchecked)


def _delta(node, run, parent_run):
    """Is the change that ran the change that was declared?

    From a real round (e2e_3D_detection, 2026-08-14): a baseline at AP50 26.76 was
    compared against a new arm at 7.88, and the 3.4x was attributed to the
    technique. Four unintended differences were sitting between them --
    `rot_aug_deg` 0 -> 15, `num_points` 40000 -> 145000, `no_thin_cloud` on -> off,
    `warm_lr_epochs` 9 -> 4 -- plus 8 GPUs down to 1. The arm was not measuring
    what it said it measured, and nothing raised.

    ‼️ TWO RECORD FIELDS, NOT ONE. `lineage.variation_summary` covers
    `runtime_params`; the GPU count lives in `workload.world_size`. A check that
    read only the first would have passed that round while missing the 8x -- the
    exact shape of a guard that reports the conclusion it exists to prevent.

    Prose saying "this arm adds CDN" and a config diff saying it also picked up
    last Tuesday's changed default are both present, and only one is checkable.
    """
    declared = node.get("delta")
    if declared is None or run is None:
        return []
    actual = dict(((run.get("lineage") or {}).get("variation_summary") or {}))
    want = declared if isinstance(declared, dict) else {str(declared): None}
    extra = sorted(k for k in actual if k not in want)
    missing = sorted(k for k in want if k not in actual)
    out = []
    if extra:
        out.append(("critical", f"{node['id']}: declared delta {sorted(want)} but the run "
                                f"also varies {extra} -- more than one key changed, so no "
                                f"result can be attributed to any of them"))
    if missing:
        out.append(("major", f"{node['id']}: declared {missing} but the run's "
                             f"variation_summary does not carry it -- either the flag "
                             f"was dropped or the record was written from the plan"))
    # The half variation_summary cannot see.
    if parent_run:
        a, b = run.get("workload") or {}, parent_run.get("workload") or {}
        moved = sorted(k for k in ("world_size", "batch_size", "grad_accum", "epochs")
                       if a.get(k) is not None and b.get(k) is not None and a[k] != b[k])
        if moved:
            out.append(("critical", f"{node['id']}: workload differs from its parent on "
                                    f"{moved} -- outside `variation_summary`, so a check "
                                    f"reading only that field would pass this arm"))
    return out


def _eval_setting(node):
    """When the technique changes what the CORRECT measurement setting is.

    From the same round: one-to-one matching needs no NMS, and it was being
    compared against one-to-many UNDER NMS. Holding the setting fixed makes the
    contrast clean and simultaneously measures the setting rather than the
    technique -- "one-to-one HAS to be measured with nms off", said after the comparison had already
    been read once.

    The resolution that round reached is the rule: hold the setting fixed for the
    contrast AND re-evaluate at the technique's own setting. The second costs no
    training -- it is a re-evaluation of a checkpoint that already exists -- and
    it is the only number that describes what would actually ship. That arm read
    84.16 held and 92.15 at its own setting; reporting either alone would have
    been a different claim.
    """
    ev = node.get("eval_setting") or {}
    own = ev.get("own")
    if not own or node.get("state") not in ("filled", "closed"):
        return []
    res = node.get("result") or {}
    have = set(res) if isinstance(res, dict) else set()
    missing = [k for k in ("at_held", "at_own") if k not in have]
    if missing:
        return [("critical",
                 f"{node['id']}: this arm changes the evaluation setting to {own!r}, so "
                 f"one number cannot describe it. `result` needs {missing} -- `at_held` "
                 f"is the fair contrast, `at_own` is what you would ship, and the second "
                 f"is FREE (re-evaluate the checkpoint, no training)")]
    return []


def _binding(node, run, run_status):
    """-> [(severity, detail)] for one card's run binding."""
    out = []
    if run_status == "unreadable":
        return [("major", f"{node['id']}: its run's record could not be read -- the "
                          f"binding is unverifiable, which is not the same as absent")]
    if run_status == "absent":
        return []   # not staged here. Says nothing about the binding.
    v = run.get("verifies")
    if not v:
        return [("minor", f"{node['id']}: run {node['run_id']} does not name this card "
                          f"in `verifies` -- the pointer resolves one way only, and a "
                          f"one-way pointer reads exactly like a binding")]
    card = (v.get("card") or "")
    if card != PENDING and not card.endswith("#" + node["id"]):
        out.append(("critical", f"{node['id']}: run {node['run_id']} says it verifies "
                                f"{card!r} -- the two records disagree about what this "
                                f"arm was for"))
    fals = (v.get("falsified_if") or "").strip()
    if not fals:
        out.append(("critical", f"{node['id']}: run {node['run_id']} has `verifies` with "
                                f"no `falsified_if`. A hypothesis nothing can refute is "
                                f"a wish -- either write the criterion or drop the field"))
    elif not any(c.isdigit() for c in fals):
        crit = (v.get("criterion") or "") + " " + (node.get("criterion") or "")
        words = [w for w in crit.replace("/", " ").split() if len(w) > 3]
        if not any(w.lower() in fals.lower() for w in words):
            out.append(("major",
                        f"{node['id']}: `falsified_if` names neither a number nor the "
                        f"criterion's metric -- an independent reader cannot execute it, "
                        f"and a criterion nobody can execute passes every run"))
    return out


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


def _next_dispute_id(graph):
    used = [d.get("id", "") for d in graph.get("disputes", [])]
    nums = [int(i[1:]) for i in used if i.startswith("D") and i[1:].isdigit()]
    return "D%02d" % ((max(nums) + 1) if nums else 1)


def _next_id(graph):
    """Queue numbers are stable and NEVER reused -- a killed card's number stays
    spent, because cross-references would otherwise point at a different
    proposal. So this is max+1 over every card ever, not a gap filler."""
    used = [n.get("id", "") for n in graph.get("nodes", [])]
    nums = [int(i[1:]) for i in used if i.startswith("N") and i[1:].isdigit()]
    return "N%02d" % ((max(nums) + 1) if nums else 1)


# ------------------------------------------------------- dependency edges
#
# ‼️ An edge used to be a bare id, and a bare id can only say ONE thing: wait.
# That single missing distinction is what made the graph serialise on order it
# was never asked to impose. The case it was found on: N07 (fusion) needed N06's
# SIGMA -- a value, two flags in one code change -- and the graph handed it N06's
# VERDICT, four and a half hours of it, because there was no way to write the
# difference down. The only way to parallelise was to DELETE the edge, and
# deleting it threw away the half that was real: that N07's number cannot be
# read without knowing whether that sigma was calibrated.
#
# So an edge now says WHAT IT BLOCKS, and the two answers gate different verbs:
#
#   launch    B cannot start. The premise share is unmeasured, the parent ckpt
#             does not exist, the code cannot be written until A lands. Gates
#             `ready` -- this is the old meaning, and the four real dependencies
#             in SKILL.md are all this kind.
#   reading   B starts immediately; B's RESULT cannot be interpreted alone. Gates
#             NOTHING. It stamps `conditional_on` at close so the verdict says out
#             loud what it is standing on, and re-surfaces when A lands.
#
# "running first is not reading first" was written here for the noise floor and treated as that one
# number's exemption. It is the general rule: the graph exists to gate READING,
# and the launch order is a consequence of it, not the point of it.
#
# ‼️ A bare id still means `launch`. The permissive default would silently
# unblock every graph ever written; the prompt to retype lives in `ready`, which
# is the moment the edge actually costs something.
DEP_BLOCKS = ("launch", "reading")


def _parse_dep(spec):
    """CLI form. `N06` -> launch; `N06:reading` -> reading."""
    if isinstance(spec, str) and ":" in spec:
        did, _, blocks = spec.partition(":")
        if blocks not in DEP_BLOCKS:
            broke(f"--depends-on {spec!r}: blocks must be one of {list(DEP_BLOCKS)}")
        return {"id": did, "blocks": blocks}
    return spec


def _dep_edges(node):
    """[(id, blocks)] out of either stored form.

    A malformed entry comes back with `blocks=None` rather than being dropped:
    silently ignoring an edge nobody can parse is how a gate stops existing while
    the file still shows it. `None` blocks like `launch` and `check` reports it.
    """
    out = []
    for d in node.get("depends_on") or []:
        if isinstance(d, str):
            out.append((d, "launch"))
        elif isinstance(d, dict) and isinstance(d.get("id"), str):
            b = d.get("blocks", "launch")
            out.append((d["id"], b if b in DEP_BLOCKS else None))
        else:
            out.append((json.dumps(d, ensure_ascii=False), None))
    return out


def _dep_ids(node, blocks=None):
    return [i for i, b in _dep_edges(node) if blocks is None or b == blocks]


def _unsettled(node, by_id, blocks=None):
    return [i for i, b in _dep_edges(node)
            if (blocks is None or b == blocks)
            and (i not in by_id or by_id[i].get("state") not in SETTLED)]


def _derive_state(node, by_id, corpus):
    """-> (state, why) for one card. The single truth for draft/blocked/ready.

    `why` carries what is missing or unmet, so callers report a reason rather
    than recomputing one. DECLARED states are returned as stored -- there is
    nothing to derive them from and the record is the declaration.

    ‼️ Two reads are deliberately by CONTENT and not by label:
      - a card carrying a `result` is `filled` whatever the label says;
      - a card carrying a `run_id` and no result has an ARM OPEN, whatever the
        label says. That one closes the double-arm hole: `run_id` is set at the
        moment an arm opens, so forgetting the accompanying `state=running` can
        no longer leave the card sitting in the ready set.
    """
    stored = node.get("state")
    if stored in SETTLED:
        return stored, {}
    if node.get("result") is not None or stored == "filled":
        return "filled", {}
    if node.get("run_id") or stored == "running":
        return "running", {}
    missing = _missing_fields(node)
    scope = _share_scope(node, corpus)
    if missing or scope:
        return "draft", {"why": "incomplete card", "missing": missing, "scope": scope}
    # ‼️ Only `launch` edges gate. A `reading` edge that is unsettled leaves the
    # card takeable and says so -- see the block above `DEP_BLOCKS`.
    unmet = [i for i, b in _dep_edges(node) if b != "reading"
             and (i not in by_id or by_id[i].get("state") not in SETTLED)]
    if unmet:
        return "blocked", {
            "why": "dependencies unsettled", "waiting_on": unmet,
            "ask": "does this card need those VERDICTS, or only a value / an "
                   "artifact / a line of code from them? Only the first is a "
                   "`launch` edge. Retype the rest `{\"id\": \"X\", \"blocks\": "
                   "\"reading\"}` and this card is takeable now -- the verdict it "
                   "eventually gets will carry `conditional_on` instead of the "
                   "queue carrying the wait"}
    return "ready", {}


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


# ------------------------------------------------------------------ the tree
#
# ‼️ THE AXIS EVERY OTHER INVARIANT ON THIS LIST IS BLIND TO. The rest govern the
# data axis (`_share_scope`), the config axis (`_delta`) and the record axis
# (`_binding`). None of them can see the working tree an arm was written in, and
# a contaminated tree breaks all three of their conclusions while satisfying
# every one of their checks.
#
# The mechanism, end to end: two ports are half-written in one directory when arm
# A launches. `code_snapshot.py` walks that directory, writes `code_dirty.patch`,
# and the patch carries B's edits along with A's. It applies cleanly. It
# reproduces exactly. `_delta` compares `runtime_params` + `workload` and sees
# nothing, because an uncommitted edit to a model file moves neither. The record
# is internally consistent, reproducible, and about a binary nobody described --
# which is the RUBBER-STAMP shape SKILL.md's run-card chapter names: a guard
# reporting the very conclusion it exists to exclude.
#
# What this costs is not one arm. Stage 6's "the control must be re-run on
# today's code" has no referent once "today" differs per arm, so the whole
# round's comparisons go with it.


def _tree_shape(tree):
    """-> [complaint]. The field's shape, checked where it is WRITTEN.

    `set` is generic (`--set field=value`), so without this a malformed `tree`
    lands silently and `check` reports it a round later -- by which time the
    directory it describes has moved on. Same reason `depends_on` is parsed here
    rather than left to `check`.
    """
    out = []
    if not isinstance(tree, dict):
        return ["`tree` must be an object: "
                '{"branch": "explore/N07-cdn", "base": "<sha>", "head": "<sha>", '
                '"path": "<worktree dir>"}']
    unknown = sorted(set(tree) - {"branch", "base", "head", "path"})
    if unknown:
        out.append(f"`tree` has unknown key(s) {unknown} -- one of branch/base/head/path")
    if not tree.get("branch"):
        out.append("`tree.branch` is what makes an arm's edits its own; a tree with no "
                   "branch is the shared directory under another name")
    return out


def _tree_scope(node, base):
    """The code axis's scope guard -- `_share_scope`, one axis over.

    A share measured on another corpus is not weak evidence, it is evidence about
    another question. An arm branched off another base is not a weak comparison,
    it is another experiment: the control is DEFINED by the base, so two arms on
    two bases have no common control and their deltas may not be subtracted --
    CLAUDE.md's "Never compare metrics across different `mode` or non-equivalent
    `scope`", read on the code axis.

    -> a complaint string, or None. Returns None when the round declared no base:
    that is a round-level finding reported once, not a per-card one.
    """
    tree = node.get("tree")
    if not isinstance(tree, dict):
        return None
    want = (base or {}).get("commit")
    if not want:
        return None
    got = tree.get("base")
    if not got:
        return ("declares a branch and no `base` -- nothing says what its delta is "
                "measured against, which is the whole reason the branch exists")
    if got != want:
        return (f"branched from {got[:12]}, this round's base is {want[:12]} -- the "
                f"control is defined by the base, so this arm shares no control with "
                f"the rest of the round. Rebase it onto the round's base, or say out "
                f"loud that it belongs to a different round")
    return None


def _running_window(node, run, run_status, still_open):
    """When this card's arm was actually open -> (start, end); either may be None.

    ‼️ The RUN's clock first, the card's history second, and that order is the
    same principle as `arm_tree_disagrees_with_run`: `started_at` + `duration_s`
    were written by the thing that ran, while the card's history is written by
    whoever was typing. The card is the fallback, not the source.

    The window is needed for cards that have SINCE settled -- somebody asks six
    weeks later why two numbers disagree, and by then both cards read `closed`.
    A check that only looked at what is running now would be silent at exactly
    the moment it is consulted.

    `end is None` means the record does not say, and that is a THIRD state --
    unknown, which is not "did not overlap". The one substitution made here is
    for an arm that has not closed, whose end is not unknown but not yet.
    """
    start = end = None
    if run_status == "ok" and run:
        start = run.get("started_at")
        dur = run.get("duration_s")
        if start and isinstance(dur, (int, float)) and not isinstance(dur, bool):
            try:
                from datetime import datetime, timedelta
                end = (datetime.fromisoformat(start)
                       + timedelta(seconds=float(dur))).isoformat()
            except (TypeError, ValueError):
                end = None
    hstart = hend = None
    for h in node.get("history") or []:
        to, at = h.get("to"), h.get("at")
        if not at:
            continue
        if to == "running" and hstart is None:
            hstart = at
        elif hstart is not None and hend is None and to in ("filled", "closed", "killed"):
            hend = at
    start = start or hstart
    end = end or hend
    if end is None and still_open:
        end = OPEN_END
    return start, end


def _overlap(wa, wb):
    """-> True / False / None. None means the record cannot say, which is not False."""
    (sa, ea), (sb, eb) = wa, wb
    if sa is None or sb is None or ea is None or eb is None:
        return None
    return sa < eb and sb < ea


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
        "provenance": a.provenance,
        "premise": a.premise,
        "premise_share": None,
        "criterion": a.criterion,
        "guardrail": a.guardrail or [],
        "parent": a.parent,
        "depends_on": [_parse_dep(d) for d in a.depends_on or []],
        "oracle_ceiling": None,
        "kill_condition": a.kill_condition,
        "tier": None,
        "axes": {"V": None, "P": None, "U": None, "code_availability": None},
        "run_id": None,
        "result": None,
        "verdict": None,
        "conditional_on": [],
        "killed_by": None,
        "revive_if": None,
        "history": [{"at": now_utc(), "to": "draft", "note": "added",
                     "provenance": a.provenance}],
    }
    graph.setdefault("nodes", []).append(node)
    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    emit({"id": nid, "state": "draft", "provenance": a.provenance,
          "missing": _missing_fields(node),
          "note": "a draft cannot be opened as an arm. Complete it with `set`, "
                  "then `ready` will include it."})


def cmd_set(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    node = _node(graph, a.id)
    declared_prev = node["state"]
    if node["state"] in SETTLED:
        refuse(f"card {a.id} is {node['state']} -- settled cards are not edited",
               fix="a conclusion that changed is a NEW card citing this one; "
                   "rewriting a settled card destroys what the next round needs")
    for kv in a.set or []:
        if "=" not in kv:
            broke(f"--set wants field=value, got {kv!r}")
        k, v = kv.split("=", 1)
        if k == "provenance" and v not in PROVENANCE:
            broke(f"provenance must be one of {PROVENANCE}")
        try:
            node[k] = json.loads(v)
        except json.JSONDecodeError:
            node[k] = v
        if k == "conditional_on":
            # Defensive, and honestly so: a card carrying `conditional_on` is settled by
            # construction (only `close` writes it), so the SETTLED guard above already
            # refuses first. This catches the hand-edited graph -- the case this file
            # keeps a `state_drift` report for -- and it keeps `reread` the only spelling
            # anywhere, so the field cannot come to have two authors.
            broke("conditional_on is not edited directly -- `graph.py reread --id "
                  f"{a.id} --condition <ID> --note '<what re-reading showed>'`. The "
                  "field exists because nothing clears it on its own, and clearing it "
                  "silently is the one move that makes that pointless")
        if k == "depends_on":
            # Refuse an unparseable edge here rather than let `check` report it
            # later: an edge whose kind nobody can read gates like `launch`, so
            # the mistake shows up as a card that will not move.
            for i, b in _dep_edges(node):
                if b is None:
                    broke(f"depends_on entry {i!r}: `blocks` must be one of "
                          f"{list(DEP_BLOCKS)}. A bare id means `launch`.")
        if k == "tree":
            for msg in _tree_shape(node.get("tree")):
                broke(msg)
            # ‼️ A branch name is as unrecyclable as a queue number, and for the
            # same reason: it is what a settled card's evidence resolves THROUGH.
            # Pointing a second arm at an existing branch moves the ref, and the
            # first card's `head` stops naming anything -- silently, because a
            # card whose branch was reused and a card whose branch was never
            # written read identically. `run-card.md` rule 4 is the same fact
            # from the other side: a patch depends on that commit still existing,
            # so after a force-push or a deleted branch `checkout` fails where a
            # tarball would not.
            want = (node.get("tree") or {}).get("branch")
            for other in graph.get("nodes", []):
                if other["id"] == node["id"]:
                    continue
                if ((other.get("tree") or {}).get("branch")) == want:
                    broke(f"branch {want!r} already belongs to card {other['id']} -- "
                          f"two arms on one branch cannot be told apart, and moving "
                          f"the ref destroys whichever of them settles first. Give "
                          f"this arm its own")
    missing = _missing_fields(node)
    corpus = graph.get("corpus") or {}
    scope = _share_scope(node, corpus)
    if scope:
        missing.append("premise_share (" + scope + ")")
    # ‼️ Captured BEFORE the set loop, and it used to be captured after. A
    # `--set state=running` writes the label itself, so reading `prev` afterwards
    # compared `running` against `running` and appended nothing -- which left the
    # card recording when it became `ready` and when it was `filled`, and NOT when
    # its arm opened. Invariant 17 needs exactly that instant (two arms in one tree
    # is a question about overlap), and more generally a state somebody DECLARED is
    # the one a history is for; a derived one can be recomputed from the card at
    # any time.
    prev = declared_prev
    # Write the DERIVED state forward, all three of them. This used to stop at
    # draft -> blocked, which is why `ready` was in the vocabulary and unreachable:
    # a complete card with no dependencies sat in `blocked` -- contradicting that
    # word's own definition ("card complete, dependencies unmet") -- until somebody
    # hand-set it. ‼️ It is written for external readers, not read back: blocked ->
    # ready happens when ANOTHER card settles, and no `set` runs on this one then.
    # That is why every decision below goes through `_derive_state` instead.
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    derived, _why = _derive_state(node, by_id, corpus)
    if derived != prev:
        node["state"] = derived
        node["history"].append({"at": now_utc(), "to": derived,
                                "note": "derived: schema + dependencies"})
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

    # `blocked` here means NOT TAKEABLE and so holds derived `draft` as well as
    # derived `blocked` -- it always did, and each entry now says which, because
    # "finish the card" and "wait for N02" are different instructions.
    ready, blocked, states = [], [], {}
    for n in nodes:
        st, why = _derive_state(n, by_id, corpus)
        states[n["id"]] = st
        if st in DECLARED_STATES:
            continue
        if st == "ready":
            ready.append({"id": n["id"], "title": n["title"], "kind": n["kind"],
                          "tier": n.get("tier"),
                          "offline": n.get("kind") == "measurement"})
        else:
            blocked.append(dict({"id": n["id"], "state": st}, **why))

    out = {"ready": ready, "blocked": blocked,
           "running": [i for i, st in states.items() if st == "running"],
           "filled": [i for i, st in states.items() if st == "filled"]}

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
        # ‼️ `launch` edges only. A pair of cards each `reading` the other is
        # legitimate -- "run both, adjudicate together" has exactly that shape --
        # and walking every edge would report the healthiest use of the new kind
        # as a deadlock.
        for d in [i for i, b in _dep_edges(by_id[nid]) if b != "reading"]:
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
        if nid in _dep_ids(n) or n.get("parent") == nid \
                or nid in (n.get("conditional_on") or []):
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

    # ‼️ A `reading` dependency does not refuse the verdict -- it stamps it. The
    # alternative was to block `close`, which just moves the stall one state to
    # the right: a pile of 🟪 nobody may adjudicate is the same waiting, minus the
    # GPU hours. What must not happen is the verdict travelling WITHOUT its
    # condition, and that is what this field is: the card says out loud that its
    # reading rests on something still open.
    by_id = {n["id"]: n for n in graph["nodes"]}
    node["conditional_on"] = _unsettled(node, by_id, "reading")

    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    out = {"id": a.id, "state": node["state"],
           "unblocked": [n["id"] for n in graph["nodes"]
                         if a.id in _dep_ids(n, "launch")
                         and n["state"] not in SETTLED],
           # Verdicts already written that were standing on THIS card. `fill` says
           # what a result may have voided; this says what a VERDICT may have.
           "re_read": [n["id"] for n in graph["nodes"]
                       if a.id in (n.get("conditional_on") or [])]}
    if node["conditional_on"]:
        out["conditional_on"] = node["conditional_on"]
        out["note"] = ("this verdict is conditional on %s, which is still open. It "
                       "may not be quoted without them: if they land badly the "
                       "attribution here is void, not merely weaker."
                       % ", ".join(node["conditional_on"]))
    if out["re_read"]:
        out["note"] = (out.get("note", "") + " ").lstrip()
        out["note"] += ("cards %s were closed conditional on this one -- re-read "
                        "those verdicts now and clear their `conditional_on`."
                        % ", ".join(out["re_read"]))
    emit(out)


# ---------------------------------------------------------------- REREAD

def cmd_reread(a):
    """Retire one condition from a verdict, on the record.

    `close` stamps `conditional_on` when a verdict lands ahead of a `reading` upstream,
    and NOTHING clears it automatically -- that is the whole design. Which left it with no
    way to be cleared AT ALL: `set` refuses every settled card, and a card carrying this
    field is settled by construction. So `check`'s `condition_resolved_unreviewed` would
    have gone on reporting `major` for the rest of the round, on a verdict somebody had
    already re-read and had no way to say so about.

    ‼️ **A permanently red check is how a checker becomes the thing people route around**
    -- §3.5 says exactly this about disputes and then reports them as `major` for that
    reason. A finding nobody can clear teaches the same lesson faster.

    The `--note` is the part the next round can actually check.

    Deliberately narrow: it retires a condition, it does not revise a verdict. If
    re-reading changes the answer, that is `dispute` -- the losing card KEEPS its verdict
    and gains `superseded_by`, because a conclusion that was overturned and one that never
    existed are different information (§3.5).
    """
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    node = _node(graph, a.id)
    cond = node.get("conditional_on") or []
    if a.condition not in cond:
        refuse(f"card {a.id} is not conditional on {a.condition}",
               fix=f"its open conditions are {cond or 'none'}")
    by_id = {n["id"]: n for n in graph["nodes"]}
    up = by_id.get(a.condition)
    if not up or up.get("state") not in SETTLED:
        refuse(f"{a.condition} is {up.get('state') if up else 'not in this graph'}, "
               f"not settled",
               fix="a condition cannot be re-read against something still open. Leave it "
                   "standing -- `check` reports it as minor, which is the correct state")

    node["conditional_on"] = [c for c in cond if c != a.condition]
    node["history"].append({"at": now_utc(), "to": node.get("state"),
                            "reread": a.condition, "note": a.note,
                            "condition_verdict": up.get("verdict") or up.get("killed_by")})
    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    emit({"id": a.id, "retired": a.condition,
          "still_conditional_on": node["conditional_on"] or None,
          "note": "verdict unchanged. If re-reading changed the answer, that is "
                  "`dispute` -- an overturned conclusion and one that never existed are "
                  "different information, and this verb cannot tell them apart"})


# ---------------------------------------------------------------- DISPUTE

def cmd_dispute(a):
    """Record that two cards disagree -- without touching either one.

    ‼️ This verb exists so that "these two contradict each other" stops being a
    state the graph cannot hold. Before it, a result contradicting a settled
    verdict had two outcomes and both were wrong: overwrite the old card (and
    lose what the next round reads) or stay silent.
    """
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    chal, disp = _node(graph, a.id), _node(graph, a.against)
    if a.id == a.against:
        broke("a card cannot dispute itself")

    ct, dt = chal.get("tier"), disp.get("tier")
    if ct in TIER_POWER and dt in TIER_POWER and TIER_POWER[ct] < TIER_POWER[dt]:
        refuse(f"{a.id} is {ct} and {a.against} is {dt} -- a cheaper check cannot "
               f"refute a dearer one",
               fix="SKILL.md Stage 3.5 rule 2: a cheap check gives you a reason to "
                   "CONTINUE, never a reason to reject. Raise the challenger's tier "
                   "and re-measure, or record this as an observation on the card. "
                   "‼️ If the challenger is T4, note that an approximation failing "
                   "never refutes the original.")

    did = _next_dispute_id(graph)
    graph.setdefault("disputes", []).append({
        "id": did, "state": "open", "challenger": a.id, "disputed": a.against,
        "detail": a.detail, "opened_at": now_utc(),
        "tiers": {a.id: ct, a.against: dt},
        "outcome": None, "note": None,
    })
    # Both sides are MARKED, neither is edited. A reader arriving at either card
    # has to see that it is contested -- that is the entire mechanism.
    for n in (chal, disp):
        n.setdefault("disputed_by", [])
        if did not in n["disputed_by"]:
            n["disputed_by"].append(did)
    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    emit({"dispute": did, "state": "open", "marked": [a.id, a.against],
          "note": "‼️ Neither card was changed, and neither verdict was reverted. "
                  "Adjudication is yours: `graph.py resolve`. Until then both read "
                  "as contested, which is the true state."})


def cmd_resolve(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    d = next((x for x in graph.get("disputes", []) if x.get("id") == a.id), None)
    if d is None:
        refuse(f"no dispute {a.id}",
               known=[x.get("id") for x in graph.get("disputes", [])])
    if d["state"] != "open":
        refuse(f"{a.id} is already {d['state']} ({d.get('outcome')})",
               fix="a resolved dispute stays resolved. If it reopens, that is a NEW "
                   "dispute citing this one")
    d.update(state="resolved", outcome=a.outcome, note=a.note, resolved_at=now_utc())

    if a.outcome == "upheld":
        # The disputed card keeps its verdict AND gains a pointer forward. The
        # append-only rule holds: history is what the next round reads, so a
        # superseded conclusion is marked, never rewritten.
        loser = _node(graph, d["disputed"])
        loser["superseded_by"] = d["challenger"]
        loser["history"].append({"at": now_utc(), "to": loser["state"],
                                 "note": f"superseded by {d['challenger']} via {a.id}"})
    graph["updated_at"] = now_utc()
    atomic_write_json(p["graph"], graph)
    emit({"dispute": a.id, "outcome": a.outcome,
          "superseded": d["disputed"] if a.outcome == "upheld" else None,
          "meaning": DISPUTE_OUTCOMES[a.outcome]})


# ---------------------------------------------------------------- CHECK

def cmd_check(a):
    """Every invariant this record can be held to. Reports; repairs nothing.

    ‼️ No count in this sentence, deliberately. It said "the seven invariants,
    plus the two MLClaw adds" while `flag()` emitted twenty-one distinct names --
    a number with two authors, drifting exactly the way `/agent-refactor` calls
    a double protocol. The list is the numbered blocks below and the human-facing table in
    `references/experiment-graph.md`; whichever of those you change, change both.

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
        # Invariants about an ARM (2, 4) read the derived state -- a card with a
        # `run_id` and no result has one open whatever its label says, and those
        # are exactly the cards a missed `state=running` used to hide. Invariants
        # about the RECORD (1, 3, 5, 8) keep reading the stored label on purpose:
        # 5 exists to catch a hand-written `ready` on an incomplete card, and
        # derived `ready` can never be incomplete by construction.
        dst, _why = _derive_state(n, by_id, corpus)

        # 1. every settled card has a run id or a measurement source
        if st in SETTLED and not n.get("run_id") and n.get("tier") != "T0":
            flag("critical", "settled_without_source", nid,
                 "a conclusion with no run behind it -- nobody can re-check it")

        # 2. a running card's LAUNCH dependencies are all settled
        if dst in ("running", "filled"):
            unmet = _unsettled(n, by_id, "launch")
            if unmet:
                flag("critical", "gate_bypassed", nid,
                     f"opened with unsettled launch dependencies {unmet}")

        # 2b. an edge whose kind nobody can read. It gates like `launch`, so the
        # symptom is a card that never becomes takeable and no stated reason.
        for i, b in _dep_edges(n):
            if b is None:
                flag("major", "malformed_dependency", nid,
                     f"depends_on entry {i!r} has no readable `blocks` -- it is "
                     f"gating like `launch` by default, which may not be what was "
                     f"meant. One of {list(DEP_BLOCKS)}, or a bare id for launch")

        # 2c. a verdict standing on something still open. NOT a defect: it is
        # what `reading` is for, and the whole point is that the arm ran instead
        # of waiting. It is reported so the condition cannot be forgotten -- a
        # conditional verdict quoted as a plain one is this repo's oldest failure
        # (CLAUDE.md -> "Never silently", the re-reading rule).
        cond = n.get("conditional_on") or []
        if st in SETTLED and cond:
            landed = [c for c in cond if c in by_id and by_id[c]["state"] in SETTLED]
            still = [c for c in cond if c not in landed]
            if landed:
                flag("major", "condition_resolved_unreviewed", nid,
                     f"closed conditional on {landed}, which has since settled. "
                     f"Re-read this verdict against it and clear `conditional_on` "
                     f"-- nothing does that on its own")
            if still:
                flag("minor", "verdict_is_conditional", nid,
                     f"verdict holds only if {still} lands as assumed. Quote it "
                     f"with that clause or not at all")

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

    # 10. MLClaw/ARA add -- who asked for this. A card with no provenance cannot be
    # read as either a user's request or the agent's idea, and the queue's whole
    # premise is that those differ.
    for n in nodes:
        if n.get("provenance") not in PROVENANCE:
            flag("major", "untagged_provenance", n["id"],
                 "no provenance -- a card the user demanded and one the agent invented "
                 "read identically, and this queue is the user's")
    human = [n for n in nodes if n.get("provenance") in ("user", "user-revised")]
    if nodes and not human:
        flag("minor", "no_human_proposal", None,
             f"all {len(nodes)} cards are agent-originated. Not wrong, but the queue is "
             f"meant to be the user's -- worth asking what they would put on it")

    # 12. MLClaw/ARA add -- an open dispute is a state the reader must see.
    #     Major, not critical: a contested pair elsewhere in the graph must not
    #     stop unrelated arms, or `check` becomes the thing people route around.
    #     It goes critical only where something is BUILT on the contested card.
    open_disp = [d for d in graph.get("disputes", []) if d.get("state") == "open"]
    for d in open_disp:
        flag("major", "open_dispute", d.get("disputed"),
             f"{d.get('challenger')} contradicts it ({d.get('id')}): {d.get('detail')}. "
             f"Neither was reverted -- adjudicate with `resolve`")
        for n in nodes:
            if d.get("disputed") in _dep_ids(n) and n["state"] not in SETTLED:
                flag("critical", "built_on_contested", n["id"],
                     f"depends on {d.get('disputed')}, which is under open dispute "
                     f"{d.get('id')} -- this arm would stand on contested ground")

    # 13. MLClaw/ARA add -- the card <-> run binding resolves BOTH ways.
    cfg = read_json(p["config"], required=False) or {}
    target = cfg.get("target_stage") or "training"
    for n in nodes:
        if not n.get("run_id"):
            continue
        run, st = _run_json(a.project, target, n["run_id"])
        for sev, detail in _binding(n, run, st):
            flag(sev, "binding_unresolved", n["id"], detail)
        parent = by_id.get(n.get("parent"))
        prun = (_run_json(a.project, target, parent["run_id"])[0]
                if parent and parent.get("run_id") else None)
        for sev, detail in _delta(n, run, prun):
            flag(sev, "delta_not_as_declared", n["id"], detail)

    # 15. When the technique changes what the correct measurement setting is.
    for n in nodes:
        for sev, detail in _eval_setting(n):
            flag(sev, "one_number_two_settings", n["id"], detail)

    # 4. no two running cards share (parent, delta). Derived, and that is the whole
    # point of the invariant: keyed on `state == "running"` it could not see the
    # one arm shape that actually duplicates work -- a card whose `run_id` was set
    # while its label was left behind. The label is what a forgetful caller drops;
    # the `run_id` is what an opened arm cannot be without.
    seen = {}
    for n in nodes:
        if _derive_state(n, by_id, corpus)[0] != "running":
            continue
        key = (n.get("parent"), json.dumps(n.get("delta"), sort_keys=True))
        if key in seen:
            flag("major", "duplicate_arm", n["id"],
                 f"same parent and delta as {seen[key]} -- duplicated work, or one was forgotten")
        seen[key] = n["id"]

    # 11. MLClaw/ARA add -- every transcribed number carries the line it came from.
    for c in state.get("constants", []):
        for sev, detail in _grounding(f"constant {c.get('name')!r}", c):
            flag(sev, "ungrounded_number", None, detail)
    for sev, detail in _grounding("noise floor", baseline):
        flag(sev, "ungrounded_number", None, detail)
    findings_rec = read_json(os.path.join(os.path.dirname(p["state"]), "findings.json"),
                             required=False) or {}
    for e in findings_rec.get("entries", []):
        for sev, detail in _grounding(f"finding {e.get('id')!r}", e.get("measure") or {}):
            flag(sev, "ungrounded_number", None, detail)

    # 7. cited constants still hold
    declared = corpus.get("declared_at")
    for c in state.get("constants", []):
        if declared and c.get("measured_at") and c["measured_at"] < declared:
            flag("major", "stale_constant", None,
                 f"constant {c.get('name')!r} measured {c.get('measured_at')}, "
                 f"before this corpus was declared {declared} -- state.json's own "
                 f"header voids it")

    # 16. MLClaw add -- the floor itself, and it had no reader but `_grounding`.
    # Stages: the floor's runs are `/eval-run`'s per `baseline.json -> _comment_runs`,
    # but a project may measure it in its target stage, so both are searched and
    # "absent" means neither had it.
    stages = ["evaluation"] + ([target] if target != "evaluation" else [])
    floor_findings, floor_status = _floor(baseline, corpus, a.project, stages)
    for sev, detail in floor_findings:
        flag(sev, "noise_floor_unusable", None, detail)

    # The floor gates the WORDING of every result, not any arm.
    # ‼️ Keyed on the STATUS, not on `value is not None`. A retired floor and an
    # absent one gate identically because they ARE the same fact -- reading a
    # present-but-void number as a measured floor is how the 0.06 that was really
    # 0.25 got quoted, and `retires_on` says so in the record itself.
    if floor_status == "not_measured":
        hard = [n["id"] for n in nodes
                if n.get("tier") in ("T2", "T3") and n.get("result") is not None]
        if hard:
            why = ("no measured floor" if baseline.get("value") is None
                   else "a floor that does not hold here (see `noise_floor_unusable`)")
            flag("critical", "hard_result_without_noise_floor", None,
                 f"{hard} report at T2/T3 with {why}. Without one, "
                 f"'no significant improvement' is UNDECIDABLE, not negative -- "
                 f"those results are [T1 trend] at best")
    elif floor_status in ("claim", "unverifiable"):
        # A floor this pipeline did not measure on ITSELF still gates T2 -- that
        # is the whole worth of the external door, and refusing it there would
        # make the door open onto the same wall. It cannot gate T3: that is the
        # last check before a full run, the one tier whose row in SKILL.md makes
        # blind human review mandatory, and promoting a number across that line
        # is exactly what the ladder exists to stop.
        t3 = [n["id"] for n in nodes
              if n.get("tier") == "T3" and n.get("result") is not None]
        if t3:
            flag("critical", "t3_on_an_unverified_floor", None,
                 f"{t3} report at T3 against a floor that is `{floor_status}` -- nothing "
                 f"in this project confirms the two measurements behind it differed only "
                 f"in the seed. Report these at T2, or measure the floor here")
        if floor_status == "claim":
            flag("minor", "noise_floor_is_a_claim", None,
                 "the floor is declared `external`: legitimate, grounded by its "
                 "`sources`, and worth what a claim is worth. `unchecked` says what "
                 "nobody here could confirm -- every result it gates carries that")

    # 17. MLClaw add -- the tree each arm was written in, and whether two arms
    #     were ever written in the same one at the same time. The rationale is on
    #     `_tree_scope` / `_running_window`; the short form is that this is the one
    #     axis invariants 8, 13 and 15 are structurally blind to, and a tree two
    #     arms shared satisfies every one of them while making all three wrong.
    base = graph.get("base") or {}
    code_arms = [n for n in nodes if n.get("kind") in CODE_KINDS and n.get("run_id")]
    if code_arms and not base.get("commit"):
        flag("major", "round_base_undeclared", None,
             f"{len(code_arms)} arm(s) here write code and `base.commit` is null. "
             f"Nothing says what any of them branched FROM, so `arm_base_drift` "
             f"cannot be evaluated at all -- the same shape as a `premise_share` "
             f"with no `measured_on`, and it is absence, not a pass")
    windows = {}
    for n in code_arms:
        nid = n["id"]
        dst = _derive_state(n, by_id, corpus)[0]
        arm_run, arm_st = _run_json(a.project, target, n["run_id"])
        windows[nid] = _running_window(n, arm_run, arm_st, dst == "running")
        tree = n.get("tree")
        if not isinstance(tree, dict) or not tree.get("branch"):
            flag("major", "arm_tree_unrecorded", nid,
                 "an arm that writes code, with no `tree`. Its code identity exists "
                 "only in the run's snapshot -- which was read off whatever the shared "
                 "`code_dir` happened to hold at launch. That is a record of a moment, "
                 "not a record of an arm")
            continue
        sc = _tree_scope(n, base)
        if sc:
            flag("critical", "arm_base_drift", nid, sc)
        if arm_st != "ok":
            continue
        code = arm_run.get("code") or {}
        rb, rh = code.get("branch"), code.get("origin_commit")
        # ‼️ Both ends, exactly as invariant 13 does it -- and here the two ends
        # have DIFFERENT authors, which is the point. The card is written from
        # intent, before the arm opens. The snapshot is read off a disk, at
        # launch. When they disagree, the disk is right and the card is a
        # description of a run that did not happen.
        if rb and tree.get("branch") and rb != tree["branch"]:
            flag("critical", "arm_tree_disagrees_with_run", nid,
                 f"the card says this arm was written on {tree['branch']!r}; its run "
                 f"snapshot was taken on {rb!r}. One of these two records is about a "
                 f"different piece of code, and only one of them was read off a disk")
        if rh and tree.get("head") and rh != tree["head"]:
            flag("critical", "arm_tree_disagrees_with_run", nid,
                 f"the card's head {tree['head'][:12]} is not the snapshot's "
                 f"{rh[:12]}. Believe the tree and fix the card")
    # Two arms, one tree. ‼️ Severity does NOT reward silence: a pair that names
    # one shared branch and a pair that names none are both critical once the
    # overlap is proven. The floor's four states were forced by exactly the
    # opposite arrangement -- writing the truth cost two criticals while inventing
    # a reference cost one major -- and a record layer that pays a bonus to the
    # least honest route gets what it pays for. What clears this finding is two
    # DISTINCT branches, which is also the only thing that fixes the underlying
    # problem.
    for i, n in enumerate(code_arms):
        for m in code_arms[i + 1:]:
            tn = (n.get("tree") or {}).get("branch")
            tm = (m.get("tree") or {}).get("branch")
            if tn and tm and tn != tm:
                continue
            ov = _overlap(windows[n["id"]], windows[m["id"]])
            if ov is False:
                continue
            named = (f"one branch ({tn})" if tn or tm else
                     "no branch either of them names -- and MLClaw resolves ONE "
                     "`code_dir` per stage, so unless something moved them they are "
                     "the same directory")
            if ov is None:
                flag("major", "concurrent_arms_one_tree", n["id"],
                     f"{n['id']} and {m['id']} sit on {named}, and `history` does not "
                     f"record when either arm was open. Whether they overlapped is "
                     f"UNKNOWN, which is a third answer and not a no")
                continue
            dirty = 0
            for c in (n, m):
                r, st_ = _run_json(a.project, target, c["run_id"])
                if st_ == "ok":
                    dirty = max(dirty, (r.get("code") or {}).get("dirty_files_count") or 0)
            if dirty:
                flag("critical", "concurrent_arms_one_tree", n["id"],
                     f"{n['id']} and {m['id']} were open AT THE SAME TIME on {named}, "
                     f"and a snapshot was taken over {dirty} uncommitted file(s). "
                     f"Nothing can show that patch holds only its own arm's edits -- "
                     f"and it applies cleanly and reproduces exactly either way, so "
                     f"the failure leaves no trace anywhere else in this record")
            else:
                flag("critical", "concurrent_arms_one_tree", n["id"],
                     f"{n['id']} and {m['id']} were open AT THE SAME TIME on {named}. "
                     f"Both snapshots are clean commits, so WHAT RAN is pinned -- but "
                     f"in one tree the later commit contains the earlier arm's "
                     f"technique, and no field here says which one that is. A "
                     f"two-technique arm reported as a single-key delta")

    order = {"critical": 0, "major": 1, "minor": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    # ---- a settled round with nothing to hand anybody ---------------------
    #
    # ‼️ The graph is a MACHINE record. `verdict: won`, `killed_by:
    # faithful_but_inert`, a card id -- none of it is what somebody reads six
    # weeks later, and none of it survives being handed over. That is what
    # `/ara` builds, and until this check existed nothing anywhere noticed a
    # round that closed without one.
    #
    # Staleness matters as much as absence: an artifact built before the last
    # arm settled describes a different round. Dated artifacts are compared by
    # `built_at` against the newest settlement, both of which are already
    # recorded, so this is a records-only read.
    settled_at = None
    for n in nodes:
        if n.get("state") not in SETTLED:
            continue
        for h in n.get("history") or []:
            if h.get("to") in SETTLED and h.get("at"):
                if settled_at is None or h["at"] > settled_at:
                    settled_at = h["at"]
    # ‼️ Only once nothing is left open. `SETTLED` is per-CARD, so keying the nag
    # on "some card settled" fired the moment the FIRST arm landed with four still
    # running -- a handover item raised against a round that is mid-flight, which
    # is the shape of gate people learn to skip. The comment above says "a round
    # that CLOSED"; this is that sentence, computed.
    open_cards = [n["id"] for n in nodes
                  if _derive_state(n, by_id, corpus)[0] not in SETTLED]
    if settled_at:
        adir = os.path.join(a.project, "ara")
        built = None
        for d in sorted(os.listdir(adir)) if os.path.isdir(adir) else []:
            rec = read_json(os.path.join(adir, d, "ara.json"), required=False) or {}
            if rec.get("built_at") and (built is None or rec["built_at"] > built):
                built = rec["built_at"]
        if built is None:
            if not open_cards:
                flag("major", "artifact", "-",
                     "this round has settled cards and no artifact. The graph is a "
                     "machine record -- `verdict: won` is not what anybody reads six "
                     "weeks later, and it does not survive a handover. `/ara build` "
                     "after `/conclude`")
        elif built < settled_at:
            # Staleness is worth saying mid-round too: an artifact that reads as
            # current while a card has settled past it is wrong NOW, not at close.
            # It is a note until the round closes, and a handover item after.
            flag("major" if not open_cards else "minor", "artifact", "-",
                 f"the newest artifact was built at {built} and a card settled at "
                 f"{settled_at} -- it describes a different round. `/ara build` "
                 f"again; the old one stays, because it is the record of what was "
                 f"believed then")

    payload = {"cards": len(nodes), "findings": findings,
               "counts": {s: sum(1 for f in findings if f["severity"] == s)
                          for s in ("critical", "major", "minor")}}
    if any(f["severity"] == "critical" for f in findings):
        refuse("the graph has critical findings -- do not open another arm", **payload)
    emit(payload)


# ------------------------------------------------------------------- NEW

def _since(spec):
    """`3d` / `12h` / an ISO timestamp -> ISO string. Default 24h."""
    from datetime import datetime, timedelta, timezone
    spec = (spec or "24h").strip()
    if spec[-1:] in ("h", "d") and spec[:-1].isdigit():
        n = int(spec[:-1]) * (24 if spec[-1] == "d" else 1)
        return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat(timespec="seconds")
    return spec


def cmd_new(a):
    """What became a CONCLUSION since a point in time -- and what only became a number.

    This verb exists for one question, asked roughly fifteen times across the six
    days of the round this skill came from: "so what new conclusions are there?". It is the
    moment that most tempts the 🟪/✅ collapse. An arm has finished, its numbers
    are on the card, and the natural sentence is "e13h came back at 92.15" -- which
    reports a RESULT as a CONCLUSION. Often the verdict is genuinely not reachable
    yet, because it waits on another arm.

    So the answer is deliberately in two lists, and the second one is labelled.
    """
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    since = _since(a.since)
    nodes = graph.get("nodes", [])

    def landed(n, states):
        for h in reversed(n.get("history") or []):
            if h.get("to") in states and h.get("at", "") >= since:
                return h
        return None

    conclusions, numbers = [], []
    for n in nodes:
        h = landed(n, ("closed", "killed"))
        if h and n["state"] in SETTLED:
            conclusions.append({
                "id": n["id"], "title": n.get("title"), "at": h.get("at"),
                "verdict": n.get("verdict"), "killed_by": n.get("killed_by"),
                "revive_if": n.get("revive_if"), "tier": n.get("tier"),
                "superseded_by": n.get("superseded_by"),
            })
        elif n["state"] == "filled":
            f = landed(n, ("filled",))
            numbers.append({"id": n["id"], "title": n.get("title"),
                            "tier": n.get("tier"), "result": n.get("result"),
                            "filled_at": (f or {}).get("at")
                                         or ((n.get("history") or [{}])[-1].get("at"))})

    disputes = graph.get("disputes", [])
    out = {
        "since": since,
        "conclusions": sorted(conclusions, key=lambda c: c.get("at") or ""),
        "results_without_a_verdict": sorted(numbers, key=lambda c: c.get("filled_at") or ""),
        "disputes_opened": [d for d in disputes if (d.get("opened_at") or "") >= since],
        "disputes_resolved": [d for d in disputes if (d.get("resolved_at") or "") >= since],
        "still_running": [n["id"] for n in nodes if n["state"] == "running"],
    }
    if numbers:
        out["‼️"] = (f"{len(numbers)} arm(s) have NUMBERS and no verdict. Those are not "
                     f"conclusions -- a card's meaning often waits on another card, which "
                     f"is why `filled` and `closed` are different states. Report them as "
                     f"measurements, and say what each is still waiting on.")
    if not conclusions and not numbers:
        out["note"] = ("nothing settled and nothing landed in this window. That is a real "
                       "answer -- do not go looking for something to report.")
    emit(out)


# ---------------------------------------------------------------- STATUS

def cmd_status(a):
    p = _paths(a.project, a.session)
    graph = _load(p["graph"], "graph")
    baseline = read_json(p["baseline"], required=False) or {}
    cfg = read_json(p["config"], required=False) or {}
    nodes = graph.get("nodes", [])
    # ‼️ Derived, because this is the one-screen summary a PERSON reads, and read
    # off the stored label it used to report `blocked: 3` about three cards that
    # `ready` was simultaneously handing out as takeable. For an agent arriving
    # with no context that is not a cosmetic disagreement -- it reads as a stalled
    # queue and the round stops.
    by_id = {n["id"]: n for n in nodes}
    corpus = graph.get("corpus") or {}
    derived = {n["id"]: _derive_state(n, by_id, corpus)[0] for n in nodes}
    counts = {s: sum(1 for st in derived.values() if st == s) for s in STATES}
    # A stored label behind its derivation is NORMAL, not a defect: blocked ->
    # ready fires when another card settles and nothing runs `set` on this one.
    # Reported rather than repaired, so a hand-edited graph is visible too.
    drift = [{"id": n["id"], "stored": n.get("state"), "derived": derived[n["id"]]}
             for n in nodes if n.get("state") != derived[n["id"]]]
    _target = cfg.get("target_stage") or "training"
    _, _floor_state = _floor(baseline, corpus, a.project,
                             ["evaluation"] + ([_target] if _target != "evaluation" else []))
    emit({
        "corpus": graph.get("corpus"),
        # ‼️ Never a bare number here. This is the one-screen summary a PERSON
        # reads and the screen the sentence gets quoted off, and a floor that is
        # a `claim` printed as `0.25` is indistinguishable from one this project
        # measured -- which is the whole failure the four states exist to stop.
        # CLAUDE.md: the qualifier travels with the number, in every file and
        # every sentence, and this file is one of them.
        "noise_floor": {"value": baseline.get("value"),
                        "origin": baseline.get("origin") or "mlclaw",
                        "status": _floor_state,
                        "gates": FLOOR_GATES.get(_floor_state),
                        "unchecked": baseline.get("unchecked")},
        "counts": counts,
        "state_drift": drift,
        "killed": [{"id": n["id"], "killed_by": n.get("killed_by"),
                    "revive_if": n.get("revive_if")}
                   for n in nodes if n.get("state") == "killed"],
        # ‼️ Here rather than only in `check`, because this is the screen the
        # sentence gets quoted off. A verdict that ran ahead of what it rests on
        # is the right trade -- the arm ran instead of queueing -- and it stops
        # being the right trade the moment it is repeated without the clause.
        "conditional_verdicts": [
            {"id": n["id"], "verdict": n.get("verdict") or n.get("killed_by"),
             "conditional_on": n.get("conditional_on"),
             "resolved": [c for c in n.get("conditional_on") or []
                          if c in by_id and by_id[c].get("state") in SETTLED]}
            for n in nodes if n.get("conditional_on")],
        "provenance": {p: sum(1 for n in nodes if n.get("provenance") == p)
                       for p in PROVENANCE},
        "open_disputes": [{"id": d["id"], "challenger": d["challenger"],
                           "disputed": d["disputed"], "detail": d.get("detail")}
                          for d in graph.get("disputes", []) if d.get("state") == "open"],
        "awaiting_verdict": [i for i, st in derived.items() if st == "filled"],
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
    a.add_argument("--depends-on", action="append", dest="depends_on",
                   metavar="ID[:launch|:reading]",
                   help="`N06` blocks the launch (the default, and the four real "
                        "dependencies in SKILL.md are all this). `N06:reading` "
                        "does not -- the arm opens now and its verdict carries "
                        "`conditional_on`. Ask which one it is EVERY time: the "
                        "wrong answer here is a queue that serialises for no reason")
    a.add_argument("--kill-condition", dest="kill_condition")
    a.add_argument("--provenance", choices=PROVENANCE, default="ai-suggested",
                   help="who put this on the queue. Defaults to the conservative "
                        "`ai-suggested`; pass `user` when the user asked for it")
    a.set_defaults(fn=cmd_add)

    s = sub.add_parser("set", help="complete or amend a card")
    common(s)
    s.add_argument("--id", required=True)
    s.add_argument("--set", action="append", metavar="FIELD=VALUE")
    s.set_defaults(fn=cmd_set)

    rr = sub.add_parser("reread", help="retire one condition from a verdict, on the record")
    common(rr)
    rr.add_argument("--id", required=True)
    rr.add_argument("--condition", required=True, help="the settled upstream re-read against")
    rr.add_argument("--note", required=True,
                    help="what re-reading it against that upstream showed. Required for "
                         "the same reason `dispute --note` is: the verdict does not say "
                         "WHY, and why is the only part the next round can re-check")
    rr.set_defaults(fn=cmd_reread)

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

    d = sub.add_parser("dispute", help="two cards disagree; mark both, revert neither")
    common(d)
    d.add_argument("--id", required=True, help="the challenging card")
    d.add_argument("--against", required=True, help="the card it contradicts")
    d.add_argument("--detail", required=True)
    d.set_defaults(fn=cmd_dispute)

    rs = sub.add_parser("resolve", help="adjudicate one dispute")
    common(rs)
    rs.add_argument("--id", required=True)
    rs.add_argument("--outcome", required=True, choices=sorted(DISPUTE_OUTCOMES))
    rs.add_argument("--note", required=True,
                    help="WHY. The outcome alone does not say it, and why is the only "
                         "part the next round can re-check. Required here rather than "
                         "checked in the body: argparse is the enforcing layer, and a "
                         "second check behind it is unreachable code that reads as a "
                         "guard")
    rs.set_defaults(fn=cmd_resolve)

    nw = sub.add_parser("new", help="what became a CONCLUSION since a time -- and what "
                                    "only became a number")
    common(nw)
    nw.add_argument("--since", help="`3d` / `12h` / an ISO timestamp. Default 24h")
    nw.set_defaults(fn=cmd_new)

    t = sub.add_parser("status", help="one-screen summary")
    common(t)
    t.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
