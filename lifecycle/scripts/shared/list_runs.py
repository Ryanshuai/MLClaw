#!/usr/bin/env python3
"""Canonical run listing for MLClaw — the `mode` filter cannot be forgotten.

`run.json -> mode` is a correctness filter, not a display field (lifecycle/references/run-mechanics.md
"Metric comparability"): a debug mAP over 20 images and a production mAP over
5000 are different quantities sharing a name. Mixing them produces a *fake
comparison* — nothing errors, no data is missing, and a wrong conclusion gets
drawn from correctly-recorded numbers. That rule used to live as loose `jq`
snippets retyped per call site; forget `mode == "production"` once and the
leaderboard is silently wrong. It is written down here once instead.

The contract
------------
1. `mode` is a REQUIRED keyword-only argument of `query_comparable_runs()` and
   a REQUIRED CLI flag — no default, no "all"/"any" value. Omitting it is a
   TypeError at the call site; an unrecognized value raises rather than quietly
   matching nothing.
2. Every mode at once is legitimate for a listing that never compares (a menu,
   an inventory). That is a separate, loudly named affordance:
   `list_all_modes_not_comparable()` / `--all-modes-not-comparable`. Its result
   is `comparable: false` and every entry carries `not_comparable_reason`. You
   reach it by name, never by omission.
3. Runs with `mode: null` never silently pass and never silently vanish — both
   entry points move them to `excluded` with a reason.
4. `mode` alone is insufficient: two production runs over different sample
   counts are not comparable either. Entries carry a `scope_key` from
   `compare.scope_key` (the repo-wide definition of scope equivalence) and the
   result reports `distinct_scopes`. `comparable` is true only when the mode
   was filtered and all matched runs share one *specified* scope. To work
   within one scope, filter the returned entries by `scope_key`.
5. A malformed `run.json` becomes an `errors` entry, never a dead scan.

No cache (lifecycle/references/run-mechanics.md "Listing runs (no separate index)") — the `run.json` files
are the source of truth and are rescanned per call.

    query_comparable_runs(root, *, mode, ...)    -> result   # the main door
    list_all_modes_not_comparable(root, ...)     -> result   # escape hatch
    tune_comparable_runs(root, code_commit, ...) -> result   # 4-condition bundle

The result keys are the dict literal returned by `_gather`; the per-entry keys
are the dict literal built by `project_run`.

`root` may be a project root (has `stages/`), a stage dir (has `runs/`), a
`runs/` dir, a run dir, or one `run.json`. Failures exit 2 with a one-line
message, never a traceback — a broken script means "fall back and do it by
hand" (CLAUDE.md "Script Integration").
"""
import argparse
import json
import os
import sys
from datetime import datetime
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _records import parse_iso  # noqa: E402
from compare import UNSPECIFIED_SCOPE, scope_key  # noqa: E402  (sibling module)

# Known values of run.json -> mode. Extend here (one place) if the schema grows;
# do not loosen the check at a call site.
MODES = ("production", "debug", "screen")
SORT_KEYS = ("created_at", "run_id", "primary_metric", "duration_s")

# The only exclusion reason: a run that never recorded its mode belongs to no
# comparable population, whether or not it carries numbers.
MODE_NULL = "mode_null"
MODE_NULL_DETAIL = (
    "mode is null — this run's numbers describe an unknown workload and cannot "
    "be compared, ranked, or aggregated. The run skill should set "
    "run.json -> mode at launch."
)


class RunQueryError(Exception):
    """A condition that makes an honest query impossible."""


# ── loading ──────────────────────────────────────────────────────────────────

def _expand(path):
    return os.path.abspath(os.path.expanduser(str(path)))


def discover_run_files(root, stage=None):
    """Sorted absolute run.json paths under `root` (any of the five shapes).

    `stage` narrows a project root; combining it with a non-project root is an
    error rather than a silently ignored argument.
    """
    root = _expand(root)
    if os.path.isfile(root):
        if stage:
            raise RunQueryError("stage cannot be combined with a direct run.json path")
        return [root]
    if not os.path.isdir(root):
        raise RunQueryError("no such directory: " + root)

    stages_dir = os.path.join(root, "stages")
    if os.path.isdir(stages_dir):
        if stage and not os.path.isdir(os.path.join(stages_dir, stage)):
            raise RunQueryError("stage '%s' not found under %s" % (stage, stages_dir))
        patterns = [os.path.join(stages_dir, stage or "*", "runs", "*", "run.json")]
    elif os.path.isdir(os.path.join(root, "runs")):
        if stage and os.path.basename(root) != stage:
            raise RunQueryError(
                "root %s is the stage dir '%s', which does not match stage '%s'"
                % (root, os.path.basename(root), stage))
        patterns = [os.path.join(root, "runs", "*", "run.json")]
    else:
        if stage:
            raise RunQueryError(
                "stage needs a project root containing stages/; %s is not one" % root)
        patterns = [os.path.join(root, "*", "run.json"),      # a runs/ dir
                    os.path.join(root, "run.json")]           # a single run dir

    found = {os.path.abspath(p) for pat in patterns for p in glob(pat)
             if os.path.isfile(p)}
    return sorted(found)


