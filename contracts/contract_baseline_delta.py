"""A fine-tune records the base measured here, under its own weights' protocol.

Enforces `references/run-mechanics.md` -> "Record integrity" (the
fine-tune baseline row) and the mechanism section it points at.

Scope note, because it is the design: this script does NOT measure and does NOT
diff metrics. Measuring is `/eval-run`'s and diffing is `compare_baseline.py`'s,
which already reads direction from config rather than guessing from a metric's
name and already grades comparability. What is checked here is only what a
fine-tune knows and neither of those can: that the base was measured at all, and
that each measurement honoured the settings its own weights were trained under.
"""
import json
import os
import unittest

from helpers import TempDirCase, load_script

bd = load_script("train-run/baseline_delta.py")


class Args(object):
    def __init__(self, **kw):
        self.waive_setting = None
        self.output_json = None
        self.__dict__.update(kw)


def eval_run(settings=None, trained=None, weights="/ckpt/x.pt"):
    return {"measurement": {"settings": settings if settings is not None else {},
                            "trained_args": trained or {},
                            "weights": weights}}


class Fixture(TempDirCase):
    def _capture(self, fn, args):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fn(args)
        return json.loads(buf.getvalue()), code

    def rundir(self, name, run):
        d = self.path(name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(run, fh)
        return d

    def check(self, run):
        return self._capture(bd.cmd_check, Args(run_dir=self.rundir("train", run)))

    def protocol(self, before, after, **kw):
        return self._capture(bd.cmd_protocol, Args(
            before=self.rundir("before", before),
            after=self.rundir("after", after), **kw))


class AFinetuneWithoutItsBaselineIsRefused(Fixture):
    """run-mechanics.md -> "Record integrity", the fine-tune baseline row.

    The child's absolute score has no scale without the base's, and the base's
    published number was measured on another scope. A run recording only the
    child records a number nobody can read -- and it reads exactly like one they
    can.
    """

    def test_finetune_with_no_measurement_fails(self):
        rep, code = self.check({"lineage": {"parent_checkpoint": "s3://b/base.pt"}})
        self.assertEqual(rep["verdict"], "fail")
        self.assertEqual(code, 1)
        self.assertEqual([f["code"] for f in rep["findings"]], ["baseline_missing"])

    def test_the_refusal_routes_to_the_skill_that_can_fix_it(self):
        """A blocker that does not name its remedy gets worked around."""
        rep, _ = self.check({"lineage": {"parent_checkpoint": "s3://b/base.pt"}})
        f = rep["findings"][0]
        self.assertIn("/eval-run", f["message"])
        self.assertIn("eval-init", f["needs"])

    def test_a_run_that_is_not_a_finetune_is_not_nagged(self):
        rep, code = self.check({"lineage": {"parents": [], "fork_of": None}})
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)
        self.assertFalse(rep["is_finetune"])

    def test_every_way_a_run_can_be_a_finetune_is_detected(self):
        """Three recording paths, and the foreign base is the one that matters.

        A fork and a cited parent are in-project and leave an obvious trace. A
        foreign base has no citable identity at all -- it lives as a path in
        params -- and it is precisely the case whose published number must not
        be trusted, so missing it would exempt the run that needs this most.
        """
        for run in ({"lineage": {"fork_of": "training/run_1"}},
                    {"lineage": {"parents": ["training/run_1"]}},
                    {"lineage": {"parent_checkpoint": "s3://b/base.pt"}},
                    {"params": {"model": "/weights/vendor-base.pt"}},
                    {"params": {"resume_from": "/weights/last.pt"}}):
            rep, code = self.check(run)
            self.assertTrue(rep["is_finetune"], run)
            self.assertEqual(code, 1, run)

    def test_an_annotation_key_never_makes_a_run_a_finetune(self):
        rep, _ = self.check({"params": {"_model": "notes about the model"}})
        self.assertFalse(rep["is_finetune"])

    def test_a_waiver_downgrades_but_stays_visible(self):
        rep, code = self.check({
            "lineage": {"parent_checkpoint": "s3://b/base.pt"},
            "baseline_delta": {"waived": "base weights will not load under cuda 13"}})
        self.assertEqual(rep["verdict"], "warn")
        self.assertEqual(code, 0)
        self.assertIn("cuda 13", rep["findings"][0]["message"])

    def test_the_baseline_is_cited_as_an_eval_run_not_a_loose_file(self):
        """Measurements are runs, so they carry a run id, a scope and a code
        snapshot. A pair of loose JSON files would carry a schema invented here
        and nothing else."""
        rep, code = self.check({
            "lineage": {"parent_checkpoint": "s3://b/base.pt"},
            "baseline_delta": {"before": "evaluation/run_a", "after": "evaluation/run_b"}})
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)
        self.assertEqual(rep["before"], "evaluation/run_a")

    def test_before_without_after_is_a_warning_not_a_failure(self):
        rep, code = self.check({
            "lineage": {"parent_checkpoint": "s3://b/base.pt"},
            "baseline_delta": {"before": "evaluation/run_a"}})
        self.assertEqual(rep["verdict"], "warn")
        self.assertEqual(code, 0)
        self.assertEqual([f["code"] for f in rep["findings"]], ["after_missing"])


