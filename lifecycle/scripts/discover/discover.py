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
import base64
import json
import os
import re
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

# Where the thing IS, which decides which probe goes and looks. Deliberately not
# the same axis as `source_type` (where the CLAIM came from): "the code that ran
# named this bucket" is `source_type=code, on=s3`, and collapsing the two would
# give a bucket named in a running config the same credence as one named on a
# wiki page.
#
# This tuple exists because the dispatch used to be `local` / `s3` / else ->
# server, taking `on.split(":")[-1]` as the server key. So `tracking:wandb`
# probed as a server named "wandb" and reported `unreachable: no server 'wandb'
# in resources.json` — the right verdict reached by nonsense reasoning, with a
# fix instruction telling the reader to register a machine that does not exist.
# Any typo did the same. A wrong answer that reads as right is worse here than a
# refusal, because the lead then looks probed.
ON_MACHINE_PROBEABLE = ("local", "s3", "server:", "tracking:")

# A machine cannot look at these, and saying `unreachable` ("we could not look")
# would be wrong: somebody can look, it is just not a probe. They stay `claim`
# and resolve through /ask-human.
ON_ASK_A_PERSON = ("doc", "person", "cloud_console")

# Every `on` below is dispatched. An unbuilt tracking BACKEND is refused by
# probe_tracking itself rather than by a table here, because the locator is
# worth keeping even when nothing can read it — same as ingest.py recording
# `log_format: wandb` and then saying it has no adapter.

# Which `candidates[].match` values a lead's status permits. A lead in
# `leads.json` and a candidate in a stage's `input.json` are two records of ONE
# fact — "is this data here" — kept in sync by hand, and nothing joined them. So
# a lead could be probed `unreachable` while the candidate it came from still
# said `ok`, and /train-run would launch against a path nobody could reach.
#
# The load-bearing row is `claim`: a candidate is never `ok` on a lead that only
# a document or a person asserts. That is "never let somebody's word become a
# checked fact", applied to whether data exists.
MATCH_FOR_STATUS = {
    # the lead says it is there; whether it FITS is the init's own judgment
    "verified":    ("ok", "mismatch", "pending"),
    # "could not look" must not become "not there" and must never become usable
    "unreachable": ("unreachable",),
    "gone":        ("absent",),
    "claim":       ("unreachable", "absent", "pending"),
}

# Where a stage keeps the two halves of the join.
CANDIDATE_FILES = ("input.json", "artifacts.json")

# A lead whose access disappears on a known date. `--recheck-days` already covers
# staleness in one direction (the world may have changed, go look again); this is
# the other one, which nothing could say: a departing account's tracking history,
# a wiki page in a personal space, a key pending rotation. All of them read
# identically to a lead that can be resolved next month, and the ordinary advice
# for `unreachable` — "come back when access arrives" — is exactly wrong when
# access is about to be revoked instead of granted.
DEFAULT_EXPIRING_SOON_DAYS = 14.0

# A lead whose newest probe is older than this needs looking at again. Short on
# purpose: during a handover the previous owner is often still committing, so a
# path read out of their repo is a moving target, not a one-time reading.
DEFAULT_RECHECK_DAYS = 7.0

OK = "__MLCLAW_DISCOVER_OK__"


def classify_on(on):
    """-> "machine" | "ask" | "no_probe" | None. None means nothing can dispatch
    it, which `record` refuses and `probe` reports as such rather than guessing."""
    if not on:
        return None
    if on in ("local", "s3") or on.startswith(("server:", "tracking:")):
        return "machine"
    if on in ON_ASK_A_PERSON:
        return "ask"
    return None


def leads_path(project) -> str:
    return os.path.join(project, "discovery", "leads.json")


def expiry_state(lead, soon_days):
    """-> (state, days_left) where state is None | "expiring_soon" | "expired".

    `expired` on a lead that is still `claim` or `unreachable` is the transition
    nothing else observes: a source that could not be reached because access had
    not arrived, and now never will. Before the date and after it, the record
    reads the same — which is why the date has to be written down while somebody
    still knows it.
    """
    when = lead.get("access_expires_at")
    if not when:
        return None, None
    left = -age_days(when)
    if left is None:
        return None, None
    if left <= 0:
        return "expired", left
    if left <= soon_days:
        return "expiring_soon", left
    return None, left


