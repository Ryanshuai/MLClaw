#!/usr/bin/env python3
"""A fine-tune's baseline is the base measured HERE — and this does not measure it.

Measuring is `/eval-run`'s, both sides, and diffing the two is
`eval-run/compare_baseline.py`'s: it already reads direction out of the config
instead of guessing from the metric's name, and already grades comparability
into hard versus unverifiable blockers. Neither is reimplemented here. This
script holds the two things that belong to the *fine-tune*, which neither of
those can know:

  check     is this run a fine-tune, and was its base measured at all?
  protocol  were the two measurements taken the same way — and did each honour
            the settings its own weights were trained under?

**Why the base must be measured, and measured here.** A fine-tune exists because
the base was not good enough on this data, so the only number it produces that
can be read is the delta against that base. The child's absolute score has no
scale alone, and the base's *published* number is a claim measured on somebody
else's scope. It costs one eval, and both inputs are already resolved — you
cannot fine-tune without the base weights or train without the data. Take it
before launching: afterwards it is pure expenditure against a model nobody is
shipping, so it does not happen, and by the time anyone wants it the data,
weights or env has moved, so it cannot.

**Why `protocol` exists when `compare_baseline.py` already checks comparability.**
That check is about *scale* — mode, scope, dataset — and it is the right check
for two eval runs in general. It cannot see the failure specific to measuring a
model against its own ancestor: an evaluation-shaping flag left at the library's
default instead of the value the weights were trained under. Measured live on a
yolo26 segmentation fine-tune: validating at `overlap_mask=True` instead of the
`False` this model family trains under moved box mAP50-95 from 0.9142 to 0.9027
and wall from 0.9672 to 0.9445. Same weights, same images, same metric name,
same mode, same scope — `compare_baseline.py` passes it, correctly, because
nothing about the *scale* is wrong. And because both sides carried the same
default, the delta stayed honest while every absolute number quietly stopped
being comparable to any figure published for those weights.

The second half of `protocol` is free: the value was inside the checkpoint the
whole time. Nothing is fetched, asked for, or remembered.

Verdicts / exit codes follow the house rule: ok=0, warn=0, fail=1, exit 2 means
the script itself could not run. A `fail` is the answer, not a crash —
`CLAUDE.md` → "Script Integration", the fallback-rule exception.

Usage:
    python baseline_delta.py check <RUN_DIR>
    python baseline_delta.py protocol <before_eval_run> <after_eval_run>
        [--waive-setting KEY=REASON]... [--output-json PATH]

`protocol`'s two arguments are eval run directories (or their `run.json`). It
reads `settings` and `trained_args` from each; a run that records neither cannot
be checked, and says so rather than passing.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stream import finding, verdict_of  # noqa: E402

BASE_KEYS = ("model", "weights", "init_from", "resume_from", "base_model",
             "pretrained_path", "load_from")


def _real(d):
    """Drop `_`-prefixed keys: they are annotations, not settings.

    Same rule /repro's `trial` applies to scope. Failing closed on prose reads
    exactly like the guard working, which is how it survives unnoticed.
    """
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def emit_verdict(report):
    """Print the report, **return an exit code** -- 1 when the verdict is `fail`.

    Not named `emit`. `shared/_records.py -> emit(obj)` has the identical
    signature and returns `None`, and eleven scripts write `return emit({...})`
    meaning "success, exit 0". Anyone who "cleaned this up" by importing the
    shared one instead would turn every refusal this script makes into exit 0 --
    and refusing is its whole job (a fine-tune with no base measurement).
    The name is the guard, because the signatures cannot be told apart.
    """
    print(json.dumps(report, indent=2, default=str))
    return 1 if report["verdict"] == "fail" else 0


def load_run(path):
    """Accept a run dir or a run.json."""
    p = path if str(path).endswith(".json") else os.path.join(path, "run.json")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh), p


# --------------------------------------------------------------------- check

def is_finetune(run):
    """Did this run start from weights another process produced?

    Three recording paths, and taking all three matters because the most common
    one is the weakest: a fork and a cited parent are both in-project and leave
    an obvious trace, while a foreign base has no citable identity at all and
    lives as a path in params. That is precisely the case whose published number
    must not be trusted, so missing it would exempt the run that needs this most.
    """
    lin = run.get("lineage") or {}
    why = []
    if lin.get("fork_of"):
        why.append("lineage.fork_of is set")
    if lin.get("parents"):
        why.append("lineage.parents is non-empty")
    if lin.get("parent_checkpoint"):
        why.append("lineage.parent_checkpoint is set")
    for k, v in _real(run.get("params")).items():
        if k in BASE_KEYS and isinstance(v, str) and v:
            why.append("params.%s names initial weights (%s)" % (k, v))
    return bool(why), why


def cmd_check(a):
    try:
        run, rj = load_run(a.run_dir)
    except (IOError, OSError, ValueError) as exc:
        print(json.dumps({"error": "cannot read run.json: %s" % exc}), file=sys.stderr)
        return 2

    ft, why = is_finetune(run)
    if not ft:
        return emit_verdict({"verdict": "ok", "is_finetune": False, "findings": [],
                     "note": "not a fine-tune -- no base to measure against"})

    bd = run.get("baseline_delta") or {}
    before, after, waived = bd.get("before"), bd.get("after"), bd.get("waived")
    findings = []

    if not before:
        if waived:
            findings.append(finding(
                "warn", "baseline_waived",
                "no before-measurement; waived by a person: " + str(waived)))
        else:
            findings.append(finding(
                "fail", "baseline_missing",
                "this is a fine-tune and the base was never measured on this "
                "run's data, so the recorded metric has no scale. The base's "
                "published number is not a substitute -- it was measured on "
                "another scope. Measure it with /eval-run against the base "
                "checkpoint (one eval; the weights and the data are both "
                "already resolved) and cite that run in run.json -> "
                "baseline_delta.before. If it genuinely cannot be measured, "
                "say why in baseline_delta.waived.",
                is_finetune_because=why,
                needs="an evaluation stage; /eval-init if there is none"))
    elif not after:
        findings.append(finding(
            "warn", "after_missing",
            "the base was measured (%s) and the child has not been -- expected "
            "while the run is still going" % before))

    return emit_verdict({"verdict": verdict_of(findings), "is_finetune": True,
                 "is_finetune_because": why, "before": before, "after": after,
                 "findings": findings})


# ------------------------------------------------------------------ protocol

def measurement_of(run):
    """The settings an eval run measured with, and the args its weights carry.

    `/eval-run` records both; this reads them from wherever that run put them,
    tolerating the two shapes rather than demanding one.
    """
    m = run.get("measurement") or {}
    settings = _real(m.get("settings") or run.get("val_settings") or
                     (run.get("metrics") or {}).get("val_protocol"))
    trained = _real(m.get("trained_args") or run.get("trained_args"))
    weights = m.get("weights") or (run.get("sources") or {}).get("weights")
    return settings, trained, weights


def _parse_waivers(waive_settings):
    """-> (waived dict, exit_code). `exit_code` is None on success; a caller
    sees a non-None code and returns it immediately without touching `waived`."""
    waived = {}
    for w in waive_settings or []:
        if "=" not in w:
            print(json.dumps({"error": "--waive-setting takes KEY=REASON, got " + w}),
                  file=sys.stderr)
            return None, 2
        k, reason = w.split("=", 1)
        if not reason.strip():
            print(json.dumps({"error": "--waive-setting needs a reason for " + k}),
                  file=sys.stderr)
            return None, 2
        waived[k.strip()] = reason.strip()
    return waived, None


def _check_settings_recorded(bs, as_):
    """No recorded settings is not a pass. It is the check being unable to
    run, and the two must not read the same -- the same rule the repro axes
    draw between `intact` and `unverifiable`."""
    findings = []
    for name, s in (("before", bs), ("after", as_)):
        if not s:
            findings.append(finding(
                "fail", "settings_not_recorded",
                "%s records no measurement settings, so it cannot be checked "
                "against the other side or against its own weights. An "
                "unrecorded protocol is not a matching one." % name, side=name))
    return findings


def _check_settings_differ(bs, as_, waived):
    findings = []
    if bs and as_:
        for k in sorted(set(bs) | set(as_)):
            b, aa = bs.get(k, None), as_.get(k, None)
            if k in bs and k in as_ and b == aa:
                continue
            d = {"key": k, "before": b, "after": aa,
                 "before_present": k in bs, "after_present": k in as_}
            if k in waived:
                findings.append(finding(
                    "warn", "settings_differ_waived",
                    "before and after measured with different %s (%r vs %r); "
                    "waived by a person: %s" % (k, b, aa, waived[k]), **d))
            else:
                findings.append(finding(
                    "fail", "settings_differ",
                    "before and after measured with different %s (%r vs %r). Two "
                    "numbers taken under different settings are not a delta, and "
                    "nothing downstream can tell. Re-measure, or waive with "
                    "--waive-setting %s='<why it cannot matter>'." % (k, b, aa, k),
                    **d))
    return findings


def _check_measured_against_trained(bs, as_, bt, at, waived):
    findings = []
    for name, s, t in (("before", bs, bt), ("after", as_, at)):
        if not t:
            if s:
                findings.append(finding(
                    "warn", "no_trained_args_recorded",
                    "%s's checkpoint records no training args, so the free check "
                    "-- does the measurement honour the protocol these weights "
                    "were trained under -- could not run. Absent evidence, not "
                    "agreement." % name, side=name))
            continue
        for k in sorted(set(s) & set(t)):
            if s[k] == t[k]:
                continue
            d = {"key": k, "measured_with": s[k], "trained_with": t[k], "side": name}
            if k in waived:
                findings.append(finding(
                    "warn", "measured_against_trained_args_waived",
                    "%s measured with %s=%r while the checkpoint itself was "
                    "trained with %r; waived: %s"
                    % (name, k, s[k], t[k], waived[k]), **d))
            else:
                findings.append(finding(
                    "fail", "measured_against_trained_args",
                    "%s measured with %s=%r, but the checkpoint's own recorded "
                    "training args say %r. A measurement that departs from the "
                    "protocol its weights were trained under yields a number not "
                    "comparable to any figure published for them -- and it is "
                    "usually a library default nobody chose. compare_baseline.py "
                    "cannot see this: the mode and the scope are both fine. "
                    "Re-measure, or waive with --waive-setting %s='<why>'."
                    % (name, k, s[k], t[k], k), **d))
    return findings


def cmd_protocol(a):
    waived, err = _parse_waivers(a.waive_setting)
    if err is not None:
        return err

    try:
        before, bpath = load_run(a.before)
        after, apath = load_run(a.after)
    except (IOError, OSError, ValueError) as exc:
        print(json.dumps({"error": "cannot read a run.json: %s" % exc}), file=sys.stderr)
        return 2

    bs, bt, bw = measurement_of(before)
    as_, at, aw = measurement_of(after)
    findings = (_check_settings_recorded(bs, as_)
                + _check_settings_differ(bs, as_, waived)
                + _check_measured_against_trained(bs, as_, bt, at, waived))

    report = {
        "verdict": verdict_of(findings),
        "before_run": bpath, "after_run": apath,
        "before_weights": bw, "after_weights": aw,
        "settings": {"before": bs, "after": as_},
        "waived": waived or None,
        "findings": findings,
        "note": "protocol only. The metric delta is compare_baseline.py's, and "
                "it should be run too -- this says the two measurements are of "
                "the same thing, not what the difference between them is.",
    }
    if a.output_json:
        with open(a.output_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    return emit_verdict(report)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="is this fine-tune missing its baseline?")
    c.add_argument("run_dir", help="the TRAINING run dir, or its run.json")
    c.set_defaults(fn=cmd_check)

    p = sub.add_parser("protocol", help="were the two measurements taken the same "
                                        "way, and each under its own weights' protocol?")
    p.add_argument("before", help="the eval run that measured the BASE")
    p.add_argument("after", help="the eval run that measured the CHILD")
    p.add_argument("--waive-setting", action="append", metavar="KEY=REASON",
                   help="accept one differing key, with a reason on the record")
    p.add_argument("--output-json", default=None)
    p.set_defaults(fn=cmd_protocol)

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
