#!/usr/bin/env python3
"""evacuate.py -- get the work off a machine before it disappears, and PROVE it.

The recorded failure, several times over: a training run finishes, the
checkpoint comes back half-way, nobody looks, and the box is released. What is
left behind is a `.pth` with a plausible name that no longer loads and a metrics
table missing its tail. `os.path.exists` said yes the entire time, and the two
things that could have raised were both looking somewhere else -- `lease.py
release` verifies that the MACHINE is gone (the billing side), and `pool.py
release --artifacts recovered` takes the operator's word for the other side.

‼️ THE RULE THIS IS BUILT ON: **leaving a file on a box you are about to destroy
is a delete.** CLAUDE.md's 「Never delete a checkpoint outside `retention.py
plan` → `apply`. … Never delete a file you cannot rank」 therefore applies to an
evacuation, and nothing treated it that way. `plan` refuses to leave a
checkpoint behind unless a `retention.py` plan already ranked it as droppable --
the same abort condition, reached through a machine going away rather than
through `rm`.

The shape is `/data-label`'s, because it is the same operation with a different
counterparty: freeze a manifest at the SOURCE, then compute completeness against
it. Never against what arrived -- that is a tautology and it passes every
partial pull. The manifest helpers are IMPORTED from `handoff.py` rather than
reimplemented; one hashing rule written twice gets fixed once.

The transfer itself is not ours (`collect.py`'s rule). `aws s3` moves the bytes;
this decides what must move, freezes what that was, and rules on what arrived.

Verbs:
  plan       enumerate and classify what is on the box; refuse to leave what
             nothing ranked. Writes the record, moves nothing
  freeze     hash at the source and write manifest.jsonl -- the only authority
             for what was supposed to arrive
  bundle     write the ARA index -- what the input was, what came out, and
             whether code + config actually reproduce it
  push       build/run the transfer, or record that somebody else did it
  verify     read the DESTINATION back and compare against the frozen manifest
  clearance  the verdict a release reads: clear | clear_size_only | blocked
  status     open evacuations and what is blocking them
"""

import argparse
import fnmatch
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))
# The manifest primitives live in `handoff.py` because that is where the frozen
# manifest was invented, and this is the same operation with a machine on the
# other end instead of a person. Importing across skills is unusual here; the
# alternative was a second hashing implementation, and a correctness rule
# written twice gets fixed once.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "data-label"))
from _records import (atomic_write_json, broke, emit, id_stamp,  # noqa: E402
                      now_utc, read_json, refuse)
from handoff import (build_manifest, hash_file, iter_files,  # noqa: E402
                     read_manifest, write_manifest)


# What each path is. Decided once, at `plan`.
#
# ARA's four layers (arXiv:2604.24658), plus one MLClaw needs and ARA does not,
# plus the leftover bucket that is the point rather than an oversight:
#
#   src        the INPUT. Code snapshot + config + sources + environment.
#              ‼️ In architecture exploration the CODE IS THE VARIABLE, so this
#              layer is not context -- code + config IS the reproducibility
#              claim, and `plan` refuses to call a bundle reproducible when the
#              run's own `code.reproducible` says it is not.
#   evidence   the numbers and WHERE THEY CAME FROM. stream.jsonl, metrics,
#              tb/, and the raw logs -- the log belongs here rather than in a
#              bucket of its own because MLClaw's grounding rule makes the
#              transcribed log line the evidence a number was read rather than
#              recalled. Same word, same meaning, one layer down.
#   logic      the CONCLUSIONS. `/conclude`'s record: what is believed, on what,
#              and what would overturn it. ARA's `logic/claims.md`.
#   trace      the exploration DAG -- `/explore`'s graph, findings, baseline,
#              audit. ARA's `trace/exploration_tree.yaml`. This is what makes an
#              ablation readable a year later instead of a folder of runs.
#   weights    ‼️ ARA HAS NO LAYER FOR THIS, and the absence is about papers
#              rather than an oversight: a paper's artifact is its knowledge,
#              and knowledge regenerates from src + evidence. A 4GB checkpoint
#              does not. It is the one thing here that cannot be rebuilt from
#              the other four, which is why it is also the thing whose partial
#              transfer costs the most.
#   unclassified  everything the rules did not recognise. Reported and KEPT --
#              a sweep that quietly keeps only what it understood is how the one
#              file nobody thought about is the one that is lost, and it reports
#              success while doing it.
CLASSES = ("src", "evidence", "logic", "trace", "weights", "unclassified")

SRC_NAMES = ("config_snapshot.json", "sources.json", "environment.json",
             "requirements.txt", "pixi.toml", "pixi.lock", "env_snapshot.json")
