#!/usr/bin/env python3
"""Primitives every record-layer script needs, defined once.

Why this file exists
--------------------
Eleven scripts had byte-identical copies of `emit` / `refuse` / `broke` — 33
copies of the exit-code contract in CLAUDE.md "Script Integration". A contract
implemented eleven times can drift eleven ways, and two forks had already
started: the same UTC-timestamp helper was called `now_utc` in six scripts and
`utcnow` in five, and `read_json` had three signatures. None of that changed
behaviour yet, which is exactly why it was worth collapsing before it did.

Import it the way `shared/compare.py` is already imported across stage
directories -- the script dirs are hyphenated and therefore not importable
package names, so the path comes off `__file__`:

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "shared"))
    from _records import broke, emit, now_utc, read_json, refuse  # noqa: E402

That keeps every script runnable directly (`python .../retire.py plan ...`) with
no environment setup, which is the property the duplication was buying.

Stdlib only, like everything under `contracts/` -- no script may acquire a
dependency by importing this.
"""

import json
import os
import sys
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# the exit-code contract -- CLAUDE.md "Script Integration"
#
#   0  worked
#   1  worked, and the answer is no. A refusal, arrived at correctly. The
#      caller must pass it through rather than redo the work by hand, because
#      redoing it means overriding a check.
#   2  the script broke. Fall back and do the same work manually.
#
# The distinction is the whole reason these are three functions and not one
# `die()`: a skill decides whether to fall back by reading the exit code, so a
# refusal that exits 2 gets worked around and a crash that exits 1 gets
# reported to the user as a finding.
# --------------------------------------------------------------------------

def emit(payload):
    """Success payload on stdout. Machine-readable; the skill renders it."""
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def refuse(detail, **extra):
    """Exit 1 -- worked, the answer is no. Say what would have to change."""
    print(json.dumps({"refused": detail, **extra}, indent=2, ensure_ascii=False))
    sys.exit(1)


def broke(detail, **extra):
    """Exit 2 -- the script failed. The skill falls back to doing it by hand."""
    print(json.dumps({"error": detail, **extra}, indent=2, ensure_ascii=False))
    sys.exit(2)


# --------------------------------------------------------------------------
# time -- run-mechanics.md "Record integrity": UTC with an explicit offset
# --------------------------------------------------------------------------

def now_utc():
    """UTC with an explicit offset. Naive local strings from machines in
    different zones sort wrongly and look fine while doing it."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def id_stamp():
    """`YYYYmmdd_HHMMSS` for record ids. Not a timestamp -- an identifier.

    Named id_stamp, not stamp: `train-run/ingest.py` has its own unrelated
    `stamp(records, src, group)` (attaches provenance to records, not an id
    generator) -- scan.py collision, same name across two shared-ish modules
    with different arities and different meanings.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def parse_ts(v):
    """-> aware datetime, or None.

    A naive string returns None on purpose rather than being assumed local: it
    cannot be ordered against a timestamp from another machine, and pretending
    it can is how a stale record passes for a fresh one. Callers must treat
    None as "the ordering is unknown", never as "not stale".
    """
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else None


def age_days(iso):
    """Age in days (1 decimal place), or None when the timestamp cannot be
    ordered at all.

    `ndigits` used to be a parameter; every one of its 12 call sites across
    the repo passed the same default (1) and none ever varied it, so it was
    a single-value switch rather than a real axis -- inlined (scan.py `flag`).
    """
    dt = parse_ts(iso)
    if dt is None:
        return None
    return round((datetime.now(timezone.utc) - dt).total_seconds() / 86400, 1)


# There are deliberately TWO of each of the above, and picking the wrong one is
# a record-integrity bug rather than a style slip:
#
#   now_utc / parse_ts   for RECORDS and their ORDERING. Second precision, and a
#                        naive string is None — unorderable against another
#                        machine's clock, so refusing to guess is the answer.
#   now_iso / parse_iso  for run.json TIMESTAMPS and DURATIONS. Full precision,
#                        and a naive string is read as local time *with a flag
#                        saying so*, because a duration that could not be
#                        computed must be reported as such rather than dropped
#                        (run-mechanics.md "Record integrity") — and refusing
#                        outright would report every pre-offset run as
#                        durationless.
#
# `list_runs.py` used to carry its own copy of parse_iso with a note saying the
# three copies should share "a timeutil next to compare.py" if one ever existed.
# This is it.

def now_iso():
    """Full-precision UTC, for `run.json` timestamps that durations subtract."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s):
    """-> (datetime, assumed_local: bool). Always aware.

    Naive input is read as local time and the flag says so, so a caller can
    report that its answer rests on an assumption. Contrast `parse_ts`, which
    returns None instead — the difference is that ordering two records wrongly
    is silent, while a duration computed off an assumed zone is at worst a few
    hours out and worth having with a caveat attached.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.astimezone(), True
    return dt, False


# --------------------------------------------------------------------------
# record io
# --------------------------------------------------------------------------

