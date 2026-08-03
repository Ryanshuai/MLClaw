#!/usr/bin/env python3
"""discover.py — find out what data exists, when nobody can tell you.

The taking-over case, and it is the common one. There is a Confluence page from
two years ago, three repos with hardcoded paths, an S3 bucket you have partial
credentials for, a cloud console you were added to yesterday, and the person who
knew is gone. Nothing declares what data exists. `/data-check` cannot help: it
censuses a dataset you have already described, and describing it is the problem.

So this is the data-side counterpart of `/train-init`'s source sweep, and it
keeps that skill's discipline: record what was read, from where, and what is
still a guess. Its output is LEADS, never a dataset. Declaring the layout
contract stays with `/data-check`, because `unit_glob`'s depth is load-bearing
and silent when wrong — a guessed one is worse than none.

FOUR STATUSES, and the whole value is in the last two.

  claim        a doc, a code path, or a person says the data is there. Nothing
               has checked. This is what a handover hands you, and it is not
               evidence.
  verified     something other than a sentence listed it, and it is there now.
  gone         we looked where the claim pointed, and it is not there. THE
               finding of a handover: a path in a config that no longer resolves
               is data that moved or was deleted, and the sooner it is escalated
               the better the odds anybody still remembers where it went.
  unreachable  we could not look — no credentials, host down, private repo.
               NEVER `gone`. On a handover this is the majority state for weeks,
               because access arrives after responsibility does, and a report
               that spelled it `gone` would have you chasing data that is fine.

WHY THE REPORT LEADS WITH WHAT IT DID NOT CHECK. A discovery that lists only
findings reads as an inventory of what exists, which is exactly the wrong
conclusion to hand somebody on day three. `report` states unchecked sources and
unprobed leads before any count, same rule and same reason as a partial census.

Records live at `{PROJECT}/discovery/leads.json` — one living file, not a dated
scan, because a lead is long-lived and its status changes as access arrives. That
file is the handover artifact: it travels, it is git-tracked, and it is the thing
you hand the next person instead of a Confluence page.

Verbs:
  sources  what could be swept at all, and which of those are usable right now
  record   add a lead: a path, where you heard about it, and what it claims to be
  probe    go look at recorded leads and classify each one
  report   the leads, with what was never checked stated first

Exit codes per CLAUDE.md "Script Integration": 0 ok; 1 = the script worked and
the answer is no; 2 = the script broke, do it by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (age_days, atomic_write_json, broke, emit, now_utc, read_json, refuse)  # noqa: E402

# Walking a multi-terabyte tree to total it can take minutes, and a sweep nobody
# waits for is a sweep nobody runs. So measurement is bounded — and when the
# budget runs out the answer is "not measured", never a partial total presented
# as a whole one. A truncated byte count is the same class of lie as a partial
# census read as an inventory.
DEFAULT_BUDGET_S = 30.0

# Where a lead came from. Ordered by how much the mere mention is worth, which is
# the ordering a reader needs: code that ran pointed somewhere real, a doc only
# ever asserted something.
SOURCE_TYPES = ("code", "tracking", "git_history", "server", "s3", "cloud_console",
                "doc", "person", "other")

STATUSES = ("claim", "verified", "gone", "unreachable")

# A lead whose newest probe is older than this needs looking at again. Short on
# purpose: during a handover the previous owner is often still committing, so a
# path read out of their repo is a moving target, not a one-time reading.
DEFAULT_RECHECK_DAYS = 7.0

OK = "__MLCLAW_DISCOVER_OK__"


def leads_path(project) -> str:
    return os.path.join(project, "discovery", "leads.json")


def load_leads(project) -> dict:
    rec = read_json(leads_path(project), required=False)
    return rec or {"project": project, "created_at": now_utc(), "leads": []}


def workspace_resources(project):
    """`resources.json` sits at the workspace root, one level above the project.
    Absent is a normal state on day one of a handover and is reported as such
    rather than treated as "there are no sources"."""
    path = os.path.join(os.path.dirname(os.path.abspath(project)), "resources.json")
    return read_json(path, required=False), path


# --------------------------------------------------------------------------- #
# sources — the checklist, and what it can actually reach
# --------------------------------------------------------------------------- #

def cmd_sources(a) -> None:
    """What is available to sweep, and which of those are usable right now.

    This verb exists so that "what did you not check" has an answer. Without a
    list of what COULD have been checked, a findings list is unfalsifiable: it
    looks complete no matter how little was looked at.
    """
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    res, rpath = workspace_resources(project)

    out = []
    if res is None:
        out.append({"source": "resources.json", "usable": False,
                    "why": f"not found at {rpath} — no server, S3 or vendor is "
                           f"registered yet, so none of them can be swept",
                    "fix": "/resources"})
    else:
        aws = res.get("aws") or {}
        has_aws = bool(aws.get("access_key_id") or os.environ.get("AWS_ACCESS_KEY_ID")
                       or os.path.isfile(os.path.expanduser("~/.aws/credentials")))
        out.append({"source": "s3", "usable": has_aws,
                    "bucket": aws.get("s3_bucket") or None,
                    "why": None if has_aws else
                           "no AWS credentials — every s3:// lead stays "
                           "UNREACHABLE, which is not the same as empty",
                    "fix": None if has_aws else "/resources"})
        for key, srv in (res.get("servers") or {}).items():
            if key.startswith("_"):
                continue
            ok = bool(srv.get("host") or srv.get("alias"))
            out.append({"source": f"server:{key}", "usable": ok,
                        "root": srv.get("mlclaw_root") or None,
                        "why": None if ok else "no host or alias recorded"})
        for key, party in (res.get("outsourcing") or {}).items():
            if key.startswith("_"):
                continue
            # A vendor can be holding the only copy of a batch. They are a source
            # of ANSWERS, not of listings — hence /ask-human, not a probe.
            out.append({"source": f"outsourcing:{key}", "usable": False,
                        "why": "a party MLClaw cannot list; ask them",
                        "fix": "/ask-human"})
        for p in (res.get("local") or {}).get("base_paths") or []:
            out.append({"source": "local", "usable": os.path.isdir(os.path.expanduser(p)),
                        "root": p})

    # Code on disk is the highest-signal source and needs no credentials: a path
    # a training script actually read is a path that existed.
    for stage in sorted(os.listdir(os.path.join(project, "stages"))
                        if os.path.isdir(os.path.join(project, "stages")) else []):
        code = os.path.join(project, "stages", stage, "code")
        if os.path.isdir(code):
            out.append({"source": f"code:{stage}", "usable": True, "root": code,
                        "why": "grep for data roots, dataset classes, config YAMLs; "
                               "then `git log -S` for paths that were removed"})

    usable = [s for s in out if s["usable"]]
    emit({
        "project": project,
        "sources": out,
        "counts": {"total": len(out), "usable_now": len(usable),
                   "blocked": len(out) - len(usable)},
        # Said plainly, because on a handover this number is large and shrinking,
        # and a sweep run today is a sweep of a fraction of the world.
        "note": f"{len(out) - len(usable)} of {len(out)} source(s) cannot be "
                f"swept right now. Anything they hold will be recorded "
                f"UNREACHABLE, never `gone`."
                if len(usable) < len(out) else None,
    })


# --------------------------------------------------------------------------- #
# record — a lead
# --------------------------------------------------------------------------- #

def cmd_record(a) -> None:
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    rec = load_leads(project)

    on = a.on
    if on is None:
        on = "s3" if a.path.startswith("s3://") else "local"

    dup = next((l for l in rec["leads"] if l["path"] == a.path and l["on"] == on), None)
    if dup and not a.again:
        refuse(f"already recorded as {dup['lead_id']} ({dup['status']})",
               lead=dup,
               why="two leads for the same path would each be probed and each be "
                   "reported, which turns one place into two findings",
               fix="--again to add a second lead anyway (different claim about "
                   "the same path), or edit the record")

    lead = {
        "lead_id": f"lead_{len(rec['leads']) + 1:04d}",
        "path": a.path,
        "on": on,
        "source_type": a.source_type,
        # Not optional. A lead with no evidence is a rumour, and six months on
        # nobody can tell which of these came from a config file that ran and
        # which came from a wiki page somebody wrote from memory.
        "evidence": a.evidence,
        "what": a.what,
        "status": "claim",
        "recorded_at": now_utc(),
        "probes": [],
        "dataset": a.dataset,
        "note": a.note,
    }
    rec["leads"].append(lead)
    rec["updated_at"] = now_utc()
    atomic_write_json(leads_path(project), rec)
    emit({"recorded": lead["lead_id"], "lead": lead,
          "path": leads_path(project),
          "next": "probe it — a claim is not a finding"})


# --------------------------------------------------------------------------- #
# probe — go look
# --------------------------------------------------------------------------- #

def human(n):
    if n is None:
        return None
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def measure_local(path, budget_s):
    """-> (bytes, files, why_not). Bounded, and honest when the bound is hit."""
    deadline = time.time() + budget_s
    total = files = 0
    for root, _dirs, names in os.walk(path, onerror=lambda e: None):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
            files += 1
        if time.time() > deadline:
            # Both values discarded on purpose. Returning the partial count with
            # a caveat invites it to be quoted without one.
            return None, None, (f"the walk exceeded the {budget_s:g}s budget — "
                                f"size and file count are NOT MEASURED, not zero")
    return total, files, None


def probe_local(path, budget_s):
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return "gone", "no such path on this machine", None, {}
    if os.path.isfile(p):
        sz = os.path.getsize(p)
        return "verified", f"file, {human(sz)}", None, {"bytes": sz, "files": 1}
    try:
        names = sorted(os.listdir(p))
    except OSError as exc:
        # Permission denied is "could not look", not "not there". This is the
        # single most common way a handover sweep produces a false `gone`.
        return "unreachable", f"{type(exc).__name__}: {exc}", None, {}
    b, f, why = measure_local(p, budget_s)
    size = {"bytes": b, "files": f}
    if why:
        size["not_measured"] = why
    return ("verified", f"directory, {len(names)} entries"
            + (f", {human(b)} in {f} files" if b is not None else f"; {why}"),
            names[:20], size)


SIZE, FILES = "__MLCLAW_SIZE__", "__MLCLAW_FILES__"


def probe_server(key, path, res, budget_s):
    srv = ((res or {}).get("servers") or {}).get(key)
    if not srv:
        return "unreachable", f"no server {key!r} in resources.json", None, {}
    host = srv.get("alias") or srv.get("host")
    if not host:
        return "unreachable", f"server {key!r} has no host or alias", None, {}
    q = shlex.quote(path)
    # `du -sk` rather than `-sb`: `-b` is GNU-only and a capture box may be a
    # BSD. KB granularity is irrelevant next to the question being asked.
    script = (
        f'if [ -e {q} ]; then\n'
        f'  ls -1A {q} 2>/dev/null | head -n 20\n'
        f'  echo {SIZE}\n'
        f'  du -sk {q} 2>/dev/null | awk \'{{print $1}}\'\n'
        f'  echo {FILES}\n'
        f'  find {q} -type f 2>/dev/null | wc -l\n'
        f'  echo {OK}\n'
        f'else echo MISSING; echo {OK}; fi\n')
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "sh", "-s"]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True,
                           timeout=max(60.0, budget_s * 2))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "unreachable", f"{type(exc).__name__}: {exc}", None, {}
    # Both checks: a non-zero exit catches ssh refusing, the sentinel catches a
    # shell that died mid-command with a zero exit. Either alone lets a
    # truncated answer pass for a complete one.
    if p.returncode != 0 or OK not in p.stdout:
        err = (p.stderr or "").strip().splitlines()
        return ("unreachable",
                f"exit {p.returncode}: {err[-1] if err else 'no sentinel'}", None, {})
    out = p.stdout.splitlines()
    if "MISSING" in out:
        return "gone", f"{host} answered; that path is not there", None, {}

    def section(a, b):
        try:
            i = out.index(a)
        except ValueError:
            return []
        j = out.index(b) if b in out else len(out)
        return [x.strip() for x in out[i + 1:j] if x.strip()]

    names = [x for x in out[:out.index(SIZE)] if x.strip()] if SIZE in out else []
    kb, nf = section(SIZE, FILES), section(FILES, OK)
    size = {}
    try:
        size["bytes"] = int(kb[0]) * 1024
    except (IndexError, ValueError):
        size["bytes"] = None
        size["not_measured"] = "du produced no total on that host"
    try:
        size["files"] = int(nf[0])
    except (IndexError, ValueError):
        size["files"] = None
    detail = f"{len(names)} entries listed (first 20)"
    if size.get("bytes") is not None:
        detail += f", {human(size['bytes'])} in {size.get('files')} files"
    return "verified", detail, names[:20], size


def probe_s3(path, budget_s):
    if not path.startswith("s3://"):
        return "unreachable", "not an s3:// path", None, {}
    try:
        # `--summarize --recursive` is the only way to get a real total, and it
        # is also what makes this the slow probe. Bounded like the local walk.
        p = subprocess.run(["aws", "s3", "ls", "--recursive", "--summarize", path],
                           capture_output=True, text=True,
                           timeout=max(60.0, budget_s * 4))
    except FileNotFoundError:
        return "unreachable", "the aws CLI is not installed", None, {}
    except subprocess.TimeoutExpired:
        return ("unreachable",
                f"the recursive listing exceeded its budget — size is NOT "
                f"MEASURED; the prefix may still be fine", None, {})
    except OSError as exc:
        return "unreachable", f"{type(exc).__name__}: {exc}", None, {}
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        # An auth failure and an empty prefix are opposite conclusions and both
        # can exit non-zero. Only credential and access wording is allowed to
        # mean "could not look".
        low = err.lower()
        if any(k in low for k in ("credential", "accessdenied", "access denied",
                                  "expired", "unable to locate", "forbidden",
                                  "invalidaccesskey")):
            return "unreachable", err[-300:], None, {}
        if "nosuchbucket" in low.replace(" ", ""):
            return "gone", err[-300:], None, {}
        return "unreachable", f"aws exit {p.returncode}: {err[-300:]}", None, {}

    lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    objs = [x for x in lines if not x.startswith("Total ")]
    size = {"bytes": None, "files": None}
    for ln in lines:
        if ln.startswith("Total Objects:"):
            try:
                size["files"] = int(ln.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif ln.startswith("Total Size:"):
            try:
                size["bytes"] = int(ln.split(":", 1)[1].strip())
            except ValueError:
                pass
    if not objs and not size["files"]:
        # Require POSITIVE evidence that the listing completed before calling a
        # prefix empty — the same reason census.py wants a sentinel and not just
        # a zero exit. `--summarize` always prints a Total line on success, so
        # its absence next to anything on stderr means we did not get an answer,
        # and "no answer" must not become "no data".
        if "Total Objects:" not in p.stdout:
            return ("unreachable",
                    f"aws exited 0 but printed no summary line, so the prefix was "
                    f"not confirmed empty: {(p.stderr or '').strip()[-200:]}",
                    None, size)
        # The bucket answered, the summary is there, and it is zero. This is the
        # one place `gone` is a real reading rather than a guess.
        return "gone", "the prefix listed successfully and is empty", None, size
    return ("verified",
            f"{size['files']} objects, {human(size['bytes'])}",
            objs[:20], size)


def cmd_probe(a) -> None:
    project = os.path.expanduser(a.project)
    rec = read_json(leads_path(project), required=False)
    if rec is None:
        refuse("no leads recorded yet", fix="discover.py record")
    res, _ = workspace_resources(project)

    todo = [l for l in rec["leads"]
            if (a.id is None or l["lead_id"] == a.id)
            and (a.all or not l["probes"]
                 or (age_days(l["probes"][-1]["at"]) or 1e9) >= a.recheck_days)]
    if a.id and not todo:
        known = [l["lead_id"] for l in rec["leads"]]
        if a.id not in known:
            broke(f"no lead {a.id!r}", known=known)
        todo = [l for l in rec["leads"] if l["lead_id"] == a.id]
    if not todo:
        return emit({"probed": [], "note": "every lead has a probe newer than "
                                           f"{a.recheck_days} days — --all to redo them"})

    results = []
    for lead in todo:
        on = lead["on"]
        if on == "local":
            status, detail, sample, size = probe_local(lead["path"], a.budget_seconds)
        elif on == "s3":
            status, detail, sample, size = probe_s3(lead["path"], a.budget_seconds)
        else:
            status, detail, sample, size = probe_server(
                on.split(":", 1)[-1], lead["path"], res, a.budget_seconds)
        lead["probes"].append({"at": now_utc(), "result": status, "detail": detail,
                               "sample": sample, **size})
        lead["status"] = status
        # Carried onto the lead so the table can be drawn without re-walking
        # anything. `null` means not measured; it never means zero.
        lead["bytes"] = size.get("bytes")
        lead["files"] = size.get("files")
        lead["size_not_measured"] = size.get("not_measured")
        results.append({"lead_id": lead["lead_id"], "path": lead["path"],
                        "on": on, "status": status, "detail": detail,
                        "bytes": size.get("bytes"), "files": size.get("files"),
                        "size_not_measured": size.get("not_measured"),
                        "was_claimed": lead.get("what"),
                        "evidence": lead.get("evidence")})

    rec["updated_at"] = now_utc()
    atomic_write_json(leads_path(project), rec)

    gone = [r for r in results if r["status"] == "gone"]
    unreachable = [r for r in results if r["status"] == "unreachable"]
    emit({
        "probed": results,
        "counts": {k: sum(1 for r in results if r["status"] == k) for k in STATUSES},
        # The handover finding, on its own line. A claim that probed to `gone`
        # is the one worth acting on today, while somebody might still remember.
        "gone": [{"path": r["path"], "claimed": r["was_claimed"],
                  "evidence_said": r["evidence"]} for r in gone],
        "could_not_look": [{"path": r["path"], "why": r["detail"]} for r in unreachable],
        "note": None if not unreachable else
                f"{len(unreachable)} lead(s) could not be looked at. They are "
                f"UNREACHABLE, not empty — re-probe when access arrives.",
    })
    if gone:
        sys.exit(1)


# --------------------------------------------------------------------------- #
# report — gaps first, then findings
# --------------------------------------------------------------------------- #

GLYPH = {"verified": "✓", "gone": "✗", "unreachable": "?", "claim": "·"}


def table(leads, recheck_days):
    """One row per lead: what it is, where, how big, how many files.

    The `—` in a size column is load-bearing and is never a `0`. Three different
    facts print as `—`, and each is named in the row's own status: nobody looked
    yet (`claim`), we could not look (`unreachable`), or we looked and the walk
    ran out of budget. A zero would collapse all three into "there is no data
    here", which is the one conclusion none of them supports.
    """
    rows = [("", "WHAT", "WHERE", "STATUS", "SIZE", "FILES", "CHECKED")]
    for l in leads:
        p = l["probes"][-1] if l["probes"] else None
        age = age_days(p["at"]) if p else None
        unmeasured = l["status"] in ("claim", "unreachable") or l.get("size_not_measured")
        rows.append((
            GLYPH.get(l["status"], " "),
            (l.get("what") or "—")[:30],
            (f'{l["on"]}:{l["path"]}' if l["on"] not in ("local", "s3")
             else l["path"])[:46],
            l["status"],
            "—" if unmeasured or l.get("bytes") is None else human(l["bytes"]),
            "—" if unmeasured or l.get("files") is None else f'{l["files"]:,}',
            "never" if age is None else f"{age:g}d ago",
        ))
    w = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    out = []
    for n, r in enumerate(rows):
        out.append("  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            out.append("─" * len(out[0]))

    # Totals over measured rows only, said as a lower bound whenever anything
    # was not measured — the same rule as a partial census's counts.
    measured = [l for l in leads if l["status"] == "verified"
                and l.get("bytes") is not None]
    missing = [l for l in leads if l["status"] != "gone" and l not in measured]
    tb, tf = sum(l["bytes"] for l in measured), sum(l.get("files") or 0 for l in measured)
    out.append("")
    if measured:
        out.append(f"measured: {human(tb)} in {tf:,} files across "
                   f"{len(measured)} location(s)")
    else:
        # Never "0 B". A zero total beside "1 location not measured" reads as
        # "there is no data", which is the opposite of what it means, and it is
        # the sentence somebody would repeat in a handover meeting.
        out.append(f"measured: NOTHING yet — 0 of {len(missing)} location(s) "
                   f"have been sized. This is not a statement about how much "
                   f"data there is.")
    if missing and measured:
        out.append(f"NOT measured: {len(missing)} location(s) — this total is a "
                   f"LOWER BOUND, not an inventory")
    elif missing:
        out.append(f"NOT measured: {len(missing)} location(s) — any total from "
                   f"this sweep would be a LOWER BOUND, not an inventory")
    for l in leads:
        if l["status"] == "gone":
            out.append(f"GONE: {l['path']} — claimed as {l.get('what') or '?'}; "
                       f"evidence was {l.get('evidence')}")
    return "\n".join(out)


def cmd_report(a) -> None:
    project = os.path.expanduser(a.project)
    rec = read_json(leads_path(project), required=False)
    if rec is None:
        return emit({"leads": [], "note": "nothing recorded yet — discover.py "
                                          "sources lists what there is to sweep"})
    leads = rec["leads"]
    by = {k: [l for l in leads if l["status"] == k] for k in STATUSES}
    unprobed = [l for l in leads if not l["probes"]]
    stale = [l for l in leads if l["probes"]
             and (age_days(l["probes"][-1]["at"]) or 0) >= a.recheck_days]

    def slim(l):
        p = l["probes"][-1] if l["probes"] else None
        return {"lead_id": l["lead_id"], "path": l["path"], "on": l["on"],
                "status": l["status"], "source_type": l["source_type"],
                "what": l.get("what"), "evidence": l.get("evidence"),
                "dataset": l.get("dataset"),
                # null is "not measured", never zero. Three causes and the
                # status says which: never probed, could not look, or the walk
                # ran out of budget.
                "bytes": l.get("bytes"), "files": l.get("files"),
                "size_not_measured": l.get("size_not_measured"),
                "last_probe": p and {"at": p["at"], "detail": p["detail"],
                                     "age_days": age_days(p["at"])}}

    measured = [l for l in leads if l["status"] == "verified"
                and l.get("bytes") is not None]
    if not a.json:
        print(table(leads, a.recheck_days))
        return

    emit({
        "project": project,
        # Gaps BEFORE counts, deliberately. A findings list with the caveats
        # underneath it is a findings list read as an inventory — the same
        # ordering rule as a partial census banner.
        "not_checked": {
            "unprobed_leads": [slim(l) for l in unprobed],
            "unreachable": [slim(l) for l in by["unreachable"]],
            "probe_older_than_days": a.recheck_days,
            "stale_probes": [slim(l) for l in stale],
        },
        "exhaustive": False,
        "why_not_exhaustive":
            "a sweep finds what somebody wrote down or left a path to. Data that "
            "nobody documented and no surviving code points at will not appear "
            "here, and no number of clean probes changes that",
        "verified": [slim(l) for l in by["verified"]],
        "gone": [slim(l) for l in by["gone"]],
        "still_only_claimed": [slim(l) for l in by["claim"]],
        "counts": {k: len(v) for k, v in by.items()} | {"total": len(leads)},
        "measured": {
            "locations": len(measured),
            "bytes": sum(l["bytes"] for l in measured),
            "files": sum(l.get("files") or 0 for l in measured),
            # True whenever anything was not measured, which is almost always
            # during a handover. A total from a partial sweep is a floor.
            "is_lower_bound": len(measured) < len([l for l in leads
                                                   if l["status"] != "gone"]),
        },
        "next": ("/data-check to declare a layout contract for anything verified — "
                 "this skill deliberately does not, because unit_glob's depth is "
                 "load-bearing and a guess there is silent"),
    })


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sources", help="what can be swept, and what is usable now")
    s.add_argument("--project", required=True)
    s.set_defaults(fn=cmd_sources)

    r = sub.add_parser("record", help="add a lead")
    r.add_argument("--project", required=True)
    r.add_argument("--path", required=True, help="s3://..., or a path on --on")
    r.add_argument("--on", default=None,
                   help="local | s3 | server:<key>  (inferred from the path if omitted)")
    r.add_argument("--source-type", required=True, choices=SOURCE_TYPES,
                   dest="source_type", help="where you heard about it")
    r.add_argument("--evidence", required=True,
                   help="the doc, file:line, commit or person this came from")
    r.add_argument("--what", default=None, help="what it is claimed to be")
    r.add_argument("--dataset", default=None, help="dataset id, once one exists")
    r.add_argument("--note", default=None)
    r.add_argument("--again", action="store_true",
                   help="record a second lead for a path already recorded")
    r.set_defaults(fn=cmd_record)

    pr = sub.add_parser("probe", help="go look at recorded leads")
    pr.add_argument("--project", required=True)
    pr.add_argument("--id", default=None, help="one lead; default: all unprobed/stale")
    pr.add_argument("--all", action="store_true", help="re-probe everything")
    pr.add_argument("--recheck-days", type=float, default=DEFAULT_RECHECK_DAYS)
    pr.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_S,
                    help="per-lead time budget for sizing. Exceeding it records "
                         "size as NOT MEASURED, never as zero")
    pr.set_defaults(fn=cmd_probe)

    rp = sub.add_parser("report", help="the table: what, where, how big, how many")
    rp.add_argument("--project", required=True)
    rp.add_argument("--recheck-days", type=float, default=DEFAULT_RECHECK_DAYS)
    rp.add_argument("--json", action="store_true",
                    help="machine form; default is the table")
    rp.set_defaults(fn=cmd_report)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
