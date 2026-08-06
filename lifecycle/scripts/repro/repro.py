#!/usr/bin/env python3
"""Can this run be reproduced, and when the number moves, which axis moved it.

Why this exists
---------------
Every part of the reproduction contract is already written down somewhere, and
**nothing ever checks that it still holds.** `code_snapshot.py` writes
`origin_commit` + a dirty patch and calls that a reproduction contract; no verb
anywhere re-reads it to ask whether the commit still resolves. `/data-freeze`
pins membership and `/data-retire` stamps `data_retired` into the snapshot when
bytes are freed at a location; nothing joins that back to the runs that cited it
-- nor to the census that says whether a copy survived. `env.packages`
is captured per run and only ever compared inside `/train-run`. So a run rots
along five independent axes and reads as pristine the whole way down.

This script does two things nothing else can:

  check       join all five axes for one run and say, per axis, intact /
              drifted / gone / unverifiable. Records only: local git and local
              files, no network, no ssh. A dated observation, like a census.

  the loop    a repro session. Judge a re-measured number against a band this
              pipeline measured on itself, then — when it lands outside — pin
              one axis per iteration until the divergence is attributed.

Why a band rather than a tolerance
----------------------------------
`/refactor-run` ships +/-0.5% relative as a default, and it is a reasonable
default guess. It is still a guess. Whether a -0.3% delta is noise or a real
divergence is a property of this pipeline (dataloader shuffling, cudnn
autotune, AMP, nondeterministic scatter-add), and the only way to know it is to
run the same thing more than once. So `band` measures the interval N repeats
actually produce and asks whether the recorded value lies inside it. No
distribution is assumed and none is needed: the question "would this pipeline
have produced that number again" is answered by whether it did.

A band from two points is a range, not a band -> `band` refuses under 3.

Why the verdict vocabulary is wider than pass/fail
--------------------------------------------------
Two runs can produce the same aggregate metric from different predictions — the
small objects all get lost and the average lands in the same place. Reporting
that as reproduced is a fake verification of exactly the shape the rest of this
lifecycle guards against, so the metric verdict and the prediction verdict are
separate and `reproduced` requires both. That is what the probe set is for, and
why it is declared at `open`: a probe set chosen after seeing the result is a
test written to its own answer.

Exit codes per CLAUDE.md "Script Integration":
    0 = worked
    1 = worked and the answer is no (a refusal — pass it through, do not
        route around it)
    2 = broke, do the same work by hand

`check`, `status` and `attribute` never exit 1: reporting that a reproduction is
dead is not a refusal.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from _records import (atomic_write_json, broke, emit, now_utc, parse_ts, read_json, refuse, stamp)  # noqa: E402
from framework_integrity import framework_integrity  # noqa: E402

AXES = ("data", "code", "env", "params", "artifacts")

# Per-axis verdicts. `unverifiable` is the one that matters: a probe that could
# not run must never collapse into `intact`. "The commit resolves" and "there is
# no commit recorded" are different facts, and only the first is evidence.
VERDICTS = ("intact", "drifted", "gone", "unverifiable")

# Severity, which is NOT the tuple order above and must never be derived from
# it. Ranking by tuple position put `unverifiable` above `gone` and reported a
# run whose training data had been deleted as merely unverifiable -- the single
# worst cell in the table, reported as the second-mildest.
#
# `drifted` outranks `unverifiable` because a known difference is actionable now
# and an unknown one is not; the unverifiable set is reported alongside the
# overall verdict rather than folded into it, and `close` refuses `reproduced`
# on an unverifiable axis independently of any of this.
SEVERITY = {"intact": 0, "unverifiable": 1, "drifted": 2, "gone": 3}

# Cheapest first. `attribute` walks this order, and it is cost order rather than
# likelihood order on purpose: a wrong guess about likelihood costs one cheap
# iteration, a wrong guess about cost can cost three days of GPU.
PIN_COST = ("env", "params", "artifacts", "data", "code")

# Packages whose version silently changes what the model computes. The axis goes
# `drifted` only for these (plus driver/cuda/gpu). Everything else is recorded in
# `also_changed` and leaves the verdict alone -- a verdict that fires on a pandas
# patch bump gets ignored, and the torch bump gets ignored along with it.
KEY_PACKAGES = (
    "torch", "torchvision", "torchaudio", "tensorflow", "keras", "jax", "jaxlib",
    "numpy", "transformers", "timm", "mmcv", "mmdet", "detectron2", "ultralytics",
    "opencv-python", "albumentations", "kornia", "deepspeed", "accelerate",
    "flash-attn", "onnxruntime", "tensorrt", "triton", "xgboost", "lightgbm",
)
KEY_ENV_FIELDS = ("cuda", "cudnn", "nvidia_driver", "gpu", "gpu_count", "python")

TERMINAL_SESSION = ("closed",)

METRIC_VERDICTS = ("reproduced", "inconclusive", "diverged")

# --------------------------------------------------------------------------- #
# A band has a source, and the source decides which way it can answer
# --------------------------------------------------------------------------- #
#
# `trials` is run-to-run spread: repeat the whole procedure N times with nothing
# pinned and read the interval. It is the strong one and it can answer BOTH ways --
# inside means noise, far outside means divergence.
#
# `run_history` is the target run's own converged tail: the same weights trajectory
# scored at N nearby epochs. Same seed, same data order, same init, so it does NOT
# contain the sources of variation that make two fresh runs differ -- kernel
# selection under `deterministic: false`, dataloader order, initialisation. It is
# therefore a LOWER BOUND on run-to-run spread, and a lower bound is one-directional:
#
#     inside it   -> sound. delta <= lower_bound <= true noise, so the delta IS
#                    noise-sized. This confirms.
#     outside it  -> `inconclusive`, NEVER `diverged`. True noise may be wider than
#                    this band can see, so the delta is not yet shown to be real.
#
# That asymmetry is the whole point: a free band can confirm but cannot refute. It
# exists because the alternative -- demanding three repeats before any answer -- makes
# the cheap case pay the expensive case's price. Three eval trials cost two minutes;
# three retrains cost three times the original run. Same number, 500x the bill.
#
# A declared tolerance is deliberately NOT a band source. `declared_tolerance`
# already holds it and already states that it does not decide the verdict; giving a
# typed number a way to become a band would undo that in one flag.
BAND_SOURCES = ("trials", "run_history")

MIN_TRIALS_FOR_BAND = 3
MIN_HISTORY_FOR_BAND = 5   # four points is a range with extra steps, not a distribution

# What one trial costs, which is what the default number of them has to follow.
# `open` resolves --band-trials from this when the caller does not say.
DEFAULT_BAND_TRIALS = {"eval": 3, "retrain": 1}
FINAL_VERDICTS = (
    "reproduced",                        # inside the band, every axis intact, predictions agree
    "reproduced_with_drift",             # inside the band, but >=1 axis drifted
    # The same two facts for a target whose PROCEDURE was never re-run. See
    # REMEASURE_ONLY below -- this pair exists because one word was doing two jobs.
    "remeasured",
    "remeasured_with_drift",
    "metric_ok_predictions_diverged",    # the dangerous one -- never call this reproduced
    "diverged",                          # outside the band, cause attributed
    "diverged_unattributed",             # outside the band, loop exhausted, no axis explains it
    "not_reproducible",                  # an axis is gone; nothing was run
)

# Verdicts that assert THE NUMBER CAME BACK. All four share one evidence bar: a
# measured band saying `reproduced`, and the probe actually run if one was
# declared. What differs is the axis requirement, and — since the split — what the
# number coming back is evidence OF. Deliberately not named for reproduction: two
# of its four members deny exactly that, and a constant whose name overstates its
# members is how the conflation got in.
ASSERTS_THE_NUMBER_CAME_BACK = ("reproduced", "reproduced_with_drift",
                                "remeasured", "remeasured_with_drift")

# --------------------------------------------------------------------------- #
# One word was doing two jobs, and they have opposite bars
# --------------------------------------------------------------------------- #
#
# `measure_via: eval` is the default "including for training runs", and the cost
# argument for that is sound: re-measuring a surviving checkpoint answers "is the
# recorded number real" for the price of one eval, where retraining costs what the
# original cost. What was NOT sound is calling the result `reproduced`.
#
# Re-measuring a training run's artifact re-runs nothing about the training. It
# cannot see a hyperparameter recorded wrongly, a dataset recorded wrongly, or a
# recipe that would no longer produce this model -- the artifact is a GIVEN, and
# every one of those could be false while the number comes back perfectly. So:
#
#   target is an eval run,   via eval     -> a FULL reproduction. The run being
#                                            reproduced WAS a measurement, so
#                                            re-measuring is re-running it
#   target is a training run, via eval    -> a re-measurement. Says nothing about
#                                            whether the recipe still works
#   target is a training run, via retrain -> a reproduction of the training
#
# Two words, same spelling, opposite bars -- the exact defect `/discover`'s
# searches.md names for `verified` on a result lead. It matters here because
# skill-graph.md makes a closed `reproduced*` session the ONLY thing that moves an
# inherited checkpoint's `origin.confidence` off `claimed`, so the weaker fact was
# buying the stronger promotion.
REMEASURE_ONLY = {"remeasured", "remeasured_with_drift"}


def is_remeasure_only(session) -> bool:
    """True when this session's procedure was never re-run.

    Keyed on the TARGET's stage rather than on a flag, because that is the fact
    that decides it: an eval run re-measured is an eval run re-run.
    """
    target_stage = ((session.get("target") or {}).get("run") or "").split("/")[0]
    return (session.get("measure_via") == "eval"
            and target_stage not in ("evaluation", "inference"))


# --------------------------------------------------------------------------
# locating things
# --------------------------------------------------------------------------

def resolve_run_ref(project, ref):
    """`<stage>/<run_id>` -> absolute run directory. The two-part form is
    required: a bare run_id is ambiguous across stages, and guessing the stage
    is how a repro ends up comparing an eval number to a training number."""
    parts = ref.strip("/").split("/")
    if len(parts) != 2:
        refuse(f"run ref must be <stage>/<run_id>, got {ref!r}",
               why="a bare run_id does not say which stage, and the stage is "
                   "what decides whether two numbers are the same quantity")
    stage, rid = parts
    return os.path.join(project, "stages", stage, "runs", rid), stage, rid


def load_run(project, ref):
    d, stage, rid = resolve_run_ref(project, ref)
    rj = os.path.join(d, "run.json")
    if not os.path.exists(rj):
        broke(f"no run.json at {rj}")
    return read_json(rj), d, stage, rid


def code_dir_for(project, stage):
    """layout.md: `code/_source` when it exists, else `code/`. Getting this
    backwards silently checks a different tree than the one that ran."""
    base = os.path.join(project, "stages", stage, "code")
    src = os.path.join(base, "_source")
    return src if os.path.exists(src) else base


def git(cwd, *args):
    """-> (ok, stdout). Never raises: a missing git or a non-repo is a probe
    result (`unverifiable`), not a crash."""
    try:
        p = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        return p.returncode == 0, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def axis(verdict, detail, **extra):
    if verdict not in VERDICTS:
        broke(f"bad axis verdict {verdict!r}", allowed=list(VERDICTS))
    return {"verdict": verdict, "detail": detail, **extra}


def survivors_of_retirement(project, did, retired):
    """A `data_retired` stamp says units are gone **from one location**, and that
    is not the same fact as the data being gone.

    `retire.py plan` excludes `below_min_copies` by default, so the ordinary way
    to produce a stamp at all is `--waive cited_by_snapshot` alone -- a deletion
    that provably left copies behind. Reading every stamp as `gone` reports such
    a run `not_reproducible`, and per CLAUDE.md that verdict means no amount of
    relaunching gets past it. The user stops, and the data is sitting on the
    authority the whole time.

    So the stamp alone is `unverifiable`, and only a census can promote it. The
    join is records-only (the census files are local JSON) and needs a census
    that postdates the deletion: an earlier one describes a disk that has since
    been written to.

    -> (verdict, detail, extra)."""
    units = sorted({u for e in retired for u in (e.get("units") or [])})
    locs = sorted({e.get("at") for e in retired if e.get("at")})
    where = ", ".join(locs) or "an unnamed location"
    extra = {"retired_units": len(units), "retired_from": locs}

    cutoff, undated = None, []
    for e in retired:
        ts = parse_ts(e.get("retired_at"))
        if ts is None:
            undated.append(e.get("retire_id") or "?")
        elif cutoff is None or ts > cutoff:
            cutoff = ts
    if undated:
        return ("unverifiable",
                f"{len(units)} unit(s) stamped retired from {where}, but "
                f"retirement {', '.join(undated)} carries no dated "
                f"`retired_at` -- no census can be shown to postdate it",
                extra)

    cdir = os.path.join(project, "datasets", did, "census")
    newest, newest_at = None, None
    for name in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
        if not name.endswith(".json"):
            continue
        c = read_json(os.path.join(cdir, name), required=False)
        at = parse_ts((c or {}).get("scanned_at"))
        if at is None or at <= cutoff:
            continue
        if newest_at is None or at > newest_at:
            newest, newest_at = c, at
    if newest is None:
        return ("unverifiable",
                f"{len(units)} unit(s) retired from {where} at "
                f"{cutoff.isoformat()}; no census has been taken since, so "
                f"nothing on disk here can say whether another copy survives "
                f"-- run `census.py scan --dataset {did}`",
                extra)

    seen = newest.get("units") or {}
    missing = [u for u in units if not (seen.get(u) or {}).get("at")]
    extra = {**extra, "census": newest.get("census_id"),
             "census_scanned_at": newest.get("scanned_at"),
             "survivors": len(units) - len(missing)}
    if not missing:
        # Which locations still hold *every* retired unit -- the ones a
        # `resolve --at` can actually be pointed at. A location holding some of
        # them rebuilds a partial set, which is the failure this names.
        whole = sorted(set.intersection(*(set((seen.get(u) or {}).get("at") or [])
                                          for u in units)))
        alt = (f"resolve --at {whole[0]}" if whole else
               "no single location holds all of them -- resolve will not rebuild "
               "this set from any one place")
        return ("drifted",
                f"{len(units)} unit(s) retired from {where}; census "
                f"{newest.get('census_id')} still finds all of them elsewhere "
                f"-- the frozen membership is intact, but `census.py resolve "
                f"--at {locs[0] if locs else '<loc>'}` no longer rebuilds it. "
                f"Use `{alt}`",
                {**extra, "resolvable_at": whole})
    if not newest.get("complete"):
        return ("unverifiable",
                f"{len(missing)} of {len(units)} retired unit(s) are at no "
                f"location in census {newest.get('census_id')} -- but that "
                f"census is incomplete, so a location that did not answer may "
                f"be holding them. 'Could not look' is not 'not there'",
                {**extra, "missing_units": missing[:20]})
    return ("gone",
            f"{len(missing)} of {len(units)} unit(s) retired from {where} are "
            f"at no location in census {newest.get('census_id')} -- the "
            f"citation resolves and the bytes do not",
            {**extra, "missing_units": missing[:20]})


# --------------------------------------------------------------------------
# the five axis probes
# --------------------------------------------------------------------------

def probe_data(project, run):
    """The data axis, and the only one where MLClaw can give a real answer.

    A citation (`datasets/<id>@<snap>`) names a frozen membership set, so
    "retired" is a fact somebody wrote down. A bare path in `sources.json` names
    a directory, and a directory that still exists tells you nothing about
    whether its contents are the ones that trained -- that is `unverifiable`,
    and saying so is the whole point."""
    parents = (run.get("lineage") or {}).get("parents") or []
    cites = [p for p in parents if p.startswith("datasets/") and "@" in p]
    handoffs = [p for p in parents if p.startswith("handoffs/")]

    findings, worst = [], "intact"

    def worsen(v):
        nonlocal worst
        if SEVERITY[v] > SEVERITY[worst]:
            worst = v

    for cite in cites:
        body = cite[len("datasets/"):]
        did, _, sid = body.partition("@")
        spath = os.path.join(project, "datasets", did, "snapshots", sid, "snapshot.json")
        snap = read_json(spath, required=False)
        if snap is None:
            findings.append({"cite": cite, "verdict": "gone",
                             "detail": "snapshot record not found -- the citation "
                                       "no longer resolves at all"})
            worsen("gone")
            continue
        retired = snap.get("data_retired") or []
        if retired:
            verdict, detail, extra = survivors_of_retirement(project, did, retired)
            findings.append({"cite": cite, "verdict": verdict,
                             "detail": detail, **extra})
            worsen(verdict)
            continue
        findings.append({"cite": cite, "verdict": "intact",
                         "detail": f"{snap.get('count')} units, frozen {snap.get('frozen_at')} "
                                   f"from {snap.get('census_id')}",
                         "unverified_units": len(snap.get("unverified_units") or [])})

    for ref in handoffs:
        hid = ref[len("handoffs/"):]
        hpath = os.path.join(project, "handoffs", hid, "handoff.json")
        rec = read_json(hpath, required=False)
        if rec is None:
            findings.append({"cite": ref, "verdict": "gone",
                             "detail": "handoff record not found"})
            worsen("gone")
            continue
        if rec.get("status") != "accepted":
            findings.append({"cite": ref, "verdict": "drifted",
                             "detail": f"handoff status is {rec.get('status')!r}, not "
                                       f"'accepted' -- what the run consumed is not what "
                                       f"this record now describes"})
            worsen("drifted")
            continue
        cov = ((rec.get("latest") or {}).get("coverage")
               if isinstance(rec.get("latest"), dict) else None)
        findings.append({"cite": ref, "verdict": "intact",
                         "detail": f"accepted, coverage {cov}", "coverage": cov})

    if not findings:
        srcs = read_json(os.path.join(run_paths_dir(project, run), "sources.json"),
                         required=False) if run_paths_dir(project, run) else None
        n = len(srcs) if isinstance(srcs, dict) else 0
        return axis("unverifiable",
                    "the run cited no frozen dataset and no handoff, so what it read "
                    "is a path and nothing recorded what was behind it",
                    citations=0, sources_entries=n,
                    fix="future runs: cite datasets/<id>@<snapshot> "
                        "(/data-freeze) so this question has an answer")
    return axis(worst, f"{len(findings)} citation(s) checked", citations=findings)


def run_paths_dir(project, run):
    ref = f"{run.get('stage')}/{run.get('run_id')}"
    if not run.get("stage") or not run.get("run_id"):
        return None
    return os.path.join(project, "stages", run["stage"], "runs", run["run_id"])


def probe_code(project, run, stage, framework_python=None):
    """`code_snapshot.py` states the contract:
        git checkout <origin_commit> && git apply <run_dir>/<dirty_patch_path>
    This is the only thing that ever checks it."""
    code = run.get("code") or {}

    # A stage whose code_source is `framework` has no tree, so it has no SHA, and
    # a null one here is BY CONSTRUCTION rather than a capture that failed. Reading
    # it as "the tree that ran was never identified" is exactly the confusion
    # `code.kind` exists to prevent — see layout.md -> "Code Source Resolution".
    # Its contract is `install <pkg>==<version>`, so what pins it is the package
    # version, which is the ENV axis's question; the honest verdict here is
    # whichever of those the version comparison supports, not a statement about a
    # tree that never existed.
    if code.get("kind") == "framework":
        pkg, ver = code.get("framework"), code.get("framework_version")
        if not (pkg and ver):
            return axis("unverifiable",
                        "code.kind is `framework` but the pinned version is missing, "
                        "so nothing identifies the code that ran",
                        fix="code_snapshot.py --framework refuses an unpinned spec; "
                            "this record bypassed it")
        ran = ((run.get("env") or {}).get("packages") or {}).get(pkg)
        if ran and ran.split("+")[0] != ver.split("+")[0]:
            return axis("drifted",
                        f"the record pins {pkg}=={ver} but env.packages says {ran} "
                        f"actually ran -- the code that executed is not the code the "
                        f"contract names",
                        framework=pkg, pinned=ver, ran=ran,
                        fix=f"rebuild the env at {pkg}=={ran} to reproduce what ran, "
                            f"or at =={ver} to honour the pin, and say which")

        # The version matches. That used to end the axis at `intact` with a note
        # saying the remaining question -- was the installed package EDITED after
        # install -- "cannot be checked from here". It can: pip's dist-info RECORD
        # holds a sha256 per installed file, so hashing them answers it offline.
        # `discover.py verify-framework` is that check, and it needs the RUN
        # environment's interpreter because the package lives there and not here.
        #
        # Absence of the interpreter must NOT read as a pass. Two verdicts, and the
        # split is the point: checked-and-clean is `intact`, unchecked is
        # `unverifiable`. Collapsing them would make a question nobody asked look
        # like a question answered -- the one thing the fourth verdict exists for.
        integ = None
        if framework_python:
            integ = framework_integrity(f"{pkg}=={ver}", python=framework_python)
        base = (f"no tree by design (code_source `framework`); the contract is "
                f"`install {pkg}=={ver}`"
                + (f", and env.packages agrees ({ran})" if ran else ""))
        if integ is None:
            return axis("unverifiable",
                        base + " -- but whether that installed package was EDITED "
                               "after install was not checked, and an unchecked "
                               "question is not a clean one",
                        framework=pkg, framework_version=ver,
                        integrity="not_checked",
                        fix="pass --framework-python <the run environment's "
                            "interpreter> to close it: pip's RECORD holds a sha256 "
                            "per installed file, so the check is offline and exact")
        st = integ.get("state")
        if st == "as_published":
            return axis("intact",
                        base + f", and all {integ.get('files_checked')} hashed "
                               f"file(s) match the RECORD pip wrote at install time "
                               f"-- this IS the published artifact",
                        framework=pkg, framework_version=ver,
                        integrity=integ)
        if st in ("edited", "incomplete"):
            what = ("was modified after install"
                    if st == "edited" else "is missing files the RECORD names")
            return axis("drifted",
                        base + f" -- but the installed package {what} "
                               f"({integ.get('files_mismatched')} mismatched, "
                               f"{integ.get('files_missing')} missing of "
                               f"{integ.get('files_checked')} checked). "
                               f"`install {pkg}=={ver}` will NOT reproduce what ran",
                        framework=pkg, framework_version=ver, integrity=integ,
                        fix="capture the difference before that environment is "
                            "rebuilt -- it is the dirty patch a git tree would have "
                            "produced, and nothing else records it")
        return axis("unverifiable",
                    base + f" -- the edit check did not produce a clean result "
                           f"({st}: {integ.get('detail')})",
                    framework=pkg, framework_version=ver, integrity=integ)

    sha = code.get("origin_commit")
    if not sha:
        return axis("unverifiable", "no origin_commit recorded -- the tree that ran "
                                    "was never identified",
                    fix="nothing to do retroactively; code_snapshot.py refuses "
                        "non-git trees so this run predates it or bypassed it. When "
                        "the stage genuinely has no tree, the record should carry "
                        "code.kind `framework` instead")

    cdir = code_dir_for(project, stage)
    if not os.path.isdir(cdir):
        return axis("unverifiable", f"code dir absent: {cdir}")

    ok, _ = git(cdir, "cat-file", "-e", f"{sha}^{{commit}}")
    if not ok:
        return axis("gone", f"origin_commit {sha[:12]} does not resolve in {cdir} -- "
                            "the tree that ran cannot be rebuilt",
                    origin_commit=sha,
                    fix="fetch the remote, or find the branch that still contains it")

    notes = {}
    patch_rel = code.get("dirty_patch_path")
    if patch_rel:
        rd, _, _ = resolve_run_ref(project, f"{stage}/{run.get('run_id')}")
        ppath = os.path.join(rd, patch_rel)
        if not os.path.exists(ppath):
            return axis("drifted", f"origin_commit resolves but the dirty patch is "
                                   f"missing: {patch_rel}",
                        origin_commit=sha, dirty_files_count=code.get("dirty_files_count"),
                        fix="checkout alone rebuilds a different tree than the one "
                            "that ran; the patch was the difference")
        notes["dirty_patch"] = patch_rel
        notes["dirty_files_count"] = code.get("dirty_files_count")

    if code.get("reproducible") is False:
        # The VERDICT is right whatever the cause: the record says checkout+apply
        # rebuilds a tree that is not the one that ran, so this can never be
        # `intact`. The DETAIL used to assert one specific cause -- an
        # oversized file the patch could not embed -- because that was the only
        # way `code_snapshot.py` produced the flag. Records now arrive from other
        # places (an imported external run whose launch script was edited without
        # being committed, for one), and asserting a cause the record does not
        # state is a made-up fact sitting in the field a reader acts on. So the
        # recorded reason is surfaced when there is one, and the generic clause
        # only names what the flag MEANS.
        why = [w for w in (code.get("warnings") or []) if isinstance(w, str)]
        return axis("unverifiable",
                    "code.reproducible is false -- checkout+apply rebuilds a tree "
                    "that is NOT the one that ran, so this cannot become intact "
                    "later. Recorded reason: "
                    + ("; ".join(w.strip() for w in why) if why else
                       "none -- the flag was set without a warning saying why, which "
                       "is itself worth chasing: the next reader cannot tell whether "
                       "the gap is an oversized patch, an uncommitted edit, or "
                       "something nobody wrote down"),
                    origin_commit=sha, recorded_warnings=why or None,
                    untracked_skipped=code.get("untracked_skipped") or [], **notes)
    if code.get("reproducible") is None:
        return axis("unverifiable", "code.reproducible was never recorded",
                    origin_commit=sha, **notes)

    return axis("intact", f"origin_commit {sha[:12]} resolves; "
                          f"{'patch present' if patch_rel else 'tree was clean'}",
                origin_commit=sha, **notes)


def current_env(packages):
    """Ask capture_env.py rather than reimplementing it -- one answer, one place."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "..", "shared", "capture_env.py")
    if not os.path.exists(script):
        return None, "capture_env.py not found"
    try:
        p = subprocess.run([sys.executable, script, ",".join(packages)],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if p.returncode != 0:
        return None, (p.stderr or "capture_env.py failed").strip()[:300]
    try:
        return json.loads(p.stdout), None
    except ValueError as exc:
        return None, f"capture_env.py output unparseable: {exc}"


def probe_env(run):
    was = run.get("env") or {}
    recorded = {k: v for k, v in (was.get("packages") or {}).items() if v}
    if not recorded and not any(was.get(f) for f in KEY_ENV_FIELDS):
        return axis("unverifiable", "no env recorded for this run")

    now, err = current_env(sorted(recorded) or list(KEY_PACKAGES))
    if now is None:
        return axis("unverifiable", f"could not read the current env: {err}",
                    fix="run capture_env.py by hand in the project env and diff it")

    key_drift, also = [], []
    for pkg, old in sorted(recorded.items()):
        new = (now.get("packages") or {}).get(pkg)
        if new == old:
            continue
        entry = {"package": pkg, "was": old, "now": new}
        (key_drift if pkg in KEY_PACKAGES else also).append(entry)
    for field in KEY_ENV_FIELDS:
        old, new = was.get(field), now.get(field)
        if old and new and old != new:
            key_drift.append({"field": field, "was": old, "now": new})

    if key_drift:
        return axis("drifted", f"{len(key_drift)} behaviour-affecting difference(s)",
                    key_drift=key_drift, also_changed=also)
    return axis("intact", "every behaviour-affecting package and device field matches",
                also_changed=also)


def probe_params(project, run, stage):
    """A param that was overridable when the run launched and is hardcoded now
    means the code moved under the recorded config. The run's own numbers are
    still real; relaunching with that config no longer produces them."""
    rd, _, _ = resolve_run_ref(project, f"{stage}/{run.get('run_id')}")
    snap = read_json(os.path.join(rd, "config_snapshot.json"), required=False)
    if snap is None:
        return axis("unverifiable", "no config_snapshot.json -- what this run was "
                                    "launched with was never frozen")
    params = snap.get("runtime_params")
    if not isinstance(params, dict):
        params = snap if isinstance(snap, dict) else {}

    stage_cfg = read_json(os.path.join(project, "stages", stage, "config.json"),
                          required=False) or {}
    inj = ((stage_cfg.get("param_injection") or {}).get("items") or {})
    if not inj:
        return axis("unverifiable", f"{len(params)} params snapshotted but the stage "
                                    f"has no param_injection to check them against",
                    params=len(params),
                    fix="/train-init Step 2b classifies them")

    lost = [k for k in params if k in inj and inj[k].get("overridable") is False]
    unknown = [k for k in params if k not in inj]
    if lost:
        return axis("drifted", f"{len(lost)} param(s) can no longer be set from "
                               f"outside: {', '.join(sorted(lost))}",
                    no_longer_overridable=sorted(lost), unclassified=sorted(unknown))
    if unknown:
        return axis("unverifiable", f"{len(unknown)} snapshotted param(s) are absent "
                                    f"from param_injection",
                    unclassified=sorted(unknown))
    return axis("intact", f"all {len(params)} params still externally settable")


def probe_artifacts(project, run):
    """Upstream run outputs this run consumed. run-mechanics: "Base's artifact
    must exist for this run to be reproducible." retention.py is what deletes
    them, and it has no idea who cited them."""
    parents = [p for p in ((run.get("lineage") or {}).get("parents") or [])
               if not p.startswith(("datasets/", "handoffs/"))]
    if not parents:
        return axis("intact", "no upstream run artifacts consumed")

    findings, worst = [], "intact"
    for ref in parents:
        try:
            pd, _, _ = resolve_run_ref(project, ref)
        except SystemExit:
            findings.append({"parent": ref, "verdict": "unverifiable",
                             "detail": "parent ref is not <stage>/<run_id>"})
            if SEVERITY["unverifiable"] > SEVERITY[worst]:
                worst = "unverifiable"
            continue
        prun = read_json(os.path.join(pd, "run.json"), required=False)
        if prun is None:
            findings.append({"parent": ref, "verdict": "gone",
                             "detail": "parent run record not found"})
            worst = "gone"
            continue
        outs = prun.get("outputs") or {}
        missing = [k for k, v in outs.items()
                   if isinstance(v, str) and v and not os.path.exists(
                       v if os.path.isabs(v) else os.path.join(pd, v))]
        if missing:
            findings.append({"parent": ref, "verdict": "gone",
                             "detail": f"declared output(s) absent on disk: "
                                       f"{', '.join(sorted(missing))}",
                             "missing": sorted(missing)})
            worst = "gone"
            continue
        findings.append({"parent": ref, "verdict": "intact",
                         "detail": f"{len(outs)} declared output(s) present"})
    return axis(worst, f"{len(findings)} parent run(s) checked", parents=findings)


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def assess(project, ref, skip_env=False, framework_python=None):
    run, rd, stage, rid = load_run(project, ref)
    axes = {
        "data": probe_data(project, run),
        "code": probe_code(project, run, stage, framework_python),
        "env": axis("unverifiable", "skipped by --no-env") if skip_env else probe_env(run),
        "params": probe_params(project, run, stage),
        "artifacts": probe_artifacts(project, run),
    }
    worst = max(axes.values(), key=lambda a: SEVERITY[a["verdict"]])["verdict"]
    unverifiable_axes = sorted(k for k, v in axes.items()
                               if v["verdict"] == "unverifiable")

    if worst == "gone":
        overall = "not_reproducible"
    elif worst == "drifted":
        overall = "reproducible_with_drift"
    elif worst == "unverifiable":
        overall = "reproducible_unverifiably"
    else:
        overall = "reproducible"

    best = ((run.get("metrics") or {}).get("best") or {})
    still = []
    if overall == "not_reproducible":
        ck = (run.get("outputs") or {}).get("best_checkpoint")
        if ck:
            still.append(f"re-measure the number from the surviving checkpoint "
                         f"({ck}) with /eval-run -- that verifies the metric even "
                         f"when the training data is gone")
        else:
            still.append("nothing: the data is gone and no checkpoint is declared "
                         "in outputs")
    if (run.get("mode") is None) and best.get("primary_metric_value") is not None:
        still.append("this run's `mode` is null, so its number is not comparable "
                     "to anything -- a repro session cannot judge it")

    return {
        "run": ref, "stage": stage, "run_id": rid, "checked_at": now_utc(),
        "status": run.get("status"), "mode": run.get("mode"), "scope": run.get("scope") or {},
        "target_metric": best.get("primary_metric"),
        "target_value": best.get("primary_metric_value"),
        "axes": axes, "overall": overall,
        "unverifiable_axes": unverifiable_axes, "you_can_still": still,
    }, rd


def cmd_check(a):
    project = os.path.expanduser(a.project)
    report, rd = assess(project, a.run, skip_env=a.no_env,
                        framework_python=a.framework_python)
    if not a.no_write:
        out = os.path.join(rd, "repro", f"check_{stamp()}.json")
        atomic_write_json(out, report)
        report["written_to"] = os.path.relpath(out, project)
    if a.json:
        emit(report)
    else:
        render_check(report)
    return 0


def render_check(r):
    print(f"{r['run']}  ·  {r['checked_at']}")
    print(f"  status {r['status']}  mode {r['mode']}  "
          f"{r['target_metric']} = {r['target_value']}\n")
    for name in AXES:
        ax = r["axes"][name]
        print(f"  {name:<10} {ax['verdict']:<14} {ax['detail']}")
        # A nested axis's summary line ("1 citation(s) checked") does not say
        # what happened to that citation, and the whole point is that somebody
        # reads this and acts. Surface the findings that are not intact.
        for key in ("citations", "parents"):
            for f in (ax.get(key) or []):
                if isinstance(f, dict) and f.get("verdict") != "intact":
                    who = f.get("cite") or f.get("parent") or "?"
                    print(f"  {'':<10} {'':<14}  {who}: {f.get('detail')}")
    print(f"\n  verdict: {r['overall'].upper()}")
    if r.get("unverifiable_axes"):
        print(f"  not known to have matched: "
              f"{', '.join(r['unverifiable_axes'])}")
    for line in r["you_can_still"]:
        print(f"  you can still: {line}")
    if r.get("written_to"):
        print(f"\n  written to {r['written_to']}")


# --------------------------------------------------------------------------
# the session
# --------------------------------------------------------------------------

def session_dir(project, sid):
    return os.path.join(project, "repro", sid)


def load_session(project, sid):
    p = os.path.join(session_dir(project, sid), "session.json")
    s = read_json(p, required=False)
    if s is None:
        broke(f"no such repro session: {sid}", looked_at=p)
    return s, p


def save_session(project, sid, s):
    atomic_write_json(os.path.join(session_dir(project, sid), "session.json"), s)


def cmd_open(a):
    project = os.path.expanduser(a.project)
    # **`open` does not register `--framework-python`; only `check` does.** So this
    # was `getattr(a, 'framework_python', None)` returning None on every call, and
    # the getattr made it look like the flag might be there. Explicit, because the
    # consequence is stated in that flag's own help: without it the code axis stays
    # `unverifiable` and can never reach `intact` -- so a repro session opened here
    # can never close the axis that says whether the installed package was edited
    # after install. Whether `open` should grow the flag is not a refactor's call
    # to make; leaving it visible is.
    report, _ = assess(project, a.run, skip_env=False, framework_python=None)

    if report["status"] != "completed":
        refuse(f"target run status is {report['status']!r}, not 'completed'",
               why="a run that did not finish has no number to reproduce")
    if report["target_value"] is None:
        refuse("target run has no metrics.best.primary_metric_value",
               why="there is nothing to reproduce; extraction failure and 'the run "
                   "never produced it' are different facts and neither is a target")
    if report["mode"] is None:
        refuse("target run has mode: null",
               why="metrics are only comparable within the same mode and an "
                   "equivalent scope. A null mode cannot be matched, so no trial "
                   "can be shown to be the same quantity")
    if report["overall"] == "not_reproducible":
        refuse("an axis is gone -- nothing can be relaunched",
               axes={k: v["verdict"] for k, v in report["axes"].items()},
               you_can_still=report["you_can_still"],
               fix="close this out as not_reproducible with `check`'s record; if a "
                   "checkpoint survives, open a session against the eval number "
                   "instead of the training one")

    if a.measure_via == "retrain" and not a.i_accept_the_cost:
        refuse("measure_via=retrain needs --i-accept-the-cost",
               why="retraining to verify a number costs what the original run cost, "
                   "and nondeterminism means the answer is a band rather than a "
                   "match. Re-measuring the checkpoint through eval answers the "
                   "same question for the price of one eval")

    # The symmetric gate, and the reason this skill has a strict standard at all.
    # `retrain` costs money, so it is gated on somebody typing that they accept
    # the cost. `eval` against a TRAINING target costs almost nothing and buys a
    # weaker answer, so it is gated on somebody typing that they accept THAT — or
    # the default path of a skill called `/repro` quietly answers a question
    # nobody asked and stamps a verdict that sounds like the one they wanted.
    #
    # Same discipline as `/data-label`'s `--spec | --no-spec`: the absence has to
    # be something a person typed rather than something that just didn't happen.
    stage_of_target = a.run.split("/")[0]
    if (a.measure_via == "eval" and stage_of_target not in ("evaluation", "inference")
            and not a.remeasure_only):
        refuse(f"the target is a {stage_of_target} run and measure_via is `eval`, so "
               f"this session will not reproduce anything",
               target=a.run,
               why="running inference over a val set re-measures the artifact the "
                   "training produced. It re-runs no part of the training, so a "
                   "hyperparameter or dataset recorded wrongly, and a recipe that "
                   "would no longer produce this model, are all invisible to it. "
                   "The best verdict reachable is `remeasured_with_drift`",
               your_two_options={
                   "--remeasure-only": "accept the weaker question: does this "
                                       "artifact still score what the record says. "
                                       "Cheap, and often what you actually want",
                   "--measure-via retrain --i-accept-the-cost":
                       "reproduce the training itself. Costs what the original run "
                       "cost and answers a fuzzier question, because "
                       "nondeterminism makes the best possible answer a band",
               },
               fix="pass one of them. Which question this is cannot be defaulted")

    # The default number of repeats follows what one repeat COSTS. A single number
    # for both routes makes the cheap case free and the expensive case a 3x bill for
    # a case that may never arise -- and the only way to learn whether it arises is
    # to run the first trial. See BAND_SOURCES for what fills the gap: the target's
    # own converged tail is a free lower-bound band, enough to confirm, and repeats
    # get bought only when the answer needs refuting.
    band_trials = (a.band_trials if a.band_trials is not None
                   else DEFAULT_BAND_TRIALS[a.measure_via])
    if band_trials < 1:
        refuse(f"--band-trials {band_trials} plans for no measurement at all",
               why="a reproduction with zero trials has re-run nothing")

    # Same rule and same shape as create_run.py's `allocate_run_dir`: the stamp
    # has one-second resolution, so two sessions opened in the same second would
    # share a directory and the second write would destroy the first record.
    # Suffix rather than refuse -- an id clash is not the user's mistake, and
    # `_2` is what run ids already do, so a reader has seen it before.
    base_sid = f"repro_{stamp()}" + (f"_{a.name}" if a.name else "")
    sid, collision = base_sid, None
    for n in range(1, 1000):
        sid = base_sid if n == 1 else f"{base_sid}_{n}"
        if not os.path.exists(os.path.join(session_dir(project, sid), "session.json")):
            break
    else:
        broke(f"could not allocate a session id from {base_sid}")
    if sid != base_sid:
        collision = (f"{base_sid} was already taken; allocated {sid} instead. "
                     f"Another session opened in the same second.")

    s = {
        "session_id": sid,
        "opened_at": now_utc(),
        "closed_at": None,
        "status": "open",
        "target": {
            "run": a.run,
            "metric": report["target_metric"],
            "value": report["target_value"],
            "direction": a.direction,
            "mode": report["mode"],
            "scope": report["scope"],
        },
        "measure_via": a.measure_via,
        "_comment_probe": "Fixed inputs run through both the original artifact and "
                          "each reproduction, rendered side by side for a person. "
                          "Declared here and never afterwards: a probe set chosen "
                          "once the result is known is a test written to its own answer.",
        "probe": ({"cite": a.probe, "declared_at": now_utc()} if a.probe else None),
        "_comment_tolerance": "The declared expectation, recorded before any trial "
                              "runs. It does NOT decide the verdict -- `band` does. "
                              "It is kept so the two can be compared: a default "
                              "wider than the measured spread would have passed a "
                              "real divergence.",
        "declared_tolerance": {"relative_pct": a.tolerance_pct,
                               "absolute": a.tolerance_abs},
        "band_target_trials": band_trials,
        "_comment_band_trials": (
            "How many unpinned repeats this session plans for, and it follows what one "
            "costs -- not one number for both routes. Three eval trials are two "
            "minutes; three retrains are three times the original run. Planning for 1 "
            "does not mean going without a band: `band --from-history` builds one from "
            "the target's own converged tail for free. That band is a lower bound, so "
            "it can confirm and never refute, and the refuting case is the only one "
            "that has to buy repeats."),
        "axes_at_open": {k: v["verdict"] for k, v in report["axes"].items()},
        "axes_detail_at_open": report["axes"],
        "trials": [],
        "band": None,
        "metric_verdict": None,
        "predictions_agree": None,
        "attributed_to": None,
        "verdict": None,
        "caveats": [],
    }
    save_session(project, sid, s)
    # SKILL.md Step 1's discipline, applied to the other ceiling: "say that up
    # front rather than at close". A caller who learns at close that the best word
    # available was never the one they wanted has already spent the trials.
    remeasure = is_remeasure_only(s)
    worst = max((SEVERITY[v] for v in s["axes_at_open"].values()), default=0)
    clean = "remeasured" if remeasure else "reproduced"
    ceiling = clean if worst == 0 else f"{clean}_with_drift"
    out = {"session_id": sid, "opened": True,
           "target": s["target"], "measure_via": s["measure_via"],
           "probe_declared": bool(a.probe),
           "verdict_ceiling": ceiling,
           "reproduces_the_procedure": not remeasure,
           "ceiling_why": (
               ("the target is a training run measured through eval, so the training "
                "is not re-run and no verdict here is a reproduction; "
                if remeasure else "")
               + (f"axes not intact at open: "
                  f"{', '.join(k for k, v in s['axes_at_open'].items() if v != 'intact')}"
                  if worst else "every axis intact at open")),
           "axes_at_open": s["axes_at_open"],
           "band_target_trials": band_trials,
           "next": f"run {band_trials} unpinned trial(s) through "
                   f"{'/eval-run' if a.measure_via == 'eval' else '/train-run'}, "
                   f"register each with `repro.py trial`, then `repro.py band`"
                   + (f" --from-history <the target's converged tail> -- "
                      f"{band_trials} trial(s) is below the {MIN_TRIALS_FOR_BAND} a "
                      f"run-to-run band needs, so the band comes from the target's own "
                      f"wobble. It can confirm but not refute; if it lands "
                      f"`inconclusive`, THAT is when more trials are worth their price"
                      if band_trials < MIN_TRIALS_FOR_BAND else ""),
           "written_to": os.path.relpath(
               os.path.join(session_dir(project, sid), "session.json"), project)}
    if collision:
        out["id_collision"] = collision
        sys.stderr.write(f"repro: warning: {collision}\n")
    emit(out)
    return 0


def cmd_trial(a):
    project = os.path.expanduser(a.project)
    s, _ = load_session(project, a.session)
    if s["status"] in TERMINAL_SESSION:
        refuse(f"session {a.session} is {s['status']}", why="a closed session's "
               "trials are its evidence; adding to them after the verdict changes "
               "what the verdict was based on")

    trun, _, tstage, _ = load_run(project, a.run)
    if trun.get("status") != "completed":
        refuse(f"trial run status is {trun.get('status')!r}, not 'completed'")

    # Comparability, not politeness. run-mechanics "Metric comparability": a
    # debug trial against a production target is a fake comparison -- nothing
    # errors, no data is missing, and the conclusion is wrong.
    tgt = s["target"]
    if trun.get("mode") != tgt["mode"]:
        refuse(f"trial mode {trun.get('mode')!r} != target mode {tgt['mode']!r}",
               why="metrics are comparable only within the same mode; these two "
                   "numbers describe different workloads and happen to share a name")
    # `_`-prefixed keys are commentary everywhere else in this codebase (`_comment`,
    # `_note`, `_delta_vs_target`), and a differing sentence is not a differing
    # measurement. Comparing them made prose decide comparability, which is the
    # opposite of what this guard is for -- and it fails CLOSED, so it reads as the
    # guard working.
    t_scope = {k: v for k, v in (trun.get("scope") or {}).items()
               if not k.startswith("_")}
    g_scope = {k: v for k, v in (tgt["scope"] or {}).items()
               if not k.startswith("_")}
    if t_scope != g_scope:
        differing = sorted(set(t_scope) | set(g_scope)
                           if set(t_scope) != set(g_scope)
                           else [k for k in t_scope if t_scope[k] != g_scope.get(k)])
        # A person who has LOOKED can say the difference is immaterial -- and then
        # it goes in the record, not into a shrug. Same discipline as
        # `--i-accept-the-cost` and `--remeasure-only`: the absence of an objection
        # has to be something somebody typed. What is refused is the DEFAULT, which
        # must never quietly compare two different quantities.
        if not a.scope_differs_immaterially:
            refuse("trial scope differs from the target's scope",
                   trial_scope=t_scope, target_scope=g_scope,
                   differing_keys=differing,
                   why="two production runs over different sample counts are not "
                       "comparable either. This compares the measurement only -- "
                       "`_`-prefixed commentary is ignored",
                   your_option={
                       "--scope-differs-immaterially '<why>'":
                           "record the difference as looked-at and judged too small "
                           "to move the metric, and proceed. The text is stored on "
                           "the trial and surfaces in the verdict's caveats"})
        scope_waiver = {"differing_keys": differing, "trial": t_scope,
                        "target": g_scope,
                        "judged_by_a_person": a.scope_differs_immaterially}
    else:
        scope_waiver = None

    best = ((trun.get("metrics") or {}).get("best") or {})
    value = a.value if a.value is not None else best.get("primary_metric_value")
    if value is None:
        refuse("trial has no metric value",
               why="record-integrity rule: a metric that could not be read is not "
                   "recorded as anything. Pass --value only if you read it yourself")
    if a.value is None and best.get("primary_metric") != tgt["metric"]:
        refuse(f"trial's primary_metric is {best.get('primary_metric')!r}, "
               f"target's is {tgt['metric']!r}",
               fix="--value <n> to register a number you read from the right metric")

    pinned = sorted(set(a.pinned or []))
    for p in pinned:
        if p not in AXES:
            broke(f"unknown axis {p!r}", allowed=list(AXES))

    delta = value - tgt["value"]
    entry = {
        "n": len(s["trials"]) + 1,
        "run": a.run,
        "stage": tstage,
        "pinned": pinned,
        "value": value,
        "delta": round(delta, 10),
        "delta_pct": (round(100.0 * delta / tgt["value"], 6) if tgt["value"] else None),
        "probe_run": a.probe_run,
        "predictions_agree": a.predictions_agree,
        "scope_waiver": scope_waiver,
        "at": now_utc(),
    }
    s["trials"].append(entry)
    if scope_waiver:
        # Into `caveats`, where the verdict is read. A waived difference that only
        # lives on the trial is a difference the conclusion does not carry.
        note = (f"trial {entry['n']} was compared across a scope difference in "
                f"{', '.join(scope_waiver['differing_keys'])}, waived by hand: "
                f"{scope_waiver['judged_by_a_person']}")
        if note not in s["caveats"]:
            s["caveats"].append(note)
    if a.predictions_agree is not None:
        s["predictions_agree"] = a.predictions_agree
    save_session(project, a.session, s)
    emit({"session_id": a.session, "registered": entry,
          "unpinned_trials": sum(1 for t in s["trials"] if not t["pinned"]),
          "next": "`repro.py band` once there are 3 unpinned trials"})
    return 0


def read_history_values(spec):
    """-> list[float]. A JSON array, inline or in a file.

    The caller supplies the target run's own converged-tail values. This function
    does not go looking for them: which epochs count as converged is a judgement
    about that run (where its schedule changed, when augmentation closed), and a
    guess at it would silently widen or narrow every band built from it.
    """
    raw = spec
    if os.path.exists(os.path.expanduser(spec)):
        with open(os.path.expanduser(spec)) as fh:
            raw = fh.read()
    try:
        vals = json.loads(raw)
    except Exception as exc:
        broke(f"--from-history is neither a readable file nor valid JSON ({exc})",
              hint="pass a JSON array of the metric's values over the target's "
                   "converged tail, or a path to a file holding one")
    if not isinstance(vals, list) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        broke("--from-history must be a JSON array of numbers",
              hint="one value per epoch of the target's converged tail")
    return [float(v) for v in vals]


def cmd_band(a):
    project = os.path.expanduser(a.project)
    s, _ = load_session(project, a.session)
    base = [t for t in s["trials"] if not t["pinned"]]
    hist = read_history_values(a.from_history) if a.from_history else None
    thist = read_history_values(a.trial_history) if a.trial_history else None
    if thist is not None and hist is None:
        broke("--trial-history without --from-history",
              hint="the trial's tail is only used to make the comparison like for "
                   "like against the TARGET's tail; on its own it bands nothing")
    if thist is not None and len(thist) < MIN_HISTORY_FOR_BAND:
        refuse(f"{len(thist)} trial history value(s) -- needs at least "
               f"{MIN_HISTORY_FOR_BAND}",
               why="a handful of points is a range with extra steps, not a "
                   "distribution")

    # A trials band is strictly stronger, so it wins whenever it exists. History
    # supplied alongside it is kept as a cross-check, not discarded: a trials band
    # NARROWER than the target's own within-run wobble is a signal that something
    # about the repeats was pinned when it should not have been.
    source = "trials" if len(base) >= MIN_TRIALS_FOR_BAND else (
        "run_history" if hist is not None else None)

    if source is None:
        if hist is None:
            refuse(f"{len(base)} unpinned trial(s) and no history -- "
                   f"a band needs {MIN_TRIALS_FOR_BAND} trials or a converged tail",
                   why="two points are a range, not a band. Whether a delta is noise "
                       "is a property of this pipeline, and one repeat cannot show it",
                   your_two_options={
                       "run more trials": f"{MIN_TRIALS_FOR_BAND - len(base)} more "
                                          f"with nothing pinned -- run-to-run spread, "
                                          f"answers both ways",
                       "--from-history": "the target's own converged tail, if its "
                                         "per-epoch metric history survives. Free, but "
                                         "a LOWER BOUND: it can confirm, never refute"})
        refuse("no band could be built", why="unreachable")

    if source == "run_history":
        if len(hist) < MIN_HISTORY_FOR_BAND:
            refuse(f"{len(hist)} history value(s) -- a tail band needs at least "
                   f"{MIN_HISTORY_FOR_BAND}",
                   why="a handful of points is a range, not a distribution, and the "
                       "whole value of this band is that it describes a wobble",
                   fix="pass more of the target's converged tail, or run "
                       f"{MIN_TRIALS_FOR_BAND} trials instead")
        if not base:
            refuse("a history band has nothing to judge -- no trial has been registered",
                   why="the band says how big this pipeline's noise is; something has "
                       "to be measured against it. With `run_history` that something "
                       "is the fresh trial, not the target: the interval already came "
                       "from the target's own run",
                   fix="register the reproduction with `repro.py trial`, then re-band")

    if source == "trials":
        vals = sorted(t["value"] for t in base)
        n, tested, tested_what = len(base), s["target"]["value"], "target"
    else:
        vals = sorted(hist)
        n = len(hist)
        if thist is not None:
            # LIKE FOR LIKE. A best-checkpoint pick is the MAX of a tail, and
            # testing a max against the range of the draws it was drawn from is
            # comparing an order statistic to individuals -- it sits at or above
            # the top by construction, so `outside` carries no information. The
            # tail MEAN is a draw-comparable summary of where the pipeline sits,
            # and testing that against the target's own range is a question the
            # range can answer.
            #
            # This is not an escape hatch: it needs the trial's tail SUPPLIED,
            # both distributions are recorded side by side, and the one-directional
            # rule below is unchanged -- outside still cannot mean `diverged`.
            tested = sum(thist) / len(thist)
            tested_what = "trial_tail_mean"
        else:
            tested = sorted(t["value"] for t in base)[-1]
            tested_what = "trial"

    lo, hi = vals[0], vals[-1]
    spread = hi - lo
    tv = tested
    inside = lo <= tv <= hi
    nearest_edge_gap = 0.0 if inside else min(abs(tv - lo), abs(tv - hi))

    if inside:
        verdict = "reproduced"
        why = (f"the recorded {tv} lies inside the interval {n} repeats produced"
               if source == "trials" else
               f"the trial's {tested_what.replace('_', ' ')} {tv:.6g} lies inside the "
               f"target's own converged wobble [{lo:.6g}, {hi:.6g}]. A lower bound on "
               f"noise, so a delta inside it is noise-sized whatever the true "
               f"run-to-run spread is")
    elif source == "run_history":
        # The asymmetry, enforced. See BAND_SOURCES: this band cannot see kernel
        # selection, dataloader order or init, so outside it is not yet outside noise.
        verdict = "inconclusive"
        why = (f"the trial's {tv} sits {nearest_edge_gap:.6g} outside the target's "
               f"within-run tail [{lo:.6g}, {hi:.6g}]. That tail is a LOWER BOUND on "
               f"run-to-run spread -- same seed, same data order, same init -- so this "
               f"cannot say the delta is real. Only repeats can")
    elif spread > 0 and nearest_edge_gap <= spread:
        verdict = "inconclusive"
        why = (f"{tv} sits {nearest_edge_gap:.6g} outside [{lo:.6g}, {hi:.6g}], which "
               f"is within one measured spread ({spread:.6g}) of the edge -- more "
               f"repeats may widen the interval over it")
    else:
        verdict = "diverged"
        why = (f"{tv} sits {nearest_edge_gap:.6g} outside [{lo:.6g}, {hi:.6g}], "
               f"further than the measured spread ({spread:.6g})")

    band = {"source": source, "n": n, "min": lo, "max": hi,
            "mean": sum(vals) / len(vals), "spread": spread,
            "tested": tested_what, "tested_value": tv,
            "target": s["target"]["value"], "target_inside": inside,
            "nearest_edge_gap": nearest_edge_gap, "measured_at": now_utc(),
            "from_trials": [t["n"] for t in base],
            "lower_bound": source == "run_history",
            "can_refute": source == "trials"}
    if source == "run_history":
        band["history_what"] = a.history_what
    if thist is not None:
        import statistics as _st
        band["trial_history"] = {
            "n": len(thist), "min": min(thist), "max": max(thist),
            "mean": sum(thist) / len(thist),
            "stdev": _st.stdev(thist) if len(thist) > 1 else None,
            "what": a.trial_history_what,
        }
        band["both_readings"] = {
            "like_for_like": {"what": "trial tail mean vs the target's tail range",
                              "trial": band["trial_history"]["mean"],
                              "target_mean": sum(vals) / len(vals),
                              "delta": band["trial_history"]["mean"] - sum(vals) / len(vals)},
            "extreme_vs_extreme": {
                "what": "best-checkpoint pick vs best-checkpoint pick -- both are the "
                        "MAX of a 40-point tail, so this one is symmetric and fair, but "
                        "it is not what the range can adjudicate",
                "trial": max(thist), "target": s["target"]["value"],
                "delta": max(thist) - s["target"]["value"]},
        }

    extra_caveats = []

    # The selection effect, and it is one-directional. A best-checkpoint pick is the
    # MAX of a wobbling tail, so comparing a fresh converged run against it charges
    # the reproduction for the original's luckiest epoch -- every time, in the same
    # direction, with nothing anywhere raising. This is the one check that needs the
    # history even when the band came from trials, so it runs on any supplied tail.
    if hist is not None and len(hist) >= MIN_HISTORY_FOR_BAND:
        target_v = s["target"]["value"]
        h_lo, h_hi = min(hist), max(hist)
        h_mean = sum(hist) / len(hist)
        direction = s["target"].get("direction", "max")
        extreme = h_hi if direction == "max" else h_lo
        if abs(target_v - extreme) <= 1e-9:
            band["target_is_tail_extreme"] = True
            extra_caveats.append(
                f"the target metric {target_v:.6g} is the {'max' if direction == 'max' else 'min'} "
                f"of its own {len(hist)}-point converged tail (mean {h_mean:.6g}, range "
                f"[{h_lo:.6g}, {h_hi:.6g}]) -- a best-checkpoint pick, not a converged "
                f"value. A reproduction landing on the tail MEAN has matched this run; "
                f"judged against the recorded number it reads as short by "
                f"{abs(extreme - h_mean):.6g}, and the bias always runs that way")
        else:
            band["target_is_tail_extreme"] = False

    # Both available: keep the free one as a cross-check rather than dropping it.
    if source == "trials" and hist is not None and len(hist) >= MIN_HISTORY_FOR_BAND:
        h_spread = max(hist) - min(hist)
        band["also_measured"] = {"source": "run_history", "n": len(hist),
                                 "min": min(hist), "max": max(hist),
                                 "spread": h_spread, "what": a.history_what}
        if h_spread > spread:
            extra_caveats.append(
                f"the trials band ({spread:.6g}) is NARROWER than the target's own "
                f"within-run wobble ({h_spread:.6g}). Run-to-run spread cannot be "
                f"smaller than within-run spread, so something was held fixed across "
                f"the repeats that the original run did not hold fixed -- check what "
                f"the trials actually varied before trusting this interval")

    dec = s.get("declared_tolerance") or {}
    tol_abs = dec.get("absolute")
    tol_rel = dec.get("relative_pct")
    declared_half = None
    for cand in (tol_abs, (abs(tv) * tol_rel / 100.0 if tol_rel is not None else None)):
        if cand is not None:
            declared_half = cand if declared_half is None else max(declared_half, cand)
    tol_note = None
    if declared_half is not None and spread > 0:
        if declared_half > spread:
            tol_note = (f"the declared tolerance (+/-{declared_half:.6g}) is WIDER than "
                        f"the spread this pipeline actually produces ({spread:.6g}) -- it "
                        f"would have accepted a divergence {declared_half / spread:.1f}x "
                        f"larger than real noise")
        else:
            tol_note = (f"the declared tolerance (+/-{declared_half:.6g}) is tighter than "
                        f"the measured spread ({spread:.6g}) -- judging on it would call "
                        f"this pipeline's own noise a divergence")

    s["band"] = band
    s["metric_verdict"] = verdict
    for c in ([tol_note] if tol_note else []) + extra_caveats:
        if c not in s["caveats"]:
            s["caveats"].append(c)
    save_session(project, a.session, s)

    out = {"session_id": a.session, "band": band, "metric_verdict": verdict,
           "why": why, "declared_tolerance_note": tol_note,
           "caveats_added": extra_caveats}
    if verdict == "inconclusive" and source == "run_history":
        out["next"] = (f"this band cannot refute. To settle it, run "
                       f"{MIN_TRIALS_FOR_BAND - len(base)} more unpinned trial(s) for a "
                       f"run-to-run band -- and that is the ONLY case where the extra "
                       f"trials are worth their price")
    elif verdict == "inconclusive":
        out["next"] = (f"run {len(base)} more unpinned trial(s) and re-band -- the "
                       f"interval is not yet wide enough to answer either way")
    elif verdict == "diverged":
        out["next"] = "`repro.py attribute` for which axis to pin first"
    else:
        out["next"] = ("`repro.py close` -- but the verdict downgrades if any axis "
                       "drifted or the probe predictions disagree")
    emit(out)
    return 0


def cmd_attribute(a):
    """Bookkeeping, deliberately not judgement. Which axes drifted, which have
    been pinned, what each pin did to the delta, and what is cheapest next."""
    project = os.path.expanduser(a.project)
    s, _ = load_session(project, a.session)
    band = s.get("band")
    if not band:
        emit({"session_id": a.session, "attributed_to": None,
              "why": "no band measured yet -- there is nothing to attribute",
              "next": "`repro.py band` (needs 3 unpinned trials)"})
        return 0

    lo, hi = band["min"], band["max"]
    suspect = [ax for ax, v in (s.get("axes_at_open") or {}).items()
               if v in ("drifted", "unverifiable")]
    pinned_runs = {}
    for t in s["trials"]:
        for ax in t["pinned"]:
            pinned_runs.setdefault(ax, []).append(t)

    resolved = []
    for ax, trials in sorted(pinned_runs.items()):
        landed = [t for t in trials if lo <= t["value"] <= hi
                  or abs(t["value"] - s["target"]["value"]) <= band["spread"]]
        resolved.append({"axis": ax, "trials": [t["n"] for t in trials],
                         "moved_delta_into_range": bool(landed),
                         "values": [t["value"] for t in trials]})

    implicated = [r["axis"] for r in resolved if r["moved_delta_into_range"]]
    remaining = [ax for ax in PIN_COST if ax in suspect and ax not in pinned_runs]

    out = {"session_id": a.session,
           "metric_verdict": s.get("metric_verdict"),
           "suspect_axes": suspect,
           "pins_tried": resolved,
           "implicated": implicated,
           "unpinned_suspects_by_cost": remaining}

    if implicated:
        out["attributed_to"] = implicated[0]
        out["why"] = (f"pinning {implicated[0]} brought the number back into the "
                      f"measured range; the other axes did not")
        out["next"] = f"`repro.py close --verdict diverged --attributed-to {implicated[0]}`"
    elif remaining:
        out["attributed_to"] = None
        out["next"] = (f"pin {remaining[0]} next (cheapest unpinned suspect), rerun, "
                       f"and register it with --pinned {remaining[0]}")
        out["why"] = f"{len(remaining)} suspect axis/axes never tested"
    else:
        out["attributed_to"] = None
        out["why"] = ("every drifted or unverifiable axis has been pinned and the "
                      "number still sits outside the band. No axis MLClaw records "
                      "explains it -- which is itself the finding: either the "
                      "nondeterminism is wider than these repeats measured, or "
                      "something nobody recorded changed")
        out["next"] = "`repro.py close --verdict diverged_unattributed`"

    if not suspect and s.get("metric_verdict") == "diverged":
        out["note"] = ("no axis drifted, yet the number is outside the band. Pinning "
                       "an intact axis is a no-op, so do not spend a run on it -- "
                       "this points at nondeterminism the repeats under-measured, or "
                       "at an axis nothing here records (data order, host, clock)")
    emit(out)
    return 0


def cmd_close(a):
    project = os.path.expanduser(a.project)
    s, _ = load_session(project, a.session)
    if s["status"] in TERMINAL_SESSION:
        refuse(f"session already {s['status']}", closed_at=s.get("closed_at"))
    if a.verdict not in FINAL_VERDICTS:
        broke(f"unknown verdict {a.verdict!r}", allowed=list(FINAL_VERDICTS))

    axes = s.get("axes_at_open") or {}
    drifted = sorted(k for k, v in axes.items() if v == "drifted")
    unverifiable = sorted(k for k, v in axes.items() if v == "unverifiable")
    band = s.get("band")

    if a.verdict != "not_reproducible" and not band:
        refuse("no band was measured",
               why="a verdict on a delta requires knowing this pipeline's own "
                   "noise. Judging against a declared tolerance is the guess this "
                   "whole loop exists to replace",
               fix="3 unpinned trials, then `repro.py band`")

    # Both of these assert "the number came back", so both carry the full
    # evidence bar. Hanging the probe check on `reproduced` alone let
    # `reproduced_with_drift` close with a declared probe that was never run --
    # a verdict claiming reproduction while the stronger check went unperformed.
    # Which FAMILY of verdict this session is entitled to, before any of the
    # evidence checks below. A training run measured through eval has had its
    # artifact re-measured and its procedure not re-run, and the two must not
    # share a word: skill-graph.md makes a closed `reproduced*` the only thing
    # that promotes an inherited checkpoint's `origin.confidence`, so letting the
    # weaker fact wear the stronger word sells the promotion at the wrong price.
    remeasure_only = is_remeasure_only(s)
    if remeasure_only and a.verdict in ("reproduced", "reproduced_with_drift"):
        refuse(f"this session re-measured an artifact; it did not reproduce a "
               f"procedure, so it cannot close as {a.verdict!r}",
               target=(s.get("target") or {}).get("run"),
               measure_via=s.get("measure_via"),
               why="the target is a training run and measure_via is `eval`, so "
                   "nothing about the training was re-run. A wrong hyperparameter, "
                   "a wrong dataset, or a recipe that would no longer produce this "
                   "model are all invisible to this session — the artifact was a "
                   "given and only its number was checked",
               fix=f"close as {a.verdict.replace('reproduced', 'remeasured')!r}, "
                   f"or re-open with measure_via=retrain "
                   f"--i-accept-the-cost to reproduce the training itself")
    if not remeasure_only and a.verdict in REMEASURE_ONLY:
        refuse(f"{a.verdict!r} understates this session",
               why="the procedure WAS re-run, so the stronger word is the accurate "
                   "one and recording the weaker one loses a fact nobody can "
                   "recover later",
               fix=f"close as {a.verdict.replace('remeasured', 'reproduced')!r}")

    if a.verdict in ASSERTS_THE_NUMBER_CAME_BACK:
        if s.get("metric_verdict") != "reproduced":
            refuse(f"metric verdict is {s.get('metric_verdict')!r}",
                   fix="band says the recorded value is not inside the measured "
                       "interval; this cannot be closed as a reproduction")
        if s.get("predictions_agree") is False:
            refuse("the probe predictions disagree",
                   fix="verdict metric_ok_predictions_diverged -- same average from "
                       "different outputs is the failure this probe exists to catch")
        if s.get("probe") and s.get("predictions_agree") is None:
            refuse("a probe set was declared but no trial reported "
                   "predictions_agree",
                   probe=s.get("probe"),
                   why="the probe is the stronger of the two checks; closing as a "
                       "reproduction without it reports the weaker one as if both "
                       "had passed",
                   fix="run the probe through /infer-run against both artifacts and "
                       "register it with `trial --predictions-agree` / "
                       "`--predictions-differ`")

    # The drift downgrade applies to whichever family this session is in: the
    # weaker word and the drift caveat are independent axes, and a `remeasured`
    # over a drifted env is exactly as much a weaker fact as a `reproduced` is.
    clean_word = "remeasured" if remeasure_only else "reproduced"
    if a.verdict == clean_word:
        drift_word = f"{clean_word}_with_drift"
        if drifted:
            refuse(f"cannot call this {clean_word}: {', '.join(drifted)} drifted",
                   fix=f"verdict {drift_word} -- the number came back, but "
                       "not from the same conditions, and that is a weaker fact "
                       "that has to keep saying so")
        if unverifiable:
            refuse(f"cannot call this {clean_word}: {', '.join(unverifiable)} is "
                   f"unverifiable",
                   why="an axis nobody could check is not an axis that matched. "
                       "code.reproducible false in particular means the rebuilt "
                       "tree is not the tree that ran, so a matching number is "
                       "evidence and not proof",
                   fix=f"verdict {drift_word}, and the caveat travels with it")

    if a.verdict == "diverged" and not (a.attributed_to or s.get("attributed_to")):
        refuse("verdict diverged needs --attributed-to <axis>",
               allowed=list(AXES),
               fix="if no axis explains it, that is diverged_unattributed and it is "
                   "a real conclusion, not a failure to finish")
    if a.attributed_to and a.attributed_to not in AXES:
        broke(f"unknown axis {a.attributed_to!r}", allowed=list(AXES))

    s["status"] = "closed"
    s["closed_at"] = now_utc()
    s["verdict"] = a.verdict
    if a.attributed_to:
        s["attributed_to"] = a.attributed_to
    if a.note:
        s["caveats"].append(a.note)
    for ax in drifted:
        note = f"{ax} drifted between the original run and every trial here"
        if note not in s["caveats"]:
            s["caveats"].append(note)
    for ax in unverifiable:
        note = f"{ax} could not be verified, so it is not known to have matched"
        if note not in s["caveats"]:
            s["caveats"].append(note)
    save_session(project, a.session, s)

    emit({"session_id": a.session, "closed": True, "verdict": a.verdict,
          "attributed_to": s.get("attributed_to"), "band": band,
          "trials": len(s["trials"]), "caveats": s["caveats"],
          "target": s["target"]})
    return 0


def cmd_status(a):
    project = os.path.expanduser(a.project)
    root = os.path.join(project, "repro")
    if not os.path.isdir(root):
        emit({"project": project, "sessions": [], "note": "no repro/ in this project"})
        return 0
    rows = []
    for sid in sorted(os.listdir(root), reverse=True):
        s = read_json(os.path.join(root, sid, "session.json"), required=False)
        if not s:
            continue
        if a.open_only and s.get("status") in TERMINAL_SESSION:
            continue
        rows.append({"session_id": sid, "status": s.get("status"),
                     "target": (s.get("target") or {}).get("run"),
                     "metric": (s.get("target") or {}).get("metric"),
                     "value": (s.get("target") or {}).get("value"),
                     "measure_via": s.get("measure_via"),
                     "trials": len(s.get("trials") or []),
                     "band": s.get("band") is not None,
                     "metric_verdict": s.get("metric_verdict"),
                     "verdict": s.get("verdict"),
                     "attributed_to": s.get("attributed_to"),
                     "opened_at": s.get("opened_at")})
    emit({"project": project, "sessions": rows})
    return 0


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="five-axis audit of one run; records only")
    c.add_argument("--project", required=True)
    c.add_argument("--run", required=True, help="<stage>/<run_id>")
    c.add_argument("--no-env", action="store_true",
                   help="skip the env probe (it shells out to pip)")
    c.add_argument("--no-write", action="store_true",
                   help="do not persist the dated observation")
    c.add_argument("--json", action="store_true")
    c.add_argument("--framework-python", dest="framework_python", default=None,
                   help="interpreter of the RUN environment, for a `framework` code "
                        "source. Closes the one question a version pin cannot "
                        "answer -- whether the installed package was EDITED after "
                        "install -- by hashing its files against the RECORD pip "
                        "wrote. Offline. Omitting it leaves the code axis "
                        "`unverifiable`, never `intact`: an unchecked question is "
                        "not a clean one")
    c.set_defaults(fn=cmd_check)

    o = sub.add_parser("open", help="open a repro session; declares the target")
    o.add_argument("--project", required=True)
    o.add_argument("--run", required=True, help="<stage>/<run_id> to reproduce")
    o.add_argument("--name", default=None, help="slug appended to the session id")
    o.add_argument("--measure-via", choices=("eval", "retrain"), default="eval",
                   help="how the number gets re-measured. eval re-runs the "
                        "surviving checkpoint; retrain re-runs training")
    o.add_argument("--i-accept-the-cost", action="store_true",
                   help="required for --measure-via retrain")
    o.add_argument("--remeasure-only", action="store_true",
                   help="required for --measure-via eval against a TRAINING target: "
                        "acknowledges that the training is not being re-run, so the "
                        "verdict ceiling is `remeasured_with_drift` and not any kind "
                        "of reproduction")
    o.add_argument("--direction", choices=("max", "min"), default="max")
    o.add_argument("--probe", default=None,
                   help="fixed probe inputs for the prediction check, ideally "
                        "datasets/<id>@<snapshot>. Declared now, never later")
    o.add_argument("--tolerance-pct", type=float, default=0.5,
                   help="declared expectation only; the band decides the verdict")
    o.add_argument("--tolerance-abs", type=float, default=None)
    o.add_argument("--band-trials", type=int, default=None,
                   help="how many unpinned repeats to plan for. Default follows what "
                        "one costs: %s. A retrain trial costs the original run, so "
                        "three of them is three times the bill for a case that may "
                        "not occur -- pay for the second and third only if the first "
                        "lands ambiguous. See `band --from-history`"
                        % ", ".join(f"{k}={v}" for k, v in DEFAULT_BAND_TRIALS.items()))
    o.set_defaults(fn=cmd_open)

    t = sub.add_parser("trial", help="register a completed run as a trial")
    t.add_argument("--project", required=True)
    t.add_argument("--session", required=True)
    t.add_argument("--run", required=True, help="<stage>/<run_id> of the trial")
    t.add_argument("--pinned", action="append", default=[],
                   help="repeatable; axes pinned back for this trial. Omit for a "
                        "band trial")
    t.add_argument("--value", type=float, default=None,
                   help="metric value, when the trial's primary_metric differs "
                        "from the target's")
    t.add_argument("--probe-run", default=None,
                   help="<stage>/<run_id> of the /infer-run over the probe set")
    t.add_argument("--scope-differs-immaterially", dest="scope_differs_immaterially",
                   default=None, metavar="WHY",
                   help="record a scope difference as looked-at and judged too small "
                        "to move the metric, and proceed. Stored on the trial and "
                        "surfaced in the verdict's caveats. Without it a differing "
                        "scope is refused, because the DEFAULT must never quietly "
                        "compare two different quantities")
    grp = t.add_mutually_exclusive_group()
    grp.add_argument("--predictions-agree", dest="predictions_agree",
                     action="store_true", default=None)
    grp.add_argument("--predictions-differ", dest="predictions_agree",
                     action="store_false")
    t.set_defaults(fn=cmd_trial)

    b = sub.add_parser("band", help="measure the noise band from unpinned trials")
    b.add_argument("--project", required=True)
    b.add_argument("--session", required=True)
    b.add_argument("--from-history", default=None,
                   help="JSON array (inline or a file path) of the target metric's "
                        "values over the target run's own CONVERGED tail. Builds a "
                        "band without repeats -- free, but a lower bound on run-to-run "
                        "spread, so it can confirm and never refute. Also enables the "
                        "best-checkpoint selection check")
    b.add_argument("--trial-history", dest="trial_history", default=None,
                   help="JSON array (inline or a file) of the TRIAL's own converged "
                        "tail. Supply it whenever the target metric is a "
                        "best-checkpoint pick: the trial's pick is then also a max, "
                        "and testing a max against the range it was drawn from is "
                        "comparing an order statistic to individuals. With this, the "
                        "trial's tail MEAN is tested instead and both distributions "
                        "are recorded side by side")
    b.add_argument("--trial-history-what", dest="trial_history_what", default=None,
                   help="what the trial's tail values are, same standard as "
                        "--history-what")
    b.add_argument("--history-what", default=None,
                   help="what those values are, e.g. 'epochs 101-140 of the target's "
                        "train_results; mosaic closed at 100'. Recorded with the band: "
                        "which epochs count as converged is a judgement, and a band "
                        "whose window nobody wrote down cannot be checked later")
    b.set_defaults(fn=cmd_band)

    at = sub.add_parser("attribute", help="which axis is implicated, what to pin next")
    at.add_argument("--project", required=True)
    at.add_argument("--session", required=True)
    at.set_defaults(fn=cmd_attribute)

    cl = sub.add_parser("close", help="write the conclusion")
    cl.add_argument("--project", required=True)
    cl.add_argument("--session", required=True)
    cl.add_argument("--verdict", required=True, choices=list(FINAL_VERDICTS))
    cl.add_argument("--attributed-to", default=None, choices=list(AXES))
    cl.add_argument("--note", default=None)
    cl.set_defaults(fn=cmd_close)

    st = sub.add_parser("status", help="repro sessions in this project")
    st.add_argument("--project", required=True)
    st.add_argument("--open-only", action="store_true")
    st.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
