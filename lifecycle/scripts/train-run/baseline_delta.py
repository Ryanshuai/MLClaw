#!/usr/bin/env python3
"""A fine-tune's baseline is the base measured HERE, and this refuses without it.

A fine-tune exists because the base was not good enough on this data. So the
only readable number it produces is the delta against that base — the child's
absolute score has no scale on its own, and the base's *published* number is a
claim measured on somebody else's scope (`/train-run` SKILL.md → "Fine-tuning
from a base this project did not train"). That leaves exactly one baseline worth
the name: the base run through the same measurement, on the same data, with the
same settings.

It costs one val. Both inputs are already in hand — you cannot fine-tune without
the base weights, and you cannot train without the data — which is why the
measurement is cheap **now** and usually impossible later: after the run, taking
it is pure expenditure against a model nobody is shipping, so it does not happen,
and by the time anyone wants it the data, the weights or the env has moved.

Two verbs.

  check    — is this run a fine-tune, and does its record carry a before? A
             fine-tune without one, and without a typed waiver, is `fail`.
  compare  — the two measurements were taken the same way, or this refuses.
             Then it computes the deltas.

`compare` is where the real defect lives, and it is not the missing measurement.
It is two measurements that both exist, both look fine, and were not taken the
same way. Measured live on a yolo26 segmentation fine-tune: validating at the
library's `overlap_mask=True` instead of the `False` the model family trains
under moved box mAP50-95 from 0.9142 to 0.9027 and wall from 0.9672 to 0.9445 —
same weights, same images, same metric name, no warning anywhere. Because both
sides of that comparison carried the same wrong setting, the *delta* stayed
honest while every absolute number silently stopped being comparable to any
published figure. So:

  * settings must match between before and after, key for key;
  * and where the measured checkpoint's own recorded training args name the same
    key, the measurement must agree with them too. That second check is the one
    that catches a library default standing in for the project's protocol, and
    it is free: `overlap_mask` was sitting in the checkpoint the whole time.

Both are waivable per key, with a reason that lands in the record. Unwaived, they
refuse.

Verdicts / exit codes follow the house rule: ok=0, warn=0, fail=1, and exit 2
means the script itself could not run. A `fail` is the answer, not a crash —
`CLAUDE.md` → "Script Integration", the fallback-rule exception.

Measurement file shape (one per side; the run skill writes them):

    {"role": "before"|"after",
     "weights": "<path or uri>",
     "settings": {...},          # how it was measured — framework's own keys
     "scope":    {...},          # what it was measured on
     "metrics":  {"name": number, ...},
     "trained_args": {...}}      # OPTIONAL: the args recorded inside `weights`

Usage:
    python baseline_delta.py check <run_dir>
    python baseline_delta.py compare <before.json> <after.json>
        [--direction <metrics.json>] [--waive-setting KEY=REASON]...
        [--output-json PATH]
"""
import argparse
import json
import os
import sys

# `_`-prefixed keys are annotations, not settings. Same rule /repro's `trial`
# applies to scope: prose that differs must not make two runs incomparable.
def _real(d):
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def finding(level, code, message, **extra):
    f = {"level": level, "code": code, "message": message}
    f.update(extra)
    return f


def verdict_of(findings):
    if any(f["level"] == "fail" for f in findings):
        return "fail"
    if any(f["level"] == "warn" for f in findings):
        return "warn"
    return "ok"


def emit(report):
    print(json.dumps(report, indent=2, default=str))
    return 1 if report["verdict"] == "fail" else 0


def load(path):
    with open(path) as fh:
        return json.load(fh)


# --------------------------------------------------------------------- check

def is_finetune(run):
    """Did this run start from weights somebody else's process produced?

    Three shapes, and the point of taking all three is that they are recorded in
    different places by different paths: a fork inside this project, a run cited
    as a parent, and a foreign base that has no citable identity at all and so
    lives as a path in `sources` or `runtime_params`. Missing any one of them
    would let the most common case -- the foreign base -- slip the check, and
    that is precisely the case with no published number worth trusting.
    """
    why = []
    if run.get("lineage", {}).get("fork_of"):
        why.append("lineage.fork_of is set")
    if run.get("lineage", {}).get("parents"):
        why.append("lineage.parents is non-empty")
    base = (run.get("lineage") or {}).get("parent_checkpoint")
    if base:
        why.append("lineage.parent_checkpoint is set")
    for k, v in _real(run.get("params") or {}).items():
        if k in ("model", "weights", "init_from", "resume_from", "base_model",
                 "pretrained_path", "load_from") and isinstance(v, str) and v:
            why.append("params.%s names initial weights (%s)" % (k, v))
    return (bool(why), why)


