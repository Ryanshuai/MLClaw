#!/usr/bin/env python3
"""`eval-init/validate_ground_truth.py` is a gate, and every gate this session
opened had a defect. This one had no check at all.

Its own docstring states the bar: *everything checked here is a failure that
produces PLAUSIBLE NUMBERS rather than an error.* The evaluation completes, the
metric is a real number, and it answers a different question than the one asked.
That is exactly CLAUDE.md -> "Contracts": a record written now and read later by
someone who can no longer verify it.

The checks below are aimed at the severity BOUNDARY, not at coverage. An error
stops `/eval-init` from saving; a warning does not. Every rule here has a stated
reason for which side of that line it sits on, and moving one is a one-word edit
that nothing else in the repo would notice:

  a subset marked `ok`        error. CLAUDE.md's skill table states it verbatim --
                              *`samples` ≠ `dataset.num_samples` is `mismatch`*,
                              a subset is a different measurement
  `num_samples` not set       warning. Cannot check is not the same as wrong, and
                              blocking on it would make an unpinned dataset
                              unusable rather than unverified
  class COUNT differs         error -- per-class metrics misalign
  class count same, order not warning -- the report is mislabeled, not misaligned
  preprocessing differs       error -- the eval measures a different model than
                              was trained, and nothing raises at run time
  augmentation non-empty      error, unless --allow-tta says it was deliberate
"""
import json
import os
import unittest

from helpers import TempDirCase, load_script, run_script

vgt = load_script("eval-init/validate_ground_truth.py")
SCRIPT = "eval-init/validate_ground_truth.py"


class EvalStageCase(TempDirCase):

    def stage(self, *, inputs=None, config=None, artifacts=None, training=None):
        base_in = {"ground_truth": {"items": {}, "sources": {}}, "preprocessing": {}}
        base_in.update(inputs or {})
        self.write_json("proj/stages/evaluation/input.json", base_in)
        self.write_json("proj/stages/evaluation/config.json", config or {})
        if artifacts is not None:
            self.write_json("proj/stages/evaluation/artifacts.json", artifacts)
        if training is not None:
            self.write_json("proj/stages/training/input.json", training)
        return self.path("proj", "stages", "evaluation")

    def report(self, **kw):
        return vgt.validate(self.stage(**kw))

    def keys(self, findings):
        return sorted(f["key"] for f in findings)


class ASubsetIsADifferentMeasurement(EvalStageCase):
    """CLAUDE.md -> skill table, `/eval-init`: *Gate: `samples` ≠
    `dataset.num_samples` is `mismatch` — a subset is a different measurement.*

    mAP over 500 images and mAP over 5000 are both real numbers with the same
    name. Diffed against a baseline measured on the other one, the delta is
    sampling noise wearing a result's clothes.
    """

    def _cands(self, **entry):
        return {"candidates": {"items": {"val": [dict({"match": "ok"}, **entry)]}}}

    def test_a_smaller_candidate_marked_ok_is_an_error(self):
        r = self.report(inputs=self._cands(samples=500),
                        config={"dataset": {"num_samples": 5000}})
        self.assertIn("candidates.items.val[0]", self.keys(r["errors"]))

    def test_a_matching_count_passes(self):
        r = self.report(inputs=self._cands(samples=5000),
                        config={"dataset": {"num_samples": 5000}})
        self.assertEqual(r["errors"], [])

    def test_ok_with_no_sample_count_at_all_is_an_error(self):
        """Nothing pins what the metric is measured over, which is the same
        failure as a wrong count and not a milder one."""
        r = self.report(inputs=self._cands(),
                        config={"dataset": {"num_samples": 5000}})
        self.assertIn("candidates.items.val[0]", self.keys(r["errors"]))

    def test_an_unset_num_samples_warns_rather_than_blocks(self):
        """Cannot check is not the same as wrong. Erroring here would make an
        unpinned dataset unusable rather than unverified -- the three-facts
        split, one gate over."""
        r = self.report(inputs=self._cands(samples=500), config={"dataset": {}})
        self.assertEqual(r["errors"], [])
        self.assertIn("candidates.items.val[0]", self.keys(r["warnings"]))

    def test_a_candidate_not_marked_ok_is_not_gated_on_count(self):
        r = self.report(
            inputs={"candidates": {"items": {"val": [
                {"match": "mismatch", "samples": 500}]}}},
            config={"dataset": {"num_samples": 5000}})
        self.assertEqual(r["errors"], [])

    def test_an_unknown_match_value_is_an_error_not_a_pass(self):
        r = self.report(
            inputs={"candidates": {"items": {"val": [{"match": "probably"}]}}},
            config={"dataset": {"num_samples": 5000}})
        self.assertEqual(len(r["errors"]), 1)