SRC_EXT = (".py", ".yaml", ".yml", ".toml", ".cfg", ".sh", ".patch", ".diff")
EVIDENCE_NAMES = ("run.json", "stream.jsonl", "stream_meta.json",
                  "retention_plan.json", "metrics.json")
LOGIC_NAMES = ("conclusions.json", "conclusions.md")
TRACE_NAMES = ("graph.json", "findings.json", "baseline.json", "audit.json",
               "state.json", "chain.md")
WEIGHT_EXT = (".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".onnx", ".engine")
EVIDENCE_DIRS = ("logs", "tb")


# Per-file, from reading the destination back. Five, and the two that get
# collapsed elsewhere are the two that matter: `truncated` is the user's
# recurring failure and `exists()` cannot see it; `unverifiable` is the
# destination not answering and must never be written down as `missing`.
FILE_STATES = ("verified", "size_only", "truncated", "corrupt", "missing",
               "unverifiable")

VERDICTS = ("clear", "clear_size_only", "blocked")

STATUSES = ("planned", "frozen", "pushed", "verified", "cleared", "blocked")


# ------------------------------------------------------------------ classify

def classify(rel):
    """-> one of CLASSES, from the path alone.

    Order matters. `weights` is tested before `src` because a `.bin` under a
    code directory is still a checkpoint, and `logic`/`trace` before `evidence`
    because `state.json` is an exploration record rather than a metric.
    """
    parts = rel.replace("\\", "/").split("/")
    base = parts[-1]
    ext = os.path.splitext(base)[1].lower()
    if ext in WEIGHT_EXT:
        return "weights"
    if base in LOGIC_NAMES:
        return "logic"
    if base in TRACE_NAMES:
        return "trace"
    if base in SRC_NAMES or ext in SRC_EXT:
        return "src"
    if base in EVIDENCE_NAMES or ext in (".log", ".out", ".err", ".jsonl"):
        return "evidence"
    if any(p in EVIDENCE_DIRS for p in parts[:-1]):
        return "evidence"
    return "unclassified"


def _retention_droppable(path):
    """-> set of basenames a `retention.py` plan ranked as deletable.

    ‼️ Read from the PLAN, never decided here. `retention.py` imports its
    ranking from `select_checkpoint.inventory()` precisely so that two rankings
    cannot disagree, and re-deriving one here would recreate exactly the split
    it exists to prevent.
    """
    if not path:
        return set()
    plan = read_json(path, required=False)
    if plan is None:
        refuse(f"no retention plan at {path}",
               why="without one, nothing here can rank a checkpoint, and an "
                   "unranked checkpoint may not be left on a box that is about "
                   "to be destroyed")
    out = set()
    for entry in plan.get("delete") or plan.get("delete_files") or []:
        out.add(os.path.basename(entry if isinstance(entry, str)
                                 else entry.get("path") or entry.get("file") or ""))
    return {b for b in out if b}


def reproducibility(root):
    """-> {"verdict", "reason", "runs"}. Three values, never two.

    The user's framing, and it is the right one: in architecture exploration the
    code IS the variable, so **code + config is the reproducibility claim** --
    not context around one. This is what checks it, and it does not re-derive
    anything: `code_snapshot.py` already computed `code.reproducible` at launch
    and refused a non-git tree outright, so the verdict is READ, not recomputed.
    `reproducible: false` there means a differing file was too large to embed,
    which means `git checkout && git apply` rebuilds a DIFFERENT tree.

    ‼️ This never refuses an evacuation. Losing the bytes is strictly worse than
    saving them under an honest label, so a bundle that cannot be reproduced is
    still evacuated -- and stamped, at the top of `ARTIFACT.md`, exactly the way
    a census that could not reach a machine is stamped `complete: false` rather
    than withheld.
    """
    runs, bad, unknown = [], [], []
    for dirpath, _, files in os.walk(root):
        if "run.json" not in files:
            continue
        rec = read_json(os.path.join(dirpath, "run.json"), required=False) or {}
        rid = rec.get("run_id") or os.path.basename(dirpath)
        code = rec.get("code") or {}
        runs.append(rid)
        if code.get("reproducible") is True:
            continue
        (bad if code.get("reproducible") is False else unknown).append(rid)
    if not runs:
        return {"verdict": "unknown", "runs": [],
                "reason": "no run.json under the source root -- nothing states "
                          "whether code + config rebuild this"}
    if bad:
        return {"verdict": "no", "runs": runs,
                "reason": f"{len(bad)} run(s) recorded `code.reproducible: false` "
                          f"-- a differing file was too large to embed, so "
                          f"checkout + apply rebuilds a different tree: {bad[:5]}"}
    if unknown:
        return {"verdict": "unknown", "runs": runs,
                "reason": f"{len(unknown)} run(s) never recorded a snapshot verdict "
                          f"-- not the same fact as `false`: {unknown[:5]}"}
    return {"verdict": "yes", "runs": runs,
            "reason": f"all {len(runs)} run(s) carry a reproducible code snapshot"}


# ------------------------------------------------------------------- citations
#
# The join nothing else performs. A conclusion cites a run; a run's lineage
# cites a checkpoint. Destroying the box holding either does not break the
# citation -- it goes on resolving in the record and reading as reproducible
# while it no longer is, which is CLAUDE.md's 「Never delete data a frozen
# snapshot still names」 one line over, on the model side where there is no
# `retire.py` to know.

def cited_paths(project):
    """-> [{"by", "cites"}] every path a conclusion or a run record names."""
    out = []
    conc = read_json(os.path.join(project, "knowledge", "conclusions.json"),
                     required=False) or {}
    for c in conc.get("conclusions") or []:
        for e in c.get("evidence") or []:
            ref = (e.get("ref") or "").split("#", 1)[0]
            if ref and not str(e.get("kind")) == "external":
                out.append({"by": f"conclusion {c.get('id')}", "cites": ref})
    stages = os.path.join(project, "stages")
    for stage in sorted(os.listdir(stages)) if os.path.isdir(stages) else []:
        runs = os.path.join(stages, stage, "runs")
        for rid in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
            rec = read_json(os.path.join(runs, rid, "run.json"), required=False) or {}
            for parent in ((rec.get("lineage") or {}).get("parents") or []):
                p = parent if isinstance(parent, str) else (parent.get("path") or "")
                if p:
                    out.append({"by": f"run {rid} lineage", "cites": p})
    return out


# ---------------------------------------------------------------------- io

def evac_dir(project, eid):
    return os.path.join(project, "evacuations", eid)


def rec_path(project, eid):
    return os.path.join(evac_dir(project, eid), "evacuation.json")


def _load(project, eid):
    rec = read_json(rec_path(project, eid), required=False)
    if rec is None:
        refuse(f"no evacuation {eid} in {project}",
               fix="`evacuate.py plan` first, or `status` to list them")
    return rec


def _save(project, rec):
    atomic_write_json(rec_path(project, rec["evacuation_id"]), rec)


def _template():
    return read_json(os.path.join(os.path.dirname(os.path.dirname(_HERE)),
                                  "evacuate", "evacuation.json"), required=False) or {}


def _latest(project, host=None):
    base = os.path.join(project, "evacuations")
    ids = sorted(os.listdir(base)) if os.path.isdir(base) else []
    for eid in reversed(ids):
        rec = read_json(rec_path(project, eid), required=False)
        if rec and (host is None or (rec.get("source") or {}).get("host") == host):
            return rec
    return None


# -------------------------------------------------------------------- plan

def _walk(root, excludes):
    """-> ([rel], [rel excluded]). Excludes are globs against the relative path."""
    kept, dropped = [], []
    for rel in iter_files(root):
        if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(os.path.basename(rel), g)
               for g in excludes):
            dropped.append(rel)
        else:
            kept.append(rel)
    return kept, dropped


