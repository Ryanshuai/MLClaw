#!/usr/bin/env python3
"""Apply a checkpoint retention policy — the only irreversible thing MLClaw does.

`keep_best_only` deletes real files. If the ranking behind it is wrong, the best
model is gone and there is no undo. The existing safeguard is "confirm with the
user before any deletion", which is not a safeguard: the user is shown a list of
filenames, and a list of filenames contains no evidence about whether the sort
that produced it was right. You cannot eyeball `epoch_43.pt` and tell that it
should have been kept.

So deletion is split in two, and the plan carries the numbers:

  plan   ranks every file, prints the metric value that decided its fate, runs
         five abort checks, writes retention_plan.json. Deletes nothing, ever.
  apply  re-stats every file against the plan's digest, refuses on any drift,
         requires the plan's own confirmation token, then deletes.

Ranking is imported from select_checkpoint.inventory() rather than recomputed —
and so is the ranking-to-file join (`best_file`), because half a shared ranking
is not a shared ranking: whoever re-walks the ranking to find "the best one that
has a file" can reach a different file than the keeper selection did. Two
rankings that disagree is precisely how the best model gets deleted while the
log says the best model was kept.

Abort conditions (plan refuses to produce a deleting plan):
  1. the ranking itself has a `fail` finding — an unreliable rank must not drive rm
  2. the chosen best is not in the keep set
  3. the keep set is empty
  4. a file scheduled for deletion has no metric — never delete what you cannot rank
  5. every file is scheduled for deletion

Exit: 0 ok / 1 refused (a verdict, not a crash) / 2 script error.

Usage:
    python retention.py plan  <output_json> <jsonl> --output-dir <dir> --plan <path>
    python retention.py apply --plan <path> --confirm <token>
"""
import argparse
import hashlib
import json
import os
import sys

from _stream import StreamError, emit, finding, load_inputs
from select_checkpoint import inventory

DEFAULT_KEEP_LAST_N = 3
POLICIES = ("keep_all", "keep_last_n", "keep_best_only", "keep_best_and_last")


