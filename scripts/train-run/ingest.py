"""Ingest a run's metric source into the normalized stream.

    python ingest.py <output_json> --run-dir <RUN_DIR>

One records layer, N thin source adapters, and sinks that share it. Not a
per-format converter: a `tb_to_stream.py` alongside a `wandb_to_stream.py` would
each re-implement grouping, provenance stamping, and the write discipline, and
they would drift. See `references/run-mechanics.md` -> "Metric stream"
for the vocabulary (source / stream / record) and the rules enforced here.

Two of those rules are the reason this file is small and boring on purpose:

  no renaming   A field arrives named whatever the author named it and leaves
                named that. `train/loss` stays `train/loss`. reconcile_metrics
                compares the declared schema against these records, and a layer
                that tidies names launders exactly the mismatch it is there to
                catch.
  no invented   `type` is written only for sources that actually declare a
  record type   record's kind. Loose (tag, step, value) triples do not, and
                stamping a constant would make find_type_key report full
                coverage for a classification that never happened.

Everything above `read_tfevents` is stdlib-only and pure, so the logic that can
be wrong is reachable from `contracts/` on a machine with no ML packages
installed. The dependency-bound adapters sit at the bottom and are imported
lazily, in the training environment, where the writer's own version lives.
"""
import argparse
import json
import os
import re
import sys

from _stream import (CANONICAL_STREAM, INDEX_KEYS, Refusal, StreamError, emit,
                     finding, read_jsonl, refusal_report, verdict_of)

STREAM_META = "stream_meta.json"


# Grouping rules for sources that emit loose triples. There is no universally
# correct choice — see run-mechanics.md -> "Metric stream" — so the decision is
# recorded in output.json and stamped on every record, never defaulted silently.
GROUP_BY = ("step", "step+namespace", "step+wall_time")

# The x-axis preference for *plotting*, which is deliberately not the ranking order
# in `_stream.index_of`. Ranking asks "which observation is this" and prefers
# `epoch`; a curve wants the densest monotone axis, and preferring `epoch` would
# collapse every step inside an epoch onto one x. Same names, different order,
# stated rather than assumed -- and the assert below is what actually keeps the
# two from drifting apart; retyping the five names here does not.
PLOT_INDEX_KEYS = ("step", "global_step", "iteration", "iter", "epoch")
assert set(PLOT_INDEX_KEYS) == set(INDEX_KEYS), (
    "PLOT_INDEX_KEYS must name the same keys as _stream.INDEX_KEYS, only "
    "reordered -- a key added to one and not the other silently drops out of "
    "either ranking or plotting")

# Keys that are never a curve: the indices themselves (a plot of `step` against
# `step` is a diagonal), the record kind, and the timestamp.
_NOT_A_CURVE = frozenset(INDEX_KEYS + ("type", "wall_time"))


def _num(text):
    """'3' -> 3, '1e-4' -> 0.0001, 'cosine' -> 'cosine'. Regex groups arrive as
    strings; a metric left as a string is silently skipped by every ranking."""
    # `int()` first would construct and raise a ValueError for every float, which on
    # a metrics log is most values — measured 4.6x slower over 500k tokens.
    if text.isdigit() or (text[:1] in "+-" and text[1:].isdigit()):
        return int(text)
    try:
        return float(text)
    except ValueError:
        return text


def namespace_of(tag):
    """'train/loss' -> 'train'; 'loss' -> ''. The author's own hierarchy is the
    only grouping signal a triple carries."""
    return tag.split("/", 1)[0] if "/" in tag else ""