class TwoMeasurementsTakenDifferentlyAreNotADelta(Fixture):
    """run-mechanics.md -> "Baseline measurement (fine-tune only)"."""

    def test_differing_settings_refuse(self):
        rep, code = self.protocol(eval_run({"imgsz": 1024}), eval_run({"imgsz": 640}))
        self.assertEqual(rep["verdict"], "fail")
        self.assertEqual(code, 1)
        self.assertIn("settings_differ", [f["code"] for f in rep["findings"]])

    def test_a_setting_present_on_one_side_only_is_a_difference(self):
        """Absent and equal are different -- the same distinction the metric
        rules draw between an extraction failure and a metric never produced."""
        rep, _ = self.protocol(eval_run({"imgsz": 1024}),
                               eval_run({"imgsz": 1024, "half": True}))
        f = [x for x in rep["findings"] if x["code"] == "settings_differ"][0]
        self.assertEqual(f["key"], "half")
        self.assertFalse(f["before_present"])

    def test_annotation_keys_do_not_break_comparability(self):
        """`_`-prefixed keys are prose. Failing closed on prose reads exactly
        like the guard working, which is how it survives unnoticed -- the defect
        /repro's trial had against `scope`."""
        rep, code = self.protocol(
            eval_run({"imgsz": 1024, "_note": "base"}, {"imgsz": 1024}),
            eval_run({"imgsz": 1024, "_note": "child"}, {"imgsz": 1024}))
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)

    def test_unrecorded_settings_do_not_pass(self):
        """A protocol nobody wrote down is not a matching protocol. Same rule as
        a repro axis that could not be probed: `unverifiable`, never `intact`."""
        rep, code = self.protocol(eval_run({}), eval_run({}))
        self.assertEqual(rep["verdict"], "fail")
        self.assertEqual(code, 1)
        self.assertEqual(
            2, [f["code"] for f in rep["findings"]].count("settings_not_recorded"))

    def test_a_waiver_needs_a_reason_and_lands_in_the_record(self):
        rep, code = self.protocol(
            eval_run({"imgsz": 1024}), eval_run({"imgsz": 640}),
            waive_setting=["imgsz=both exceed the model's native stride"])
        self.assertEqual(code, 0)
        self.assertEqual(rep["verdict"], "warn")
        self.assertIn("imgsz", rep["waived"])

    def test_a_waiver_without_a_reason_is_rejected(self):
        rep_code = bd.cmd_protocol(Args(
            before=self.rundir("b", eval_run({"imgsz": 1024})),
            after=self.rundir("a", eval_run({"imgsz": 640})),
            waive_setting=["imgsz="]))
        self.assertEqual(rep_code, 2)


