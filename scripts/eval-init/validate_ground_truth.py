#!/usr/bin/env python3
"""Validate the ground-truth configuration of an evaluation stage.

Called from /eval-init Step 5. Everything checked here is a failure that
produces *plausible numbers* rather than an error: annotations that aren't
where they're declared, a class count that disagrees with the dataset block, or
eval preprocessing that no longer matches the training stage (CLAUDE.md,
"Preprocessing contract (cross-stage)"). None of those raise anything at run
time — evaluation completes and reports a metric measured on something other
than what the config says.

`${}` references are **not** checked here — that is `infer-init/validate_refs.py`,
which knows all four files and folds `ground_truth.items` into the `input`
namespace. Two validators answering the same question is how /eval-init ended
up with one of them declaring every correct eval config broken.

What it checks
--------------
1. ground_truth items    — `type` present and in the item vocabulary; `pairing`
                           set and one of single_file|directory|embedded|index.
2. ground_truth sources  — key matches an item; `source` in the source
                           vocabulary; a `local` path resolves on disk; the
                           path's kind matches `pairing` (file vs directory);
                           the file extension matches the declared `format`.
3. dataset agreement     — `config.json -> dataset.classes` vs the categories
                           actually in a resolvable COCO-style annotation file;
                           `dataset.num_samples` vs its image count.
4. preprocessing         — `normalization` / `input_layout` / `label_transform`
                           must equal the training stage's; `augmentation` must
                           be empty here (training-only).

Output: JSON on stdout — `errors` / `warnings` / `info`, each finding carrying
the `file` and `key` that produced it.
Exit:   0 = no errors (warnings may exist)
        1 = at least one error — the script worked and the answer is no;
            /eval-init must not save until they're fixed
        2 = could not run (stage_dir or input.json missing/unreadable) — fall
            back per CLAUDE.md "Script Integration"

Usage:
    python validate_ground_truth.py <stage_dir>
        [--training-input <path>]   # default: <stage_dir>/../training/input.json
        [--project-root <path>]     # for resolving relative source paths
        [--allow-tta]               # downgrade non-empty augmentation to a warning
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
from compare import norm_scalar  # noqa: E402

ITEM_TYPES = {"video", "image", "text", "tabular", "json", "binary",
              "model", "checkpoint", "config", "log"}
SOURCE_KINDS = {"local", "s3", "server", "stage_output", "registry"}
PAIRING_MODES = {"single_file", "directory", "embedded", "index"}

# Don't parse an annotation file bigger than this just to count categories.
MAX_ANNOTATION_MB = 200


class ValidationError(Exception):
    """The script cannot run at all."""


class Findings:
    """Findings carry the file + key that produced them, per /eval-init Step 5."""

    def __init__(self):
        self.errors = []
        self.warnings = []
        self.info = []

    def error(self, file, key, message):
        self.errors.append({"file": file, "key": key, "message": message})

    def warn(self, file, key, message):
        self.warnings.append({"file": file, "key": key, "message": message})

    def note(self, file, key, message):
        self.info.append({"file": file, "key": key, "message": message})


def load_json_lenient(path, required=False):
    """Absent OR unreadable -> None (unless `required`). The lenient one of three:
    see `validate_refs.load_json_absent_ok` and `compare_baseline.load_json_required`,
    which differ on exactly the unreadable case. The name carries the difference
    because a bare `load_json` bound to three contracts is how a caller picks wrong."""
    if not os.path.isfile(path):
        if required:
            raise ValidationError("missing %s" % path)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        if required:
            raise ValidationError("cannot read %s: %s" % (path, e))
        return None


def resolve_path(path, project_root, stage_dir):
    """Expand `~` and make a relative source path absolute against the project."""
    p = os.path.expanduser(str(path))
    if os.path.isabs(p):
        return os.path.normpath(p)
    base = project_root or stage_dir
    return os.path.normpath(os.path.join(base, p))


def preproc_equal(a, b, tol=1e-9):
    """Compare two preprocessing values. Leaves go through the shared
    `norm_scalar`; the structural walk stays here because it must be
    **positional**.

    `shared/compare.py -> values_equal` order-normalizes all-scalar lists, which
    is right for a run's `scope` (`samples: [3,1,2]` is the same three samples)
    and wrong for everything in this block: `mean: [0.485, 0.456, 0.406]` vs
    `[0.406, 0.456, 0.485]` is the RGB/BGR swap CLAUDE.md names as a canonical
    silent-degradation case, and `size: [640, 480]` vs `[480, 640]` is an H×W/W×H
    swap. Sorting either one makes the mismatch compare equal. Different
    question, not a different answer to the same question.
    """
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(preproc_equal(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(preproc_equal(a[k], b[k], tol) for k in a)
    na, nb = norm_scalar(a), norm_scalar(b)
    if isinstance(na, float) and isinstance(nb, float):
        return abs(na - nb) <= tol
    return na == nb


def is_empty(v):
    return v is None or v == "" or v == [] or v == {}


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_items(gt_items, pairing, fnd):
    if not gt_items:
        fnd.warn("input.json", "ground_truth.items",
                 "no ground truth items declared — an evaluation stage without GT "
                 "cannot compute a metric against a reference. Intentional only if "
                 "the metric is reference-free (e.g. FID against a stats file).")
        return
    if is_empty(pairing):
        fnd.error("input.json", "pairing",
                  "ground_truth.items is non-empty but `pairing` is empty — "
                  "/eval-run has no way to associate an input with its annotation. "
                  "Set one of: %s." % ", ".join(sorted(PAIRING_MODES)))
    elif pairing not in PAIRING_MODES:
        fnd.error("input.json", "pairing",
                  "unknown pairing mode %r — allowed: %s."
                  % (pairing, ", ".join(sorted(PAIRING_MODES))))

    for name, item in sorted(gt_items.items()):
        key = "ground_truth.items.%s" % name
        if not isinstance(item, dict):
            fnd.error("input.json", key, "item must be an object, got %s" % type(item).__name__)
            continue
        itype = item.get("type")
        if is_empty(itype):
            fnd.error("input.json", key + ".type",
                      "missing `type` — required by the item schema (one of: %s)."
                      % ", ".join(sorted(ITEM_TYPES)))
        elif itype not in ITEM_TYPES:
            fnd.warn("input.json", key + ".type",
                     "unrecognized type %r — expected one of: %s."
                     % (itype, ", ".join(sorted(ITEM_TYPES))))
        if is_empty(item.get("format")) and pairing != "embedded":
            fnd.warn("input.json", key + ".format",
                     "no `format` declared — /eval-run cannot check that the file it "
                     "is handed is the annotation format the code parses.")


def check_sources(gt_items, gt_sources, pairing, project_root, stage_dir, fnd):
    """Validate every GT source. Returns COCO-style facts for the dataset check."""
    facts = {}
    for name in sorted(set(gt_sources) - set(gt_items)):
        fnd.error("input.json", "ground_truth.sources.%s" % name,
                  "source declared for %r but there is no matching entry in "
                  "ground_truth.items — nothing will ever read it." % name)

    for name in sorted(gt_items):
        key = "ground_truth.sources.%s" % name
        src = gt_sources.get(name)
        if src is None:
            fnd.note("input.json", key,
                     "no source entry yet — normal at init; /eval-run fills the "
                     "concrete path.")
            continue
        if not isinstance(src, dict):
            fnd.error("input.json", key, "source must be an object, got %s" % type(src).__name__)
            continue

        kind = src.get("source")
        path = src.get("path")
        if is_empty(kind) and is_empty(path):
            fnd.note("input.json", key, "source not filled yet — /eval-run resolves it.")
            continue
        if is_empty(kind):
            fnd.error("input.json", key + ".source",
                      "path is set (%r) but `source` is empty — /eval-run cannot tell "
                      "whether to read it locally, over SSH, or from S3." % path)
        elif kind not in SOURCE_KINDS:
            fnd.error("input.json", key + ".source",
                      "unknown source kind %r — allowed: %s."
                      % (kind, ", ".join(sorted(SOURCE_KINDS))))
        if kind in ("s3", "server", "registry") and is_empty(src.get("credentials")):
            fnd.warn("input.json", key + ".credentials",
                     "source is %r but no credentials key is set — /eval-run will have "
                     "to fall back to /resources at run time." % kind)

        if is_empty(path):
            fnd.note("input.json", key + ".path", "path not filled yet.")
            continue
        if "${" in str(path):
            fnd.note("input.json", key + ".path",
                     "path contains a ${} reference — resolved at run time; whether it "
                     "points at a declared item is validate_refs.py's job.")
            continue
        if kind != "local":
            fnd.note("input.json", key + ".path",
                     "source is %r — existence not verifiable from this machine." % kind)
            continue

        resolved = resolve_path(path, project_root, stage_dir)
        if not os.path.exists(resolved):
            fnd.error("input.json", key + ".path",
                      "ground truth declared but unresolvable on disk: %s. Evaluation "
                      "cannot score against annotations that are not there." % resolved)
            continue

        _check_path_shape(name, resolved, gt_items.get(name) or {}, pairing, fnd)
        _check_annotation_file(name, resolved, fnd, facts)

    return facts


def _check_path_shape(name, resolved, item, pairing, fnd):
    key = "ground_truth.sources.%s.path" % name
    if pairing == "single_file" and os.path.isdir(resolved):
        fnd.error("input.json", key,
                  "pairing is 'single_file' but %s is a directory — one of the two is "
                  "wrong, and /eval-run would hand the code a path it cannot open." % resolved)
    if pairing == "directory" and os.path.isfile(resolved):
        fnd.error("input.json", key,
                  "pairing is 'directory' but %s is a file — per-input annotations were "
                  "expected in a parallel tree." % resolved)

    fmt = item.get("format")
    if not is_empty(fmt) and os.path.isfile(resolved):
        actual = os.path.splitext(resolved)[1]
        declared = str(fmt) if str(fmt).startswith(".") else "." + str(fmt)
        if actual and actual.casefold() != declared.casefold():
            fnd.error("input.json", "ground_truth.items.%s.format" % name,
                      "declared format %s but the resolved path is %s (%s). The parser "
                      "chosen from `format` would not match the file."
                      % (declared, actual, resolved))


def _check_annotation_file(name, resolved, fnd, facts):
    """Collect COCO-style facts for the dataset cross-check; report read problems.

    Only `categories` and the image *count* survive; the parsed `data` is dropped
    at the end of this function. The bulk of a COCO file is `annotations`, which
    nothing downstream reads — a 9 MB file retains ~65 MB as live Python objects,
    and MAX_ANNOTATION_MB is 200, so holding one per GT source until the run ends
    costs over a gigabyte to answer two questions.
    """
    if not os.path.isfile(resolved) or os.path.splitext(resolved)[1].casefold() != ".json":
        return
    try:
        size_mb = os.path.getsize(resolved) / (1024 * 1024)
    except OSError:
        return
    if size_mb > MAX_ANNOTATION_MB:
        fnd.note("input.json", "ground_truth.sources.%s.path" % name,
                 "annotation file is %.0f MB — skipped the category/image cross-check "
                 "to keep init fast." % size_mb)
        return
    try:
        with open(resolved, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        fnd.error("input.json", "ground_truth.sources.%s.path" % name,
                  "annotation file exists but does not parse as JSON: %s" % e)
        return
    if isinstance(data, dict):
        imgs = data.get("images")
        facts[name] = {"path": resolved,
                       "categories": data.get("categories"),
                       "num_images": len(imgs) if isinstance(imgs, list) else None}


def check_dataset_agreement(dataset, gt_items, label_transform, facts, fnd):
    if is_empty(dataset.get("name")):
        fnd.warn("config.json", "dataset.name",
                 "empty — runs become hard to compare later, and /eval-run needs it to "
                 "decide whether a baseline was measured on the same data.")
    classes = dataset.get("classes") or []
    if gt_items and not classes:
        fnd.warn("config.json", "dataset.classes",
                 "no class list recorded while ground truth is declared — per-class "
                 "metrics and any class-count check are unavailable.")

    for name, fact in sorted((facts or {}).items()):
        cats = fact["categories"]
        num_images = fact["num_images"]
        if not isinstance(cats, list) and num_images is None:
            continue  # not a COCO-style file; nothing to cross-check

        if isinstance(cats, list) and classes:
            if len(cats) != len(classes):
                fnd.error(
                    "config.json", "dataset.classes",
                    "declares %d classes but the ground truth file %s contains %d "
                    "categories. Per-class metrics would be misaligned and any "
                    "class-indexed report mislabeled."
                    % (len(classes), fact["path"], len(cats)))
            else:
                gt_names = [c.get("name") for c in cats if isinstance(c, dict)]
                if gt_names and all(isinstance(c, str) for c in classes):
                    if [str(c).casefold() for c in classes] != [str(c).casefold() for c in gt_names]:
                        fnd.warn("config.json", "dataset.classes",
                                 "same count as %s but a different order/spelling — "
                                 "class index N in the report may not be class N in the "
                                 "annotations." % fact["path"])

        if isinstance(cats, list) and cats:
            ids = [c.get("id") for c in cats if isinstance(c, dict) and isinstance(c.get("id"), int)]
            if ids:
                contiguous = sorted(ids) == list(range(min(ids), min(ids) + len(ids)))
                if not contiguous and is_empty((label_transform or {}).get("class_mapping")):
                    fnd.warn(
                        "input.json", "preprocessing.label_transform.class_mapping",
                        "%s has non-contiguous category ids (min %d, max %d, %d "
                        "categories) but no class_mapping is recorded — the COCO "
                        "91-vs-80 remap is the classic silent-degradation case."
                        % (fact["path"], min(ids), max(ids), len(ids)))

        num_samples = dataset.get("num_samples")
        if num_images is not None and isinstance(num_samples, int) and num_samples != num_images:
            fnd.warn("config.json", "dataset.num_samples",
                     "says %d but %s lists %d images. If this is a deliberate subset, "
                     "record that in the run's `scope`; otherwise one of the two is stale."
                     % (num_samples, fact["path"], num_images))


PREPROC_BLOCKS = {
    "normalization": ("mean", "std"),
    "input_layout": ("size", "resize_mode", "channel_order"),
    "label_transform": ("index_base", "class_mapping", "background_class"),
}


MATCH_VALUES = {"ok", "mismatch", "absent", "pending", "unreachable"}


def check_candidates(which, cands, dataset, project_root, fnd):
    """Gate every candidate somebody marked usable.

    In evaluation a candidate is not merely a copy of the data — it decides what
    the metric MEANS. Three ways an `ok` here produces a number that answers a
    different question than the one being asked, and none of them errors at
    runtime:

      a sample count that differs from `dataset.num_samples`
          mAP over 500 images and mAP over 5000 are both real numbers with the
          same name. Diffed against a baseline measured on the other one, the
          delta is sampling noise. /eval-init Step 4 already refuses an
          unqualified baseline for this reason; this is the same rule early
          enough to prevent rather than caveat.
      a checkpoint from a debug run
          it carries a debug run's data scope, so its number is comparable to
          nothing.
      a `pending` handoff that already closed
          either an `ok` nobody promoted or a fiction, and both send /eval-run
          to wait for work that already came back.
    """
    declared = dataset.get("num_samples")
    for name, entries in (cands or {}).items():
        if not isinstance(entries, list):
            fnd.error(which, "candidates.items.%s" % name,
                      "must be a list of candidate entries")
            continue
        for i, c in enumerate(entries):
            key = "candidates.items.%s[%d]" % (name, i)
            match = c.get("match")
            loc = c.get("location") or ""
            if match not in MATCH_VALUES:
                fnd.error(which, key, "match=%r is not one of %s"
                          % (match, ", ".join(sorted(MATCH_VALUES))))
                continue

            if match == "ok":
                n = c.get("samples")
                if not isinstance(declared, int):
                    fnd.warn(which, key,
                             "cannot check the sample count: config.json -> "
                             "dataset.num_samples is not set, so nothing pins what "
                             "this metric is measured over")
                elif not isinstance(n, int):
                    fnd.error(which, key,
                              "marked ok with no `samples` count. In evaluation a "
                              "subset is not a smaller copy of the data, it is a "
                              "different measurement — record the count so it can "
                              "be compared against dataset.num_samples=%d" % declared)
                elif n != declared:
                    fnd.error(which, key,
                              "marked ok but holds %d samples while "
                              "dataset.num_samples=%d. This is `mismatch`, not `ok`: "
                              "the run would complete and report a real number "
                              "measured over a different set than the baseline it "
                              "gets diffed against" % (n, declared))

            if loc.startswith("run:") and match == "ok":
                ref = loc.split(":", 1)[1]
                stage, _, run_id = ref.partition("/")
                rpath = os.path.join(project_root, "stages", stage, "runs",
                                     run_id, "run.json")
                run = load_json_lenient(rpath)
                if run is None:
                    fnd.error(which, key,
                              "names run %s, whose run.json is not at %s" % (ref, rpath))
                elif run.get("mode") != "production":
                    fnd.error(which, key,
                              "names run %s with mode=%r. Only a production run's "
                              "checkpoint carries a comparable data scope; a debug "
                              "run's number is comparable to nothing"
                              % (ref, run.get("mode")))

            if match == "pending":
                if not loc.startswith("handoff:"):
                    fnd.error(which, key,
                              "match=pending but location=%r — pending means the "
                              "asset resolves by somebody else finishing, which "
                              "only handoff: can express" % loc)
                    continue
                hid = loc.split(":", 1)[1]
                hpath = os.path.join(project_root, "handoffs", hid, "handoff.json")
                h = load_json_lenient(hpath)
                if h is None:
                    fnd.error(which, key,
                              "names handoff %s, which does not exist at %s" % (hid, hpath))
                elif h.get("status") in ("accepted", "rejected", "cancelled"):
                    fnd.error(which, key,
                              "names handoff %s, which already closed as %r. A "
                              "pending candidate pointing at a closed handoff is "
                              "either an ok nobody promoted or a fiction, and both "
                              "make /eval-run wait for work that came back"
                              % (hid, h.get("status")))


def check_preprocessing(eval_preproc, train_preproc, train_path, allow_tta, fnd):
    """references/run-mechanics.md 'Preprocessing contract (cross-stage)'.

    The three shared blocks must be identical to training's; a difference means
    evaluation is not measuring the model that was trained, and nothing errors.
    `augmentation` is training-only.
    """
    aug = (eval_preproc or {}).get("augmentation")
    if not is_empty(aug):
        msg = ("augmentation is non-empty (%s) in an evaluation stage. Per CLAUDE.md "
               "this is training-only — a bug, not a variation. If this is deliberate "
               "test-time augmentation, say so explicitly and re-run with --allow-tta."
               % json.dumps(aug)[:200])
        (fnd.warn if allow_tta else fnd.error)("input.json", "preprocessing.augmentation", msg)

    if train_preproc is None:
        fnd.note("input.json", "preprocessing",
                 "no training stage input.json at %s — the cross-stage preprocessing "
                 "contract could not be verified." % train_path)
        return

    for block, fields in PREPROC_BLOCKS.items():
        ev = (eval_preproc or {}).get(block) or {}
        tr = train_preproc.get(block) or {}
        for field in fields:
            key = "preprocessing.%s.%s" % (block, field)
            e_val, t_val = ev.get(field), tr.get(field)
            if is_empty(e_val) and is_empty(t_val):
                continue
            if is_empty(e_val):
                fnd.warn("input.json", key,
                         "training declares %s but evaluation leaves it blank — cannot "
                         "verify the contract. Read it out of the eval code."
                         % json.dumps(t_val))
                continue
            if is_empty(t_val):
                fnd.warn("input.json", key,
                         "evaluation declares %s but the training stage leaves it blank "
                         "— cannot verify the contract." % json.dumps(e_val))
                continue
            if not preproc_equal(e_val, t_val):
                fnd.error("input.json", key,
                          "MISMATCH vs training: eval=%s, training=%s (%s). "
                          "Preprocessing that differs from training makes this "
                          "evaluation measure a different model than the one trained — "
                          "the metric is wrong and nothing will raise."
                          % (json.dumps(e_val), json.dumps(t_val), train_path))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def validate(stage_dir, training_input=None, project_root=None, allow_tta=False):
    stage_dir = os.path.abspath(os.path.expanduser(stage_dir))
    if not os.path.isdir(stage_dir):
        raise ValidationError("stage_dir does not exist: %s" % stage_dir)

    inputs = load_json_lenient(os.path.join(stage_dir, "input.json"), required=True)
    config = load_json_lenient(os.path.join(stage_dir, "config.json")) or {}

    if project_root is None:
        # stage_dir is <project>/stages/<stage>
        parent = os.path.dirname(stage_dir)
        project_root = os.path.dirname(parent) if os.path.basename(parent) == "stages" else stage_dir
    project_root = os.path.abspath(os.path.expanduser(project_root))

    if training_input is None:
        training_input = os.path.join(project_root, "stages", "training", "input.json")
    training_input = os.path.abspath(os.path.expanduser(training_input))
    train_json = load_json_lenient(training_input)

    fnd = Findings()
    if not os.path.isfile(os.path.join(stage_dir, "config.json")):
        fnd.warn("config.json", "<file>",
                 "not found in %s — the dataset-agreement check was skipped." % stage_dir)

    gt = inputs.get("ground_truth") or {}
    gt_items = gt.get("items") or {}
    gt_sources = gt.get("sources") or {}
    pairing = inputs.get("pairing")
    eval_preproc = inputs.get("preprocessing") or {}

    check_items(gt_items, pairing, fnd)
    facts = check_sources(gt_items, gt_sources, pairing, project_root, stage_dir, fnd)
    check_dataset_agreement(config.get("dataset") or {}, gt_items,
                            eval_preproc.get("label_transform"), facts, fnd)
    dataset = config.get("dataset") or {}
    check_candidates("input.json", (inputs.get("candidates") or {}).get("items"),
                     dataset, project_root, fnd)
    artifacts = load_json_lenient(os.path.join(stage_dir, "artifacts.json")) or {}
    # Artifact candidates are weights, not data, so the sample-count gate does
    # not apply to them — an empty dataset here suppresses it while leaving the
    # run: and pending: checks, which do.
    check_candidates("artifacts.json",
                     (artifacts.get("candidates") or {}).get("items"),
                     {}, project_root, fnd)
    check_preprocessing(eval_preproc,
                        (train_json or {}).get("preprocessing") if train_json else None,
                        training_input, allow_tta, fnd)

    return {
        "stage_dir": stage_dir,
        "checked": {
            "input.json": os.path.join(stage_dir, "input.json"),
            "config.json": os.path.join(stage_dir, "config.json"),
            "artifacts.json": os.path.join(stage_dir, "artifacts.json"),
            "training_input": training_input if train_json else None,
        },
        "errors": fnd.errors,
        "warnings": fnd.warnings,
        "info": fnd.info,
        "summary": {"errors": len(fnd.errors), "warnings": len(fnd.warnings), "info": len(fnd.info)},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage_dir", help="path to stages/evaluation/")
    ap.add_argument("--training-input", default=None,
                    help="training stage input.json (default: <project>/stages/training/input.json)")
    ap.add_argument("--project-root", default=None,
                    help="project root, for resolving relative source paths")
    ap.add_argument("--allow-tta", action="store_true",
                    help="treat non-empty augmentation as declared test-time augmentation "
                         "(warning instead of error)")
    args = ap.parse_args()

    try:
        report = validate(args.stage_dir, args.training_input, args.project_root, args.allow_tta)
    except ValidationError as e:
        json.dump({"error": str(e)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.stderr.write("validate_ground_truth: %s\n" % e)
        sys.exit(2)

    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    for e in report["errors"]:
        sys.stderr.write("validate_ground_truth: error: %s -> %s: %s\n"
                         % (e["file"], e["key"], e["message"]))
    sys.exit(1 if report["errors"] else 0)


if __name__ == "__main__":
    main()