def records_from_triples(triples, group_by="step", src="tensorboard"):
    """[(tag, step, wall_time, value)] -> (records, notes).

    Triples are what a scalar-series source gives you: no notion of which values
    were observed together. Grouping reconstructs that, which is why the rule
    used is stamped on every record as `_group` — a reader has to be able to tell
    a co-occurrence the author wrote from one this function assembled.

    A step that appears twice for the same key with different values is a restart
    re-running steps. Last write wins and the collision is counted, because the
    alternative is a ranking over a stream where one step holds two values.
    """
    if group_by not in GROUP_BY:
        raise Refusal("group_by_invalid",
                      f"normalize.group_by is {group_by!r}; expected one of "
                      f"{', '.join(GROUP_BY)}")
    if group_by == "step+wall_time":
        raise Refusal("group_by_unimplemented",
                      "group_by 'step+wall_time' is not implemented — it needs an epsilon "
                      "nobody can set correctly. Use 'step' or 'step+namespace'")

    # Hoisted out of the loop: the mode test is loop-invariant, and a tag's namespace
    # has a handful of distinct values across a million triples. Provenance goes in
    # the literal rather than in a second pass over the finished records.
    by_ns = group_by == "step+namespace"
    ns_memo = {}
    groups, overwritten = {}, 0
    for tag, step, wall_time, metric_value in triples:
        if by_ns:
            ns = ns_memo.get(tag)
            if ns is None:
                ns = ns_memo[tag] = namespace_of(tag)
            key = (step, ns)
        else:
            key = (step, "")
        rec = groups.get(key)
        if rec is None:
            rec = groups[key] = {"step": step, "wall_time": wall_time,
                                 "_src": src, "_group": group_by}
        elif tag in rec:
            overwritten += 1
        rec[tag] = metric_value
        # Earliest observation in the group dates it; a later restart writing the
        # same step should not make the record look newer than the run.
        prev = rec["wall_time"]
        if wall_time is not None and (prev is None or wall_time < prev):
            rec["wall_time"] = wall_time

    # Keys are already `(step, namespace)`; C tuple comparison gives the same order
    # as a key function without a Python call per group.
    records = [groups[k] for k in sorted(groups)]
    return records, {"overwritten_by_restart": overwritten}


def records_from_stdout_regex(path, extractor, src="stdout_regex"):
    """Plain-text prints -> records. Named groups become fields under their own
    names; the pattern names the record type, so `type` is known here and gets
    written — unlike a triple source, where it would be a fabrication.
    """
    patterns = (extractor or {}).get("patterns") or []
    if not patterns:
        raise Refusal("extractor_missing",
                      "log_format is stdout_regex but metrics.stdout_extractor has "
                      "no patterns")
    compiled = []
    for i, p in enumerate(patterns):
        try:
            compiled.append((p.get("type"), re.compile(p["regex"])))
        except (KeyError, re.error) as e:
            raise Refusal("extractor_unusable",
                          f"stdout_extractor.patterns[{i}] is unusable: {e}")

    if not os.path.isfile(path):
        raise StreamError(f"no stdout to extract from: {path}")
    records, lines_matched = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            for rtype, rx in compiled:
                m = rx.search(line)
                if not m:
                    continue
                rec = {k: _num(v) for k, v in m.groupdict().items() if v is not None}
                if rtype:
                    rec["type"] = rtype
                rec["_src"] = src
                rec["_group"] = "pattern"
                records.append(rec)
                lines_matched += 1
                break
    # A pattern emits one record per line today, so this equals len(records); it is
    # kept separate because a multi-record pattern would make them differ.
    return records, {"lines_matched": lines_matched}


def read_tfevents(log_dir):
    """tfevents -> (triple iterator, dropped).

    The triples are yielded, not listed: `records_from_triples` consumes them exactly
    once, and at 10^6 events a list costs ~133 MB held alive next to the grouped
    records and EventAccumulator's own unbounded scalar store — three copies of the
    same numbers, every monitoring tick. The one dependency-bound adapter; run it in
    the training environment, where the writer's own tensorboard version is
    installed.

    `size_guidance={'scalars': 0}` is not a tuning knob. EventAccumulator's default
    caps scalars and reservoir-samples the excess, so a long run's peak epoch can be
    dropped — producing a complete, plausible, wrong best-checkpoint pick. 0 means
    load everything.

    **Only `scalars` is overridden, and that asymmetry is deliberate: in this API
    `0` means unbounded, not "none".** `{'images': 0}` would pull every sample image
    a run ever logged into memory. The defaults (4 images, 4 audio, 1 histogram) are
    what we want for the tag kinds we do not ingest.

    `dropped` names the non-scalar tag kinds that were present. A run's images do
    not belong in the stream — nothing ranks a checkpoint by a segmentation mask —
    but dropping them silently makes "MLClaw ignored your images" indistinguishable
    from "the run logged none", and the user who logged them will go looking.
    """
    try:
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
    except ImportError as e:
        raise StreamError(
            f"reading tfevents needs the `tensorboard` package in the environment "
            f"running this script ({e}). It is installed wherever the training code "
            f"wrote the events — run ingest there, not in MLClaw's env")

    acc = EventAccumulator(log_dir, size_guidance={"scalars": 0})
    acc.Reload()
    tags = acc.Tags() or {}

    def triples():
        for tag in tags.get("scalars", []):
            for ev in acc.Scalars(tag):
                yield tag, ev.step, ev.wall_time, ev.value

    dropped = {}
    for kind in ("images", "histograms", "compressedHistograms", "audio", "tensors"):
        present = tags.get(kind) or []
        if present:
            # Names only. Loading the payloads is the thing this avoids.
            dropped[kind] = sorted(present) if isinstance(present, list) else len(present)
    if tags.get("graph"):
        dropped["graph"] = True
    return triples(), dropped