def load_run_file(path):
    """Load one run.json into a dict. Raises RunQueryError with a short reason."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:            # JSONDecodeError
        raise RunQueryError("invalid JSON: %s" % exc)
    except (OSError, UnicodeDecodeError) as exc:
        raise RunQueryError("%s: %s" % (type(exc).__name__, exc))
    if not isinstance(data, dict):
        raise RunQueryError("top-level JSON is %s, expected an object"
                            % type(data).__name__)
    return data


def scan(root, stage=None):
    """-> ([(path, dict)], [{"path", "error"}]). One bad file never aborts."""
    runs, errors = [], []
    for path in discover_run_files(root, stage):
        try:
            runs.append((path, load_run_file(path)))
        except RunQueryError as exc:
            errors.append({"path": path, "error": str(exc)})
    return runs, errors


# ── projection ───────────────────────────────────────────────────────────────

def _dig(obj, *keys):
    """Nested get that tolerates missing keys and non-dict intermediates."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _stage_from_path(run_json_path):
    # .../stages/<stage>/runs/<run_id>/run.json
    parts = os.path.normpath(_expand(run_json_path)).split(os.sep)
    return parts[-4] if len(parts) >= 4 and parts[-3] == "runs" else ""


def project_run(run, run_json_path):
    """Project one run.json dict to the canonical listing entry."""
    run_dir = os.path.dirname(_expand(run_json_path))
    stage = run.get("stage") or _stage_from_path(run_json_path)
    run_id = run.get("run_id") or os.path.basename(run_dir)
    scope = run.get("scope")
    if not isinstance(scope, dict):      # a malformed scope is an unknown,
        scope = {}                       # not an equivalence class
    return {
        "run_id": run_id,
        "alias": run.get("alias") or None,
        "stage": stage or None,
        "status": run.get("status"),
        "mode": run.get("mode"),
        "scope": scope,
        "scope_key": scope_key(scope),
        "duration_s": run.get("duration_s"),
        "primary_metric": _dig(run, "metrics", "best", "primary_metric_value"),
        "primary_metric_name": _dig(run, "metrics", "best", "primary_metric"),
        "path": "stages/%s/runs/%s" % (stage, run_id) if stage else run_dir,
        "run_dir": run_dir,
        "run_json": _expand(run_json_path),
        "session": _dig(run, "lineage", "session"),
        "origin_commit": _dig(run, "code", "origin_commit"),
        "created_at": run.get("created_at") or "",
    }


# ── sorting ──────────────────────────────────────────────────────────────────

def _created_at_key(value):
    """Sort a timestamp by its instant, not its characters.

    Raw ISO strings do not string-sort correctly across writers: a `Z`-suffixed
    value sorts after `+00:00` whatever the actual time, and runs written before
    this helper existed are naive. Unparseable values fall back to their raw
    string and order among themselves, after the parsed ones when descending.
    """
    try:
        return (1, parse_iso(value)[0].timestamp(), "")
    except (ValueError, TypeError, OSError, OverflowError):
        return (0, 0.0, value)


def _sort_value(entry, sort_by):
    """-> (present, key). Entries missing the key always sort last."""
    value = entry.get(sort_by)
    if sort_by in ("primary_metric", "duration_s"):
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        return (True, value) if numeric else (False, None)
    if not isinstance(value, str) or not value:
        return (False, None)
    return (True, _created_at_key(value) if sort_by == "created_at" else value)


def sort_entries(entries, sort_by="created_at", descending=True):
    """Sort projected entries. Missing values go last in BOTH directions —
    a run with no metric must never head a leaderboard."""
    if sort_by not in SORT_KEYS:
        raise RunQueryError("unknown sort key '%s' — expected one of %s"
                            % (sort_by, "|".join(SORT_KEYS)))
    present, missing = [], []
    for entry in entries:
        ok, key = _sort_value(entry, sort_by)
        (present if ok else missing).append((key, entry))
    present.sort(key=lambda pair: pair[0], reverse=descending)
    return [e for _, e in present] + [e for _, e in missing]


