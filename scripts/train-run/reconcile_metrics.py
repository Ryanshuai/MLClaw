#!/usr/bin/env python3
"""Reconcile a training stage's declared metric schema against the actual stream.

`/train-init` writes `output.json -> metrics` by reading the training code. That
record is then trusted for the rest of the run's life: it decides which number
is `primary_metric`, which checkpoint is best, and which value lands in the
leaderboard. Nobody re-reads it at the moment it is written, and nothing errors
if it is wrong — the run completes, a number is recorded, and it is the wrong
number.

The failure this exists to catch, in the words of the person who hit it: the
config says `val_loss` and the code emits `train_loss`. Both are real numbers,
both move during training, both look plausible in a report. Checkpoint selection
then optimizes for fitting the training set.

So: read the stream, and say out loud which field the config actually claimed.

Verdicts
  ok    declared schema matches what the stream emits
  warn  usable, but something is worth a human's eye before trusting numbers
  fail  the declared schema does not describe this stream; do not select a
        checkpoint or record a metric from it until it is fixed

Exit code mirrors the verdict (0 / 0 / 1). A `fail` is a finding, not a crash:
findings go to stdout as JSON. Exit 2 means the script itself could not run.

Usage:
    python reconcile_metrics.py <output_json> <jsonl_path>
    python reconcile_metrics.py <output_json> --run-dir <run_dir>
"""
import argparse
import sys

from _stream import (StreamError, classify, emit, expected_direction, finding,
                     find_type_key, load_inputs, near_misses, normalize_direction,
                     numeric_series, observed_fields, resolve_stream, split_of,
                     unnormalized_finding, verdict_of)


def _check_declared_vs_observed(by_type, unclassified, declared_types, observed_types):
    """-> [finding]. Declared record_types and their fields against what the
    stream actually emits: a type or field named in output.json but never
    seen, or records the stream emits that output.json never described."""
    findings = []
    for t, count in observed_types.items():
        if count == 0:
            findings.append(finding(
                "fail", "record_type_never_emitted",
                f"record_type `{t}` is declared but never appears in the stream",
                record_type=t))
    if unclassified:
        findings.append(finding(
            "warn", "unclassified_records",
            f"{len(unclassified)} record(s) match no declared record_type — the stream emits "
            f"more than output.json describes",
            sample_keys=sorted(observed_fields(unclassified))[:12]))

    for t, rs in by_type.items():
        if not rs:
            continue
        present = observed_fields(rs)
        for field in (declared_types.get(t, {}).get("fields") or []):
            if field not in present:
                findings.append(finding(
                    "fail", "field_never_emitted",
                    f"`{t}.{field}` is declared but no `{t}` record carries it",
                    record_type=t, field=field,
                    did_you_mean=near_misses(field, list(present))))
    return findings


def _check_primary_metric(primary, all_fields, by_type, records):
    """-> [finding]. Whether the primary metric exists, is numeric, has more
    than one point to rank by, is not a train-split metric masquerading as a
    selection signal, and is not ambiguous across record types."""
    findings = []
    if not primary:
        findings.append(finding(
            "fail", "primary_metric_unset",
            "metrics.primary_metric is empty — checkpoint selection has nothing to rank by"))
        return findings
    if primary not in all_fields:
        findings.append(finding(
            "fail", "primary_metric_absent",
            f"primary_metric `{primary}` never appears in the stream",
            metric=primary, did_you_mean=near_misses(primary, list(all_fields)),
            available=sorted(all_fields)[:20]))
        return findings

    carriers = [t for t, rs in by_type.items() if any(primary in r for r in rs)]
    series = numeric_series(records, primary)
    if not series:
        findings.append(finding(
            "fail", "primary_metric_not_numeric",
            f"`{primary}` appears {all_fields[primary]} time(s) but never as a number",
            metric=primary))
    elif len(series) == 1:
        findings.append(finding(
            "warn", "primary_metric_single_point",
            f"`{primary}` has one value in the whole stream — nothing to rank",
            metric=primary))

    # The train_loss-as-val_loss case, stated explicitly.
    if split_of(primary) == "train":
        held_out = [f for f in all_fields if split_of(f) == "held_out"]
        findings.append(finding(
            "fail" if held_out else "warn", "primary_metric_is_train_split",
            f"`{primary}` is a training-split metric being used to select checkpoints"
            + (f"; the stream also emits held-out metrics: {', '.join(sorted(held_out)[:6])}"
               if held_out else "; no held-out metric found in the stream either"),
            metric=primary, held_out_alternatives=sorted(held_out)[:6]))
    elif split_of(primary) == "unknown":
        siblings = [f for f in all_fields if split_of(f) == "held_out"
                    and f.endswith(primary)]
        if siblings:
            findings.append(finding(
                "warn", "primary_metric_split_ambiguous",
                f"`{primary}` carries no split prefix, and the stream also emits "
                f"{', '.join(sorted(siblings))} — confirm which one selection should rank by",
                metric=primary, held_out_alternatives=sorted(siblings)))

    if len(carriers) > 1:
        findings.append(finding(
            "warn", "primary_metric_multi_type",
            f"`{primary}` appears in more than one record type ({', '.join(sorted(carriers))}) "
            f"— ranking will mix them unless selection filters by type",
            metric=primary, record_types=sorted(carriers)))
    return findings