def read_json(path, required=True):
    """-> parsed JSON, or None when `required` is false and it is absent.

    `required=False` is how a caller says "absent is a legitimate answer here".
    Unreadable is never that: a permission error or truncated file exits 2, so
    "could not read it" and "it is not there" stay different facts.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if required:
            broke(f"not found: {path}")
        return None
    except (OSError, ValueError) as exc:
        broke(f"unreadable: {path}", why=str(exc))


def atomic_write_json(path, payload, *, fsync=False, ensure_ascii=False):
    """Write via a temp file and `os.replace`, so a crash mid-write cannot
    leave a truncated record where a valid one used to be.

    `fsync=True` also forces the bytes to disk before the rename -- worth it for
    a record something irreversible will be decided against (a census that a
    deletion plan reads), not for every write.
    """
    parent = os.path.dirname(path)
    os.makedirs(parent or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=ensure_ascii)
        fh.write("\n")
        if fsync:
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(tmp_path, path)


# --------------------------------------------------------------------------- #
# Committing a record — the step between writing it and it surviving
# --------------------------------------------------------------------------- #

def _git(root, *args):
    """-> (ok, stdout, stderr). No shell, no cwd change."""
    import subprocess
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", f"{type(exc).__name__}: {exc}"
    return p.returncode == 0, (p.stdout or "").strip(), (p.stderr or "").strip()


def git_tracked(root, path):
    """-> True if git already knows this path. `atomic_write_json` producing a
    file is not the same as the file being kept."""
    ok, _out, _err = _git(root, "ls-files", "--error-unmatch", "--", path)
    return ok


def git_save(root, paths, message):
    """Commit exactly `paths` and nothing else. -> report dict.

    `atomic_write_json` makes a record crash-safe on one disk. It does not make
    it survive a `git checkout`, a `git clean`, or — the case that matters here —
    a handover, which happens by pushing or cloning the repo. A record whose whole
    purpose is to be read later by somebody who can no longer verify it, sitting
    untracked, is a record that does not go with the project.

    **Path-scoped on purpose, and this is the safety property.** `git add -- <p>`
    then `git commit -m <msg> -- <p>`: the second form commits the working-tree
    version of those paths regardless of what else is staged, and leaves every
    other change alone. A record skill that ran `git add -A` would sweep up
    whatever the user had in progress and commit it under a message about a
    dataset sweep — a real harm, and one they would find much later.

    Return shape rather than raising, because the caller decides whether a
    non-git tree is a refusal or a shrug. Nothing-to-commit is success: running
    it twice must be safe.
    """
    report = {"committed": False, "paths": list(paths), "why": None,
              "sha": None, "skipped_ignored": []}
    ok, top, err = _git(root, "rev-parse", "--show-toplevel")
    if not ok:
        report["why"] = (f"not a git work tree ({err or 'no toplevel'}) — the "
                         f"record is on disk but nothing will carry it to "
                         f"anybody else")
        return report

    keep = []
    for p in paths:
        if not os.path.exists(p):
            continue
        # A path excluded by .gitignore cannot be committed, and silently
        # dropping it would report a save that did not happen. `secrets.json` is
        # ignored deliberately — this is the branch that says so out loud.
        ignored, _o, _e = _git(root, "check-ignore", "-q", "--", p)
        if ignored:
            report["skipped_ignored"].append(p)
            continue
        keep.append(p)
    if not keep:
        report["why"] = ("nothing to commit: no record file exists"
                         if not report["skipped_ignored"] else
                         "every record path is excluded by .gitignore")
        return report

    ok, _o, err = _git(root, "add", "--", *keep)
    if not ok:
        report["why"] = f"git add failed: {err}"
        return report
    ok, _o, _e = _git(root, "diff", "--cached", "--quiet", "--", *keep)
    if ok:                                   # exit 0 from --quiet == no diff
        report["why"] = "already committed — the record has not changed"
        return report

    ok, _o, err = _git(root, "commit", "-m", message, "--", *keep)
    if not ok:
        report["why"] = f"git commit failed: {err}"
        return report
    _ok, sha, _e = _git(root, "rev-parse", "--short", "HEAD")
    report["committed"] = True
    report["sha"] = sha or None
    return report


# --------------------------------------------------------------------------
# number grounding -- CLAUDE.md "Never record a metric you did not read"
#
# Here rather than in one skill's script because TWO records now cite numbers
# back to a transcribed line (`graph.json -> sources[].quote`,
# `conclusions.json -> evidence[].quote`) and this is the rule that decides
# whether the citation is real. A correctness rule written twice gets fixed
# once.
# --------------------------------------------------------------------------

def digits(x):
    """Significant digits of a number, leading zeros stripped.

    0.0462 -> "462"   so that a log reporting it as "4.62%" still matches, which
    is the common case: records keep the fraction, logs print the percentage.
    """
    out = "".join(c for c in str(x) if c.isdigit()).lstrip("0")
    return out or "0"


def quotes_the_number(value, quote):
    """Does `quote` contain `value`'s digits?

    ‼️ A FLOOR, not a proof. A quote containing the digits does not show the
    source was open -- but a quote NOT containing them shows it was not, and
    that is the failure worth catching: a number written from memory and
    back-cited to a plausible path. Non-numbers are not checkable here and pass.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return digits(value) in "".join(c for c in str(quote) if c.isdigit())
