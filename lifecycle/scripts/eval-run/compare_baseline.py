#!/usr/bin/env python3
"""Compare a completed eval run's metrics against its recorded baseline.

Called from /eval-run Step 4.5. Two things make this more than a subtraction:

**Direction comes from the config, never from the metric's name.** A drop in
`loss` is an improvement; a drop in `mAP` is a regression; `latency_ms` and
`throughput` point opposite ways and both look like performance. This script
reads the direction out of `output.json` and, when a metric has none recorded,
reports the raw delta with `verdict: "unknown_direction"` instead of guessing.

**Comparability is checked before any delta is computed.** CLAUDE.md "Metric
comparability": a 20-image debug run and a 5000-image production baseline
produce correctly-recorded numbers whose difference means nothing, and
formatting that difference as `+2.5%` is what turns a mistake into a decision.
Nothing else in the system catches it. So the run and the baseline must agree
on `mode`, on an equivalent `scope`, and on the dataset (name + split);
otherwise no deltas are emitted at all.

    Blockers come in two grades:
      hard          — both sides known and different (debug vs production).
                      Cannot be overridden; there is nothing to confirm.
      unverifiable  — one side is missing or free text (a paper baseline whose
                      scope reads "COCO val2017, all 5000 images"). A human who
                      knows the two describe the same measurement can clear
                      these with --assert-comparable "<reason>", which is
                      recorded in the output.

Where direction is read from, in order:
    1. output.json -> metrics.definitions.<name>.direction   ("max" | "min")
    2. output.json -> metrics.direction, only for the metric named by
       output.json -> metrics.primary_metric
    Anything else -> unknown, reported as such.

Baseline argument accepts (see output.json -> metrics.baseline):
    * a run id            "run_20260316_153024" or "evaluation/run_20260316_153024"
    * a path              to a run.json, a run directory, or a JSON file holding
                          an external baseline object
    * inline JSON         '{"source": "...", "scope": ..., "metrics": {...}}'

Output: JSON on stdout.
Exit:   0 = comparable, deltas computed
        1 = NOT COMPARABLE. Per CLAUDE.md "Script Integration": the script
            worked and the answer is no. Do NOT fall back to computing the
            deltas by hand — hand-computing this exact delta is the failure the
            script exists to prevent. Report the blocking dimension instead.
        2 = script error (bad argument, unreadable file) — the script broke,
            fall back per CLAUDE.md "Script Integration".

Usage:
    python compare_baseline.py <run_json> <baseline>
        [--output-json <path>]          # default: <run_dir>/../../output.json
        [--assert-comparable "<reason>"]
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from compare import (UNSPECIFIED_SCOPE, norm_scalar, normalize,  # noqa: E402
                     normalize_direction, scope_key, scopes_equivalent,
                     values_equal)

# run.json -> metrics keys that are not scalar metrics.
NON_METRIC_KEYS = {"per_class", "best", "history", "curves", "confusion_matrix"}


class CompareError(Exception):
    """The script cannot run at all."""


def load_json_required(path, what="file"):
    """Absent OR unreadable -> raise. The strict one of three; the other two return
    None on absence (`validate_refs.load_json_absent_ok`) or on either
    (`validate_ground_truth.load_json_lenient`)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise CompareError("cannot read %s %s: %s" % (what, path, e))
    except ValueError as e:
        raise CompareError("%s %s is not valid JSON: %s" % (what, path, e))


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def scalar_metrics(blob):
    """Flat numeric metrics out of a `metrics` block. Returns (metrics, skipped)."""
    metrics, skipped = {}, []
    for k, v in (blob or {}).items():
        if k.startswith("_") or k in NON_METRIC_KEYS:
            skipped.append(k)
        elif is_number(v):
            metrics[k] = float(v)
        else:
            skipped.append(k)
    return metrics, skipped


# --------------------------------------------------------------------------
# loading the two sides
# --------------------------------------------------------------------------