def cmd_plan(a):
    project = os.path.expanduser(a.project)
    root = os.path.expanduser(a.source_root)
    if not os.path.isdir(root):
        # Not `absent`. Nobody looked, and that is the third fact.
        refuse(f"cannot read {root}",
               why="the source did not answer, which is `unverifiable` -- never "
                   "record it as 'there was nothing there'. Reach the box, or "
                   "attach its volume elsewhere, before planning an evacuation")

    kept, dropped = _walk(root, a.exclude or [])
    droppable = _retention_droppable(a.retention_plan)

    # ‼️ THE REFUSAL. A weight file being excluded is a weight file being left
    # on a machine that is about to stop existing, which is a deletion performed
    # by the release rather than by `rm`. `retention.py plan` is the only thing
    # that may authorise one, for the same reason it is the only thing that may
    # authorise the other: a list of filenames carries no evidence that the sort
    # behind it was right.
    unranked = [r for r in dropped
                if classify(r) == "weights" and os.path.basename(r) not in droppable]
    if unranked:
        refuse(f"{len(unranked)} checkpoint(s) would be left behind with nothing "
               f"ranking them",
               files=sorted(unranked)[:20],
               why="leaving a file on a box you are about to destroy IS a delete, "
                   "and CLAUDE.md forbids deleting a checkpoint nothing ranked. "
                   "Pass --retention-plan <path> so the ranking that decided it "
                   "is on the record, or drop the --exclude")

    classes = {c: [] for c in CLASSES}
    total = 0
    for rel in kept:
        classes[classify(rel)].append(rel)
        try:
            total += os.path.getsize(os.path.join(root, rel))
        except OSError:
            pass

    cites = [c for c in cited_paths(project)] if a.project else []
    repro = reproducibility(root)

    eid = a.id or f"evac_{id_stamp()}"
    rec = _template()
    rec.update({
        "evacuation_id": eid,
        "project": os.path.basename(os.path.abspath(project)),
        "source": {"host": a.host, "root": root, "reached_at": now_utc(),
                   "via": a.via},
        "destination": {"kind": a.dest_kind, "bucket": a.bucket,
                        "prefix": a.prefix, "checksum_algorithm": None},
        "classes": {c: len(v) for c, v in classes.items()},
        "reproducible": repro,
        "cited_by": cites,
        "status": "planned",
        "created_at": now_utc(),
    })
    os.makedirs(evac_dir(project, eid), exist_ok=True)
    with open(os.path.join(evac_dir(project, eid), "plan.jsonl"),
              "w", encoding="utf-8") as fh:
        for rel in kept:
            fh.write(json.dumps({"item": rel, "class": classify(rel)},
                                ensure_ascii=False) + "\n")
    _save(project, rec)

    out = {"ok": True, "evacuation_id": eid, "files": len(kept),
           "bytes": total, "classes": {c: len(v) for c, v in classes.items()},
           "left_behind": len(dropped), "cited_paths": len(cites),
           "reproducible": repro["verdict"],
           "next": "freeze -- nothing is protected until the manifest exists"}
    if repro["verdict"] != "yes":
        out["reproducibility"] = repro["reason"]
    if not classes["src"]:
        out["‼️src"] = ("no `src` layer: nothing here is code, config or "
                        "environment. Weights and numbers with no way to "
                        "regenerate them is a BACKUP, not an artifact -- and "
                        "an ablation read off it a year from now cannot say "
                        "what was different between the arms")
    if classes["unclassified"]:
        out["‼️"] = (f"{len(classes['unclassified'])} file(s) matched no rule and are "
                     f"classified `unclassified`. They ARE being evacuated -- this is "
                     f"a notice, not a warning. A sweep that kept only what it "
                     f"recognised would report success while dropping the one file "
                     f"nobody thought about")
        out["unclassified_sample"] = sorted(classes["unclassified"])[:10]
    emit(out)