def stage_candidates(project, stage):
    """-> [(file, item_name, index, entry)] for every candidate the stage lists,
    plus the declared `items` so coverage can be checked against them."""
    found, declared = [], {}
    for fname in CANDIDATE_FILES:
        path = os.path.join(project, "stages", stage, fname)
        doc = read_json(path, required=False)
        if not doc:
            continue
        for name in (doc.get("items") or {}):
            declared.setdefault(name, fname)
        block = ((doc.get("candidates") or {}).get("items") or {})
        for name, entries in block.items():
            if not isinstance(entries, list):
                continue
            for i, entry in enumerate(entries):
                if isinstance(entry, dict):
                    found.append((fname, name, i, entry))
    return found, declared


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

    It used to list only what `resources.json` registered, which made it wrong in
    the one direction that matters: with nothing registered it reported a single
    blocked source, so a project whose git history and on-disk tracking leftovers
    were sitting there unread looked like a project with nothing to read. Those are
    the CREDENTIAL-FREE sources — the only ones available in the weeks a handover
    actually starts in — and omitting them turned this verb into an argument for
    waiting. Every family `searches.md` ranks now has a row.

    Two distinctions the rows carry, because collapsing either one sends the reader
    to the wrong place:

      `kind`        mine (a place to find candidate locations) vs probe (a location
                    kind something can go and classify) vs ask (a person)
      `blocked_by`  credential (a key away) vs human (no key will ever help) vs
                    registration vs absent. "Blocked" alone routes a wiki page to
                    the same queue as an expired AWS key.
    """
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    res, rpath = workspace_resources(project)

    out = []

    def add(source, kind, usable, why=None, blocked_by=None, fix=None, **extra):
        row = {"source": source, "kind": kind, "usable": bool(usable),
               "why": why, "blocked_by": None if usable else blocked_by,
               "fix": fix}
        row.update(extra)
        out.append(row)

    stages_dir = os.path.join(project, "stages")
    stages = sorted(os.listdir(stages_dir)) if os.path.isdir(stages_dir) else []
    code_dirs = []
    for stage in stages:
        code = os.path.join(stages_dir, stage, "code")
        if os.path.isdir(code):
            code_dirs.append((stage, code))

    # --- credential-free, and therefore first. searches.md's ranking, in order.

    # code — a path a script read is a path that existed.
    for stage, code in code_dirs:
        add(f"code:{stage}", "mine", True, root=code,
            why="grep for data roots, dataset classes, config YAMLs, "
                "`--data-dir` defaults, docker mounts, .env files")
    if not code_dirs:
        add("code", "mine", False, blocked_by="absent",
            why="no stage has a code/ directory yet — nothing to grep",
            fix="/train-init or /eval-init")

    # git history — the only source that shows what MOVED, and free.
    for stage, code in code_dirs:
        is_git = os.path.isdir(os.path.join(code, ".git"))
        add(f"git_history:{stage}", "mine", is_git, root=code,
            why="`git log -S<path>`, `--diff-filter=D`, deleted configs, and "
                "commit messages — which carry intent no config does"
            if is_git else "the code snapshot is not a git tree, so removed "
                           "paths and commit messages are unavailable",
            blocked_by="absent")

    # tracking, disk family — no import, no network, no key. The one tracking
    # history readable on day one, which is why it outranks every service backend.
    disk = sorted(k for k, s in TRACKING.items() if s["family"] == "disk")
    somewhere = bool(code_dirs) or bool((res or {}).get("local", {}).get("base_paths"))
    add("tracking_disk", "probe", somewhere,
        backends=disk,
        why=f"glob for {', '.join(sorted({m for k in disk for m in TRACKING[k]['markers']}))}"
        if somewhere else "no code directory and no local base path to glob under",
        blocked_by="absent")

    # tracking, service family — one row each, because "which backends can I list
    # right now" is the question a takeover asks and a single row cannot answer.
    for backend in sorted(k for k, s in TRACKING.items() if s["family"] == "service"):
        spec = TRACKING[backend]
        have, where = credential_present(spec)
        add(f"tracking:{backend}", "probe", have,
            why=(f"credential from {where}" if have else where),
            blocked_by="credential", fix=None if have else "/resources or the key owner",
            listing=("rest" if spec.get("listing") in REST_LISTINGS
                     else spec.get("listing")))

    # --- registered, and therefore contingent on somebody having registered it.

    if res is None:
        add("resources.json", "probe", False, blocked_by="registration",
            why=f"not found at {rpath} — no server, S3 or vendor is registered "
                f"yet, so none of them can be swept", fix="/resources")
    else:
        aws = res.get("aws") or {}
        has_aws = bool(aws.get("access_key_id") or os.environ.get("AWS_ACCESS_KEY_ID")
                       or os.path.isfile(os.path.expanduser("~/.aws/credentials")))
        add("s3", "probe", has_aws, bucket=aws.get("s3_bucket") or None,
            why=None if has_aws else
                "no AWS credentials — every s3:// lead stays UNREACHABLE, which "
                "is not the same as empty",
            blocked_by="credential", fix=None if has_aws else "/resources")
        for key, srv in (res.get("servers") or {}).items():
            if key.startswith("_"):
                continue
            ok = bool(srv.get("host") or srv.get("alias"))
            add(f"server:{key}", "probe", ok,
                root=srv.get("mlclaw_root") or None,
                why=None if ok else "no host or alias recorded",
                blocked_by="registration", fix=None if ok else "/resources")
        for p in (res.get("local") or {}).get("base_paths") or []:
            there = os.path.isdir(os.path.expanduser(p))
            add("local", "probe", there, root=p,
                why=None if there else "the base path is not on this machine",
                blocked_by="absent")
        for key, _party in (res.get("outsourcing") or {}).items():
            if key.startswith("_"):
                continue
            # A vendor can be holding the only copy of a batch. They are a source
            # of ANSWERS, not of listings — hence /ask-human, not a probe.
            add(f"outsourcing:{key}", "ask", False, blocked_by="human",
                why="a party MLClaw cannot list; ask them", fix="/ask-human")

    # --- never machine-swept, and that is a property of the source, not a gap.
    #
    # Listed anyway: "did anybody read the handover page" is a real question, and a
    # verb that omits the sources it cannot automate reports a complete sweep of
    # the half of the world it can reach.
    add("doc", "ask", False, blocked_by="human",
        why="no doc probe exists; query by project/repo/dataset name and record "
            "what you find as `claim`. Uniquely good for two things no listing "
            "has: WHY a thing exists, and numbers nobody logged",
        fix="/ask-human, and --access-expires-at if the page itself is at risk")
    add("person", "ask", False, blocked_by="human",
        why="the last resort and often the only one; their answer is a `claim` "
            "until something else agrees", fix="/ask-human")

    usable = [s for s in out if s["usable"]]
    tally = {}
    for s in out:
        if not s["usable"]:
            tally[s["blocked_by"] or "unstated"] = tally.get(s["blocked_by"] or "unstated", 0) + 1
    notes = []
    access = tally.get("credential", 0) + tally.get("registration", 0)
    if access:
        notes.append(f"{access} source(s) cannot be swept until access or "
                     f"registration arrives. Anything they hold will be recorded "
                     f"UNREACHABLE, never `gone`.")
    if tally.get("human"):
        notes.append(f"{tally['human']} source(s) are never machine-swept (docs, "
                     f"people, vendors). What they yield stays `claim` — no "
                     f"credential changes that.")
    free = [s["source"] for s in usable if s["kind"] == "mine"
            or s["source"] == "tracking_disk"]
    if free:
        notes.append(f"Sweepable right now with no credential at all: "
                     f"{', '.join(free)}. Start here.")
    emit({
        "project": project,
        "sources": out,
        "counts": {"total": len(out), "usable_now": len(usable),
                   "blocked": len(out) - len(usable)},
        "blocked_by": tally,
        # Said plainly, because on a handover this number is large and shrinking,
        # and a sweep run today is a sweep of a fraction of the world.
        "note": " ".join(notes) or None,
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
    if not classify_on(on):
        broke(f"unknown --on {on!r}",
              allowed=list(ON_MACHINE_PROBEABLE) + list(ON_ASK_A_PERSON),
              why="validated here rather than at probe time so a value nothing "
                  "can dispatch never enters the record. An unknown `on` used to "
                  "fall through to the server probe and come back as a confident "
                  "`unreachable`, which reads as probed")

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
        # None means "no known deadline", which is the normal case. A date here
        # says this lead stops being resolvable then — see DEFAULT_EXPIRING_SOON_DAYS.
        "access_expires_at": a.access_expires_at,
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
        return ("unreachable", f"no server {key!r} in resources.json", None,
                {"blocker": f"server:{key}:not_registered"})
    host = srv.get("alias") or srv.get("host")
    if not host:
        return ("unreachable", f"server {key!r} has no host or alias", None,
                {"blocker": f"server:{key}:no_host"})
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


def last_meaningful(text, limit=300):
    """The last non-empty LINE, capped — not `text[-limit:]`, which cuts
    mid-token at an offset that depends on the path length. That is how one
    AccessDenied on three buckets produced three different-looking blockers and
    defeated the access worklist's grouping."""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return (lines[-1] if lines else "")[:limit]


def probe_s3(path, budget_s):
    if not path.startswith("s3://"):
        return "unreachable", "not an s3:// path", None, {"blocker": "s3:bad_uri"}
    try:
        # `--summarize --recursive` is the only way to get a real total, and it
        # is also what makes this the slow probe. Bounded like the local walk.
        p = subprocess.run(["aws", "s3", "ls", "--recursive", "--summarize", path],
                           capture_output=True, text=True,
                           timeout=max(60.0, budget_s * 4))
    except FileNotFoundError:
        return ("unreachable", "the aws CLI is not installed", None,
                {"blocker": "s3:no_cli"})
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
            return ("unreachable", last_meaningful(err), None,
                    {"blocker": "s3:access_denied"})
        if "nosuchbucket" in low.replace(" ", ""):
            return "gone", last_meaningful(err), None, {}
        return ("unreachable", f"aws exit {p.returncode}: {last_meaningful(err)}",
                None, {"blocker": f"s3:exit_{p.returncode}"})

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