# ── the query ────────────────────────────────────────────────────────────────

def _match_status(entry, status):
    if status is None:                                   # unfiltered
        return True
    if isinstance(status, (list, tuple, set)):
        return entry.get("status") in status
    return entry.get("status") == status


def _gather(root, stage, mode, status, session, code_commit,
            sort_by, descending, limit):
    """The engine. `mode` is a value from MODES, or None for "every mode".

    None reaches here only from list_all_modes_not_comparable(), and it is also
    exactly the condition under which the result is not comparable — so there is
    no second flag repeating it.
    """
    runs, errors = scan(root, stage)

    matched, excluded = [], []
    for path, run in runs:
        entry = project_run(run, path)
        if entry["mode"] is None:
            # An integrity problem, not a filter miss — surfaced, never dropped.
            entry["exclusion_reason"] = MODE_NULL
            entry["exclusion_detail"] = MODE_NULL_DETAIL
            excluded.append(entry)
        elif ((mode is None or entry["mode"] == mode)
                and _match_status(entry, status)
                and (session == "*" or entry["session"] == session)
                and (code_commit is None or entry["origin_commit"] == code_commit)):
            matched.append(entry)

    matched = sort_entries(matched, sort_by, descending)
    total_before_limit = len(matched)
    if limit is not None:
        if limit < 0:
            raise RunQueryError("limit must be >= 0")
        matched = matched[:limit]

    distinct_scopes = sorted({e["scope_key"] for e in matched})
    scope_uniform = (len(distinct_scopes) == 1
                     and distinct_scopes[0] != UNSPECIFIED_SCOPE)

    warnings = []
    if excluded:
        warnings.append(
            "%d run(s) excluded (mode is null). Set run.json -> mode at launch; "
            "without it a run's numbers belong to no comparable population."
            % len(excluded))
    if len(distinct_scopes) > 1:
        warnings.append(
            "matched runs span %d distinct scopes — metrics are comparable only "
            "within one. Filter the entries by scope_key." % len(distinct_scopes))
    if UNSPECIFIED_SCOPE in distinct_scopes:
        warnings.append(
            "%d matched run(s) have an empty scope — comparability with the rest "
            "is unverifiable, not established."
            % sum(1 for e in matched if e["scope_key"] == UNSPECIFIED_SCOPE))

    return {
        "root": _expand(root),
        "stage": stage or "*",
        "comparable": mode is not None and scope_uniform,
        "filters": {"mode": mode, "stage": stage, "status": status,
                    "session": session, "code_commit": code_commit,
                    "sort_by": sort_by, "descending": descending, "limit": limit},
        "scanned_count": len(runs),
        "matched": matched,
        "matched_count": len(matched),
        "matched_total_before_limit": total_before_limit,
        "excluded": excluded,
        "excluded_count": len(excluded),
        "filtered_out_count": len(runs) - total_before_limit - len(excluded),
        "distinct_scopes": distinct_scopes,
        "errors": errors,
        "warnings": warnings,
    }


def query_comparable_runs(root, *, mode, stage=None, status="completed",
                          session="*", code_commit=None,
                          sort_by="created_at", descending=True, limit=None):
    """List runs that are comparable to each other. `mode` is required.

    Keyword-only with no default: omitting `mode` is a TypeError at the call
    site, not a permissive "all runs" fallback. A default here is exactly how
    debug and production metrics end up in one ranking.

        mode         one of MODES; no "all" value exists — use
                     list_all_modes_not_comparable() for a raw mixed listing
        stage        narrows a project root, e.g. "training"
        status       "completed" (default) | a list of statuses | None for any
        session      "*" (default, any) | None (ad-hoc only) | "<session id>"
        code_commit  exact run.json -> code.origin_commit
        sort_by      one of SORT_KEYS; missing values always sort last
        limit        top-N after sorting

    Check `result["comparable"]` before ranking: false when the matched runs
    span more than one scope, or their scope is unspecified.
    """
    if mode is None or (isinstance(mode, str) and mode.lower() in ("all", "any", "*")):
        raise RunQueryError(
            "mode is required and has no 'all' value — mixing modes is what this "
            "function prevents. Pass mode=%s, or call "
            "list_all_modes_not_comparable() for a listing that never compares."
            % "|".join('"%s"' % m for m in MODES))
    if mode not in MODES:
        raise RunQueryError("unknown mode '%s' — expected one of %s"
                            % (mode, "|".join(MODES)))
    return _gather(root, stage, mode, status, session, code_commit,
                   sort_by, descending, limit)