# ------------------------------------------------------------------- freeze

def cmd_freeze(a):
    project = os.path.expanduser(a.project)
    rec = _load(project, a.id)
    root = (rec.get("source") or {}).get("root")
    if not root or not os.path.isdir(root):
        refuse(f"source root {root!r} is not readable now",
               why="a manifest frozen from anywhere but the source is not a "
                   "manifest. If the box is already gone, this evacuation is "
                   "`unverifiable` forever -- what arrived cannot testify about "
                   "what did not")

    plan_path = os.path.join(evac_dir(project, a.id), "plan.jsonl")
    rels = []
    with open(plan_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rels.append(json.loads(line)["item"])

    records = build_manifest(root, rels, a.hash)
    mpath = os.path.join(evac_dir(project, a.id), "manifest.jsonl")
    write_manifest(mpath, records, a.hash)
    rec["manifest"] = {"path": "manifest.jsonl", "frozen_at": now_utc(),
                       "count": len(records),
                       "bytes": sum(r.get("bytes") or 0 for r in records),
                       "hash_algo": a.hash}
    rec["status"] = "frozen"
    _save(project, rec)
    emit({"ok": True, "evacuation_id": a.id, "count": len(records),
          "bytes": rec["manifest"]["bytes"], "hash_algo": a.hash,
          "note": "this is now the only authority for what was supposed to "
                  "arrive. Completeness is computed against it, never against "
                  "what shows up at the destination"})


# ------------------------------------------------------------------- bundle
#
# ARA's `PAPER.md`: the page somebody opens instead of the tree. It exists
# because an S3 prefix full of correct files is not an artifact -- an artifact
# says what the input was, what came out, and whether the two still connect.

def _layer_rows(project, eid):
    counts, byte_totals = {c: 0 for c in CLASSES}, {c: 0 for c in CLASSES}
    mpath = os.path.join(evac_dir(project, eid), "manifest.jsonl")
    plan_path = os.path.join(evac_dir(project, eid), "plan.jsonl")
    sizes = {}
    if os.path.exists(mpath):
        for e in read_manifest(mpath)[1]:
            sizes[e["item"]] = e.get("bytes") or 0
    with open(plan_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            o = json.loads(line)
            counts[o["class"]] += 1
            byte_totals[o["class"]] += sizes.get(o["item"], 0)
    return counts, byte_totals


LAYER_BLURB = {
    "src": "INPUT — code snapshot + training config. In an architecture search "
           "the code is the variable, so this layer IS the reproducibility claim",
    "evidence": "the numbers, and the lines they were read from",
    "logic": "the conclusions: what is believed, on what, and what would overturn it",
    "trace": "the exploration graph — which arms ran, which won, what killed the rest",
    "weights": "‼️ the one layer ARA has no equivalent for, because a paper's "
               "knowledge regenerates from src+evidence and a checkpoint does not",
    "unclassified": "matched no rule and was kept anyway",
}


def cmd_bundle(a):
    project = os.path.expanduser(a.project)
    rec = _load(project, a.id)
    bdir = os.path.join(evac_dir(project, a.id), "bundle")
    os.makedirs(bdir, exist_ok=True)

    # The two small layers are copied PHYSICALLY, because they must be readable
    # without pulling forty gigabytes of weights back down. Everything else is
    # described by the manifest and stays where the transfer put it.
    copied = []
    for layer, src in (("logic", os.path.join(project, "knowledge")),
                       ("trace", os.path.join(project, "stages", "exploration"))):
        if not os.path.isdir(src):
            continue
        for name in sorted(os.listdir(src)):
            if not name.endswith((".json", ".md")):
                continue
            dst = os.path.join(bdir, layer, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(os.path.join(src, name), "rb") as fa, open(dst, "wb") as fb:
                fb.write(fa.read())
            copied.append(f"{layer}/{name}")

    counts, byte_totals = _layer_rows(project, a.id)
    repro = rec.get("reproducible") or {}
    v = rec.get("verification") or {}
    cl = rec.get("clearance") or {}
    MARK = {"yes": "✅", "no": "❌", "unknown": "❓"}

    L = [f"# {rec.get('project')} — evacuated artifact `{a.id}`", "",
         "> Structure follows ARA (arXiv:2604.24658). **Input** is `src/` — the code",
         "> snapshot and the training config. **Output** is `evidence/` (the numbers),",
         "> `logic/` (the conclusions), `trace/` (the ablation graph) and `weights/`.",
         "> `weights/` is MLClaw's fifth layer: ARA has none, because a paper's",
         "> knowledge regenerates from src + evidence and a checkpoint does not.", "",
         f"Source: `{(rec.get('source') or {}).get('host')}`:"
         f"`{(rec.get('source') or {}).get('root')}`  ",
         f"Destination: `{(rec.get('destination') or {}).get('bucket')}/"
         f"{(rec.get('destination') or {}).get('prefix') or ''}`", "",
         f"## Reproducible? {MARK.get(repro.get('verdict'), '❓')} "
         f"**{repro.get('verdict', 'unknown')}**", "",
         repro.get("reason", "not assessed"), ""]
    if repro.get("verdict") != "yes":
        L += ["> ‼️ 这不是拒绝，是标签。丢字节比标签不准更坏，所以东西照搬，",
              "> 但这一行必须跟着它走 —— 和普查记 `complete: false` 是同一条规矩。", ""]

    L += ["## Layers", "", "| layer | files | bytes | what it is |", "|---|---|---|---|"]
    for c in CLASSES:
        if counts[c]:
            L.append(f"| `{c}/` | {counts[c]} | {byte_totals[c]:,} | {LAYER_BLURB[c]} |")
    L.append("")

    conc = read_json(os.path.join(project, "knowledge", "conclusions.json"),
                     required=False) or {}
    if conc.get("conclusions"):
        L += ["## Conclusions — `logic/`", "",
              "| id | status | tier | corpus | statement |", "|---|---|---|---|---|"]
        for c in conc["conclusions"]:
            sc = c.get("scope") or {}
            L.append(f"| {c.get('id')} | {c.get('status') or '—'} | "
                     f"{c.get('tier') or '—'} | `{sc.get('corpus')}` | "
                     f"{c.get('statement')} |")
        L += ["", "‼️ `status` 和 `tier` 是 `conclude.py check` 算出来的，"
              "在这份快照被写下的那一刻为真。证据搬走之后它们不会自己更新 —— "
              "重新引用之前跑一次 `check`。", ""]

    counts_v = v.get("counts") or {}
    L += ["## Transfer", "",
          f"- manifest frozen at `{(rec.get('manifest') or {}).get('frozen_at')}` — "
          f"{(rec.get('manifest') or {}).get('count')} files",
          f"- verified: " + ", ".join(f"{k} {n}" for k, n in counts_v.items() if n),
          f"- clearance: **{cl.get('verdict') or 'not decided'}**"]
    if cl.get("blocked_by"):
        L += ["", "‼️ 未放行：", ""] + [f"- {b}" for b in cl["blocked_by"]]
    L.append("")

    path = os.path.join(bdir, "ARTIFACT.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    rec["bundle"] = {"path": "bundle", "written_at": now_utc(), "copied": copied}
    _save(project, rec)
    emit({"ok": True, "evacuation_id": a.id, "artifact": path,
          "copied_into_bundle": copied,
          "note": "logic/ and trace/ are copied in physically so the artifact "
                  "stays readable without pulling the weights back down"})


# --------------------------------------------------------------------- push
#
# The transfer is not ours -- `collect.py`'s rule, and the same reasoning: rsync
# and the aws cli are better at it than anything written here, and pretending
# otherwise means owning their retry semantics.

def build_push_cmd(rec, root):
    d = rec.get("destination") or {}
    if d.get("kind") != "s3":
        broke(f"no push builder for destination kind {d.get('kind')!r}")
    uri = f"s3://{d.get('bucket')}/{(d.get('prefix') or '').strip('/')}"
    # ‼️ --checksum-algorithm SHA256 is not optional decoration. Without it the
    # only thing `head-object` returns is an ETag, which equals the MD5 for
    # single-part uploads and equals NOTHING for a multipart one -- so exactly
    # the large checkpoints get a hash that can never be compared, and `verify`
    # is reduced to `size_only` on the files where corruption costs most.
    return ["aws", "s3", "sync", root, uri, "--checksum-algorithm", "SHA256"]


def cmd_push(a):
    project = os.path.expanduser(a.project)
    rec = _load(project, a.id)
    if rec.get("status") == "planned":
        refuse("this evacuation has no frozen manifest",
               why="pushing first and manifesting afterwards records what "
                   "arrived as what was supposed to arrive -- a tautology that "
                   "passes every partial transfer",
               fix="`freeze` first")
    root = (rec.get("source") or {}).get("root")

    if a.already_pushed:
        rec["push"] = {"by": "external", "at": now_utc(), "returncode": None}
        rec["status"] = "pushed"
        _save(project, rec)
        emit({"ok": True, "evacuation_id": a.id, "pushed_by": "external",
              "next": "verify -- an external transfer is a claim until the "
                      "destination is read back"})
        return

    cmd = build_push_cmd(rec, root)
    if a.dry:
        emit({"ok": True, "would_run": cmd})
        return
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           timeout=a.timeout)
    except (OSError, subprocess.SubprocessError) as e:
        broke(f"transfer could not run: {type(e).__name__}: {e}")
    rec["destination"]["checksum_algorithm"] = "SHA256"
    rec["push"] = {"by": " ".join(cmd), "at": now_utc(), "returncode": p.returncode}
    rec["status"] = "pushed"
    _save(project, rec)
    out = {"ok": p.returncode == 0, "returncode": p.returncode,
           "evacuation_id": a.id, "next": "verify"}
    if p.returncode:
        out["‼️"] = ("the transfer reported a failure. It may still have moved most "
                     "of the bytes -- which is precisely the state that gets read as "
                     "success. `verify` is what decides")
        out["stderr_tail"] = (p.stderr or "")[-800:]
    emit(out)


# ------------------------------------------------------------------- verify

def _dest_listing(a, rec):
    """-> ({item: {bytes, sha256}}, reachable). Three sources, one shape.

    `--listing` exists so a destination nothing here can speak to (an S3 bucket
    behind someone else's tooling, a tape robot) can still be verified: whatever
    produced the listing did the reading, and this does the comparing.
    """
    if a.listing:
        got = {}
        with open(os.path.expanduser(a.listing), encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                o = json.loads(line)
                # S3 tooling calls it ChecksumSHA256; `handoff.py`'s manifest calls
                # it `hash`. Normalise on the way in -- a field-name mismatch here
                # does not raise, it silently downgrades every file to `size_only`,
                # which is this skill's own failure mode turned on itself.
                got[o["item"]] = {"bytes": o.get("bytes"),
                                  "hash": (o.get("hash") or o.get("sha256")
                                           or o.get("checksum"))}
        return got, True
    if a.dest_root:
        root = os.path.expanduser(a.dest_root)
        if not os.path.isdir(root):
            return {}, False
        algo = (rec.get("manifest") or {}).get("hash_algo") or "sha256"
        got = {}
        for rel in iter_files(root):
            full = os.path.join(root, rel)
            got[rel] = {"bytes": os.path.getsize(full),
                        "hash": hash_file(full, algo) if algo != "size" else None}
        return got, True
    broke("no destination to read", fix="pass --dest-root or --listing")


def cmd_verify(a):
    project = os.path.expanduser(a.project)
    rec = _load(project, a.id)
    mpath = os.path.join(evac_dir(project, a.id), "manifest.jsonl")
    if not os.path.exists(mpath):
        refuse("no frozen manifest",
               why="there is nothing to compare against, so the honest verdict "
                   "is `unverifiable` -- and it is permanent if the source is gone")
    _, sent = read_manifest(mpath)
    got, reachable = _dest_listing(a, rec)

    files, counts = [], {s: 0 for s in FILE_STATES}
    for entry in sent:
        item = entry["item"]
        if not reachable:
            state = "unverifiable"
        elif item not in got:
            state = "missing"
        elif got[item].get("bytes") != entry.get("bytes"):
            state = "truncated"
        elif got[item].get("hash") and entry.get("hash"):
            state = "verified" if got[item]["hash"] == entry["hash"] else "corrupt"
        else:
            state = "size_only"
        counts[state] += 1
        if state != "verified":
            files.append({"item": item, "state": state,
                          "expected_bytes": entry.get("bytes"),
                          "found_bytes": (got.get(item) or {}).get("bytes")})

    # ‼️ A guard on this check rather than on the data. When the manifest
    # carries hashes AND the destination reported them, `size_only` is not a
    # possible answer -- its appearance means the two sides disagreed about a
    # FIELD NAME, not about the bytes. That bug does not raise: it downgrades
    # every file to the weaker verdict and ships looking like it works. It
    # happened here once, between `hash` and `sha256`.
    if counts["size_only"] and (rec.get("manifest") or {}).get("hash_algo") not in (None, "size"):
        if any((got.get(e["item"]) or {}).get("hash") for e in sent):
            broke("the manifest and the destination both carry hashes, yet "
                  f"{counts['size_only']} file(s) came back `size_only` -- the two "
                  "sides are reading different fields. This is a defect in the "
                  "verifier, not a finding about the transfer")

    rec["verification"] = {"checked_at": now_utc(), "counts": counts,
                           "files": files[:500], "reachable": reachable}
    rec["status"] = "verified"
    _save(project, rec)
    out = {"ok": True, "evacuation_id": a.id, "counts": counts,
           "problems": files[:20]}
    if counts["corrupt"]:
        out["‼️"] = (f"{counts['corrupt']} file(s) are the RIGHT LENGTH and the WRONG "
                     f"BYTES. Only the hash sees this one -- every size check in the "
                     f"world passes it")
    if counts["truncated"]:
        out["‼️"] = (f"{counts['truncated']} file(s) arrived at the WRONG LENGTH. This "
                     f"is the failure `os.path.exists` cannot see: a half-written "
                     f"checkpoint has a plausible name and does not load")
    if not reachable:
        out["‼️"] = ("the destination did not answer. Every file is `unverifiable`, "
                     "which is NOT `missing` -- nothing here has evidence either way")
    emit(out)


# ---------------------------------------------------------------- clearance

def _unevacuated_citations(project, rec):
    """Cited paths that are about to stop existing anywhere.

    A conclusion citing a run whose record is on this box does not become false
    when the box dies -- it becomes `unverifiable`, silently, weeks later, with
    the citation still resolving in `conclusions.json`. `conclude.py check` is
    what eventually notices; this is what stops it happening. It is CLAUDE.md's
    「Never delete data a frozen snapshot still names」 on the model side, where
    there is no `retire.py` that knows.

    Three ways a citation is SAFE, and the first is the common one -- without it
    every ordinary evacuation would block on a record that was synced back weeks
    ago:

      1. the path exists in the local project already. It is not on this box's
         critical path at all.
      2. the source root IS that path (evacuating the run dir itself).
      3. the manifest carries items under it (evacuating a tree above it).

    Anything else is reported. ‼️ Reported, not assumed gone: a citation this
    cannot locate might live on a third machine, so `clearance` names it and
    makes somebody look rather than silently clearing or silently blocking
    forever.
    """
    root = os.path.abspath((rec.get("source") or {}).get("root") or "")
    mpath = os.path.join(evac_dir(project, rec["evacuation_id"]), "manifest.jsonl")
    items = []
    if os.path.exists(mpath):
        items = [e["item"].replace("\\", "/") for e in read_manifest(mpath)[1]]

    out = []
    for c in rec.get("cited_by") or []:
        cites = str(c["cites"]).replace("\\", "/").strip("/")
        if not cites:
            continue
        if os.path.exists(os.path.join(project, cites)):
            continue                                             # 1
        if root.replace("\\", "/").endswith("/" + cites):
            continue                                             # 2
        if any(i == cites or i.startswith(cites + "/") for i in items):
            continue                                             # 3
        out.append(c)
    return out


def cmd_clearance(a):
    project = os.path.expanduser(a.project)
    rec = _load(project, a.id) if a.id else _latest(project, a.host)
    if rec is None:
        refuse(f"no evacuation recorded for host {a.host!r}",
               why="a release with no evacuation on record is the exact failure "
                   "this skill exists to stop. `unverifiable` is not `nothing "
                   "was there`")

    v = rec.get("verification") or {}
    counts = v.get("counts") or {}
    blocked = []
    if not v.get("checked_at"):
        blocked.append("never verified -- the destination has not been read back")
    for s in ("missing", "truncated", "corrupt", "unverifiable"):
        if counts.get(s):
            blocked.append(f"{counts[s]} file(s) {s}")
    stranded = _unevacuated_citations(project, rec)
    for c in stranded:
        blocked.append(f"{c['cites']} is cited by {c['by']} and is not in the manifest")

    n_ver, n_size = counts.get("verified", 0), counts.get("size_only", 0)
    if blocked:
        verdict = "blocked"
    elif n_size:
        verdict = "clear_size_only"
    else:
        verdict = "clear"

    rec["clearance"] = {"verdict": verdict, "decided_at": now_utc(),
                        "hash_verified": n_ver, "of": n_ver + n_size,
                        "blocked_by": blocked}
    rec["status"] = "cleared" if verdict != "blocked" else "blocked"
    _save(project, rec)

    out = {"evacuation_id": rec["evacuation_id"], "verdict": verdict,
           "hash_verified": n_ver, "of": n_ver + n_size, "blocked_by": blocked}
    if verdict == "clear_size_only":
        out["‼️"] = (f"{n_size} file(s) were never hash-compared -- the destination "
                     f"reported no comparable checksum. Present and the right length "
                     f"is a LOWER BOUND on intact, and must be quoted as one")
    if verdict == "blocked":
        out["do_not"] = ("release or destroy this machine. `pool.py release "
                         "--artifacts recovered` here would be somebody's word "
                         "standing in for a check")
        emit(out)
        sys.exit(1)
    emit(out)


# ------------------------------------------------------------------- status

def cmd_status(a):
    project = os.path.expanduser(a.project)
    base = os.path.join(project, "evacuations")
    rows = []
    for eid in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        rec = read_json(rec_path(project, eid), required=False)
        if not rec:
            continue
        cl = rec.get("clearance") or {}
        if a.open_only and cl.get("verdict") in ("clear", "clear_size_only"):
            continue
        rows.append({"evacuation_id": eid, "host": (rec.get("source") or {}).get("host"),
                     "status": rec.get("status"), "verdict": cl.get("verdict"),
                     "blocked_by": cl.get("blocked_by") or []})
    emit({"project": a.project, "n": len(rows), "evacuations": rows,
          "blocked": [r["evacuation_id"] for r in rows if r["verdict"] == "blocked"],
          "unreleased_unverified": [r["evacuation_id"] for r in rows
                                    if r["verdict"] is None]})


# --------------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--project", required=True)
        return sp

    s = common(sub.add_parser("plan"))
    s.add_argument("--source-root", required=True,
                   help="the path being evacuated, as this process can read it")
    s.add_argument("--host", default=None)
    s.add_argument("--via", default=None, help="ssh alias, mount, lease id")
    s.add_argument("--bucket", default=None)
    s.add_argument("--prefix", default=None)
    s.add_argument("--dest-kind", default="s3")
    s.add_argument("--exclude", action="append", default=[])
    s.add_argument("--retention-plan", default=None,
                   help="the retention_plan.json that ranked any checkpoint being "
                        "left behind. Required for that; nothing else may rank one")
    s.add_argument("--id", default=None)
    s.set_defaults(func=cmd_plan)

    s = common(sub.add_parser("freeze"))
    s.add_argument("--id", required=True)
    s.add_argument("--hash", default="sha256", choices=("sha256", "size"))
    s.set_defaults(func=cmd_freeze)

    s = common(sub.add_parser("bundle"))
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_bundle)

    s = common(sub.add_parser("push"))
    s.add_argument("--id", required=True)
    s.add_argument("--dry", action="store_true")
    s.add_argument("--already-pushed", action="store_true",
                   help="somebody else moved the bytes; record that and verify")
    s.add_argument("--timeout", type=int, default=7200)
    s.set_defaults(func=cmd_push)

    s = common(sub.add_parser("verify"))
    s.add_argument("--id", required=True)
    s.add_argument("--dest-root", default=None)
    s.add_argument("--listing", default=None,
                   help="JSONL of {item, bytes, sha256} read from the destination")
    s.set_defaults(func=cmd_verify)

    s = common(sub.add_parser("clearance"))
    s.add_argument("--id", default=None)
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_clearance)

    s = common(sub.add_parser("status"))
    s.add_argument("--open-only", action="store_true")
    s.set_defaults(func=cmd_status)

    a = p.parse_args()
    try:
        a.func(a)
    except SystemExit:
        raise
    except Exception as e:                                   # noqa: BLE001
        broke(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