def stamp(records, src, group):
    """Provenance for records an adapter did not build itself — the jsonl path, where
    the dicts come straight out of `read_jsonl`. Adapters that construct records put
    both keys in the literal instead; this is the one case with nothing to construct.

    Which layer a number came from is the difference between 'the code reported this'
    and 'we assembled it', and a reader three months out cannot re-derive it.
    """
    for rec in records:
        rec["_src"] = src
        rec["_group"] = group
    return records


def collect(output_json, run_dir):
    """-> (records, meta, findings).

    Each branch answers only the format-specific question — which file, which
    adapter, which grouping label, what to warn about — and the shared tail builds
    `meta`. Keeping the tail in one place is what makes "the `_group` on a record
    equals the `group_by` in the sidecar" true by construction rather than by three
    matched pairs of literals, which is how it was written first.
    """
    metrics = (output_json or {}).get("metrics") or {}
    fmt = metrics.get("log_format") or ""
    if not fmt:
        raise Refusal("log_format_empty",
                      "output.json -> metrics.log_format is empty; /train-init records "
                      "the source format and nothing can be read without it")

    log_path = metrics.get("log_path") or ""
    group_by = ((metrics.get("normalize") or {}).get("group_by")) or "step"
    findings, notes, inferred, dropped = [], {}, [], {}

    if fmt in ("jsonl", "jsonl_stdout"):
        src_path = (os.path.join(run_dir, log_path) if fmt == "jsonl"
                    else os.path.join(run_dir, "logs", "stdout.log"))
        records, line_errors = read_jsonl(src_path)
        # The author already decided which values were observed together; there is
        # nothing to group and nothing to guess. These records were not constructed
        # here, so they are the one path that needs a stamping pass.
        group = "author"
        stamp(records, fmt, group)
        if line_errors:
            findings.append(finding("warn", "malformed_lines",
                                    f"{len(line_errors)} unparseable line(s) in the source",
                                    count=len(line_errors), first=line_errors[0]))

    elif fmt == "stdout_regex":
        src_path = os.path.join(run_dir, "logs", "stdout.log")
        group = "pattern"
        records, notes = records_from_stdout_regex(src_path, metrics.get("stdout_extractor"),
                                                  fmt)
        if not records:
            findings.append(finding("fail", "extractor_matched_nothing",
                                    "stdout_extractor matched no line — the regex and the "
                                    "code's print format have diverged, and a run monitored "
                                    "this way would report no progress rather than an error"))

    elif fmt == "tensorboard":
        src_path = os.path.join(run_dir, log_path)
        _refuse_render_target(run_dir, src_path)
        group = group_by
        triples, dropped = read_tfevents(src_path)
        records, notes = records_from_triples(triples, group_by, fmt)
        # No `type` was determined and none is invented; say so where a reader looks.
        inferred.append({"field": "type", "status": "absent",
                         "why": "tfevents carries no record-kind; records are classified "
                                "downstream by field set"})
        if dropped:
            findings.append(finding(
                "warn", "non_scalar_tags_not_ingested",
                f"the source also holds {', '.join(sorted(dropped))} — not in the stream, "
                f"because nothing ranks a checkpoint by an image. View them with "
                f"`tensorboard --logdir {src_path}`",
                kinds=sorted(dropped)))
        if notes.get("overwritten_by_restart"):
            findings.append(finding("warn", "overlapping_steps",
                                    f"{notes['overwritten_by_restart']} tag/step collisions — "
                                    f"a restart re-ran steps; last write won",
                                    count=notes["overwritten_by_restart"]))

    else:
        raise Refusal("no_adapter",
                      f"log_format {fmt!r} has no adapter. `wandb` is recorded by "
                      f"/train-init but is not readable yet; see run-mechanics.md -> "
                      f"\"Metric stream\"")

    meta = {"source_format": fmt, "group_by": group, "sources": [_stat(src_path)],
            "notes": notes, "inferred": inferred, "not_ingested": dropped}
    return records, meta, findings