def _check_direction(direction, raw_direction, primary):
    """-> [finding]. `direction` parses, and — when a primary metric is set —
    agrees with the convention its own name implies (a `*_loss` should be
    `min`, an `*_acc` should be `max`)."""
    findings = []
    if direction is None:
        findings.append(finding(
            "fail", "direction_invalid",
            f"metrics.direction is {raw_direction!r}; expected 'max' or 'min' "
            f"(or a recognized alias such as 'maximize' / 'lower_is_better')"))
    elif primary:
        expected = expected_direction(primary)
        if expected and expected != direction:
            findings.append(finding(
                "fail", "direction_contradicts_name",
                f"`{primary}` is ranked `{direction}`, but a metric with that name is "
                f"normally `{expected}` — one of the two is wrong, and selection will "
                f"pick the worst checkpoint if it is the direction",
                metric=primary, declared=direction, expected=expected))
    return findings


def _check_watch_and_done(m, all_fields, declared_types, by_type):
    """-> [finding]. watch_step/watch_epoch name fields the stream actually
    emits, and done_signal's record_type is declared and has been seen."""
    findings = []
    for list_name in ("watch_step", "watch_epoch"):
        for field in (m.get(list_name) or []):
            if field not in all_fields:
                findings.append(finding(
                    "warn", "watched_field_absent",
                    f"{list_name} names `{field}`, which never appears in the stream",
                    field=field, did_you_mean=near_misses(field, list(all_fields))))

    done = m.get("done_signal") or {}
    if done.get("type") == "record":
        rt = done.get("record_type")
        if rt and rt not in declared_types:
            findings.append(finding(
                "warn", "done_signal_type_undeclared",
                f"done_signal expects record_type `{rt}`, which is not in record_types",
                record_type=rt))
        elif rt and not by_type.get(rt):
            findings.append(finding(
                "warn", "done_signal_absent",
                f"no `{rt}` record in the stream — this run has not signalled completion",
                record_type=rt))
    return findings


def reconcile(output_json, records, line_errors):
    """Pure function. -> report dict."""
    m = output_json.get("metrics") or {}
    declared_types = m.get("record_types") or {}
    primary = m.get("primary_metric") or ""
    raw_direction = m.get("direction") or ""
    # One vocabulary for `direction` across the whole tool — `higher_is_better`
    # must not be legal for /eval-run and fatal here. None = unrecorded or
    # unrecognized, which is still the `direction_invalid` failure below.
    direction = normalize_direction(raw_direction)
    findings = []

    if line_errors:
        tail_only = len(line_errors) == 1 and line_errors[0].get("line")
        findings.append(finding(
            "warn" if tail_only else "fail", "malformed_lines",
            f"{len(line_errors)} unparseable line(s) in the stream"
            + (" — a truncated final line is normal while a run is live" if tail_only else ""),
            lines=[e["line"] for e in line_errors[:10]]))

    if not records:
        findings.append(finding("fail", "empty_stream", "the stream contains no records"))
        return {"verdict": "fail", "findings": findings, "primary_metric": primary,
                "records": 0, "record_types": {}}

    type_key, coverage, note = find_type_key(records, declared_types)
    if note:
        findings.append(finding("warn", "type_key_uncertain", note, coverage=round(coverage, 3)))
    by_type, unclassified = classify(records, declared_types, type_key)
    observed_types = {t: len(rs) for t, rs in by_type.items()}
    all_fields = observed_fields(records)

    findings += _check_declared_vs_observed(by_type, unclassified, declared_types, observed_types)
    findings += _check_primary_metric(primary, all_fields, by_type, records)
    findings += _check_direction(direction, raw_direction, primary)
    findings += _check_watch_and_done(m, all_fields, declared_types, by_type)

    return {
        "verdict": verdict_of(findings),
        "records": len(records),
        "record_types": observed_types,
        "unclassified": len(unclassified),
        "type_key": type_key,
        "primary_metric": primary,
        "direction": direction or raw_direction,
        "observed_fields": dict(sorted(all_fields.items())),
        "findings": findings,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check output.json's metric schema against the real stream.")
    ap.add_argument("output_json")
    ap.add_argument("jsonl", nargs="?", help="path to the metric stream")
    ap.add_argument("--run-dir", help="resolve the stream from output.json -> metrics.log_path")
    args = ap.parse_args(argv)

    try:
        output_json, records, line_errors, kind = load_inputs(args.output_json, args.jsonl,
                                                         args.run_dir)
    except StreamError as e:
        sys.stderr.write(f"reconcile_metrics: {e}\n")
        return 2

    report = reconcile(output_json, records, line_errors)
    path, _ = resolve_stream(output_json, args.jsonl, args.run_dir)
    report["stream"] = {"path": path, "kind": kind}
    unnormalized = unnormalized_finding(kind, path)
    if unnormalized:
        report["findings"].append(unnormalized)
        report["verdict"] = verdict_of(report["findings"])
    return emit(report, "reconcile_metrics")


if __name__ == "__main__":
    sys.exit(main())