class OnlyAProductionCheckpointHasAComparableScope(EvalStageCase):
    """A debug run's checkpoint carries a debug run's data scope, so its number
    is comparable to nothing -- and `run:` candidates are how a checkpoint gets
    into an eval."""

    def _with_run(self, mode):
        self.write_json("proj/stages/training/runs/run_001/run.json",
                        {"stage": "training", "run_id": "run_001", "mode": mode})
        return {"candidates": {"items": {"ckpt": [
            {"match": "ok", "location": "run:training/run_001", "samples": 10}]}}}

    def test_a_debug_run_checkpoint_is_an_error(self):
        r = self.report(inputs=self._with_run("debug"),
                        config={"dataset": {"num_samples": 10}})
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("comparable to nothing", r["errors"][0]["message"])

    def test_a_production_run_checkpoint_passes(self):
        r = self.report(inputs=self._with_run("production"),
                        config={"dataset": {"num_samples": 10}})
        self.assertEqual(r["errors"], [])

    def test_a_run_whose_record_is_not_there_is_an_error(self):
        r = self.report(
            inputs={"candidates": {"items": {"ckpt": [
                {"match": "ok", "location": "run:training/run_999", "samples": 10}]}}},
            config={"dataset": {"num_samples": 10}})
        self.assertEqual(len(r["errors"]), 1)


class APendingCandidateMustPointAtOpenWork(EvalStageCase):
    """A `pending` candidate naming a closed handoff is either an `ok` nobody
    promoted or a fiction, and both send `/eval-run` to wait for work that
    already came back."""

    def _handoff(self, status):
        self.write_json("proj/handoffs/h1/handoff.json",
                        {"handoff_id": "h1", "status": status})
        return {"candidates": {"items": {"val": [
            {"match": "pending", "location": "handoff:h1"}]}}}

    def test_a_closed_handoff_is_an_error(self):
        for status in ("accepted", "rejected", "cancelled"):
            with self.subTest(status=status):
                r = self.report(inputs=self._handoff(status))
                self.assertEqual(len(r["errors"]), 1, status)

    def test_an_open_handoff_passes(self):
        r = self.report(inputs=self._handoff("sent"))
        self.assertEqual(r["errors"], [])

    def test_pending_pointing_at_a_path_is_an_error(self):
        """`pending` means the asset resolves by somebody else finishing, which
        only `handoff:` can express. A path cannot become true by waiting."""
        r = self.report(inputs={"candidates": {"items": {"val": [
            {"match": "pending", "location": "/data/val"}]}}})
        self.assertEqual(len(r["errors"]), 1)


class PreprocessingThatDiffersMeasuresADifferentModel(EvalStageCase):
    """run-mechanics.md "Preprocessing contract (cross-stage)". A difference
    means the evaluation is not measuring the model that was trained, and
    nothing errors at run time."""

    TRAIN = {"preprocessing": {"normalization": {"mean": [0.5], "std": [0.5]},
                               "input_layout": {"size": [640, 640]}}}

    def test_a_normalization_mismatch_is_an_error(self):
        r = self.report(
            inputs={"preprocessing": {"normalization": {"mean": [0.0], "std": [0.5]}}},
            training=self.TRAIN)
        self.assertIn("preprocessing.normalization.mean", self.keys(r["errors"]))

    def test_matching_preprocessing_passes(self):
        r = self.report(inputs={"preprocessing": self.TRAIN["preprocessing"]},
                        training=self.TRAIN)
        self.assertEqual(r["errors"], [])

    def test_a_blank_on_either_side_warns_because_nothing_was_verified(self):
        r = self.report(inputs={"preprocessing": {"normalization": {}}},
                        training=self.TRAIN)
        self.assertEqual(r["errors"], [])
        self.assertTrue(r["warnings"])

    def test_no_training_stage_is_recorded_as_unverified_not_as_passed(self):
        r = self.report(inputs={"preprocessing": self.TRAIN["preprocessing"]})
        self.assertEqual(r["errors"], [])
        self.assertTrue(any("could not be verified" in f["message"] for f in r["info"]),
                        "an unverifiable contract must not read as a verified one")

    def test_augmentation_in_an_eval_stage_is_an_error(self):
        r = self.report(inputs={"preprocessing": {"augmentation": {"flip": True}}},
                        training=self.TRAIN)
        self.assertIn("preprocessing.augmentation", self.keys(r["errors"]))

    def test_allow_tta_downgrades_it_to_a_warning_and_nothing_else(self):
        d = self.stage(inputs={"preprocessing": {"augmentation": {"flip": True}}},
                       training=self.TRAIN)
        r = vgt.validate(d, allow_tta=True)
        self.assertEqual(r["errors"], [])
        self.assertIn("preprocessing.augmentation", self.keys(r["warnings"]))