def credential_present(spec):
    """-> (found, where). Env var first, then the config file the CLI writes.

    Checked BEFORE importing anything or touching a network, because "there is no
    credential" is the most common answer on a handover, the cheapest one to
    reach, and the one that belongs on the access worklist. Getting it without a
    package installed also means a bare interpreter can still produce the
    worklist, which is the state a takeover actually starts in.
    """
    for var in spec.get("env", ()):
        if os.environ.get(var):
            return True, f"{var} in env"
    for path, needle in spec.get("files", ()):
        full = os.path.expanduser(path)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                if not needle or needle in fh.read():
                    return True, f"{path}" + (f" (entry for {needle})" if needle else "")
        except OSError:
            continue
    names = ", ".join(spec.get("env", ())) or "—"
    return False, (f"no credential found: none of [{names}] in env, and no "
                   f"config file at "
                   f"{' / '.join(p for p, _ in spec.get('files', ())) or '—'}")


# Every tracking backend this can be pointed at, and the split that matters:
# whether probing it needs a credential at all.
#
#   disk     the runs are FILES. A marker glob finds them, nothing is imported,
#            no network is touched, and it works on day one of a handover before
#            any access has arrived. This is the family to probe first, and
#            train-init Step 0 already knows it — its "Local tracking leftovers"
#            row is a default-yes precisely because `wandb/`, `mlruns/` and
#            `lightning_logs/` need no key.
#   service  a project on somebody's server. Credential, then package, then a
#            listing. The first two stages are real and testable on a bare
#            interpreter; only the listing needs an adapter.
#
# A backend with no listing adapter is still worth probing: "the config exists but
# the package is missing" and "there is no credential at all" are different,
# actionable answers, and both come out of stages 1-2. Only wandb is in that state
# now — every other service backend has a urllib listing (see REST_LISTINGS).
TRACKING = {
    "tensorboard": {"family": "disk", "markers": ("events.out.tfevents.*",),
                    "what": "tfevents files"},
    "lightning":   {"family": "disk", "markers": ("lightning_logs/version_*",
                                                  "version_*/hparams.yaml"),
                    "what": "lightning_logs versions"},
    "mlruns":      {"family": "disk", "markers": ("mlruns/*/meta.yaml",
                                                  "*/meta.yaml"),
                    "what": "local mlflow runs"},
    "wandb_local": {"family": "disk", "markers": ("wandb/run-*", "run-*"),
                    "what": "offline wandb run dirs"},

    "wandb": {"family": "service", "pkg": "wandb",
              "env": ("WANDB_API_KEY",),
              "files": (("~/.netrc", "api.wandb.ai"),),
              "listing": "wandb"},
    # `rest` beats `pkg`: urllib needs nothing installed, so the listing runs on a
    # bare interpreter AND is testable against a stub server. mlflow, clearml,
    # neptune and comet all go that way. wandb publishes no REST surface of the same
    # kind, so it is the one backend still on the package path — which is also why
    # it is the one that needed a real account to exercise.
    "mlflow": {"family": "service", "pkg": "mlflow",
               "env": ("MLFLOW_TRACKING_URI", "MLFLOW_TRACKING_TOKEN"),
               "files": (),
               "listing": "mlflow_rest"},
    "clearml": {"family": "service", "pkg": "clearml",
                "env": ("CLEARML_API_ACCESS_KEY", "CLEARML_API_SECRET_KEY",
                        "CLEARML_API_HOST"),
                "files": (("~/clearml.conf", ""),),
                "listing": "clearml_rest"},
    "neptune": {"family": "service", "pkg": "neptune",
                "env": ("NEPTUNE_API_TOKEN",), "files": (),
                "listing": "neptune_rest"},
    "comet": {"family": "service", "pkg": "comet_ml",
              "env": ("COMET_API_KEY",),
              "files": (("~/.comet.config", ""),), "listing": "comet_rest"},
    "aim": {"family": "disk", "markers": (".aim/meta/*", "meta/*"),
            "what": "aim repo metadata"},
    "dvclive": {"family": "disk", "markers": ("dvclive/*", "*/metrics.json"),
                "what": "dvclive outputs"},
}

WANDB_ENTITY_LISTING = (
    "import json,sys\n"
    "try:\n"
    "    import wandb\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e':'no_pkg','m':str(e)})); sys.exit(0)\n"
    "try:\n"
    "    api = wandb.Api(timeout=25)\n"
    "    ent = %r or getattr(api.viewer,'entity',None) or getattr(api.viewer,'username',None)\n"
    "    ps = [p.name for p in api.projects(ent)]\n"
    "    print(json.dumps({'entity': ent, 'projects': ps}))\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e': type(e).__name__, 'm': str(e)[:300]}))\n")

WANDB_LISTING = (
    "import json,sys\n"
    "try:\n"
    "    import wandb\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e':'no_pkg','m':str(e)})); sys.exit(0)\n"
    "try:\n"
    "    api = wandb.Api(timeout=20)\n"
    "    rs = api.runs(%r, per_page=50)\n"
    "    names = []\n"
    "    for i, r in enumerate(rs):\n"
    "        if i >= 20: break\n"
    "        names.append(f'{r.name} [{r.state}]')\n"
    "    print(json.dumps({'n': len(rs), 'names': names}))\n"
    "except Exception as e:\n"
    "    print(json.dumps({'e': type(e).__name__, 'm': str(e)[:300]}))\n")


def http_json(url, payload=None, token=None, timeout=20.0, headers=None):
    """-> (status_code, parsed_or_None, error_or_None). urllib only: a listing that
    needs no package runs on the interpreter a handover actually starts with.

    `headers` exists because the four REST backends disagree about how to carry a
    credential and there is no winning that argument: MLflow wants
    `Authorization: Bearer`, ClearML wants Basic for its login call and Bearer
    after, Comet wants the raw key in `Authorization` with no scheme at all, and
    Neptune wants `X-Neptune-Api-Token`. `token` stays as the Bearer shorthand
    since three of the five call sites want exactly that.
    """
    import urllib.error
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, val in (headers or {}).items():
        req.add_header(key, val)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = (e.read() or b"")[:300].decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, None, body or str(e)
    except urllib.error.URLError as e:
        return None, None, str(e.reason)
    except (OSError, ValueError) as e:
        return None, None, f"{type(e).__name__}: {e}"