def cmd_check(a):
    run_dir = a.run_dir.rstrip("/")
    rj = run_dir if run_dir.endswith(".json") else os.path.join(run_dir, "run.json")
    if not os.path.exists(rj):
        print(json.dumps({"error": "no run.json at " + rj}), file=sys.stderr)
        return 2
    run = load(rj)
    ft, why = is_finetune(run)
    base_dir = os.path.dirname(rj)
    findings = []

    if not ft:
        return emit({"verdict": "ok", "is_finetune": False,
                     "note": "not a fine-tune -- no base to measure against",
                     "findings": []})

    before = os.path.join(base_dir, "output", "baseline_before.json")
    after = os.path.join(base_dir, "output", "baseline_after.json")
    have_before, have_after = os.path.exists(before), os.path.exists(after)
    waiver = (run.get("baseline_delta") or {}).get("waived")

    if not have_before:
        if waiver:
            findings.append(finding(
                "warn", "baseline_waived",
                "no before-measurement; waived by a person: " + str(waiver)))
        else:
            findings.append(finding(
                "fail", "baseline_missing",
                "this is a fine-tune and the base was never measured on this "
                "run's data, so the recorded metric has no scale. The base's "
                "published number is not a substitute: it was measured on "
                "another scope. Measure it (one val, the weights and the data "
                "are both already here) or waive it in run.json -> "
                "baseline_delta.waived with a reason.",
                looked_for=before, is_finetune_because=why))
    if have_before and not have_after:
        findings.append(finding(
            "warn", "after_missing",
            "before-measurement present, child not measured yet -- expected "
            "while the run is still going", looked_for=after))

    return emit({"verdict": verdict_of(findings), "is_finetune": True,
                 "is_finetune_because": why,
                 "before": before if have_before else None,
                 "after": after if have_after else None,
                 "findings": findings})


# ------------------------------------------------------------------- compare

def settings_diff(before, after):
    b, a = _real(before.get("settings")), _real(after.get("settings"))
    out = []
    for k in sorted(set(b) | set(a)):
        if b.get(k, "\0absent") != a.get(k, "\0absent"):
            out.append({"key": k, "before": b.get(k, None), "after": a.get(k, None),
                        "before_present": k in b, "after_present": k in a})
    return out


def against_trained_args(side):
    """Where the checkpoint's own training recorded a value for a key the
    measurement also sets, they must agree.

    This is the free check. The value is already inside the weights -- nothing
    has to be fetched, asked for, or remembered -- and the divergence it catches
    is a library default quietly standing in for the project's own protocol,
    which produces a plausible number and no error.
    """
    s, t = _real(side.get("settings")), _real(side.get("trained_args"))
    if not t:
        return []
    return [{"key": k, "measured_with": s[k], "trained_with": t[k]}
            for k in sorted(set(s) & set(t)) if s[k] != t[k]]