class MisalignedAndMislabeledAreDifferentSeverities(EvalStageCase):
    """A class COUNT difference misaligns every per-class metric -- an error. The
    same count in a different order mislabels the report while the numbers stay
    right -- a warning. Collapsing them either blocks a working config or lets a
    mislabeled report through."""

    def _coco(self, cats, num_images=2):
        p = self.path("proj", "ann.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"images": [{"id": i} for i in range(num_images)],
                       "annotations": [], "categories": cats}, f)
        return {"ground_truth": {
            "items": {"ann": {"type": "json", "pairing": "single_file"}},
            "sources": {"ann": {"source": "local", "path": p, "format": "json"}}},
            "pairing": "single_file"}

    def test_a_different_class_count_is_an_error(self):
        r = self.report(
            inputs=self._coco([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]),
            config={"dataset": {"classes": ["a"], "num_samples": 2}})
        self.assertIn("dataset.classes", self.keys(r["errors"]))

    def test_the_same_count_in_a_different_order_is_only_a_warning(self):
        r = self.report(
            inputs=self._coco([{"id": 1, "name": "b"}, {"id": 2, "name": "a"}]),
            config={"dataset": {"classes": ["a", "b"], "num_samples": 2}})
        self.assertEqual(r["errors"], [])
        self.assertIn("dataset.classes", self.keys(r["warnings"]))

    def test_non_contiguous_ids_with_no_class_mapping_warn(self):
        """The COCO 91-vs-80 remap, which is the classic silent degradation:
        every number stays plausible and every class is off by a shifting
        amount."""
        r = self.report(
            inputs=self._coco([{"id": 1, "name": "a"}, {"id": 90, "name": "b"}]),
            config={"dataset": {"classes": ["a", "b"], "num_samples": 2}})
        self.assertIn("preprocessing.label_transform.class_mapping",
                      self.keys(r["warnings"]))

    def test_a_sample_count_that_disagrees_with_the_file_warns(self):
        r = self.report(inputs=self._coco([], num_images=7),
                        config={"dataset": {"num_samples": 5000}})
        self.assertIn("dataset.num_samples", self.keys(r["warnings"]))


class ItDoesNotAnswerTheOtherValidatorsQuestion(EvalStageCase):
    """Its own docstring: *`${}` references are not checked here -- two
    validators answering the same question is how /eval-init ended up with one
    of them declaring every correct eval config broken.*"""

    def test_a_reference_placeholder_is_not_treated_as_a_broken_path(self):
        r = self.report(inputs={"ground_truth": {
            "items": {"ann": {"type": "json", "pairing": "single_file"}},
            "sources": {"ann": {"source": "local",
                                "path": "${resources.data.val}/ann.json"}}},
            "pairing": "single_file"})
        self.assertEqual(
            [f for f in r["errors"] if "does not" in f["message"]], [],
            "resolving `${}` is validate_refs.py's job; duplicating it here is "
            "what broke every correct config once already")


class TheExitCodesSayWhichKindOfNo(EvalStageCase):
    """CLAUDE.md -> "Script Integration". 1 means /eval-init must not save; 2
    means fall back and validate by hand, which for a gate means not validating
    at all."""

    def test_a_clean_stage_is_exit_0(self):
        d = self.stage()
        rc, _out, _err = run_script(SCRIPT, d)
        self.assertEqual(rc, 0)

    def test_an_error_is_exit_1(self):
        d = self.stage(inputs={"candidates": {"items": {"val": [
            {"match": "ok", "samples": 5}]}}},
            config={"dataset": {"num_samples": 5000}})
        rc, out, _err = run_script(SCRIPT, d)
        self.assertEqual(rc, 1)
        self.assertEqual(out["summary"]["errors"], 1)

    def test_a_warning_alone_is_still_exit_0(self):
        d = self.stage(inputs={"candidates": {"items": {"val": [
            {"match": "ok", "samples": 5}]}}}, config={"dataset": {}})
        rc, out, _err = run_script(SCRIPT, d)
        self.assertEqual(rc, 0)
        self.assertTrue(out["summary"]["warnings"])

    def test_a_missing_stage_dir_is_exit_2(self):
        rc, out, _err = run_script(SCRIPT, self.path("nothing"))
        self.assertEqual(rc, 2)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