def _digest(entry):
    st = os.stat(entry["path"])
    return {"path": entry["path"], "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns}


def _sort_index(f):
    v = f.get("epoch")
    if v is None:
        v = f.get("step")
    return v if v is not None else -1


def _policy_of(output):
    """-> (policy, n). Accepts both the bare string and the {policy, n} form."""
    raw = (output.get("checkpoints") or {}).get("retention", "keep_all")
    if isinstance(raw, dict):
        return raw.get("policy", "keep_all"), raw.get("n", DEFAULT_KEEP_LAST_N)
    return raw or "keep_all", DEFAULT_KEEP_LAST_N


def _keep_reasons(files, policy, n, best_file, best_by, direction):
    """-> {path: why it survives}. The best is written first under every policy."""
    keep_reason = {}
    if policy == "keep_all":
        for f in files:
            keep_reason[f["path"]] = "policy keep_all"
        return keep_reason

    if best_file:
        keep_reason[best_file["path"]] = f"best: {best_by}={best_file['metric']} ({direction})"
    by_recency = sorted(files, key=_sort_index, reverse=True)
    if policy == "keep_best_and_last" and by_recency:
        keep_reason.setdefault(by_recency[0]["path"], f"last: {_label(by_recency[0])}")
    elif policy == "keep_last_n":
        for f in by_recency[:n]:
            keep_reason.setdefault(f["path"], f"among the {n} most recent: {_label(f)}")
    return keep_reason


def _decisions(files, keep_reason, best_by, policy):
    """One row per file, each carrying the number that decided its fate."""
    return [{
        "path": f["path"],
        "epoch": f.get("epoch"),
        "step": f.get("step"),
        # The number that decided this file's fate, printed next to it.
        # This is the whole point: a filename list is not reviewable.
        "metric": f.get("metric"),
        "metric_name": best_by,
        "size_bytes": f["size_bytes"],
        "fate": "keep" if f["path"] in keep_reason else "delete",
        "reason": keep_reason.get(f["path"], f"not selected by policy {policy}"),
    } for f in sorted(files, key=_sort_index)]


def _abort_checks(inv, best_file, keep_reason, to_delete, to_keep):
    """The five conditions under which no deleting plan may be produced."""
    best_by, files = inv["best_by"], inv["files"]
    findings = []
    if any(f["level"] == "fail" for f in inv["findings"]):
        findings.append(finding(
            "fail", "ranking_unreliable",
            "the checkpoint ranking has unresolved failures — refusing to delete anything "
            "on the basis of a ranking that is known to be wrong"))
    if to_delete and best_file is None:
        findings.append(finding(
            "fail", "no_best_identified",
            "no checkpoint could be identified as best, yet the policy would delete files"))
    if to_delete and best_file and best_file["path"] not in keep_reason:
        findings.append(finding(
            "fail", "best_not_kept",
            f"the best checkpoint ({best_file['path']}, {best_by}={best_file['metric']}) "
            f"is not in the keep set", path=best_file["path"]))
    if files and not to_keep:
        findings.append(finding(
            "fail", "keep_set_empty",
            "the policy would delete every checkpoint and keep none"))
    unranked = [d for d in to_delete if d["metric"] is None]
    if unranked:
        findings.append(finding(
            "fail", "deleting_unranked_files",
            f"{len(unranked)} file(s) scheduled for deletion have no metric in the stream. "
            f"A file that cannot be ranked cannot be shown to be worse than the one kept — "
            f"never delete what you cannot rank",
            paths=[d["path"] for d in unranked][:10]))
    return findings


def build_plan(output, records, output_dir):
    """Pure planning. Never touches the filesystem beyond stat/glob."""
    inv = inventory(output, records, output_dir)
    findings = list(inv["findings"])
    files = inv["files"]
    best_by, direction = inv["best_by"], inv["direction"]
    # Not re-derived here: `best_file` is the same join the keeper selection used.
    best_file = inv["best_file"]

    policy, n = _policy_of(output)
    if policy not in POLICIES:
        findings.append(finding("fail", "policy_unknown",
                                f"retention policy {policy!r} is not one of {', '.join(POLICIES)}"))
        return _refuse(findings, policy, files, inv)

    keep_reason = _keep_reasons(files, policy, n, best_file, best_by, direction)
    decisions = _decisions(files, keep_reason, best_by, policy)
    to_delete = [d for d in decisions if d["fate"] == "delete"]
    to_keep = [d for d in decisions if d["fate"] == "keep"]
    findings += _abort_checks(inv, best_file, keep_reason, to_delete, to_keep)

    refused = any(f["level"] == "fail" for f in findings)
    plan = {
        "verdict": "refused" if refused else ("ok" if not to_delete else "ready"),
        "policy": policy,
        "keep_last_n": n if policy == "keep_last_n" else None,
        "metric": best_by,
        "direction": direction,
        "output_dir": output_dir,
        "best": {"path": best_file["path"], "value": best_file["metric"]} if best_file else None,
        "decisions": decisions,
        "summary": {"total": len(files), "keep": len(to_keep), "delete": len(to_delete),
                    "bytes_freed": sum(d["size_bytes"] for d in to_delete)},
        "findings": findings,
    }
    if not refused and to_delete:
        plan["digest"] = [_digest(f) for f in files]
        plan["confirm_token"] = _token(plan)
    return plan


def _label(f):
    if f.get("epoch") is not None:
        return f"epoch {f['epoch']}"
    if f.get("step") is not None:
        return f"step {f['step']}"
    return os.path.basename(f["path"])


def _refuse(findings, policy, files, inv):
    return {"verdict": "refused", "policy": policy, "keep_last_n": None,
            "metric": inv["best_by"], "direction": inv["direction"],
            "output_dir": inv["output_dir"], "best": None,
            "decisions": [], "summary": {"total": len(files), "keep": 0, "delete": 0,
                                         "bytes_freed": 0},
            "findings": findings}


def _token(plan):
    """A token derived from the plan's content, so a stale plan cannot authorize."""
    material = json.dumps({"decisions": plan["decisions"], "digest": plan["digest"]},
                          sort_keys=True).encode()
    return hashlib.sha256(material).hexdigest()[:16]


def apply_plan(plan, confirm):
    """Re-verify, then delete. -> (report, exit_code)."""
    if plan.get("verdict") == "refused":
        return {"applied": False, "reason": "the plan was refused at planning time"}, 1
    if not plan.get("digest"):
        return {"applied": False, "reason": "the plan deletes nothing"}, 0
    if confirm != plan.get("confirm_token"):
        return {"applied": False,
                "reason": "confirmation token does not match this plan — pass the "
                          "`confirm_token` from the plan file itself"}, 1

    drift = []
    for d in plan["digest"]:
        if not os.path.isfile(d["path"]):
            drift.append(f"{d['path']}: gone since the plan was made")
            continue
        st = os.stat(d["path"])
        if st.st_size != d["size_bytes"] or st.st_mtime_ns != d["mtime_ns"]:
            drift.append(f"{d['path']}: changed since the plan was made")
    known = {d["path"] for d in plan["digest"]}
    import glob as globmod
    for extra in globmod.glob(os.path.join(plan["output_dir"], "*")):
        if os.path.isfile(extra) and extra not in known and extra.endswith(
                tuple({os.path.splitext(p)[1] for p in known} or {".pt"})):
            drift.append(f"{extra}: new checkpoint appeared after the plan was made")

    if drift:
        return {"applied": False, "reason": "the checkpoint directory changed since the plan "
                                            "was made; re-plan rather than deleting against a "
                                            "stale ranking", "drift": drift}, 1

    deleted, failed = [], []
    for d in plan["decisions"]:
        if d["fate"] != "delete":
            continue
        try:
            os.remove(d["path"])
            deleted.append(d["path"])
        except OSError as e:
            failed.append({"path": d["path"], "error": str(e)})

    return {"applied": True, "deleted": deleted, "failed": failed,
            "kept": [d["path"] for d in plan["decisions"] if d["fate"] == "keep"],
            "bytes_freed": plan["summary"]["bytes_freed"]}, (1 if failed else 0)


def _print_table(plan):
    w = max([len(os.path.basename(d["path"])) for d in plan["decisions"]] + [4])
    sys.stderr.write(f"\n  {'file'.ljust(w)}  {'idx':>6}  {(plan['metric'] or 'metric'):>12}  fate    why\n")
    for d in plan["decisions"]:
        idx = d["epoch"] if d["epoch"] is not None else d["step"]
        val = "—" if d["metric"] is None else f"{d['metric']:.6g}"
        sys.stderr.write(f"  {os.path.basename(d['path']).ljust(w)}  {str(idx):>6}  {val:>12}  "
                         f"{d['fate']:<6}  {d['reason']}\n")
    s = plan["summary"]
    sys.stderr.write(f"\n  keep {s['keep']} / delete {s['delete']} of {s['total']}"
                     f"  ({s['bytes_freed'] / 1e6:.1f} MB would be freed)\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="compute keep/delete with justification; deletes nothing")
    p.add_argument("output_json")
    p.add_argument("jsonl", nargs="?", help="path to the metric stream; omit and pass "
                                            "--run-dir to resolve it")
    p.add_argument("--run-dir", help="resolve the stream from the run dir — prefers the "
                                    "normalized stream.jsonl over the raw source")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--plan", help="write the plan here (default: <output-dir>/retention_plan.json)")

    a = sub.add_parser("apply", help="execute a plan after re-verifying the directory")
    a.add_argument("--plan", required=True)
    a.add_argument("--confirm", required=True, help="the confirm_token from the plan file")

    args = ap.parse_args(argv)

    if args.cmd == "plan":
        try:
            output, records, _, _kind = load_inputs(args.output_json, args.jsonl,
                                                    args.run_dir)
        except StreamError as e:
            sys.stderr.write(f"retention: {e}\n")
            return 2

        plan = build_plan(output, records, args.output_dir)
        dest = args.plan or os.path.join(args.output_dir, "retention_plan.json")
        try:
            with open(dest, "w") as f:
                json.dump(plan, f, indent=2)
        except OSError as e:
            sys.stderr.write(f"retention: cannot write plan to {dest}: {e}\n")
            return 2

        if plan["decisions"]:
            _print_table(plan)
        # `refused` is exit 1 — the script worked and the answer is no. Never 2:
        # an agent reading 2 falls back to deleting by hand, which is the one
        # thing this refusal exists to prevent.
        code = emit(plan, "retention", fail_verdicts=("refused",),
                    payload={"verdict": plan["verdict"], "plan_path": dest,
                             "summary": plan["summary"],
                             "confirm_token": plan.get("confirm_token")})
        if code:
            sys.stderr.write("retention: REFUSED — no plan written that would delete anything\n")
        return code

    try:
        with open(args.plan) as f:
            plan = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"retention: cannot read plan {args.plan}: {e}\n")
        return 2

    report, code = apply_plan(plan, args.confirm)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    if not report.get("applied"):
        sys.stderr.write(f"retention: not applied — {report['reason']}\n")
        for d in report.get("drift", []):
            sys.stderr.write(f"retention:   {d}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
