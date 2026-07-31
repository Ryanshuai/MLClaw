#!/usr/bin/env python3
"""Pick the best checkpoint, and show the numbers that justified the choice.

`/train-run` Step 5a describes this as three lines of prose: rank the epoch
records by `selection.best_by`, resolve the file, record it. Every part of that
can go wrong without raising — rank by a field that is a training-split metric,
rank in the wrong direction, resolve `{epoch}` against a pattern the script
actually numbers by `{step}`, or pick an epoch whose file was never written. The
run then completes and hands a checkpoint downstream that is not the best one.
`/eval-run` evaluates it, the number is a bit low, and the code takes the blame.

So this does not just return a path. It returns the ranking with the actual
values read out of the jsonl, the raw record behind the winner, and every
mismatch it found on the way. `retention.py` imports `inventory()` from here so
that deletion ranks by exactly the same computation that chose the keeper —
two independent rankings that disagree is how you delete the best model. That
is only true if the *join* is shared too, not just the sort: `inventory()`
therefore returns `ranked_with_files` and `best_file` already resolved, and
neither caller is in a position to walk the ranking its own way.

Verdicts: ok / warn / fail, exit 0 / 0 / 1. Exit 2 = the script could not run.

Usage:
    python select_checkpoint.py <output_json> <jsonl> --output-dir <dir> [--top 5]
"""
import argparse
import glob as globmod
import os
import sys

from _stream import (StreamError, emit, expected_direction, finding, find_type_key,
                     load_inputs, near_misses, normalize_direction, observed_fields,
                     resolve_pattern, split_of, verdict_of)

SCRIPT_SAVED_NAMES = ("best.pt", "best.pth", "best.ckpt", "best_model.pt", "model_best.pth.tar")


def _index_of(record):
    """-> (kind, number) for whichever of epoch/step the record carries."""
    for key in ("epoch", "step", "global_step", "iteration", "iter"):
        v = record.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        return ("epoch" if key == "epoch" else "step"), int(v)
    return None, None


def _check_selection(output, records, best_by, direction, raw_direction):
    """Everything wrong with the selection config, before a single record is ranked."""
    findings = []
    if not best_by:
        findings.append(finding("fail", "best_by_unset",
                                "checkpoints.selection.best_by is empty — nothing to rank by"))
    if direction is None:
        findings.append(finding(
            "fail", "direction_invalid",
            f"checkpoints.selection.direction is {raw_direction!r}; expected 'max' or 'min' "
            f"(or a recognized alias such as 'maximize' / 'lower_is_better')"))

    primary = ((output.get("metrics") or {}).get("primary_metric")) or ""
    if best_by and primary and best_by != primary:
        findings.append(finding(
            "warn", "best_by_differs_from_primary",
            f"selection ranks by `{best_by}` but metrics.primary_metric is `{primary}` — "
            f"the checkpoint chosen here and the number on the leaderboard describe "
            f"different things",
            best_by=best_by, primary_metric=primary))

    all_fields = observed_fields(records)
    if best_by and best_by not in all_fields:
        findings.append(finding(
            "fail", "best_by_absent",
            f"`{best_by}` never appears in the stream — no ranking is possible",
            metric=best_by, did_you_mean=near_misses(best_by, list(all_fields)),
            available=sorted(all_fields)[:20]))

    if best_by and split_of(best_by) == "train":
        findings.append(finding(
            "fail", "best_by_is_train_split",
            f"ranking checkpoints by `{best_by}`, a training-split metric — this selects "
            f"the checkpoint that fits the training set best",
            metric=best_by))

    if best_by and direction:
        exp = expected_direction(best_by)
        if exp and exp != direction:
            findings.append(finding(
                "fail", "direction_contradicts_name",
                f"ranking `{best_by}` by `{direction}`, but that name normally ranks `{exp}` — "
                f"if the direction is the wrong one, this picks the worst checkpoint",
                metric=best_by, declared=direction, expected=exp))
    return findings