def _run_dataset(run_dir, run):
    """dataset block for a run: frozen snapshot first, stage config as fallback."""
    snap = os.path.join(run_dir, "config_snapshot.json")
    if os.path.isfile(snap):
        try:
            d = (load_json_required(snap, "config snapshot") or {}).get("dataset")
            if d:
                return d, "config_snapshot.json"
        except CompareError:
            pass
    stage_cfg = os.path.join(os.path.dirname(os.path.dirname(run_dir)), "config.json")
    if os.path.isfile(stage_cfg):
        try:
            d = (load_json_required(stage_cfg, "stage config") or {}).get("dataset")
            if d:
                return d, "stage config.json"
        except CompareError:
            pass
    return (run or {}).get("dataset"), "run.json" if (run or {}).get("dataset") else None


def load_run_side(run_json_path, label):
    run_json_path = os.path.abspath(os.path.expanduser(run_json_path))
    if os.path.isdir(run_json_path):
        run_json_path = os.path.join(run_json_path, "run.json")
    if not os.path.isfile(run_json_path):
        raise CompareError("no %s run.json at %s" % (label, run_json_path))
    run = load_json_required(run_json_path, "%s run.json" % label)
    run_dir = os.path.dirname(run_json_path)
    dataset, dataset_from = _run_dataset(run_dir, run)
    metrics, skipped = scalar_metrics(run.get("metrics"))
    return {
        "kind": "run",
        "id": run.get("run_id") or os.path.basename(run_dir),
        "path": run_json_path,
        "status": run.get("status"),
        "mode": run.get("mode"),
        "scope": run.get("scope"),
        "dataset": dataset,
        "dataset_from": dataset_from,
        "metrics": metrics,
        "skipped_metric_keys": skipped,
    }


def _external_side(obj, origin):
    """An inline / file baseline: {source, scope, metrics, [mode], [dataset]}."""
    if not isinstance(obj, dict):
        raise CompareError("baseline must be a JSON object, got %s" % type(obj).__name__)
    if "metrics" not in obj:
        raise CompareError("baseline object from %s has no `metrics` key" % origin)
    metrics, skipped = scalar_metrics(obj.get("metrics"))
    return {
        "kind": "external",
        "id": obj.get("source") or origin,
        "path": None if origin == "inline JSON" else origin,
        "status": None,
        "mode": obj.get("mode"),
        "scope": obj.get("scope"),
        "dataset": obj.get("dataset"),
        "dataset_from": "baseline object" if obj.get("dataset") else None,
        "metrics": metrics,
        "skipped_metric_keys": skipped,
    }


def _looks_like_run_record(obj):
    return isinstance(obj, dict) and ("run_id" in obj or "steps" in obj)


def resolve_baseline(spec, run_json_path):
    """Turn the baseline argument into a normalized side dict."""
    spec = str(spec).strip()
    if spec.startswith("{"):
        try:
            return _external_side(json.loads(spec), "inline JSON")
        except ValueError as e:
            raise CompareError("baseline is not valid inline JSON: %s" % e)

    expanded = os.path.expanduser(spec)
    if os.path.isdir(expanded):
        return load_run_side(os.path.join(expanded, "run.json"), "baseline")
    if os.path.isfile(expanded):
        obj = load_json_required(expanded, "baseline")
        if _looks_like_run_record(obj):
            return load_run_side(expanded, "baseline")
        return _external_side(obj, os.path.abspath(expanded))

    # A run id, optionally stage-qualified: "evaluation/run_20260316_153024".
    run_dir = os.path.dirname(os.path.abspath(os.path.expanduser(run_json_path)))
    runs_dir = os.path.dirname(run_dir)
    stage_dir = os.path.dirname(runs_dir)
    project_root = os.path.dirname(os.path.dirname(stage_dir))

    stage, _, run_id = spec.rpartition("/")
    candidates = []
    if stage:
        candidates.append(os.path.join(project_root, "stages", stage, "runs", run_id, "run.json"))
    candidates.append(os.path.join(runs_dir, run_id, "run.json"))
    candidates.extend(sorted(glob.glob(
        os.path.join(project_root, "stages", "*", "runs", run_id, "run.json"))))
    for c in candidates:
        if os.path.isfile(c):
            return load_run_side(c, "baseline")
    raise CompareError(
        "cannot resolve baseline %r — not inline JSON, not a path, and no run.json "
        "found at: %s" % (spec, ", ".join(candidates)))