def cmd_compare(a):
    before, after = load(a.before), load(a.after)
    waived = {}
    for w in a.waive_setting or []:
        if "=" not in w:
            print(json.dumps({"error": "--waive-setting takes KEY=REASON, got " + w}),
                  file=sys.stderr)
            return 2
        k, reason = w.split("=", 1)
        if not reason.strip():
            print(json.dumps({"error": "--waive-setting needs a reason for " + k}),
                  file=sys.stderr)
            return 2
        waived[k.strip()] = reason.strip()

    findings = []

    for d in settings_diff(before, after):
        if d["key"] in waived:
            findings.append(finding(
                "warn", "settings_differ_waived",
                "before and after were measured with different %s (%r vs %r); "
                "waived by a person: %s" % (d["key"], d["before"], d["after"],
                                            waived[d["key"]]), **d))
        else:
            findings.append(finding(
                "fail", "settings_differ",
                "before and after were measured with different %s (%r vs %r). "
                "Two numbers taken under different settings are not a delta, and "
                "nothing downstream can tell. Re-measure, or waive this key with "
                "--waive-setting %s='<why it cannot matter>'."
                % (d["key"], d["before"], d["after"], d["key"]), **d))

    for side_name, side in (("before", before), ("after", after)):
        for d in against_trained_args(side):
            if d["key"] in waived:
                findings.append(finding(
                    "warn", "measured_against_trained_args_waived",
                    "%s measured with %s=%r while the checkpoint itself was "
                    "trained with %r; waived: %s"
                    % (side_name, d["key"], d["measured_with"], d["trained_with"],
                       waived[d["key"]]), side=side_name, **d))
            else:
                findings.append(finding(
                    "fail", "measured_against_trained_args",
                    "%s measured with %s=%r, but the checkpoint's own recorded "
                    "training args say %r. A measurement that departs from the "
                    "protocol the weights were trained under produces a number "
                    "that is not comparable to any figure published for them -- "
                    "and it is usually a library default that nobody chose. "
                    "Re-measure, or waive with --waive-setting %s='<why>'."
                    % (side_name, d["key"], d["measured_with"], d["trained_with"],
                       d["key"]), side=side_name, **d))

    bs, as_ = _real(before.get("scope")), _real(after.get("scope"))
    if bs != as_:
        findings.append(finding(
            "fail", "scope_differs",
            "the two measurements are not of the same data",
            before=bs, after=as_))

    bm = {k: v for k, v in (before.get("metrics") or {}).items()
          if isinstance(v, (int, float))}
    am = {k: v for k, v in (after.get("metrics") or {}).items()
          if isinstance(v, (int, float))}
    shared = sorted(set(bm) & set(am))
    if not shared:
        findings.append(finding(
            "fail", "no_shared_metric",
            "the two measurements share no metric name, so there is nothing to "
            "diff", before_keys=sorted(bm), after_keys=sorted(am)))

    directions = {}
    if a.direction and os.path.exists(a.direction):
        out = load(a.direction)
        m = out.get("metrics") or out
        prim, d = m.get("primary_metric"), m.get("direction")
        if prim and d:
            directions[prim] = d

    deltas = {}
    for k in shared:
        delta = am[k] - bm[k]
        row = {"before": bm[k], "after": am[k], "delta": delta}
        d = directions.get(k)
        if d in ("maximize", "minimize"):
            row["improved"] = (delta > 0) if d == "maximize" else (delta < 0)
            row["direction"] = d
        else:
            # No declared direction means no verdict. Guessing one from the
            # metric's name is how a loss gets reported as an improvement.
            row["improved"] = None
            row["direction"] = None
        deltas[k] = row

    only_before = sorted(set(bm) - set(am))
    only_after = sorted(set(am) - set(bm))
    for k in only_before:
        findings.append(finding(
            "warn", "metric_only_before",
            "%s was measured on the base and not on the child -- absent, which "
            "is not the same as unchanged" % k))
    for k in only_after:
        findings.append(finding(
            "warn", "metric_only_after",
            "%s was measured on the child and not on the base, so it has no "
            "baseline" % k))

    report = {
        "verdict": verdict_of(findings),
        "before_weights": before.get("weights"),
        "after_weights": after.get("weights"),
        "settings": _real(before.get("settings")),
        "scope": bs,
        "deltas": deltas,
        "metrics_without_a_counterpart": {"only_before": only_before,
                                          "only_after": only_after},
        "waived": waived or None,
        "findings": findings,
    }
    if a.output_json:
        with open(a.output_json, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
    return emit(report)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="is this fine-tune missing its baseline?")
    c.add_argument("run_dir")
    c.set_defaults(fn=cmd_check)

    p = sub.add_parser("compare", help="diff two measurements, refusing if they "
                                       "were not taken the same way")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--direction", default=None,
                   help="stage output.json, for primary_metric + direction")
    p.add_argument("--waive-setting", action="append", metavar="KEY=REASON",
                   help="accept one differing setting, with a reason on the record")
    p.add_argument("--output-json", default=None)
    p.set_defaults(fn=cmd_compare)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # exit 2 == the script broke; fall back and do it by hand
        print(json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}), file=sys.stderr)
        sys.exit(2)
