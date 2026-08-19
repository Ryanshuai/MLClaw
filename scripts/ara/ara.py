#!/usr/bin/env python3
"""ara.py -- assemble a round's work into an Agent-Native Research Artifact.

ARA (arXiv:2604.24658) argues that the artifact IS the research object rather
than a byproduct of it, and gives it four layers: `logic/` (what is claimed),
`src/` (what produced it), `evidence/` (the numbers), `trace/` (the exploration
DAG). MLClaw produces all four already and had nowhere to put them -- a
finished round left behind a directory of runs, which is a different thing from
an artifact somebody can read.

MLClaw's version differs in exactly one way, and the difference is about what is
being preserved rather than a liberty taken:

  weights/   ‼️ ARA has no equivalent. A paper's artifact is its KNOWLEDGE, and
             knowledge regenerates from src + evidence. A checkpoint does not. It is the one layer the other four cannot rebuild, which is
             also why it is the one whose partial transfer costs the most.

And one bucket that is not a layer at all:

  unclassified   whatever matched no rule, kept anyway and named. A sweep that
                 keeps only what it recognised loses the file nobody thought
                 about, and reports success while doing it.

WHAT THIS IS NOT. `/evacuate` empties a machine before it is destroyed, and it
is tempting to read this as a stage inside that. It is the other way round: an
evacuation is scoped to a MACHINE (which may hold pieces of three rounds, or
none, plus files belonging to no artifact at all) and is gated on a lease. This
is scoped to a ROUND and has no deadline. `/evacuate` CALLS this, because the
moment before a box dies is the last moment the source can be read -- the
deadline forces the artifact to be finished, it does not contain it.

Verbs:
  build   assemble the layers from a root, write ARTIFACT.md and ara.json
  check   has the artifact drifted from what it was built out of -- reported,
          never repaired
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))
# `iter_files` lives with the frozen-manifest code in `handoff.py`, which is
# where walking a tree reproducibly was first needed. Imported rather than
# reimplemented: two walkers that sort differently produce two manifests of the
# same tree that do not diff.
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "data-label"))
from _records import (atomic_write_json, broke, emit, id_stamp,  # noqa: E402
                      now_utc, read_json, refuse)
from handoff import iter_files                                  # noqa: E402


CLASSES = ("src", "evidence", "logic", "trace", "weights", "unclassified")

SRC_NAMES = ("config_snapshot.json", "sources.json", "environment.json",
             "requirements.txt", "pixi.toml", "pixi.lock", "env_snapshot.json",
             # What a stage's `-init` declared: what it consumes, what it runs,
             # what it is expected to produce, and how much of that was checked
             # rather than guessed. `src` because ARA's `src` is the INPUT layer
             # -- these are the files that say what the numbers were produced
             # FROM, and an ablation read off an artifact without them cannot
             # say what differed between its two arms.
             #
             # Recognised by NAME and not by position, so they are still found
             # under `/evacuate`'s `--root`, where the tree is a machine's and
             # not a project's. The cost is that a same-named file somewhere
             # else lands in `src` rather than `unclassified` -- the cheap
             # direction to be wrong in, since both keep the bytes and `src` is
             # the layer whose ABSENCE gets an artifact named a backup.
             "config.json", "artifacts.json", "input.json", "output.json",
             "provenance.json", "plan.json", "recipe.md")
SRC_EXT = (".py", ".yaml", ".yml", ".toml", ".cfg", ".sh", ".patch", ".diff")
EVIDENCE_NAMES = ("run.json", "stream.jsonl", "stream_meta.json",
                  "retention_plan.json", "metrics.json")
LOGIC_NAMES = ("conclusions.json", "conclusions.md")
# `state.json` + `chain.md` are a tune session's, `graph.json` an exploration's,
# `session.json` a repro loop's, a triage session's and an adaptation campaign's.
# One kind of thing -- a dated record of a multi-step process, which is what
# `trace` IS -- and not one of them is named after the stage it sits under.
TRACE_NAMES = ("graph.json", "findings.json", "baseline.json", "audit.json",
               "state.json", "chain.md", "session.json")
WEIGHT_EXT = (".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".onnx", ".engine")
EVIDENCE_DIRS = ("logs", "tb")

# ‼️ What is in the artifact BY REFERENCE rather than by copy. `code/` and
# `original/` are git trees full of somebody else's `.md`; `data/` and
# `artifacts/` are bulk; `runs/` is per-run and is already what `--root` walks,
# so copying it would duplicate every run record once per dated artifact and
# grow with the run count forever. Everything outside them is a RECORD.
BULK_DIRS = ("code", "artifacts", "data", "runs", "original")
RECORD_EXT = (".json", ".md")

LAYER_BLURB = {
    "src": "INPUT — code snapshot, plus what each stage declared it would run. "
           "In an architecture search the code is the variable, so this layer "
           "IS the reproducibility claim",
    "evidence": "the numbers, and the lines they were read from",
    "logic": "the conclusions: what is believed, on what, and what would overturn it",
    "trace": "the dated record of how the work went — an exploration's graph of "
             "arms, a tune session's chain, a repro loop's session. Which ran, "
             "which won, what killed the rest",
    "weights": "‼️ the one layer ARA has no equivalent for, because a paper's "
               "knowledge regenerates from src+evidence and a checkpoint does not",
    "unclassified": "matched no rule and was kept anyway",
}


def classify(rel):
    """-> one of CLASSES, from the path alone.

    Order matters. `weights` is tested first because a `.bin` under a code
    directory is still a checkpoint, and `logic`/`trace` before `evidence`
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