# --------------------------------------------------------------------------
# comparability  (run-mechanics.md "Metric comparability")
# --------------------------------------------------------------------------
# "Are these two values equivalent" lives in `shared/compare.py` and nowhere
# else. What stays here is the *grading* — which mismatch is a hard stop and
# which a human may clear — plus the external-baseline case: paper numbers
# carry a free-text `scope` and have no run.json, which a run-tree query has no
# reason to model.

HARD = "hard"
UNVERIFIABLE = "unverifiable"


def _compare_mode(a, b):
    if not a or not b:
        missing = "run" if not a else "baseline"
        return UNVERIFIABLE, ("%s has no `mode` recorded — a metric whose scale is "
                              "unknown cannot be compared. Confirm what it was run at."
                              % missing)
    if not values_equal(a, b):
        return HARD, ("mode differs: run=%s, baseline=%s. These describe different "
                      "workloads; their difference is not a result." % (a, b))
    return None, "both %s" % a


def _compare_scope(a, b):
    ka, kb = scope_key(a), scope_key(b)
    if UNSPECIFIED_SCOPE in (ka, kb):
        missing = "run" if ka == UNSPECIFIED_SCOPE else "baseline"
        return UNVERIFIABLE, ("%s has no `scope` recorded — what the numbers were "
                              "measured on is unknown." % missing)
    if scopes_equivalent(a, b):
        return None, ("identical free-text scope %r" % a if isinstance(a, str)
                      else "equivalent %s" % ka)
    if isinstance(a, dict) and isinstance(b, dict):
        # Which keys differ is computed on the normalized dicts (so a dropped
        # null or a `_comment` never shows up as a difference), but the message
        # quotes what the user actually wrote.
        na, nb = normalize(a), normalize(b)
        diff = sorted(k for k in set(na) | set(nb)
                      if not values_equal(na.get(k), nb.get(k)))
        return HARD, ("scope differs%s: run=%s, baseline=%s."
                      % (" on " + ", ".join(diff) if diff else "",
                         json.dumps(a, sort_keys=True, default=str),
                         json.dumps(b, sort_keys=True, default=str)))
    if isinstance(a, str) and isinstance(b, str):
        return UNVERIFIABLE, ("scopes are free text and differ textually: run=%r, "
                              "baseline=%r. A human must confirm they describe the "
                              "same measurement." % (a, b))
    return UNVERIFIABLE, ("scope shapes differ (run=%s, baseline=%s) — cannot be "
                          "checked mechanically."
                          % (json.dumps(a, default=str), json.dumps(b, default=str)))


def _dataset_key(d):
    if not isinstance(d, dict):
        return None
    name, split = d.get("name"), d.get("split")
    if not name and not split:
        return None
    return (norm_scalar(name), norm_scalar(split))


def _compare_dataset(a, b):
    ka, kb = _dataset_key(a), _dataset_key(b)
    if ka is None or kb is None:
        missing = "run" if ka is None else "baseline"
        return UNVERIFIABLE, "%s has no dataset name/split recorded." % missing
    if ka != kb:
        return HARD, ("dataset differs: run=%s/%s, baseline=%s/%s."
                      % (a.get("name"), a.get("split"), b.get("name"), b.get("split")))
    return None, "both %s/%s" % (a.get("name"), a.get("split"))