def list_all_modes_not_comparable(root, stage=None, status=None, session="*",
                                  code_commit=None, sort_by="created_at",
                                  descending=True, limit=None):
    """Raw listing across ALL modes. The result is NOT comparable — by name.

    Legitimate: a menu of recent runs, an inventory, "what is in this project".
    Illegitimate: any leaderboard, baseline diff, best-so-far curve, aggregate.
    The result is `comparable: false`, carries a loud warning, and tags every
    entry with `not_comparable_reason`, so a mixed population cannot be mistaken
    downstream for a ranked one.
    """
    result = _gather(root, stage, None, status, session, code_commit,
                     sort_by, descending, limit)
    mixed = "/".join(MODES)
    for entry in result["matched"]:
        entry["not_comparable_reason"] = (
            "listed with the mode filter off — this population mixes %s "
            "workloads" % mixed)
    result["filters"]["mode"] = "ALL_MODES_NOT_COMPARABLE"
    result["warnings"].insert(0, (
        "MODE FILTER OFF — these entries mix %s workloads. Use for listing and "
        "menus only; do not rank, diff, or aggregate their metrics. See "
        "lifecycle/references/run-mechanics.md 'Metric comparability'." % mixed))
    return result


def tune_comparable_runs(root, code_commit, stage="training",
                         sort_by="created_at", descending=True, limit=None):
    """Runs comparable for /train-tune observation.

    Four conditions that must all hold and are each easy to forget on their
    own: production mode, status completed, the same code SHA, and ad-hoc only
    (`lineage.session is None`) — so a previous tune session's trials do not
    contaminate the population the next hypothesis is measured against.
    """
    return query_comparable_runs(root, mode="production", stage=stage,
                                 status="completed", session=None,
                                 code_commit=code_commit, sort_by=sort_by,
                                 descending=descending, limit=limit)


# ── CLI ──────────────────────────────────────────────────────────────────────

_EPILOG = """\
canonical queries:
  # all completed production runs in a stage
  list_runs.py <project> --stage training --mode production

  # runs comparable for /train-tune (same SHA, ad-hoc only, full scale)
  list_runs.py <project> --stage training --mode production --commit $SHA --no-session

  # most recent 10 runs for a menu (mixed modes, NOT comparable)
  list_runs.py <project> --all-modes-not-comparable --status any --limit 10

Exactly one of --mode / --all-modes-not-comparable is required: there is no
default mode, and no way to mix debug and production metrics by omission.
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="list_runs.py", epilog=_EPILOG,
        description="Canonical MLClaw run listing — the mode filter cannot be forgotten.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root", help="project root, stage dir, runs dir, run dir, or run.json")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mode", choices=list(MODES),
                      help="required for any comparison — metrics mean the same thing only within one mode")
    mode.add_argument("--all-modes-not-comparable", action="store_true",
                      help="raw listing across all modes; result is marked not comparable")
    p.add_argument("--stage", default=None, help="narrow a project root, e.g. training")
    p.add_argument("--status", default="completed",
                   help="status to keep, 'any' for all (default: completed)")
    session = p.add_mutually_exclusive_group()
    session.add_argument("--session", default=None, help="only runs in this tune session")
    session.add_argument("--no-session", action="store_true",
                         help="only ad-hoc runs (lineage.session is null)")
    p.add_argument("--commit", default=None, help="exact code.origin_commit")
    p.add_argument("--sort", choices=list(SORT_KEYS), default="created_at")
    p.add_argument("--asc", action="store_true", help="ascending (default: descending)")
    p.add_argument("--limit", type=int, default=None, help="top-N after sorting")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    status = None if str(args.status).lower() in ("any", "*") else args.status
    session = None if args.no_session else (args.session or "*")
    common = dict(stage=args.stage, status=status, session=session,
                  code_commit=args.commit, sort_by=args.sort,
                  descending=not args.asc, limit=args.limit)

    if args.all_modes_not_comparable:
        result = list_all_modes_not_comparable(args.root, **common)
    else:
        result = query_comparable_runs(args.root, mode=args.mode, **common)

    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    for err in result["errors"]:
        sys.stderr.write("list_runs: skipped %s: %s\n" % (err["path"], err["error"]))
    for warning in result["warnings"]:
        sys.stderr.write("list_runs: warning: %s\n" % warning)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RunQueryError as exc:
        sys.stderr.write("list_runs: %s\n" % exc)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.stderr.write("list_runs: interrupted\n")
        sys.exit(130)
    except Exception as exc:  # the script broke — fall back and do it by hand
        sys.stderr.write("list_runs: %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(2)
