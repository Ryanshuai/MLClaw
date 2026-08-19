#!/usr/bin/env python3
"""Value sets that more than one script has to agree on — one author each.

Admission rule, and it is narrow on purpose: a tuple belongs here when **two or
more scripts must hold the same set for the record to mean anything**, and
nowhere else. A vocabulary one script owns stays in that script. This is not a
constants drawer.

What it collapsed, and what each pair would have cost
-----------------------------------------------------
`protocols.py` reports these as VOCAB — the same fact written twice, where the
bad thing is neither end. Nothing raises when the two copies drift, because
each side is internally consistent; the reader simply stops recognising a value
the writer now emits, and reports a legal-looking wrong answer.

  * `TIERS` — `explore/graph.py` and `conclude/conclude.py`, byte-identical, and
    `conclude.py`'s own comment already conceded the duplication ("Same ladder
    as `graph.py`, same reason"). CLAUDE.md's rule is that *the tier travels
    with the number forever, in every file and every sentence*; a ladder with
    two authors is the one place that promise can quietly stop being true.

  * `PROVENANCE` — same two files, byte-identical. A card the user demanded and
    a card the agent invented mean different things, and `check` reports the
    distribution as a signal. A value `graph.py` writes that `conclude.py` does
    not know is silently not that signal any more.

  * `HANDOFF_TERMINAL` — `data-label/handoff.py` is the author (it writes these
    statuses) and `data/phase.py` restated them as `TERMINAL_HANDOFF`. This is
    the reader-restates-the-writer shape, and its failure is the expensive one
    on this line: add a terminal status to `handoff.py` and `phase.py` counts
    those handoffs as still open — forever, and the conversation-start check in
    CLAUDE.md then chases a batch that came back weeks ago.

`TIER_POWER` is deliberately NOT here. Both files have one and they DIFFER —
`graph.py` scores T4 as 0 because an approximation may not refute anything,
`conclude.py` omits T4 entirely. That is per-consumer policy over a shared
ladder, and hoisting it would erase a distinction that is doing work. Same for
`ask.py -> TERMINAL` and `repro.py -> TERMINAL_SESSION`: same shape of name,
different record, different values, one author apiece.

Import as `shared/_records.py` documents; alias at the import when a caller's
local name is already load-bearing, the way `graph.py` does with `digits`.

Stdlib only — in fact nothing is imported at all, which is the point: a
vocabulary that needs code to produce it is not a vocabulary.
"""

# --------------------------------------------------------------------------
# the experiment/belief ladder — `explore/graph.py`, `conclude/conclude.py`
# --------------------------------------------------------------------------

TIERS = ("T0", "T1", "T2", "T3", "T4")

PROVENANCE = ("user", "ai-suggested", "ai-executed", "user-revised")

# --------------------------------------------------------------------------
# the data line — `data-label/handoff.py` writes these, `data/phase.py` reads
# --------------------------------------------------------------------------

HANDOFF_TERMINAL = ("accepted", "rejected", "cancelled")

# --------------------------------------------------------------------------
# how long an OPEN EXCHANGE may go quiet before it is stale — the three
# thresholds CLAUDE.md "On Conversation Start" step 3 reports in one pass:
# `handoff.py status`, `ask.py status`, `adapt.py status`.
#
# One table with three entries, not one number: CLAUDE.md argues the split
# itself — *"stale at 7 days, not 14 — a question someone could answer in a
# minute going unanswered for a week is a worse signal than a labeling batch
# taking three weeks."* That sentence was the only place the ladder existed,
# and CLAUDE.md's own rule is that a copy in prose is the one no test executes.
# Each entry is separately movable on purpose; what has one author is the
# table.
#
# NOT here, and it is the distinction the whole entry turns on: `phase.py`'s
# census threshold. It reads 14.0 today too, and it is a different subject —
# how old a READING of the disks is, not how long somebody has been sitting on
# work. CLAUDE.md gives the census no threshold at all. Two values that are
# equal today and must stay free to diverge, which is why merging them on the
# strength of the number would have been the wrong fix.
# --------------------------------------------------------------------------

HANDOFF_STALE_DAYS = 14.0        # an artifact batch out with another party

ASK_STALE_DAYS = 7.0             # a question put to a person

ADAPTATION_STALE_DAYS = 7.0      # a data<->model feedback session left open.
                                 # Question-shaped rather than batch-shaped:
                                 # the other end is a colleague deciding, not a
                                 # team working through a queue.