def check_comparability(run, baseline, assert_reason=None):
    """Decide whether these two sides may be subtracted from each other."""
    dims, blockers = {}, []
    for name, fn, ra, ba in (
        ("mode", _compare_mode, run.get("mode"), baseline.get("mode")),
        ("scope", _compare_scope, run.get("scope"), baseline.get("scope")),
        ("dataset", _compare_dataset, run.get("dataset"), baseline.get("dataset")),
    ):
        severity, detail = fn(ra, ba)
        dims[name] = {
            "run": ra, "baseline": ba,
            "verdict": "equivalent" if severity is None else severity,
            "detail": detail,
        }
        if severity is not None:
            blockers.append({"dimension": name, "severity": severity, "detail": detail})

    hard = [b for b in blockers if b["severity"] == HARD]
    soft = [b for b in blockers if b["severity"] == UNVERIFIABLE]
    comparable = not blockers
    overridden = []
    if not comparable and not hard and assert_reason:
        comparable = True
        overridden = soft

    return {
        "comparable": comparable,
        "dimensions": dims,
        "blockers": [] if comparable else blockers,
        "overridden": overridden,
        "asserted_reason": assert_reason if overridden else None,
    }


# --------------------------------------------------------------------------
# directions + deltas
# --------------------------------------------------------------------------

def metric_directions(output_json, warnings=None):
    """{metric: {"direction": "max"|"min"|None, "source": str|None}} from output.json."""
    warnings = warnings if warnings is not None else []
    out = {}
    metrics = (output_json or {}).get("metrics") or {}

    for name, defn in (metrics.get("definitions") or {}).items():
        if not isinstance(defn, dict) or "direction" not in defn:
            continue
        raw = defn.get("direction")
        norm = normalize_direction(raw)
        if norm is None:
            warnings.append("unrecognized direction %r at output.json -> "
                            "metrics.definitions.%s.direction — treated as unknown."
                            % (raw, name))
            continue
        out[name] = {"direction": norm,
                     "source": "output.json -> metrics.definitions.%s.direction" % name}

    primary = metrics.get("primary_metric")
    raw = metrics.get("direction")
    if primary and raw and primary not in out:
        norm = normalize_direction(raw)
        if norm:
            out[primary] = {"direction": norm,
                            "source": "output.json -> metrics.direction (primary_metric)"}
        else:
            warnings.append("unrecognized direction %r at output.json -> metrics.direction "
                            "— treated as unknown." % raw)
    return out


