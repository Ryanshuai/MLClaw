#!/usr/bin/env python3
"""Ask a person something, and record the answer as the kind of thing it is.

`/data-label` exchanges ARTIFACTS: something goes out, a manifest is frozen, and
what comes back is reconciled against it. That rigor does not transfer to
"has 260731 been shot yet?" — there is no artifact and nothing to freeze. Those
are two different exchanges and pretending one is the other left `data_request`
as a handoff with an empty manifest.

What this records instead:

    who was asked · what · by when · what came back · AND WHAT KIND OF THING
    THAT ANSWER IS

The last one is the whole point, and it is why this is not a todo list. "The
operator says the shoot finished" and "the census counted 52 finished units" are
different facts. Downstream they read identically once someone writes "done" in
a notes field, and the first one has been wrong before. So an answer carries its
evidential status, `claim` is the default, and an answer that COULD have been
checked cannot be filed as `verified` without the check having run.

This is CLAUDE.md "Never record a metric you did not read", one domain over: a
human's word and a verified fact must not both become the same green cell.

**Nothing is sent.** The channel is the user's, and MLClaw does not message
people on their behalf. `channel: manual` means a human carries it. The adapter
seam is documented in the skill file so a real channel is one new file later,
the same promise `/lease` makes for providers — but no adapter exists today and
this script never pretends otherwise.

Exit codes per CLAUDE.md "Script Integration": 1 = worked, the answer is no;
2 = broke, do it by hand.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (age_days, atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402

# What is being asked for. Shapes what an answer even means, so it is a fixed
# vocabulary rather than free text.
KINDS = ("question", "request", "approval", "heads_up")

# How much weight the answer can carry. The ordering is deliberate: `claim` is
# the default because it is what an answer usually IS, and defaulting to
# anything stronger would launder hearsay into evidence.
ANSWER_KINDS = ("claim", "verified", "decision", "refused", "unknown")

# Only `verified` asserts that something other than a person confirmed it.
NEEDS_EVIDENCE = ("verified",)

TERMINAL = ("answered", "cancelled")
VERIFY_TIMEOUT_S = 120


def asks_dir(project):
    return os.path.join(os.path.expanduser(project), "asks")


def ask_path(project, ask_id):
    return os.path.join(asks_dir(project), f"{ask_id}.json")


def cmd_open(a):
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    if a.kind not in KINDS:
        broke(f"unknown kind {a.kind!r}", allowed=list(KINDS))

    os.makedirs(asks_dir(project), exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    for suffix in [""] + [f"_{i}" for i in range(2, 100)]:
        aid = f"ask_{stamp}{suffix}"
        if not os.path.exists(ask_path(project, aid)):
            break
    else:
        broke("could not allocate a unique ask_id")

    rec = {
        "ask_id": aid,
        "project": os.path.basename(project.rstrip("/")),
        "kind": a.kind,
        "status": "open",
        # The party key in resources.json -> outsourcing, or a plain name for a
        # one-off. The key only: contact details stay in the never-committed
        # file, because asks/ is git-tracked. Same split as /data-label.
        "to": a.to,
        "asked": a.asked,
        "why": a.why,
        "about": a.about,
        "channel": a.channel,
        "channel_ref": a.channel_ref,
        # A command that could answer this without a person. Its presence is
        # what makes `verified` provable — and its absence is what makes
        # `verified` refusable.
        "verify": a.verify,
        "due_at": a.due,
        "follow_up_of": a.follow_up,
        "opened_at": now_utc(),
        "answered_at": None,
        "answer": None,
    }
    atomic_write_json(ask_path(project, aid), rec)
    emit({"ask_id": aid, "to": a.to, "kind": a.kind, "due_at": a.due,
          "verify": a.verify,
          "channel": a.channel,
          "note": "nothing was sent — MLClaw does not message people on your behalf",
          "next": f"ask.py answer --project {a.project} --id {aid} --says '...' --as claim"})


def run_verify(cmd):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8",
                           timeout=VERIFY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, f"verify exceeded {VERIFY_TIMEOUT_S}s"
    except OSError as exc:
        return None, str(exc)
    return p.returncode, (p.stdout or p.stderr or "").strip()[:600]


def cmd_answer(a):
    project = os.path.expanduser(a.project)
    path = ask_path(project, a.id)
    rec = read_json(path)
    if rec.get("status") in TERMINAL:
        refuse(f"{a.id} is already {rec['status']}",
               answered_at=rec.get("answered_at"),
               hint="open a follow-up (--follow-up) rather than overwriting an "
                    "answer somebody acted on")
    if a.as_ not in ANSWER_KINDS:
        broke(f"unknown answer kind {a.as_!r}", allowed=list(ANSWER_KINDS))

    verified_by = None
    if a.as_ in NEEDS_EVIDENCE:
        # `verified` is the one status that asserts something other than a
        # person confirmed it. Letting it be typed freely turns the whole
        # distinction into a formality — the exact laundering this script
        # exists to stop.
        if not rec.get("verify") and not a.evidence:
            refuse("cannot file this as `verified`: nothing checked it",
                   why="`verified` means something other than the person's word "
                       "confirmed the answer. This ask declared no `verify` "
                       "command and no --evidence was given, so the only thing "
                       "supporting it is what they said",
                   fix="file it as `claim` (which is what it is), or pass "
                       "--evidence '<what checked it>', or re-open with --verify")
        if rec.get("verify") and not a.skip_verify:
            rc, out = run_verify(rec["verify"])
            if rc is None:
                refuse("the verify command could not run",
                       detail=out, verify=rec["verify"],
                       fix="fix it, or file as `claim`, or --skip-verify to "
                           "record that it was not run")
            if rc != 0:
                refuse("the verify command contradicted the answer",
                       verify=rec["verify"], exit_code=rc, output=out,
                       why="the person said one thing and the check said another; "
                           "recording `verified` here would file the wrong one")
            verified_by = {"verify": rec["verify"], "exit_code": rc,
                           "output": out, "ran_at": now_utc()}
        elif rec.get("verify") and a.skip_verify:
            # A check that did not run is not a check. Letting --skip-verify
            # through with kind=`verified` would leave the machine-readable
            # field saying "verified" while nothing verified anything — the
            # laundering this whole script exists to stop, committed via its
            # own escape hatch. `--evidence` is the only way past: something
            # OTHER than the person corroborated it.
            if not a.evidence:
                refuse("--skip-verify cannot produce a `verified` answer",
                       why="the declared check was not run, so nothing but the "
                           "person's word supports this. A check that did not "
                           "run is not a check",
                       verify=rec["verify"],
                       fix="drop --skip-verify to run it, file as `claim`, or "
                           "pass --evidence naming what corroborated it instead")
            verified_by = {"verify": rec["verify"], "ran": False,
                           "corroborated_by": a.evidence,
                           "note": "declared check NOT run; verified on --evidence instead"}

    rec["answer"] = {
        "says": a.says,
        # The evidential status. Everything downstream that reads this record
        # must branch on it rather than on the text of `says`.
        "kind": a.as_,
        "by": a.by or rec.get("to"),
        "evidence": a.evidence,
        "verified_by": verified_by,
        # An answer about the world has a shelf life. "this data is fine to use"
        # was true in July; nothing makes it true today.
        "valid_until": a.valid_until,
        "at": now_utc(),
    }
    rec["status"] = "answered"
    rec["answered_at"] = now_utc()
    atomic_write_json(path, rec)

    out = {"ask_id": a.id, "status": "answered", "kind": a.as_,
           "by": rec["answer"]["by"], "valid_until": a.valid_until}
    if a.as_ == "claim":
        out["note"] = ("recorded as a CLAIM — nothing verified it. Downstream "
                       "must not treat this as a checked fact")
    if verified_by and verified_by.get("ran") is False:
        out["warning"] = "verify was declared but skipped; this is not a passed check"
    emit(out)


def cmd_cancel(a):
    project = os.path.expanduser(a.project)
    path = ask_path(project, a.id)
    rec = read_json(path)
    if rec.get("status") in TERMINAL:
        refuse(f"{a.id} is already {rec['status']}")
    rec.update(status="cancelled", answered_at=now_utc(),
               answer={"says": a.reason or "cancelled", "kind": "unknown",
                       "by": None, "evidence": None, "verified_by": None,
                       "valid_until": None, "at": now_utc()})
    atomic_write_json(path, rec)
    emit({"ask_id": a.id, "status": "cancelled"})


def cmd_status(a):
    root = asks_dir(a.project)
    rows, errors = [], []
    if os.path.isdir(root):
        for f in sorted(os.listdir(root)):
            if not (f.startswith("ask_") and f.endswith(".json")):
                continue
            p = os.path.join(root, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    r = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                errors.append({"path": p, "error": str(exc)})
                continue
            open_ = r.get("status") == "open"
            if a.open_only and not open_:
                continue
            age = age_days(r.get("opened_at"))
            ans = r.get("answer") or {}
            expired = bool(ans.get("valid_until") and ans["valid_until"] < now_utc())
            rows.append({
                "ask_id": r.get("ask_id"), "kind": r.get("kind"),
                "status": r.get("status"), "to": r.get("to"),
                "asked": (r.get("asked") or "")[:90],
                "age_days": age,
                "due_at": r.get("due_at"),
                "overdue": bool(r.get("due_at") and open_ and r["due_at"] < now_utc()),
                "stale": bool(open_ and age is not None and age >= a.stale_days),
                "answer_kind": ans.get("kind"),
                # An expired answer is not a current fact, and the record is the
                # only place that can say so.
                "answer_expired": expired,
                "path": p,
            })
    emit({"asks": rows,
          "open": sum(1 for r in rows if r["status"] == "open"),
          "overdue": sum(1 for r in rows if r["overdue"]),
          "stale": sum(1 for r in rows if r["stale"]),
          "expired_answers": [r["ask_id"] for r in rows if r["answer_expired"]],
          "unverified_claims": [r["ask_id"] for r in rows
                                if r["answer_kind"] == "claim"],
          "stale_threshold_days": a.stale_days,
          "errors": errors})


def cmd_show(a):
    emit(read_json(ask_path(os.path.expanduser(a.project), a.id)))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open", help="record a question or request to a person")
    o.add_argument("--project", required=True)
    o.add_argument("--to", required=True,
                   help="key in resources.json -> outsourcing, or a plain name")
    o.add_argument("--asked", required=True, help="what you are asking, verbatim")
    o.add_argument("--kind", default="question", choices=KINDS)
    o.add_argument("--why", default=None, help="what is blocked on this")
    o.add_argument("--about", default=None, help="dataset / run / handoff it concerns")
    o.add_argument("--verify", default=None,
                   help="command that could answer this without a person; makes "
                        "`verified` provable and its absence makes it refusable")
    o.add_argument("--due", default=None, help="ISO timestamp with offset")
    o.add_argument("--channel", default="manual")
    o.add_argument("--channel-ref", default=None)
    o.add_argument("--follow-up", default=None, help="prior ask_id this follows")
    o.set_defaults(fn=cmd_open)

    an = sub.add_parser("answer", help="record what came back, and what kind it is")
    an.add_argument("--project", required=True)
    an.add_argument("--id", required=True)
    an.add_argument("--says", required=True, help="the reply, in their words")
    an.add_argument("--as", dest="as_", default="claim", choices=ANSWER_KINDS,
                    help="evidential status; `claim` is the default and usually correct")
    an.add_argument("--by", default=None, help="who actually answered")
    an.add_argument("--evidence", default=None,
                    help="what corroborated it, if anything")
    an.add_argument("--valid-until", default=None,
                    help="when this stops being current")
    an.add_argument("--skip-verify", action="store_true",
                    help="records the check as NOT RUN, never as passed")
    an.set_defaults(fn=cmd_answer)

    c = sub.add_parser("cancel", help="the question stopped mattering")
    c.add_argument("--project", required=True)
    c.add_argument("--id", required=True)
    c.add_argument("--reason", default=None)
    c.set_defaults(fn=cmd_cancel)

    st = sub.add_parser("status", help="what is outstanding; no network")
    st.add_argument("--project", required=True)
    st.add_argument("--open-only", action="store_true")
    st.add_argument("--stale-days", type=float, default=7.0)
    st.set_defaults(fn=cmd_status)

    sh = sub.add_parser("show", help="print one ask")
    sh.add_argument("--project", required=True)
    sh.add_argument("--id", required=True)
    sh.set_defaults(fn=cmd_show)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