def _rank(records, best_by, direction):
    """-> (ranked, findings). Best first; entries are light (see `inventory`)."""
    ranked = []
    for i, r in enumerate(records):
        v = r.get(best_by)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        kind, num = _index_of(r)
        # Four keys, deliberately: a fifth pushes every one of these dicts into
        # the next size class, which at 50k ranked records is megabytes spent so
        # that five of them can carry a display label.
        ranked.append({"value": float(v), "index_kind": kind, "index": num,
                       "record_index": i})
    # Stable, deterministic: metric first, then index, so ties never depend on
    # dict ordering or filesystem order.
    ranked.sort(key=lambda e: (-e["value"] if direction == "max" else e["value"],
                               e["index"] if e["index"] is not None else 1 << 30))

    findings = []
    if len(ranked) > 1 and ranked[0]["value"] == ranked[1]["value"]:
        tied = [e for e in ranked if e["value"] == ranked[0]["value"]]
        findings.append(finding(
            "warn", "tie_at_top",
            f"{len(tied)} checkpoints tie at {best_by}={ranked[0]['value']}; the earliest "
            f"index wins by rule, but the choice carries no information",
            value=ranked[0]["value"], indices=[e["index"] for e in tied][:10]))

    for e in ranked:
        if e["index"] is None:
            findings.append(finding(
                "warn", "record_without_index",
                f"a record with {best_by}={e['value']} carries no epoch/step field, so no "
                f"checkpoint file can be matched to it"))
            break
    return ranked, findings


def _scan_files(pattern, output_dir, ranked, best_by):
    """Glob the checkpoints and join them to the ranking, in both directions.

    -> (files, findings). Each file gets the metric of the record that names its
    index; each ranked entry gets `file` set to the file that carries its index.
    One join, so the keeper selection and the deletion plan cannot disagree.
    """
    findings = []
    if not pattern:
        return [], [finding("fail", "path_pattern_unset",
                            "checkpoints.path_pattern is empty — no file can be resolved")]

    glob_pat, rx = resolve_pattern(pattern, output_dir)
    entry_by_index = {}
    for e in ranked:
        if e["index"] is not None:
            entry_by_index.setdefault((e["index_kind"], e["index"]), e)

    on_disk = sorted(globmod.glob(glob_pat))
    if not on_disk:
        findings.append(finding(
            "fail", "no_checkpoints_on_disk",
            f"path_pattern resolved to `{glob_pat}` and matched no files",
            glob=glob_pat))

    files, file_by_index = [], {}
    for path in on_disk:
        m = rx.match(path)
        entry = {"path": path, "size_bytes": os.path.getsize(path),
                 "epoch": None, "step": None, "metric": None, "matched_record": False}
        if m:
            g = m.groupdict()
            for kind in ("epoch", "step"):
                if g.get(kind) is None:
                    continue
                idx = int(g[kind])
                entry[kind] = idx
                file_by_index[(kind, idx)] = entry
                rec = entry_by_index.get((kind, idx))
                if rec:
                    entry["metric"] = rec["value"]
                    entry["matched_record"] = True
        files.append(entry)

    for e in ranked:
        f = file_by_index.get((e["index_kind"], e["index"])) if e["index"] is not None else None
        if f:
            e["file"] = f

    unmatched = [f for f in files if not f["matched_record"]]
    if unmatched:
        findings.append(finding(
            "warn", "checkpoints_without_a_metric",
            f"{len(unmatched)} checkpoint file(s) on disk have no record in the stream "
            f"carrying `{best_by}` — they cannot be ranked, and retention must not "
            f"delete what it cannot rank",
            paths=[f["path"] for f in unmatched][:10]))
    return files, findings


def _attach_records(records, declared_types, reportable):
    """Give the entries that can surface in a report their raw jsonl record.

    Only those. Stapling the parsed dict onto all 50k ranked entries would keep
    the whole stream alive for as long as a caller held the ranking — for the
    sake of one `evidence_record` and five display labels.
    """
    if not reportable:
        return
    type_key, _, _ = find_type_key(records, declared_types or {})
    for e in reportable:
        r = records[e["record_index"]]
        e["record"] = r
        e["record_type"] = r.get(type_key) if type_key else None


def inventory(output, records, output_dir, top=5):
    """The single source of ranking truth. -> dict, shared with retention.py.

    The ranking (best first), the checkpoint files on disk, and — already joined —
    `ranked_with_files` and `best_file`. The join is here, not in each caller:
    `select()` names the keeper, `build_plan()` names what may be deleted, and the
    day those two walk the ranking separately is the day they name different files.

    Ranked entries carry `value`, `index_kind`, `index`, `record_index`, plus
    `file` once one matched. `record` / `record_type` are set only on entries that
    can be reported — the file-matched set and the top `top` — so read them with
    `.get()`; `record_index` rehydrates any other entry from the caller's own list.
    """
    ck = (output.get("checkpoints") or {})
    sel = ck.get("selection") or {}
    best_by = sel.get("best_by") or ""
    raw_direction = sel.get("direction") or ""
    direction = normalize_direction(raw_direction)
    pattern = ck.get("path_pattern") or ""

    findings = _check_selection(output, records, best_by, direction, raw_direction)
    ranked, rank_findings = _rank(records, best_by, direction)
    findings += rank_findings
    files, file_findings = _scan_files(pattern, output_dir, ranked, best_by)
    findings += file_findings

    ranked_with_files = [e for e in ranked if e.get("file")]
    _attach_records(records, (output.get("metrics") or {}).get("record_types"),
                    ranked[:top] + ranked_with_files)

    return {"best_by": best_by, "direction": direction, "pattern": pattern,
            "output_dir": output_dir, "ranked": ranked, "files": files,
            "ranked_with_files": ranked_with_files,
            "best_file": ranked_with_files[0]["file"] if ranked_with_files else None,
            "findings": findings}