def compute_deltas(run_metrics, base_metrics, directions, watch=None):
    """Per-metric delta rows for every metric present on both sides."""
    watch = list(watch or [])
    shared = set(run_metrics) & set(base_metrics)
    ordered = [m for m in watch if m in shared] + sorted(shared - set(watch))

    rows = []
    for name in ordered:
        cur, base = run_metrics[name], base_metrics[name]
        delta = cur - base
        pct = (delta / abs(base) * 100.0) if base else None
        info = directions.get(name) or {}
        direction = info.get("direction")
        if direction is None:
            verdict = "unknown_direction"
        elif delta == 0:
            verdict = "unchanged"
        elif (delta > 0) == (direction == "max"):
            verdict = "improvement"
        else:
            verdict = "regression"
        rows.append({
            "metric": name,
            "run": cur,
            "baseline": base,
            "delta": delta,
            "pct": pct,
            "direction": direction,
            "direction_source": info.get("source"),
            "verdict": verdict,
            "watched": name in watch,
        })
    return rows


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def compare(run_json_path, baseline_spec, output_json_path=None, assert_reason=None):
    run = load_run_side(run_json_path, "run")
    # Everything downstream keys off the *resolved* run.json path — run_json_path
    # may have been the run directory.
    run_json_path = run["path"]
    baseline = resolve_baseline(baseline_spec, run_json_path)

    warnings = []
    if run["status"] and run["status"] != "completed":
        warnings.append("run status is %r, not 'completed' — its metrics may be partial."
                        % run["status"])
    if baseline["kind"] == "run" and baseline["status"] not in (None, "completed"):
        warnings.append("baseline run status is %r, not 'completed'." % baseline["status"])

    run_dir = os.path.dirname(run_json_path)
    if output_json_path is None:
        output_json_path = os.path.join(os.path.dirname(os.path.dirname(run_dir)), "output.json")
    output_json_path = os.path.abspath(os.path.expanduser(output_json_path))
    output_json = None
    if os.path.isfile(output_json_path):
        output_json = load_json_required(output_json_path, "output.json")
    else:
        warnings.append("no output.json at %s — no metric direction is available, so "
                        "every delta is reported as unknown_direction."
                        % output_json_path)

    directions = metric_directions(output_json, warnings)
    watch = ((output_json or {}).get("metrics") or {}).get("watch") or []

    comparability = check_comparability(run, baseline, assert_reason)

    report = {
        "run": {k: run[k] for k in ("id", "path", "status", "mode", "scope", "dataset")},
        "baseline": {k: baseline[k] for k in ("kind", "id", "path", "mode", "scope", "dataset")},
        "comparability": comparability,
        "comparable": comparability["comparable"],
        "deltas": None,
        "only_in_run": sorted(set(run["metrics"]) - set(baseline["metrics"])),
        "only_in_baseline": sorted(set(baseline["metrics"]) - set(run["metrics"])),
        "unknown_direction": [],
        "warnings": warnings,
        "guidance": None,
    }

    if not comparability["comparable"]:
        report["guidance"] = (
            "NOT COMPARABLE (%s). Report the blocking dimension instead of a delta — "
            "do not compute these differences by hand, and do not print a percentage "
            "next to them. %s"
            % (", ".join(b["dimension"] for b in comparability["blockers"]),
               "Hard mismatch: the two numbers measure different workloads."
               if any(b["severity"] == HARD for b in comparability["blockers"])
               else "Unverifiable only: if you know both sides describe the same "
                    "measurement, re-run with --assert-comparable \"<reason>\".")
        )
        return report

    rows = compute_deltas(run["metrics"], baseline["metrics"], directions, watch)
    report["deltas"] = rows
    report["unknown_direction"] = [r["metric"] for r in rows if r["verdict"] == "unknown_direction"]
    report["summary"] = {
        "compared": len(rows),
        "improvement": sum(1 for r in rows if r["verdict"] == "improvement"),
        "regression": sum(1 for r in rows if r["verdict"] == "regression"),
        "unchanged": sum(1 for r in rows if r["verdict"] == "unchanged"),
        "unknown_direction": len(report["unknown_direction"]),
    }
    if report["unknown_direction"]:
        report["guidance"] = (
            "No direction recorded for: %s. Their deltas are raw numbers — say so "
            "rather than calling them better or worse. Record `direction` (\"max\" or "
            "\"min\") under output.json -> metrics.definitions.<name> to fix this."
            % ", ".join(report["unknown_direction"]))
    if report["only_in_baseline"]:
        warnings.append("baseline has metrics this run did not produce: %s"
                        % ", ".join(report["only_in_baseline"]))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_json", help="path to the completed run's run.json (or its run dir)")
    ap.add_argument("baseline", help="run id, path to run.json / baseline JSON, or inline JSON")
    ap.add_argument("--output-json", default=None,
                    help="stage output.json holding metric directions "
                         "(default: <run_dir>/../../output.json)")
    ap.add_argument("--assert-comparable", default=None, metavar="REASON",
                    help="clear UNVERIFIABLE blockers only (never a hard mismatch); "
                         "the reason is recorded in the output")
    args = ap.parse_args()

    try:
        report = compare(args.run_json, args.baseline, args.output_json, args.assert_comparable)
    except CompareError as e:
        json.dump({"error": str(e)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.stderr.write("compare_baseline: %s\n" % e)
        sys.exit(2)

    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    for w in report["warnings"]:
        sys.stderr.write("compare_baseline: warning: %s\n" % w)
    if not report["comparable"]:
        sys.stderr.write("compare_baseline: %s\n" % report["guidance"])
        sys.stderr.write("compare_baseline: exit 1 is this verdict, not a script failure "
                         "— do not fall back and subtract these by hand.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