class TheCheckpointsOwnArgsAreTheProtocol(Fixture):
    """run-mechanics.md -> "Baseline measurement (fine-tune only)".

    The free check, and the one nothing else can perform. Measured live:
    validating a yolo26 segmentation fine-tune at `overlap_mask=True` (the
    library default) instead of the `False` its weights were trained under moved
    box mAP50-95 0.9142 -> 0.9027 and wall 0.9672 -> 0.9445. Mode and scope were
    both fine, so `compare_baseline.py` passes it correctly; and because both
    sides carried the same default the DELTA stayed honest while every absolute
    number stopped being comparable to anything published.
    """

    def test_measuring_at_the_library_default_is_caught(self):
        rep, code = self.protocol(
            eval_run({"overlap_mask": True}, {"overlap_mask": False}),
            eval_run({"overlap_mask": True}, {"overlap_mask": False}))
        self.assertEqual(rep["verdict"], "fail")
        self.assertEqual(code, 1)
        self.assertEqual(
            2, [f["code"] for f in rep["findings"]].count("measured_against_trained_args"))

    def test_the_two_sides_agreeing_is_not_sufficient(self):
        """The whole point: the buggy comparison IS internally consistent, so a
        check that only compares the two sides to each other passes it."""
        rep, _ = self.protocol(
            eval_run({"overlap_mask": True}, {"overlap_mask": False}),
            eval_run({"overlap_mask": True}, {"overlap_mask": False}))
        self.assertEqual([], [f for f in rep["findings"] if f["code"] == "settings_differ"])
        self.assertEqual(rep["verdict"], "fail")

    def test_agreement_with_the_weights_passes(self):
        rep, code = self.protocol(
            eval_run({"overlap_mask": False}, {"overlap_mask": False, "epochs": 120}),
            eval_run({"overlap_mask": False}, {"overlap_mask": False, "epochs": 200}))
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)

    def test_a_key_the_weights_never_recorded_is_not_invented(self):
        rep, code = self.protocol(
            eval_run({"overlap_mask": False, "batch": 1}, {"overlap_mask": False}),
            eval_run({"overlap_mask": False, "batch": 1}, {"overlap_mask": False}))
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)

    def test_no_recorded_trained_args_is_silence_not_approval(self):
        """A checkpoint recording nothing cannot confirm the protocol -- and must
        not fabricate a failure either. Absent evidence is neither, and it is
        reported so rather than passing quietly."""
        rep, code = self.protocol(eval_run({"overlap_mask": True}, {}),
                                  eval_run({"overlap_mask": True}, {}))
        self.assertEqual(rep["verdict"], "warn")
        self.assertEqual(code, 0)
        self.assertEqual(
            2, [f["code"] for f in rep["findings"]].count("no_trained_args_recorded"))


class ItDoesNotReimplementWhatEvalAlreadyOwns(Fixture):
    """run-mechanics.md -> "Baseline measurement (fine-tune only)".

    `/eval-init` refuses to grow its own sweep because `/train-init` has one:
    "two implementations of 'where is the data' is how they start disagreeing."
    Same rule here. `compare_baseline.py` owns direction and comparability; this
    script must not acquire a second, weaker copy of either.
    """

    def test_it_reports_no_metric_delta(self):
        rep, _ = self.protocol(eval_run({"imgsz": 1024}), eval_run({"imgsz": 1024}))
        for leaked in ("deltas", "improved", "direction", "metrics"):
            self.assertNotIn(leaked, rep)
        self.assertIn("compare_baseline.py", rep["note"])

    def test_it_has_no_direction_handling(self):
        with open(os.path.join(os.path.dirname(bd.__file__), "baseline_delta.py"), encoding="utf-8") as fh:
            src = fh.read()
        for owned_elsewhere in ("maximize", "minimize", "primary_metric"):
            self.assertNotIn('"%s"' % owned_elsewhere, src,
                             "direction is compare_baseline.py's")


if __name__ == "__main__":
    unittest.main()