def probe_mlflow_rest(path, where, budget_s):
    """Count experiments and runs on an MLflow tracking server over its REST API.

    `path` is either a full tracking URI or an experiment name, in which case
    MLFLOW_TRACKING_URI supplies the base. Both forms occur in the wild — a config
    names the server, a wiki page names the experiment.
    """
    base = (path if path.startswith(("http://", "https://"))
            else os.environ.get("MLFLOW_TRACKING_URI", ""))
    want_exp = None if path.startswith(("http://", "https://")) else path.strip("/")
    if not base.startswith(("http://", "https://")):
        return ("unreachable",
                f"no HTTP tracking URI: {path!r} is not a URL and "
                f"MLFLOW_TRACKING_URI is {os.environ.get('MLFLOW_TRACKING_URI')!r}. "
                f"A `file:` store is the on-disk family — record it as "
                f"`tracking:mlruns` instead", None,
                {"blocker": "tracking:mlflow:no_http_uri"})
    base = base.rstrip("/")
    token = os.environ.get("MLFLOW_TRACKING_TOKEN")

    code, body, err = http_json(f"{base}/api/2.0/mlflow/experiments/search",
                                {"max_results": 200}, token,
                                timeout=max(20.0, budget_s))
    if code is None:
        return ("unreachable", f"could not reach {base}: {err}", None,
                {"blocker": "tracking:mlflow:unreachable_host"})
    if code in (401, 403):
        return ("unreachable",
                f"{base} answered {code} — authentication is the blocker, not "
                f"absence (credential from {where})", None,
                {"blocker": f"tracking:mlflow:http_{code}"})
    if code == 404 or body is None:
        # Older servers only have the GET /list form.
        code, body, err = http_json(f"{base}/api/2.0/mlflow/experiments/list",
                                    None, token, timeout=max(20.0, budget_s))
        if code != 200 or body is None:
            return ("unreachable",
                    f"{base} did not answer either experiments endpoint "
                    f"(last: {code} {err})", None,
                    {"blocker": f"tracking:mlflow:http_{code}"})

    exps = body.get("experiments") or []
    if want_exp is not None:
        exps = [e for e in exps if e.get("name") == want_exp]
        if not exps:
            return ("gone",
                    f"{base} listed its experiments and none is named "
                    f"{want_exp!r}", None, {"blocker": None})
    if not exps:
        return ("gone", f"{base} answered and holds no experiments", None,
                {"blocker": None})

    ids = [e.get("experiment_id") for e in exps if e.get("experiment_id")][:50]
    code, body, err = http_json(f"{base}/api/2.0/mlflow/runs/search",
                                {"experiment_ids": ids, "max_results": 200},
                                token, timeout=max(20.0, budget_s))
    runs = (body or {}).get("runs") or []
    names = []
    for r in runs[:20]:
        info = r.get("info") or {}
        names.append(f"{info.get('run_id', '?')[:8]} [{info.get('status', '?')}]")
    if code != 200:
        # The experiments answered, so the server is real; the run listing did not.
        return ("verified",
                f"{len(exps)} experiment(s) at {base}; the run listing failed "
                f"({code} {err}) so RUNS ARE NOT COUNTED. Credential from {where}. "
                f"This verifies the RECORD exists, not any number in it",
                [e.get("name") for e in exps[:20]], {})
    return ("verified",
            f"{len(exps)} experiment(s), {len(runs)} run(s) at {base} "
            f"(credential from {where}). This verifies the RECORD exists, not any "
            f"number in it — a logged metric is a claim until /repro reproduces it",
            names or [e.get("name") for e in exps[:20]], {})


def config_value(path, keys):
    """-> {key: value} for whichever of `keys` the CLI-written config file names.

    A regex, not a parser, and that is a decision: the files this reads are HOCON
    (`clearml.conf`), INI-ish (`.comet.config`) and whatever somebody hand-edited,
    and `key = "value"` is the only shape all three share. A miss returns nothing
    and every caller degrades to `unreachable` naming the file it could not read a
    key out of — never to `gone`, because failing to parse a config says nothing
    whatsoever about the server.
    """
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    out = {}
    for key in keys:
        m = re.search(r'^[\s"]*' + re.escape(key) + r'"?\s*[:=]\s*"?([^"\s,]+)"?',
                      text, re.MULTILINE)
        if m:
            out[key] = m.group(1)
    return out


def probe_clearml_rest(path, where, budget_s):
    """Count projects and tasks on a ClearML API server over its REST API.

    Two calls deep, because ClearML does not accept the key pair directly: the
    access/secret pair buys a token from `auth.login` (HTTP Basic), and everything
    after is Bearer. Both stages are named separately in the blocker so "the key is
    wrong" and "the key is fine, the listing endpoint moved" do not arrive as one
    undifferentiated failure.

    `path` is a project name or empty (list everything the key can see) — the
    handover shape, same as wandb's entity listing.

    STATUS: exercised against a stub server only. The auth-then-list shape is
    documented ClearML; if a real server disagrees the probe answers `unreachable`
    carrying the endpoint and code, which is one line to fix and is never a false
    `gone`.
    """
    host = (path if path.startswith(("http://", "https://"))
            else os.environ.get("CLEARML_API_HOST", ""))
    conf = config_value("~/clearml.conf",
                        ("api_server", "access_key", "secret_key"))
    host = host or conf.get("api_server", "")
    access = os.environ.get("CLEARML_API_ACCESS_KEY") or conf.get("access_key", "")
    secret = os.environ.get("CLEARML_API_SECRET_KEY") or conf.get("secret_key", "")
    want = None if path.startswith(("http://", "https://")) else (path.strip("/") or None)

    if not host.startswith(("http://", "https://")):
        return ("unreachable",
                f"no ClearML API host: CLEARML_API_HOST is "
                f"{os.environ.get('CLEARML_API_HOST')!r} and ~/clearml.conf named "
                f"no api_server. Note the host must be the API server "
                f"(api.*), not the web app", None,
                {"blocker": "tracking:clearml:no_api_host"})
    if not (access and secret):
        return ("unreachable",
                f"found {where} but not both halves of the key pair "
                f"(access_key {'yes' if access else 'no'}, secret_key "
                f"{'yes' if secret else 'no'}) — ClearML needs both to log in",
                None, {"blocker": "tracking:clearml:partial_credential"})

    host = host.rstrip("/")
    basic = base64.b64encode(f"{access}:{secret}".encode()).decode()
    code, body, err = http_json(f"{host}/auth.login", None,
                                timeout=max(20.0, budget_s),
                                headers={"Authorization": f"Basic {basic}"})
    if code is None:
        return ("unreachable", f"could not reach {host}: {err}", None,
                {"blocker": "tracking:clearml:unreachable_host"})
    token = ((body or {}).get("data") or {}).get("token")
    if code != 200 or not token:
        return ("unreachable",
                f"{host}/auth.login answered {code} without a token "
                f"({last_meaningful(err or '', 160) or 'no error body'}) — "
                f"authentication is the blocker, not absence", None,
                {"blocker": f"tracking:clearml:login_{code}"})

    code, body, err = http_json(f"{host}/projects.get_all",
                                {"page": 0, "page_size": 200,
                                 "only_fields": ["id", "name"]},
                                token, timeout=max(20.0, budget_s))
    projects = ((body or {}).get("data") or {}).get("projects")
    if code != 200 or projects is None:
        return ("unreachable",
                f"logged in to {host} but projects.get_all answered {code} "
                f"({last_meaningful(err or '', 160) or 'unexpected body shape'}). "
                f"The credential works; the listing did not, so NOTHING WAS "
                f"COUNTED", None, {"blocker": f"tracking:clearml:list_{code}"})

    if want is not None:
        projects = [p for p in projects if p.get("name") == want]
        if not projects:
            return ("gone",
                    f"{host} listed its projects and none is named {want!r}",
                    None, {"blocker": None})
    if not projects:
        return ("gone", f"{host} answered and the key sees no projects", None,
                {"blocker": None})

    ids = [p.get("id") for p in projects if p.get("id")][:50]
    code, body, _e = http_json(f"{host}/tasks.get_all",
                               {"project": ids, "page": 0, "page_size": 200,
                                "only_fields": ["id", "name", "status"]},
                               token, timeout=max(20.0, budget_s))
    tasks = ((body or {}).get("data") or {}).get("tasks")
    if code != 200 or tasks is None:
        return ("verified",
                f"{len(projects)} project(s) at {host}; the task listing failed "
                f"({code}) so TASKS ARE NOT COUNTED. Credential from {where}. "
                f"This verifies the RECORD exists, not any number in it",
                [p.get("name") for p in projects[:20]], {})
    return ("verified",
            f"{len(projects)} project(s), {len(tasks)} task(s) at {host} "
            f"(credential from {where}). This verifies the RECORD exists, not any "
            f"number in it — a logged metric is a claim until /repro reproduces it",
            [f"{t.get('name', '?')} [{t.get('status', '?')}]" for t in tasks[:20]]
            or [p.get("name") for p in projects[:20]], {})