def reproducibility(root):
    """-> {"verdict", "reason", "runs"}. Three values, never two.

    In an architecture search the CODE IS THE VARIABLE, so **code + config is
    the reproducibility claim** rather than context around one. This is what
    checks it, and it re-derives nothing: `code_snapshot.py` computed
    `code.reproducible` at launch and refused a non-git tree outright, so the
    verdict is READ. `false` there means a differing file was too large to
    embed, so `git checkout && git apply` rebuilds a DIFFERENT tree.

    ‼️ This never refuses. Losing the bytes is strictly worse than saving them
    under an honest label, which is why a census that could not reach a machine
    is stamped `complete: false` rather than withheld.
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
                "reason": "no run.json under the root -- nothing states whether "
                          "code + config rebuild this"}
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


def layer_index(root, rels=None, exclude=None):
    """-> ({class: count}, {class: bytes}, {class: [rel]})."""
    counts = {c: 0 for c in CLASSES}
    byte_totals = {c: 0 for c in CLASSES}
    members = {c: [] for c in CLASSES}
    for rel in (rels if rels is not None else iter_files(root, exclude=exclude)):
        c = classify(rel)
        counts[c] += 1
        members[c].append(rel)
        try:
            byte_totals[c] += os.path.getsize(os.path.join(root, rel))
        except OSError:
            pass
    return counts, byte_totals, members


def record_files(project, skip=()):
    """-> relative paths of the project's RECORDS: every `.json`/`.md` outside
    `BULK_DIRS`. Sorted, forward slashes, dot-directories skipped.

    ‼️ **It knows no stage names, and that is the whole point.** The two source
    directories were once written here literally -- `knowledge/` and
    `stages/exploration/` -- which made this function a SECOND author of a fact
    `classify` already owned, and the two authors disagreed in both directions
    at once: `stages/exploration/config.json` was counted `unclassified` and
    copied into `trace/`, while a tune session's `chain.md` was counted `trace`
    and copied nowhere at all. An index that names a file the directory beside
    it does not hold is the exact failure this script exists to report, and it
    was doing it to itself.

    Reaching only `stages/*` would not have fixed it either: a repro session
    lives at `repro/<id>/session.json` and an adaptation campaign at
    `adaptation/`, both at project level. Hence a structural rule rather than a
    wider list of places -- the layer a record belongs to is `classify`'s to
    say, and where it happens to sit is not evidence about it.
    """
    project = os.path.abspath(project)
    skipabs = {os.path.abspath(p) for p in skip}
    out = []
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in sorted(dirnames)
                       if not d.startswith(".") and d not in BULK_DIRS
                       and os.path.abspath(os.path.join(dirpath, d)) not in skipabs]
        for name in sorted(filenames):
            if name.endswith(RECORD_EXT):
                rel = os.path.relpath(os.path.join(dirpath, name), project)
                out.append(rel.replace(os.sep, "/"))
    return out


def _excluded_under(root, *paths):
    """-> [glob] for `iter_files`, naming only the paths that lie inside root.

    What it keeps out is the artifact directory. Without it a second build
    counts the FIRST build's `ARTIFACT.md` and `ara.json` as two more
    `unclassified` files, and the noise grows by two per artifact -- a record
    that describes itself describing itself, and reports the growth as content.
    """
    rootabs = os.path.abspath(root)
    globs = []
    for pth in paths:
        pa = os.path.abspath(pth)
        if pa.startswith(rootabs + os.sep):
            globs.append(os.path.relpath(pa, rootabs).replace(os.sep, "/") + "/*")
    return globs or None


# ------------------------------------------------------------------ the index

def _conclusions(project):
    rec = read_json(os.path.join(project, "knowledge", "conclusions.json"),
                    required=False) or {}
    return rec.get("conclusions") or []


def _snapshot_of(concs):
    """What `check` diffs against later.

    ‼️ Statuses and tiers, not just ids. An ARA whose `logic/` still reads
    `supported` while `conclusions.json` has since gone `unverifiable` is
    exactly the failure `/conclude` exists to prevent, recurring one level up in
    the frozen copy -- and the frozen copy is the one people read.
    """
    return [{"id": c.get("id"), "status": c.get("status"), "tier": c.get("tier")}
            for c in concs]


def render(project, root, counts, byte_totals, repro, concs, title, extra=None,
           n_copied=0):
    MARK = {"yes": "✅", "no": "❌", "unknown": "❓"}
    L = [f"# {title}", "",
         "> Structure follows ARA (arXiv:2604.24658). **Input** is `src/` — the code",
         "> snapshot and the training config. **Output** is `evidence/` (the numbers),",
         "> `logic/` (the conclusions), `trace/` (how the work went — an ablation",
         "> graph, a tune chain, a repro session) and `weights/`.",
         "> `weights/` is MLClaw's fifth layer: ARA has none, because a paper's",
         "> knowledge regenerates from src + evidence and a checkpoint does not.", "",
         f"Assembled from `{root}` at {now_utc()}.", ""]
    if extra:
        L += extra + [""]

    L += [f"## Reproducible? {MARK.get(repro.get('verdict'), '❓')} "
          f"**{repro.get('verdict', 'unknown')}**", "",
          repro.get("reason", "not assessed"), ""]
    if repro.get("verdict") != "yes":
        L += ["> ‼️ This is a label, not a refusal. Losing the bytes is worse than an",
              "> imprecise label, so everything is kept — but this line travels with it,",
              "> the same rule as a census recording `complete: false`.", ""]

    L += ["## Layers", "", "| layer | files | bytes | what it is |", "|---|---|---|---|"]
    for c in CLASSES:
        if counts.get(c):
            L.append(f"| `{c}/` | {counts[c]} | {byte_totals[c]:,} | {LAYER_BLURB[c]} |")
    L.append("")
    if n_copied:
        L += [f"{n_copied} record file(s) are copied in **physically** — every "
              "`.json`/`.md` outside `code/` `artifacts/` `data/` `runs/` "
              "`original/`, each into the layer `classify()` assigns it, keeping "
              "its own path. So the conclusions, the ablation graph, every tune "
              "chain and every stage's declared config stay readable with the "
              "weights gone. Runs are in **by reference**.", ""]
    if not counts.get("src"):
        L += ["‼️ **No `src/` layer.** Weights and numbers with no way to regenerate them",
              "is a BACKUP, not an artifact — and an ablation read off one cannot say what",
              "differed between its two arms.", ""]
    if not counts.get("logic"):
        L += ["‼️ **No `logic/` layer.** The round ran and nobody wrote down what it is "
              "now believed to have shown — `/conclude` is where that goes.", ""]

    if concs:
        L += ["## Conclusions — `logic/`", "",
              "| id | status | tier | corpus | statement |", "|---|---|---|---|---|"]
        for c in concs:
            sc = c.get("scope") or {}
            L.append(f"| {c.get('id')} | {c.get('status') or '—'} | "
                     f"{c.get('tier') or '—'} | `{sc.get('corpus')}` | "
                     f"{c.get('statement')} |")
        L += ["", "‼️ `status` and `tier` are computed by `conclude.py check` and were "
              "**true at the moment this artifact was written**. They do not update "
              "themselves when the evidence moves — `ara.py check` reports the drift, so "
              "run it before citing these again.", ""]
    return L


def artifacts_dir(project):
    return os.path.join(project, "ara")


def resolve_out(project, aid=None, out=None, *, create=False):
    """Where one artifact lives. Dated, and NEVER overwritten by default.

    ‼️ An artifact is a dated reading, like a census or an evacuation -- not a
    file that gets refreshed. Building round two on top of round one destroys
    the only record of what was believed during round one, which is what
    explains the runs launched during round one. Same rule as `/conclude`
    refusing to edit a settled conclusion, and for the same reason.

    Rebuilding IN PLACE stays available via `--id`, because `check` reporting
    drift is a legitimate reason to refresh one -- but it has to be asked for.
    """
    if out:
        return os.path.expanduser(out)
    base = artifacts_dir(project)
    if aid:
        return os.path.join(base, aid)
    if create:
        # Collision-free, mirroring `shared/create_run.py -> allocate_run_dir`:
        # `id_stamp()` is second-resolution, so two builds in one second would
        # land on the same directory and the second would OVERWRITE the first --
        # which is precisely the loss the dating exists to prevent. Same `_2`
        # suffix, same ceiling, and the caller is told it happened.
        stamp = f"ara_{id_stamp()}"
        for n in range(1, 100):
            cand = stamp if n == 1 else f"{stamp}_{n}"
            if not os.path.exists(os.path.join(base, cand, "ara.json")):
                return os.path.join(base, cand)
        broke(f"100 artifacts already exist for {stamp} -- refusing to guess further")
    ids = sorted(d for d in (os.listdir(base) if os.path.isdir(base) else [])
                 if os.path.isdir(os.path.join(base, d)))
    if not ids:
        refuse(f"no artifact under {base}",
               fix="`ara.py build --project <p>` first")
    return os.path.join(base, ids[-1])


def cmd_build(a):
    project = os.path.expanduser(a.project)
    root = os.path.expanduser(a.root) if a.root else project
    if not os.path.isdir(root):
        refuse(f"cannot read {root}",
               why="nothing answered, which is `unverifiable` -- never record it "
                   "as 'there was nothing there'")
    out = resolve_out(project, a.id, a.out, create=True)
    os.makedirs(out, exist_ok=True)

    counts, byte_totals, members = layer_index(
        root, exclude=_excluded_under(root, artifacts_dir(project), out))
    repro = reproducibility(root)
    concs = _conclusions(project)

    # The project's RECORDS are copied PHYSICALLY. They have to stay readable
    # without pulling the weights back down, and they are the
    # part that survives when the weights do not.
    #
    # ‼️ `classify` decides the layer and is the ONLY thing that decides it. The
    # destination used to be written beside the source directory here, which
    # made this loop a second author of a fact `classify` already owned -- see
    # `record_files` for what the two of them disagreed about. The path under
    # the layer is the record's own path in the project, kept whole: `src/` now
    # holds a `config.json` from every stage, and flattening to a basename
    # would have one silently overwrite another.
    copied, copied_from = [], []
    for rel in record_files(project, skip=(artifacts_dir(project), out)):
        layer = classify(rel)
        origin = os.path.join(project, rel.replace("/", os.sep))
        dst = os.path.join(out, layer, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(origin, "rb") as fa, open(dst, "wb") as fb:
            fb.write(fa.read())
        copied.append(f"{layer}/{rel}")
        copied_from.append((f"{layer}/{rel}", origin))

    # ‼️ The copied files ARE in the artifact, so they count toward its layers --
    # but ONLY the ones the walk did not already see. Counting the root alone
    # reported "no `logic/` layer" on a bundle holding `logic/conclusions.json`
    # (an index contradicting the directory beside it); counting both
    # unconditionally reported two of a file there is one of. The root is the
    # project on an ordinary build and the doomed machine's path under
    # `/evacuate`, which is exactly when the two differ.
    rootabs = os.path.abspath(root)
    for rel, origin in copied_from:
        if os.path.abspath(origin).startswith(rootabs + os.sep):
            continue
        layer = rel.split("/", 1)[0]
        counts[layer] += 1
        try:
            byte_totals[layer] += os.path.getsize(
                os.path.join(out, rel.replace("/", os.sep)))
        except OSError:
            pass

    title = a.title or f"{os.path.basename(os.path.abspath(project))} — research artifact"
    lines = render(project, root, counts, byte_totals, repro, concs, title,
                   extra=(a.note.splitlines() if a.note else None),
                   n_copied=len(copied))
    md = os.path.join(out, "ARTIFACT.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    rec = {"built_at": now_utc(), "project": os.path.basename(os.path.abspath(project)),
           "root": root, "layers": counts, "bytes": byte_totals,
           "reproducible": repro, "conclusions": _snapshot_of(concs),
           "copied": copied}
    atomic_write_json(os.path.join(out, "ara.json"), rec)

    payload = {"ok": True, "artifact": md, "out": out,
               "id": os.path.basename(out), "layers": counts,
               "reproducible": repro["verdict"],
               "records_copied": len(copied), "copied": copied[:20]}
    if len(copied) > 20:
        # Named rather than trimmed in silence: a list that stops at twenty and
        # says nothing reads as the whole set. `ara.json` holds all of them.
        payload["copied_not_listed"] = len(copied) - 20
    if repro["verdict"] != "yes":
        payload["reproducibility"] = repro["reason"]
    if not counts["src"]:
        payload["‼️src"] = ("no `src` layer: weights and numbers with no way to "
                            "regenerate them is a BACKUP, not an artifact")
    if not counts["logic"]:
        payload["‼️logic"] = ("no `logic` layer: nothing records what this round "
                              "came to believe. That is `/conclude`'s")
    if counts["unclassified"]:
        payload["unclassified"] = members["unclassified"][:10]
    emit(payload)


def cmd_check(a):
    """Has the artifact drifted from what it was built out of? Reports only.

    The failure this catches is `/conclude`'s own, one level up: a frozen
    `logic/` that still reads `supported` while the live record has gone
    `unverifiable`. The frozen copy is the one people read, and nothing about it
    changes when its evidence rots.
    """
    project = os.path.expanduser(a.project)
    out = resolve_out(project, a.id, a.out)
    rec = read_json(os.path.join(out, "ara.json"), required=False)
    if rec is None:
        refuse(f"no artifact at {out}", fix="`ara.py build --project <p>` first")

    findings = []
    for layer in ("src", "logic"):
        if not (rec.get("layers") or {}).get(layer):
            findings.append(("critical" if layer == "src" else "major",
                             f"the artifact has no `{layer}/` layer"))

    was = {c["id"]: c for c in rec.get("conclusions") or []}
    now = {c.get("id"): c for c in _conclusions(project)}
    for cid, old in was.items():
        cur = now.get(cid)
        if cur is None:
            findings.append(("critical",
                             f"{cid} was in this artifact and is no longer in "
                             f"`conclusions.json` -- the artifact cites a belief "
                             f"nothing carries any more"))
            continue
        if cur.get("status") != old.get("status"):
            findings.append(("critical",
                             f"{cid}: the artifact froze `{old.get('status')}` and the "
                             f"record now says `{cur.get('status')}`. The frozen copy "
                             f"is the one people read, and nothing about it changed "
                             f"when its evidence did"))
        if cur.get("tier") != old.get("tier"):
            findings.append(("critical",
                             f"{cid}: the artifact froze tier {old.get('tier')} and the "
                             f"record now says {cur.get('tier')}. The tier travels with "
                             f"the number, into this file too"))
    for cid in now:
        if cid not in was:
            findings.append(("major",
                             f"{cid} was concluded after this artifact was built and "
                             f"is not in it -- rebuild before citing it"))

    if (rec.get("reproducible") or {}).get("verdict") != "yes":
        findings.append(("major", "reproducibility: "
                                  + (rec.get("reproducible") or {}).get("reason", "?")))

    order = ("critical", "major", "minor")
    findings.sort(key=lambda f: order.index(f[0]))
    n_crit = sum(1 for f in findings if f[0] == "critical")
    payload = {"artifact": out, "id": os.path.basename(out),
               "built_at": rec.get("built_at"),
               "layers": rec.get("layers"),
               "findings": [{"severity": s, "detail": d} for s, d in findings],
               "critical": n_crit, "repaired": "nothing -- this verb reports"}
    if n_crit and not a.no_fail:
        emit(payload)
        sys.exit(1)
    emit(payload)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("build")
    s.add_argument("--project", required=True)
    s.add_argument("--root", default=None,
                   help="what to classify. Defaults to the project; `/evacuate` "
                        "passes the doomed machine's path instead")
    s.add_argument("--out", default=None)
    s.add_argument("--id", default=None,
                   help="rebuild this artifact in place instead of dating a new "
                        "one. Overwrites what it believed then -- ask for it")
    s.add_argument("--title", default=None)
    s.add_argument("--note", default=None)
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("check")
    s.add_argument("--project", required=True)
    s.add_argument("--out", default=None)
    s.add_argument("--id", default=None, help="default: the newest")
    s.add_argument("--no-fail", action="store_true")
    s.set_defaults(func=cmd_check)

    a = p.parse_args()
    try:
        a.func(a)
    except SystemExit:
        raise
    except Exception as e:                                   # noqa: BLE001
        broke(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