def _refuse_render_target(run_dir, path):
    """`<RUN_DIR>/tb/` holds tfevents MLClaw rendered from the stream. Ingesting it
    would feed our own derived numbers back in as if the code had reported them,
    and the resulting curves look entirely reasonable."""
    tb = os.path.realpath(os.path.join(run_dir, "tb"))
    target = os.path.realpath(path)
    if target == tb or target.startswith(tb + os.sep):
        raise Refusal("render_target_as_source",
                      f"refusing to ingest {path}: that is MLClaw's own render target, "
                      f"not a source the training code wrote")


def _stat(path):
    try:
        st = os.stat(path)
        return {"path": path, "mtime": st.st_mtime, "bytes": st.st_size}
    except OSError:
        return {"path": path, "mtime": None, "bytes": None}


def write_stream(run_dir, records, meta):
    """Write `<run_dir>/stream.jsonl` whole, then `stream_meta.json`. -> path.

    Temp file plus `os.replace`, because monitor and the user may be reading it,
    and a half-written stream is indistinguishable from a truncated one. Whole,
    not appended: a restart's overlapping steps mean an incremental writer bakes
    in a resolution that later events invalidate.
    """
    path = os.path.join(run_dir, CANONICAL_STREAM)
    stream_tmp = path + ".tmp"
    with open(stream_tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    os.replace(stream_tmp, path)

    meta = dict(meta, stream=CANONICAL_STREAM, records=len(records))
    meta_tmp = os.path.join(run_dir, STREAM_META + ".tmp")
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(meta_tmp, os.path.join(run_dir, STREAM_META))
    return path


def tb_points(records):
    """records -> [(tag, value, step, wall_time)]. Pure; the selection logic lives
    here so it is reachable from `contracts/` without a writer installed.

    Tag names are the field names, verbatim — the no-rename rule holds on the way
    out too, so a curve in TensorBoard is labelled what the author called it.
    Provenance keys, the record type, and anything non-numeric are not points.
    """
    points = []
    for rec in records:
        step = None
        for key in PLOT_INDEX_KEYS:
            v = rec.get(key)
            if not isinstance(v, bool) and isinstance(v, (int, float)):
                step = int(v)
                break
        if step is None:
            continue  # a point with no x has nowhere to go on a curve
        wall = rec.get("wall_time")
        for field, metric_value in rec.items():
            if field in _NOT_A_CURVE or field[0] == "_":
                continue
            if isinstance(metric_value, bool) or not isinstance(metric_value, (int, float)):
                continue
            points.append((field, float(metric_value), step, wall))
    return points


def _open_tb_writer(log_dir):
    """-> a SummaryWriter, or None when no writer package is importable.

    `filename_suffix=".mlclaw"` makes the file self-identifying: this directory
    holds events MLClaw rendered, and a careless glob elsewhere must be able to
    tell them from events a training run wrote.
    """
    for mod, attr in (("torch.utils.tensorboard", "SummaryWriter"),
                      ("tensorboardX", "SummaryWriter")):
        try:
            m = __import__(mod, fromlist=[attr])
        except ImportError:
            continue
        return getattr(m, attr)(log_dir=log_dir, filename_suffix=".mlclaw")
    return None


def write_tb(run_dir, records, source_format):
    """Render `<run_dir>/tb/` for viewing. -> a dict for `stream_meta.json`.

    On by default, meaning *whenever it is possible and useful*, which is two
    conditions:

    - **Not when the source already is tfevents.** `--logdir <RUN_DIR>` overlays
      every subdirectory as a separate run, so rendering the same scalars again
      would draw every curve twice under two names. The code's own file is the
      view; this function has nothing to add to it.
    - **Not when no writer is importable.** Reported as a warning, never a failure:
      a missing viewer package must not break a run's monitoring.

    Append-only, unlike the stream. TensorBoard tails its files, so a rewritten one
    reads as steps going backwards and the live view breaks. The watermark is a
    count of records already rendered; the stream is re-derived deterministically in
    a stable order, so "the first N" is a well-defined thing to have already sent.
    Losing the watermark duplicates a curve in a picture — acceptable for something
    no decision reads, and the reason this file and the stream are separate.
    """
    if source_format == "tensorboard":
        return {"rendered": False, "warn_code": None,
                "why": "source is already tfevents; `tensorboard --logdir` reads the "
                       "code's own events"}

    tb_dir = os.path.join(run_dir, "tb")
    mark_path = os.path.join(tb_dir, ".watermark")
    sent = 0
    if os.path.isfile(mark_path):
        try:
            with open(mark_path) as f:
                sent = int((json.load(f) or {}).get("records") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            sent = 0

    reset = sent > len(records)
    if reset:
        # The stream got shorter than what we already rendered — a re-derive after a
        # config change. Appending would leave contradictory steps in one file, so
        # start the render over. Only ever inside `tb/`, which is ours and derived.
        for name in os.listdir(tb_dir) if os.path.isdir(tb_dir) else []:
            try:
                os.remove(os.path.join(tb_dir, name))
            except OSError:
                pass
        sent = 0

    fresh = records[sent:]
    if not fresh and not reset:
        return {"rendered": True, "warn_code": None, "appended": 0, "watermark": sent}

    os.makedirs(tb_dir, exist_ok=True)
    writer = _open_tb_writer(tb_dir)
    if writer is None:
        # A code, not a sentence: `main` promotes this to a finding, and deciding that
        # by substring-matching prose means rewording the message silently disables it.
        return {"rendered": False, "warn_code": "tb_writer_unavailable",
                "why": "no writer package importable (tried torch.utils.tensorboard, "
                       "tensorboardX) — curves are unavailable for this run, the stream "
                       "is unaffected"}

    points = tb_points(fresh)
    try:
        for tag, metric_value, step, wall in points:
            writer.add_scalar(tag, metric_value, global_step=step, walltime=wall)
        writer.flush()
    finally:
        writer.close()

    with open(mark_path, "w") as f:
        json.dump({"records": len(records)}, f)
    return {"rendered": True, "warn_code": None, "appended": len(points),
            "watermark": len(records), "reset": reset, "logdir": tb_dir}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("output_json")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--no-tb", action="store_true",
                    help="skip the TensorBoard render. On by default; this is for a "
                         "batch re-derive over many runs, where nobody is looking.")
    args = ap.parse_args(argv)

    # Every outcome below emits this shape. Built once, so "a caller must not have to
    # know which branch produced a report to read `stream`" is structural.
    report = {"findings": [], "verdict": "ok", "stream": None, "records": 0,
              "meta": {}, "tail": None}
    try:
        with open(args.output_json) as f:
            output_json = json.load(f)
        records, meta, findings = collect(output_json, args.run_dir)
    except Refusal as e:
        # Exit 1: the answer is no. Nothing is written — a stream derived from a
        # source we refused would be the harm the refusal exists to prevent.
        report.update(refusal_report(e))
        return emit(report, "ingest")
    except (StreamError, OSError, json.JSONDecodeError) as e:
        # Exit 2: could not run. The caller may fall back per CLAUDE.md ->
        # "Script Integration"; nothing here was a policy decision.
        sys.stderr.write(f"ingest: {e}\n")
        return 2

    report.update(findings=findings, verdict=verdict_of(findings), meta=meta)
    if report["verdict"] == "fail":
        return emit(report, "ingest")

    # The render runs before the stream is written only because `meta["tb"]` has to be
    # in the sidecar; nothing about the decision layer depends on it, which is why
    # every failure here degrades to a finding. `except Exception` and not `OSError`:
    # an exception escaping a third-party writer would exit 1, and CLAUDE.md ->
    # "Script Integration" reads exit 1 as a deliberate refusal — a viewer bug must
    # never be able to speak in the record layer's exit codes.
    if args.no_tb:
        meta["tb"] = {"rendered": False, "warn_code": None, "why": "--no-tb"}
    else:
        try:
            meta["tb"] = write_tb(args.run_dir, records, meta.get("source_format"))
        except Exception as e:  # noqa: BLE001 — a leaf viewer, deliberately contained
            meta["tb"] = {"rendered": False, "warn_code": "tb_render_failed",
                          "why": f"render failed: {type(e).__name__}: {e}"}
    if meta["tb"].get("warn_code"):
        findings.append(finding("warn", meta["tb"]["warn_code"], meta["tb"]["why"]))
        report["verdict"] = verdict_of(findings)

    try:
        report["stream"] = write_stream(args.run_dir, records, meta)
    except OSError as e:
        sys.stderr.write(f"ingest: cannot write the stream: {e}\n")
        return 2
    report["records"] = len(records)
    # The tail travels in the report so the monitor does not re-read and re-parse the
    # whole stream to find the newest values — a 41 MB file at 10^6 events, and a
    # second ssh round trip on a remote run.
    report["tail"] = records[-1] if records else None
    return emit(report, "ingest")


if __name__ == "__main__":
    sys.exit(main())