def probe_neptune_rest(path, where, budget_s):
    """List Neptune projects the API token can see.

    The token is not a bearer credential — it is base64 of a JSON object carrying
    the API address, so decoding it is also how the host is discovered. That is
    useful on a handover: a Neptune token alone tells you which deployment it
    belongs to, self-hosted included, without anybody writing the URL down.

    STATUS: the token decode and the oauth exchange are documented Neptune and are
    exercised against a stub. The project-listing endpoint is the half most likely
    to be wrong — Neptune reorganised its API between major versions — so a
    mismatch answers `unreachable` naming the endpoint and code rather than
    guessing at a fallback nobody verified.
    """
    raw = os.environ.get("NEPTUNE_API_TOKEN", "")
    if not raw:
        return ("unreachable",
                f"found {where} but NEPTUNE_API_TOKEN is not in this environment "
                f"— the token is the only thing that names the API host, so "
                f"nothing can be probed without it", None,
                {"blocker": "tracking:neptune:no_token_in_env"})
    try:
        pad = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.b64decode(pad).decode("utf-8", "replace"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return ("unreachable",
                "NEPTUNE_API_TOKEN is not decodable base64-JSON — it may be "
                "truncated by a shell quote or be a different kind of key "
                "entirely. The project is not implicated", None,
                {"blocker": "tracking:neptune:undecodable_token"})
    api = (decoded.get("api_url") or decoded.get("api_address") or "").rstrip("/")
    if not api.startswith(("http://", "https://")):
        return ("unreachable",
                f"the token decoded but names no api_url/api_address "
                f"(keys: {sorted(decoded)}) ", None,
                {"blocker": "tracking:neptune:token_names_no_host"})

    code, body, err = http_json(
        f"{api}/api/backend/v1/authorization/oauth-token", None,
        timeout=max(20.0, budget_s), headers={"X-Neptune-Api-Token": raw})
    if code is None:
        return ("unreachable", f"could not reach {api}: {err}", None,
                {"blocker": "tracking:neptune:unreachable_host"})
    access = (body or {}).get("accessToken")
    if code != 200 or not access:
        return ("unreachable",
                f"{api} answered {code} to the oauth-token exchange "
                f"({last_meaningful(err or '', 160) or 'no accessToken in body'}) "
                f"— authentication is the blocker, not absence", None,
                {"blocker": f"tracking:neptune:oauth_{code}"})

    code, body, err = http_json(f"{api}/api/backend/v1/projects", None, access,
                                timeout=max(20.0, budget_s))
    entries = body if isinstance(body, list) else (body or {}).get("entries")
    if code != 200 or entries is None:
        return ("unreachable",
                f"authenticated to {api} but "
                f"/api/backend/v1/projects answered {code} "
                f"({last_meaningful(err or '', 160) or 'unexpected body shape'}). "
                f"The token works; NOTHING WAS COUNTED", None,
                {"blocker": f"tracking:neptune:list_{code}"})

    want = path.strip("/") or None
    names = [f"{p.get('organizationName', '?')}/{p.get('name', '?')}"
             for p in entries if isinstance(p, dict)]
    if want:
        hits = [n for n in names if n == want or n.endswith("/" + want)]
        if not hits:
            return ("gone",
                    f"{api} listed {len(names)} project(s) for this token and "
                    f"none is {want!r}", None, {"blocker": None})
        names = hits
    if not names:
        return ("gone", f"{api} answered and the token sees no projects", None,
                {"blocker": None})
    return ("verified",
            f"{len(names)} project(s) visible at {api} (credential from {where}). "
            f"Neptune's REST does not give a run count per project here, so RUNS "
            f"ARE NOT COUNTED. This verifies the RECORD exists, not any number "
            f"in it", names[:40], {})


def probe_comet_rest(path, where, budget_s):
    """Count Comet projects and experiments over REST v2.

    `path` is `<workspace>` or `<workspace>/<project>`; empty asks the key what
    workspaces it can see, which is again the handover case.

    Comet is the odd one on auth: the key goes in `Authorization` bare, with no
    `Bearer` scheme. Getting that wrong returns 401, which this reports as an auth
    blocker — correct either way, but worth knowing it is the likely cause.

    STATUS: exercised against a stub server only. REST v2's workspaces/projects/
    experiments endpoints are documented and stable.
    """
    key = (os.environ.get("COMET_API_KEY")
           or config_value("~/.comet.config", ("api_key",)).get("api_key", ""))
    if not key:
        return ("unreachable",
                f"found {where} but could not read an api_key out of it — "
                f"the file exists, the key does not, and the workspace is not "
                f"implicated", None,
                {"blocker": "tracking:comet:no_key_value"})
    base = (os.environ.get("COMET_URL_OVERRIDE", "").rstrip("/")
            or "https://www.comet.com").rstrip("/")
    if base.endswith("/clientlib"):          # the SDK's form of the same setting
        base = base[: -len("/clientlib")]
    rest = f"{base}/api/rest/v2"
    auth = {"Authorization": key}

    parts = [p for p in path.strip("/").split("/") if p]
    workspace = parts[0] if parts else None
    want_project = parts[1] if len(parts) > 1 else None

    if workspace is None:
        code, body, err = http_json(f"{rest}/workspaces", None,
                                    timeout=max(20.0, budget_s), headers=auth)
        if code is None:
            return ("unreachable", f"could not reach {base}: {err}", None,
                    {"blocker": "tracking:comet:unreachable_host"})
        wss = (body or {}).get("workspaceNames")
        if code in (401, 403):
            return ("unreachable",
                    f"{base} answered {code} — the key is the blocker, not "
                    f"absence (credential from {where})", None,
                    {"blocker": f"tracking:comet:http_{code}"})
        if code != 200 or wss is None:
            return ("unreachable",
                    f"{rest}/workspaces answered {code} "
                    f"({last_meaningful(err or '', 160) or 'unexpected body'}) — "
                    f"NOTHING WAS COUNTED", None,
                    {"blocker": f"tracking:comet:list_{code}"})
        if not wss:
            return ("gone", f"{base} answered and the key sees no workspaces",
                    None, {"blocker": None})
        return ("verified",
                f"{len(wss)} workspace(s) visible at {base} (credential from "
                f"{where}). Record each workspace you care about as its own lead "
                f"to get project and experiment counts. This verifies the RECORD "
                f"exists, not any number in it", wss[:40], {})

    code, body, err = http_json(f"{rest}/projects?workspaceName={workspace}", None,
                                timeout=max(20.0, budget_s), headers=auth)
    if code is None:
        return ("unreachable", f"could not reach {base}: {err}", None,
                {"blocker": "tracking:comet:unreachable_host"})
    if code in (401, 403):
        return ("unreachable",
                f"{base} answered {code} — the key is the blocker, not absence "
                f"(credential from {where})", None,
                {"blocker": f"tracking:comet:http_{code}"})
    projects = (body or {}).get("projects")
    if code != 200 or projects is None:
        return ("unreachable",
                f"{rest}/projects answered {code} "
                f"({last_meaningful(err or '', 160) or 'unexpected body shape'}) "
                f"— NOTHING WAS COUNTED", None,
                {"blocker": f"tracking:comet:list_{code}"})
    if want_project is not None:
        projects = [p for p in projects if p.get("projectName") == want_project]
        if not projects:
            return ("gone",
                    f"workspace {workspace!r} listed and holds no project named "
                    f"{want_project!r}", None, {"blocker": None})
    if not projects:
        return ("gone", f"workspace {workspace!r} answered and holds no projects",
                None, {"blocker": None})

    total, counted, names = 0, 0, []
    for proj in projects[:20]:
        pid = proj.get("projectId")
        if not pid:
            continue
        code, body, _e = http_json(f"{rest}/experiments?projectId={pid}", None,
                                   timeout=max(20.0, budget_s), headers=auth)
        exps = (body or {}).get("experiments")
        if code != 200 or exps is None:
            continue
        counted += 1
        total += len(exps)
        names.extend(e.get("experimentKey", "?")[:8] for e in exps[:5])
    if counted == 0:
        return ("verified",
                f"{len(projects)} project(s) in workspace {workspace!r}; every "
                f"experiment listing failed so EXPERIMENTS ARE NOT COUNTED. "
                f"Credential from {where}. This verifies the RECORD exists, not "
                f"any number in it",
                [p.get("projectName") for p in projects[:20]], {})
    partial = "" if counted == len(projects[:20]) else (
        f" (counted {counted} of {len(projects)} project(s) — the rest did not "
        f"answer, so this is a LOWER BOUND)")
    return ("verified",
            f"{len(projects)} project(s), {total} experiment(s) in workspace "
            f"{workspace!r}{partial} (credential from {where}). This verifies the "
            f"RECORD exists, not any number in it — a logged metric is a claim "
            f"until /repro reproduces it", names[:40], {})


def probe_wandb_entity(entity, where, budget_s):
    """What can this credential see? Returns the project list as the sample.

    `verified` when the entity resolves and holds projects — the RECORD exists.
    An entity with no projects is `gone` on the same bar as an empty S3 prefix:
    it answered, and there is nothing in it.
    """
    try:
        p = subprocess.run(
            [sys.executable, "-c", WANDB_ENTITY_LISTING % (entity or None)],
            capture_output=True, text=True, timeout=max(60.0, budget_s * 2))
    except (subprocess.TimeoutExpired, OSError) as exc:
        return ("unreachable", f"{type(exc).__name__}: {exc}", None,
                {"blocker": "tracking:wandb:entity_probe_failed"})
    try:
        got = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ("unreachable",
                f"could not read the probe's output: "
                f"{last_meaningful(p.stderr or p.stdout, 200)}", None,
                {"blocker": "tracking:wandb:unreadable_output"})
    if got.get("e") == "no_pkg":
        return ("unreachable",
                "the wandb package is not installed in this interpreter — the "
                "entity may be perfectly fine", None,
                {"blocker": "tracking:wandb:no_package"})
    if got.get("e"):
        return ("unreachable", f"{got['e']}: {got['m']} (credential from {where})",
                None, {"blocker": "tracking:wandb:api_error"})
    ps = got.get("projects") or []
    ent = got.get("entity")
    if not ps:
        return ("gone", f"entity {ent!r} answered and holds no projects "
                f"(credential from {where})", None, {"blocker": None})
    return ("verified",
            f"entity {ent!r} holds {len(ps)} project(s), reachable with the "
            f"credential from {where}. Record each project you care about as its "
            f"own lead to get run counts. This verifies the RECORD exists, not "
            f"any number in it", ps[:40], {})


# The listings that need no package installed. Prefer one of these over `pkg`
# whenever a backend offers both: urllib runs on the bare interpreter a handover
# starts with, and — the reason it matters more — a REST listing is testable
# against a stub, so the probe can be exercised without an account. wandb is the
# only backend left on the package path because it publishes no REST surface of
# this kind.
REST_LISTINGS = {
    "mlflow_rest": lambda *a: probe_mlflow_rest(*a),
    "clearml_rest": lambda *a: probe_clearml_rest(*a),
    "neptune_rest": lambda *a: probe_neptune_rest(*a),
    "comet_rest": lambda *a: probe_comet_rest(*a),
}


def probe_tracking_disk(backend, spec, path, budget_s):
    """Count run-shaped things under a path. No credential, no import, no network.

    `gone` here is a real reading — the directory listed and holds no markers —
    which is the same bar probe_s3 sets for an empty prefix.
    """
    import glob
    root = os.path.expanduser(path)
    if not os.path.exists(root):
        return ("gone", f"no such path on this machine", None,
                {"blocker": None})
    if not os.path.isdir(root):
        return ("unreachable", f"{root} is not a directory", None,
                {"blocker": f"tracking:{backend}:not_a_dir"})
    hits, started = [], time.time()
    for marker in spec["markers"]:
        for m in glob.iglob(os.path.join(root, "**", marker), recursive=True):
            hits.append(m)
            if len(hits) >= 500 or time.time() - started > budget_s:
                break
        if hits:
            break
    if not hits:
        return ("gone",
                f"the directory listed and holds no {spec['what']} — checked "
                f"{', '.join(spec['markers'])}", None, {"blocker": None})
    runs = sorted({os.path.dirname(h) for h in hits})
    return ("verified",
            f"{len(runs)} run dir(s) holding {spec['what']}, no credential "
            f"needed. This verifies the RECORD exists, not any number in it",
            [os.path.relpath(r, root) for r in runs[:20]], {"files": len(hits)})


def probe_tracking(on, path, budget_s):
    """Does this tracking project exist, and how much history does it hold.

    The count is the point. "How much of the previous owner's history is still
    there" is what a handover needs answered, and it is exactly what stops being
    answerable when an account is deactivated — so it is worth a probe that runs
    before access arrives rather than a note to check later.

    `verified` means THE RECORD EXISTS. It says nothing about any number inside:
    a run summary reporting mAP 48.5 is a machine-made assertion, still `claimed`
    in `origin.confidence` terms, and only a closed /repro session moves it. Two
    words spelled the same with opposite bars, so every detail says so out loud.

    WHAT HAS ACTUALLY RUN, because "built" and "ran once" are different facts:
    the whole disk family; the credential staging for every service backend; all
    four REST listings against stub servers (mlflow, clearml, neptune, comet —
    urllib only, which is exactly what makes them stubbable); and wandb's listing,
    both forms, against a real account — 25 runs on one project and a 10-project
    entity listing, both matching the raw API. Against a REAL server: mlflow and
    wandb only. The other three are stub-exercised, so a live endpoint that
    disagrees with the documented shape lands as `unreachable` carrying the code
    and the URL — one line to fix, and never a false `gone`.
    """
    backend = on.split(":", 1)[1] if ":" in on else ""
    spec = TRACKING.get(backend)
    if spec is None:
        return ("unreachable",
                f"unknown tracking backend {backend!r} — the locator is recorded "
                f"and readable by hand. Do NOT read this as absent", None,
                {"blocker": f"tracking:{backend}:unknown"})

    # Tolerate `wandb:entity/project` as well as `entity/project`: the locator is
    # usually copied out of a config where it carries the prefix, and otherwise
    # the report renders `tracking:wandb:wandb:ent/proj`.
    loc = path.split(":", 1)[1] if path.lower().startswith(backend + ":") else path

    if spec["family"] == "disk":
        return probe_tracking_disk(backend, spec, loc, budget_s)

    have, where = credential_present(spec)
    if not have:
        return ("unreachable", f"{where} — this is a credential lead, not an "
                f"absence", None, {"blocker": f"tracking:{backend}:no_credential"})

    rest = REST_LISTINGS.get(spec.get("listing"))
    if rest is not None:
        return rest(loc, where, budget_s)

    if spec.get("listing") != "wandb":
        # Credential found, no listing adapter. Both halves stated: what IS known
        # (there is a key) and what is not (nobody counted the runs). Reporting
        # only the second would lose the first, which is the actionable half.
        return ("unreachable",
                f"credential found ({where}) but no listing adapter for "
                f"{backend!r} — the history is reachable BY HAND and was not "
                f"counted. Do NOT read this as absent", None,
                {"blocker": f"tracking:{backend}:no_listing_adapter"})

    loc = loc.strip("/")
    if loc.count("/") > 1:
        return ("unreachable", f"cannot parse {path!r} as <entity>[/<project>]",
                None, {"blocker": f"tracking:{backend}:bad_locator"})
    if loc.count("/") == 0:
        # No project named — ask the credential what it can see. This is the
        # handover shape: you inherit a key and nobody wrote down the project
        # names. A probe that demanded `<entity>/<project>` here would refuse the
        # one case where discovery is most needed.
        return probe_wandb_entity(loc, where, budget_s)
    try:
        p = subprocess.run([sys.executable, "-c", WANDB_LISTING % loc],
                           capture_output=True, text=True,
                           timeout=max(60.0, budget_s * 2))
    except subprocess.TimeoutExpired:
        return ("unreachable", f"the {backend} API did not answer within the "
                f"budget", None, {"blocker": f"tracking:{backend}:timeout"})
    except OSError as exc:
        return ("unreachable", f"{type(exc).__name__}: {exc}", None,
                {"blocker": f"tracking:{backend}:spawn_failed"})
    try:
        got = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ("unreachable",
                f"could not read the probe's output: "
                f"{last_meaningful(p.stderr or p.stdout, 200)}", None,
                {"blocker": f"tracking:{backend}:unreadable_output"})

    if got.get("e") == "no_pkg":
        return ("unreachable",
                f"the {spec['pkg']} package is not installed in this interpreter "
                f"— the project may be perfectly fine", None,
                {"blocker": f"tracking:{backend}:no_package"})
    if got.get("e"):
        low = (got.get("m") or "").lower()
        # Same discipline as probe_s3: only not-found wording may mean gone.
        if any(k in low for k in ("could not find", "does not exist", "not found",
                                  "no project", "404")):
            return "gone", f"{got['e']}: {got['m']}", None, {"blocker": None}
        return ("unreachable", f"{got['e']}: {got['m']} (credential from {where})",
                None, {"blocker": f"tracking:{backend}:api_error"})

    n = got.get("n")
    if not n:
        return ("gone", f"the project listed successfully and holds no runs "
                f"(credential from {where})", None, {"blocker": None})
    return ("verified",
            f"{n} run(s), reachable with the credential from {where}. This "
            f"verifies the RECORD exists, not any number in it — a run summary "
            f"is a claim until /repro reproduces it",
            got.get("names") or None, {})


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
        kind = classify_on(on)
        if on == "local":
            status, detail, sample, size = probe_local(lead["path"], a.budget_seconds)
        elif on == "s3":
            status, detail, sample, size = probe_s3(lead["path"], a.budget_seconds)
        elif on.startswith("tracking:"):
            status, detail, sample, size = probe_tracking(
                on, lead["path"], a.budget_seconds)
        elif on.startswith("server:"):
            status, detail, sample, size = probe_server(
                on.split(":", 1)[1], lead["path"], res, a.budget_seconds)
        elif kind == "ask":
            # Leave the status alone. Overwriting `claim` with `unreachable` here
            # would say "we could not look" about something a person can simply
            # answer, and would move the lead out of the set /ask-human works on.
            status, detail, sample, size = (
                lead["status"],
                f"`{on}` is not machine-probeable — somebody has to answer it. "
                f"/ask-human, and their answer is a claim until a probe agrees",
                None, {})
        else:
            # Unreachable in the most literal sense: this code cannot reach it,
            # and saying so is better than guessing a probe.
            status, detail, sample, size = (
                "unreachable", f"unknown `on` value {on!r}; no probe dispatched",
                None, {})
        lead["probes"].append({"at": now_utc(), "result": status, "detail": detail,
                               "sample": sample, **size})
        lead["status"] = status
        # Carried onto the lead so the table can be drawn without re-walking
        # anything. `null` means not measured; it never means zero.
        lead["bytes"] = size.get("bytes")
        lead["files"] = size.get("files")
        lead["size_not_measured"] = size.get("not_measured")
        results.append({"lead_id": lead["lead_id"], "path": lead["path"],
                        # The sample IS the payload for some probes — a tracking
                        # entity's project names are the answer, not a garnish —
                        # so emit it rather than only persisting it.
                        "sample": sample,
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


def cmd_reconcile(a) -> None:
    """Join the leads against a stage's candidates, both directions.

    Reports; never writes. `candidates` is filled by the stage's init skill with
    the user confirming each entry, and a discovery script reaching into a
    stage's config to "fix" it would make that confirmation a formality. The
    skill acts on this output.

    Two directions, and the second is the item-driven half of discovery:

      drift     a candidate whose `match` its lead's status does not permit.
                The one that matters is `ok` on a lead nothing has checked.
      coverage  a declared item with no usable candidate AND no lead looking for
                it — a need nothing is searching for, which is invisible today
                because an item with no candidates looks the same as an item
                whose candidates all failed.
    """
    project = os.path.expanduser(a.project)
    if not os.path.isdir(project):
        broke(f"project not found: {project}")
    rec = read_json(leads_path(project), required=False)
    leads = {l["lead_id"]: l for l in ((rec or {}).get("leads") or [])}
    entries, declared = stage_candidates(project, a.stage)
    if not entries and not declared:
        broke(f"stage {a.stage!r} has no {' or '.join(CANDIDATE_FILES)} with "
              f"items or candidates",
              looked_in=os.path.join(project, "stages", a.stage))

    drift, unlinked, linked_leads = [], [], set()
    for fname, name, idx, entry in entries:
        lid = entry.get("lead_id")
        match = entry.get("match")
        if not lid:
            # Not an error: `code_default` and `downloadable` entries are derived
            # from the code, not from a sweep, and have no lead by design.
            unlinked.append({"file": fname, "item": name, "index": idx,
                             "location": entry.get("location"),
                             "match": match, "path": entry.get("path")})
            continue
        linked_leads.add(lid)
        lead = leads.get(lid)
        if lead is None:
            drift.append({"file": fname, "item": name, "index": idx,
                          "lead_id": lid, "match": match,
                          "problem": "cites a lead_id that is not in leads.json",
                          "fix": "the lead was deleted or the id was mistyped; "
                                 "re-record it or drop the reference"})
            continue
        allowed = MATCH_FOR_STATUS.get(lead["status"], ())
        if match not in allowed:
            drift.append({
                "file": fname, "item": name, "index": idx, "lead_id": lid,
                "match": match, "lead_status": lead["status"],
                "allowed": list(allowed),
                "path": entry.get("path") or lead.get("path"),
                "problem": f"candidate says {match!r} but its lead is "
                           f"{lead['status']!r}",
                "fix": ("a candidate is never `ok` on a lead nothing has checked — "
                        "probe the lead first"
                        if lead["status"] == "claim" and match == "ok" else
                        f"set match to one of {list(allowed)}, or re-probe the lead"),
            })

    usable = {}
    for _f, name, _i, entry in entries:
        usable[name] = usable.get(name, False) or entry.get("match") == "ok"
    covered_paths = {l["path"] for lid, l in leads.items() if lid in linked_leads}
    coverage = [
        {"item": name, "declared_in": declared[name],
         "problem": "no candidate with match `ok` and no lead linked to it",
         "fix": f"discover.py record --project {a.project} --path <where it might "
                f"be> ... then link the lead_id into the candidate entry"}
        for name in sorted(declared)
        if not usable.get(name)
        and not any(e[1] == name and e[3].get("lead_id") for e in entries)
    ]

    out = {"project": project, "stage": a.stage,
           "checked": {"candidate_entries": len(entries),
                       "declared_items": len(declared),
                       "leads_linked": len(linked_leads)},
           # Gaps before findings, same rule as `report` and a partial census.
           "coverage_gaps": coverage,
           "drift": drift,
           "unlinked_candidates": unlinked,
           "note": "unlinked is normal for `code_default` and `downloadable` — "
                   "those come from the code, not from a sweep"}
    if drift or coverage:
        emit(out)
        sys.exit(1)
    emit(out | {"consistent": True})


def access_worklist(leads):
    """The distinct things somebody has to go and get, most-blocking first.

    Every `unreachable` names what was missing, so this set already exists;
    aggregating it is the one output actionable without reading a single lead —
    which key to obtain, and how many leads it unblocks.
    """
    groups = {}
    for l in leads:
        if l["status"] != "unreachable":
            continue
        last = (l.get("probes") or [{}])[-1]
        key = (l.get("blocker") or last.get("blocker")
               or (last.get("detail") or "")[:120] or "unknown")
        g = groups.setdefault(key, {"blocker": key, "blocks": 0,
                                    "example": last.get("detail"), "leads": []})
        g["blocks"] += 1
        g["leads"].append(l["lead_id"])
    return sorted(groups.values(), key=lambda e: (-e["blocks"], e["blocker"]))


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
        # Every `unreachable` names what was missing, so the distinct reasons
        # ARE the access worklist. Aggregated here rather than left implicit
        # because it is the one output somebody can act on without reading any
        # lead: it says which key to go and get, and what it unblocks.
        # Keyed on the blocker each probe ASSERTS, never on its prose. Grouping
        # on the detail string put one AccessDenied on three buckets into three
        # rows, which is the opposite of what a worklist is for.
        "access_worklist": access_worklist(leads),
        "not_checked": {
            # Before any count, because a lead whose access expires next week
            # is not a todo that can wait for the next sweep.
            "access_expiring_soon": [
                slim(l) | {"access_expires_at": l.get("access_expires_at"),
                           "days_left": expiry_state(l, a.expiring_soon_days)[1]}
                for l in leads
                if expiry_state(l, a.expiring_soon_days)[0] == "expiring_soon"],
            "access_expired_and_unresolved": [
                slim(l) | {"access_expires_at": l.get("access_expires_at"),
                           "why_it_matters": "access is gone and the lead was "
                                             "never resolved — this is the "
                                             "transition nothing else observes"}
                for l in leads
                if expiry_state(l, a.expiring_soon_days)[0] == "expired"
                and l["status"] in ("claim", "unreachable")],
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
                   help="local | s3 | server:<key> | tracking:<backend> | doc | "
                        "person | cloud_console  (inferred from the path if omitted)")
    r.add_argument("--access-expires-at", dest="access_expires_at", default=None,
                   metavar="ISO8601",
                   help="the date this source stops being resolvable at all — a "
                        "departing account, a personal wiki space, a key pending "
                        "rotation. Distinct from staleness: `unreachable` here "
                        "means come back BEFORE this date, not after it")
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

    rc = sub.add_parser("reconcile",
                        help="join leads against a stage's candidates, both ways")
    rc.add_argument("--project", required=True)
    rc.add_argument("--stage", required=True,
                    help="training | evaluation | inference | ...")
    rc.set_defaults(fn=cmd_reconcile)

    rp = sub.add_parser("report", help="the table: what, where, how big, how many")
    rp.add_argument("--project", required=True)
    rp.add_argument("--expiring-soon-days", dest="expiring_soon_days",
                    type=float, default=DEFAULT_EXPIRING_SOON_DAYS)
    rp.add_argument("--recheck-days", type=float, default=DEFAULT_RECHECK_DAYS)
    rp.add_argument("--json", action="store_true",
                    help="machine form; default is the table")
    rp.set_defaults(fn=cmd_report)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
