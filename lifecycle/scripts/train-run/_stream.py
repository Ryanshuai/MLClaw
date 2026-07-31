"""Shared reader — and shared CLI scaffolding — for a training run's jsonl metric stream.

Imported by the three reconciliation scripts in this directory (sibling import
works because Python puts a script's own directory on sys.path).

The schema in `output.json -> metrics.record_types` names record types and their
fields, but it never says *how a record declares its type* — different codebases
use `type`, `event`, `tag`, `split`, `phase`, or nothing at all. So the
discriminator is inferred from the stream rather than assumed, and when it can't
be inferred that is reported, not papered over. Guessing wrong here would
misattribute every record and quietly invalidate every check built on top.

The three scripts also share their whole outer shell — a finding record, a
verdict rule, a load-or-exit-2 preamble, and a dump-JSON-echo-findings epilogue.
Those live here too, because the thing they encode is a contract, not a
convenience: CLAUDE.md -> "Script Integration" splits **exit 2 (the script broke,
redo the work by hand)** from **exit 1 (the script worked and the answer is no)**.
Three copies of that rule is three chances for one of them to return 2 where it
meant 1, and an agent that reads a 2 will hand-override a safety refusal.
`normalize_direction` is re-exported from `shared/compare.py` for the same
reason: one config value must not be legal for `/eval-run` and fatal here.
"""
import json
import os
import re
import sys
from difflib import get_close_matches

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "shared"))
from compare import normalize_direction  # noqa: E402  (re-exported; see module docstring)

# Keys a codebase plausibly uses to tag a record's kind. Ordered by how
# unambiguous they are; the stream decides, this only breaks ties.
CANDIDATE_TYPE_KEYS = ("type", "record_type", "event", "tag", "kind", "phase", "split", "stage", "mode")

# Metric names whose direction is not a matter of opinion. Used only to flag a
# contradiction between a metric's name and its declared `direction`, never to
# override what the config says.
LOWER_IS_BETTER = ("loss", "err", "error", "nll", "mae", "mse", "rmse", "perplexity", "ppl",
                   "fid", "wer", "cer", "eer")
HIGHER_IS_BETTER = ("acc", "accuracy", "map", "ap", "f1", "iou", "miou", "dice", "auc", "auroc",
                    "psnr", "ssim", "bleu", "rouge", "recall", "precision", "r2")

# Prefixes that mark a metric as measured on held-out data. A checkpoint
# selected on a training-split metric is the `train_loss`-as-`val_loss` bug.
HELD_OUT_PREFIXES = ("val", "valid", "validation", "eval", "test", "dev", "holdout")
TRAIN_PREFIXES = ("train", "training", "tr")


class StreamError(Exception):
    """The stream cannot be read at all — no reconciliation is possible.

    Every caller turns this into **exit 2**: the script could not do its job, so
    the agent falls back to doing the work by hand. It is never used for "the
    script did its job and the answer is no" — that is a `fail` finding, exit 1.
    """


# --------------------------------------------------------------------------
# findings, verdicts, and the CLI shell the three scripts share
# --------------------------------------------------------------------------

def finding(level, code, message, **extra):
    """One reportable observation. `level` is 'fail' | 'warn' | 'info'."""
    f = {"level": level, "code": code, "message": message}
    f.update(extra)
    return f


def verdict_of(findings):
    """-> 'fail' | 'warn' | 'ok'. One fail outranks any number of warns."""
    levels = {f["level"] for f in findings}
    return "fail" if "fail" in levels else ("warn" if "warn" in levels else "ok")


def stream_path(output, jsonl_path, run_dir=None):
    """Where the metric stream lives: an explicit path, else `run_dir` joined with
    `output.json -> metrics.log_path`. Raises StreamError when neither is given."""
    if jsonl_path:
        return jsonl_path
    log_path = ((output or {}).get("metrics") or {}).get("log_path") or ""
    if not run_dir or not log_path:
        raise StreamError("give a jsonl path, or --run-dir with metrics.log_path "
                          "set in output.json")
    return os.path.join(run_dir, log_path)


def load_inputs(output_json_path, jsonl_path, run_dir=None):
    """-> (output, records, line_errors). Raises StreamError on anything unreadable.

    The one preamble for all three scripts. Everything it raises is an exit-2
    condition; a caller that catches StreamError and returns 1 has told the agent
    to accept a refusal that was really a crash.
    """
    try:
        with open(output_json_path) as f:
            output = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise StreamError(f"cannot read {output_json_path}: {e}")
    records, line_errors = read_jsonl(stream_path(output, jsonl_path, run_dir))
    return output, records, line_errors