def _cross_check_script_best(output_dir, chosen, findings):
    """-> the script's own best.pt, if it saved one. Disagreement is a warning.

    The training script tracking "best" internally is not a second opinion, it is
    a second ranking; when they name different files one of them is using a
    different metric or direction, and neither is trustworthy until that is known.
    """
    script_best = None
    for name in SCRIPT_SAVED_NAMES:
        cand = os.path.join(output_dir, name)
        if os.path.isfile(cand):
            script_best = cand
            break
    if script_best and not (chosen and
                            os.path.realpath(script_best) == os.path.realpath(chosen["path"])):
        findings.append(finding(
            "warn", "script_saved_best_differs",
            f"the training script saved its own `{os.path.basename(script_best)}`, which is "
            f"not the file this ranking chose. The script tracked best internally; if the "
            f"two disagree, one of the rankings is using a different metric or direction. "
            f"Resolve before trusting either.",
            script_best=script_best, ranked_best=chosen["path"] if chosen else None))
    return script_best


def select(output, records, output_dir, top=5):
    """-> report dict with the chosen checkpoint plus the evidence for it."""
    inv = inventory(output, records, output_dir, top=top)
    findings = list(inv["findings"])
    ranked, with_files = inv["ranked"], inv["ranked_with_files"]
    best_by = inv["best_by"]

    chosen = None
    if ranked:
        if with_files:
            e = with_files[0]
            chosen = {"path": e["file"]["path"], "metric": best_by, "value": e["value"],
                      e["index_kind"]: e["index"], "evidence_record": e.get("record")}
        else:
            findings.append(finding(
                "fail", "best_record_has_no_file",
                f"the best record ({best_by}={ranked[0]['value']} at "
                f"{ranked[0]['index_kind']} {ranked[0]['index']}) has no checkpoint file — "
                f"either the script did not save it, or path_pattern is wrong",
                best_value=ranked[0]["value"], index=ranked[0]["index"]))
        if chosen and chosen["value"] != ranked[0]["value"]:
            findings.append(finding(
                "warn", "best_record_skipped",
                f"the top-ranked record ({best_by}={ranked[0]['value']} at "
                f"{ranked[0]['index_kind']} {ranked[0]['index']}) has no file on disk; fell "
                f"through to {chosen['value']}. Legitimate when the script saves every Nth "
                f"epoch — but the run's recorded metric must then be {chosen['value']}, the "
                f"value of the checkpoint that exists, not {ranked[0]['value']}. Recording "
                f"the stream's peak next to a different artifact is a fake metric.",
                top_value=ranked[0]["value"], chosen_value=chosen["value"],
                record_this_value=chosen["value"]))

    script_best = _cross_check_script_best(inv["output_dir"], chosen, findings)

    return {
        "verdict": verdict_of(findings),
        "best_by": best_by,
        "direction": inv["direction"],
        "chosen": chosen,
        "script_saved_best": script_best,
        # The reconciliation the whole script exists for: the ranking with the
        # values as they literally appear in the jsonl.
        "ranking": [{"rank": i + 1, "value": e["value"], e["index_kind"] or "index": e["index"],
                     "record_type": e.get("record_type")}
                    for i, e in enumerate(ranked[:top])],
        "ranked_total": len(ranked),
        "files_on_disk": len(inv["files"]),
        "findings": findings,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pick the best checkpoint and show the evidence.")
    ap.add_argument("output_json")
    ap.add_argument("jsonl")
    ap.add_argument("--output-dir", required=True, help="where checkpoint files live")
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)

    try:
        output, records, _ = load_inputs(args.output_json, args.jsonl)
    except StreamError as e:
        sys.stderr.write(f"select_checkpoint: {e}\n")
        return 2

    return emit(select(output, records, args.output_dir, args.top), "select_checkpoint")


if __name__ == "__main__":
    sys.exit(main())