def emit(report, prog, payload=None, fail_verdicts=("fail",)):
    """Report on stdout as JSON, findings and verdict on stderr. -> exit code.

    `payload` overrides what goes to stdout when the machine-readable answer is a
    summary rather than the whole report (retention writes its plan to a file).
    `fail_verdicts` names the verdicts that mean "the answer is no" — exit 1,
    which is deliberately not exit 2: see CLAUDE.md -> "Script Integration".
    """
    json.dump(report if payload is None else payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    for f in report.get("findings") or ():
        sys.stderr.write(f"{prog}: {f['level'].upper()}: {f['message']}\n")
    sys.stderr.write(f"{prog}: verdict = {report['verdict']}\n")
    return 1 if report["verdict"] in fail_verdicts else 0


def read_jsonl(path):
    """-> (records, line_errors). Malformed lines are collected, not fatal.

    A half-written final line is normal for a stream tailed while the job runs.
    """
    if not os.path.isfile(path):
        raise StreamError(f"metric stream not found: {path}")
    records, errors = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append({"line": n, "error": str(e), "text": line[:120]})
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                errors.append({"line": n, "error": f"not an object ({type(obj).__name__})",
                               "text": line[:120]})
    return records, errors


def _count_declared(records, key, declared, floor=0):
    """How many records carry `key` naming a declared type.

    Gives up as soon as the count can no longer exceed `floor`: a key that is
    absent from the stream, or that names things nobody declared, is settled
    within a few records instead of a full pass. Returning a short count is safe
    because a key that cannot beat `floor` is never selected.
    """
    hits, budget = 0, len(records) - floor
    for r in records:
        v = r.get(key)
        if isinstance(v, str) and v in declared:
            hits += 1
        else:
            budget -= 1
            if budget <= 0:
                return hits
    return hits


def find_type_key(records, declared_types):
    """-> (key, coverage, note). `key` is None when no discriminator is credible.

    Picks the field whose observed values line up best with the declared record
    type names. Coverage is the fraction of records that key labels with a
    *declared* type — a key that exists everywhere but names nothing declared is
    worse than no key at all.

    Ties go to the earlier entry in CANDIDATE_TYPE_KEYS (they are ordered by how
    unambiguous they are), so two equally good fields always resolve the same way
    — a stream that classified one way today and another way tomorrow would move
    metrics between record types with nothing to show for it.
    """
    if not records:
        return None, 0.0, "stream is empty"
    declared = set(declared_types or ())
    if not declared:
        return None, 0.0, "output.json declares no record_types"

    key, best_hits, n = None, 0, len(records)
    for candidate in CANDIDATE_TYPE_KEYS:
        hits = _count_declared(records, candidate, declared, best_hits)
        if hits > best_hits:
            key, best_hits = candidate, hits
            if hits == n:
                break  # nothing can beat total coverage, and ties keep this key
    coverage = best_hits / n

    if key is None:
        return None, 0.0, (
            f"no field among {', '.join(CANDIDATE_TYPE_KEYS)} carries a record-type name; "
            f"records will be classified by which declared field-set they match"
        )
    if coverage < 0.5:
        return key, coverage, (
            f"`{key}` looks like the type field but only {coverage:.0%} of records carry a "
            f"declared type name — the declared record_types may not match this stream"
        )
    return key, coverage, ""


def classify(records, declared_types, type_key):
    """-> (by_type, unclassified). Falls back to field-set matching with no key."""
    by_type, unclassified = {t: [] for t in (declared_types or {})}, []

    if type_key:
        for r in records:
            t = r.get(type_key)
            if isinstance(t, str) and t in by_type:
                by_type[t].append(r)
            else:
                unclassified.append(r)
        return by_type, unclassified

    # No discriminator: a record belongs to the declared type whose fields it
    # fully contains, preferring the most specific (largest) match.
    specs = sorted(((t, set(d.get("fields") or ())) for t, d in (declared_types or {}).items()),
                   key=lambda kv: -len(kv[1]))
    for r in records:
        keys = set(r)
        for t, fields in specs:
            if fields and fields <= keys:
                by_type[t].append(r)
                break
        else:
            unclassified.append(r)
    return by_type, unclassified


def observed_fields(records):
    """-> {field: count} across a list of records."""
    counts = {}
    for r in records:
        for k in r:
            counts[k] = counts.get(k, 0) + 1
    return counts


def numeric_series(records, field):
    """-> [(index, value)] for records where `field` is numeric (bools excluded)."""
    out = []
    for i, r in enumerate(records):
        v = r.get(field)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out.append((i, float(v)))
    return out


def near_misses(name, candidates, n=5):
    """Keys in the stream that look like `name` but are not it.

    This is what catches a config declaring `val_loss` against a stream that only
    emits `loss` and `train_loss`.
    """
    others = [c for c in candidates if c != name]
    close = get_close_matches(name, others, n=n, cutoff=0.6)
    # Substring relatives ("loss" vs "val_loss") that difflib may rank low.
    base = re.sub(r"^(%s)[_/-]" % "|".join(HELD_OUT_PREFIXES + TRAIN_PREFIXES), "", name)
    for c in others:
        if c in close:
            continue
        if base and (base == re.sub(r"^\w+[_/-]", "", c) or base in c or c in name):
            close.append(c)
    return close[:n]


def _tokens(name):
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def split_of(name):
    """-> 'held_out' | 'train' | 'unknown', from the metric's name alone."""
    toks = _tokens(name)
    if any(t in HELD_OUT_PREFIXES for t in toks):
        return "held_out"
    if any(t in TRAIN_PREFIXES for t in toks):
        return "train"
    return "unknown"


def expected_direction(name):
    """-> 'max' | 'min' | None, from the metric's name alone. None means unknown."""
    toks = set(_tokens(name))
    lower = toks & set(LOWER_IS_BETTER)
    higher = toks & set(HIGHER_IS_BETTER)
    if lower and not higher:
        return "min"
    if higher and not lower:
        return "max"
    return None


def resolve_pattern(pattern, output_dir):
    """Turn `<output_dir>/checkpoint-{step}.pt` into a regex + glob.

    -> (glob_pattern, compiled_regex). The regex captures whichever of
    `{epoch}` / `{step}` the pattern uses, under those group names.
    """
    p = pattern.replace("<output_dir>", output_dir).replace("${output_dir}", output_dir)
    glob = re.sub(r"\{(epoch|step)\}", "*", p)

    rx, last = [], 0
    for m in re.finditer(r"\{(epoch|step)\}", p):
        rx.append(re.escape(p[last:m.start()]))
        rx.append(r"(?P<%s>\d+)" % m.group(1))
        last = m.end()
    rx.append(re.escape(p[last:]))
    return glob, re.compile("^" + "".join(rx) + "$")
