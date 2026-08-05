"""A fine-tune records the base measured here, and both sides the same way.

Enforces `lifecycle/references/run-mechanics.md` -> "Record integrity" (the
fine-tune baseline row) and the mechanism section it points at.

Same shape as every other rule in that table: breaking it raises nothing. The
run completes, a number is recorded, and it has no scale -- or worse, two
numbers are recorded, they were taken under different settings, and the delta
between them is presented as a measurement.
"""
import json
import os
import unittest

from helpers import TempDirCase, load_script

bd = load_script("train-run/baseline_delta.py")


def measurement(role, metrics, settings=None, scope=None, trained=None, weights=None):
    return {
        "role": role,
        "weights": weights or ("/ckpt/%s.pt" % role),
        "settings": settings if settings is not None else {"imgsz": 1024, "overlap_mask": False},
        "scope": scope if scope is not None else {"dataset": "d", "samples": 29},
        "metrics": metrics,
        "trained_args": trained or {},
    }


class Args(object):
    def __init__(self, **kw):
        self.direction = None
        self.waive_setting = None
        self.output_json = None
        self.__dict__.update(kw)


class Fixture(TempDirCase):
    def write(self, name, obj):
        p = self.path(name)
        with open(p, "w") as fh:
            json.dump(obj, fh)
        return p

    def compare(self, before, after, **kw):
        a = Args(before=self.write("before.json", before),
                 after=self.write("after.json", after), **kw)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bd.cmd_compare(a)
        return json.loads(buf.getvalue()), code

    def check(self, run):
        d = self.path("rundir")
        os.makedirs(os.path.join(d, "output"), exist_ok=True)
        with open(os.path.join(d, "run.json"), "w") as fh:
            json.dump(run, fh)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bd.cmd_check(Args(run_dir=d))
        return json.loads(buf.getvalue()), code, d


class AFinetuneWithoutItsBaselineIsRefused(Fixture):
    """run-mechanics.md -> "Record integrity", the fine-tune baseline row.

    The child's absolute score has no scale without the base's, and the base's
    published number was measured on another scope. A run that records only the
    child records a number nobody can read -- and it reads exactly like one they
    can.
    """

    def test_finetune_with_no_measurement_fails(self):
        rep, code, _ = self.check({"lineage": {"parent_checkpoint": "s3://b/base.pt"}})
        self.assertEqual(rep["verdict"], "fail")
        self.assertEqual(code, 1)
        self.assertTrue(rep["is_finetune"])
        self.assertEqual([f["code"] for f in rep["findings"]], ["baseline_missing"])

    def test_a_run_that_is_not_a_finetune_is_not_nagged(self):
        rep, code, _ = self.check({"lineage": {"parents": [], "fork_of": None}})
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)
        self.assertFalse(rep["is_finetune"])

    def test_every_way_a_run_can_be_a_finetune_is_detected(self):
        """Three recording paths, and the foreign base is the one that matters.

        A fork and a cited parent are both in-project and both leave an obvious
        trace. A foreign base has no citable identity at all -- it lives as a
        path in params -- and it is precisely the case whose published number
        must not be trusted, so missing it would exempt the run that needs this
        most.
        """
        for run in ({"lineage": {"fork_of": "training/run_1"}},
                    {"lineage": {"parents": ["training/run_1"]}},
                    {"lineage": {"parent_checkpoint": "s3://b/base.pt"}},
                    {"params": {"model": "/weights/vendor-base.pt"}},
                    {"params": {"resume_from": "/weights/last.pt"}}):
            rep, code, _ = self.check(run)
            self.assertTrue(rep["is_finetune"], run)
            self.assertEqual(code, 1, run)

    def test_a_waiver_downgrades_but_stays_visible(self):
        rep, code, _ = self.check({
            "lineage": {"parent_checkpoint": "s3://b/base.pt"},
            "baseline_delta": {"waived": "base weights will not load under cuda 13"}})
        self.assertEqual(rep["verdict"], "warn")
        self.assertEqual(code, 0)
        self.assertEqual([f["code"] for f in rep["findings"]], ["baseline_waived"])
        self.assertIn("cuda 13", rep["findings"][0]["message"])

    def test_before_without_after_is_a_warning_not_a_failure(self):
        rep, code, d = self.check({"lineage": {"parent_checkpoint": "s3://b/base.pt"}})
        with open(os.path.join(d, "output", "baseline_before.json"), "w") as fh:
            json.dump(measurement("before", {"m": 1.0}), fh)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bd.cmd_check(Args(run_dir=d))
        rep = json.loads(buf.getvalue())
        self.assertEqual(rep["verdict"], "warn")
        self.assertEqual([f["code"] for f in rep["findings"]], ["after_missing"])


class TwoMeasurementsTakenDifferentlyAreNotADelta(Fixture):
    """run-mechanics.md -> "Baseline measurement (fine-tune only)".

    The worse half of the rule. A missing measurement is visibly missing; two
    measurements taken under different settings both exist, both look fine, and
    the subtraction between them is reported as a result.
    """

    def test_differing_settings_refuse(self):
        rep, code = self.compare(
            measurement("before", {"m": 0.4}, settings={"imgsz": 1024}),
            measurement("after", {"m": 0.9}, settings={"imgsz": 640}))
        self.assertEqual(rep["verdict"], "fail")
        self.assertEqual(code, 1)
        self.assertIn("settings_differ", [f["code"] for f in rep["findings"]])

    def test_a_setting_present_on_one_side_only_is_a_difference(self):
        """Absent and equal are different, the same distinction the metric rules draw."""
        rep, _ = self.compare(
            measurement("before", {"m": 0.4}, settings={"imgsz": 1024}),
            measurement("after", {"m": 0.9}, settings={"imgsz": 1024, "half": True}))
        self.assertEqual(rep["verdict"], "fail")
        f = [x for x in rep["findings"] if x["code"] == "settings_differ"][0]
        self.assertEqual(f["key"], "half")
        self.assertFalse(f["before_present"])

    def test_annotation_keys_do_not_break_comparability(self):
        """`_`-prefixed keys are prose. Same rule /repro's trial applies to scope:
        a differing note must not make two runs incomparable, because failing
        closed on prose reads exactly like the guard working."""
        rep, code = self.compare(
            measurement("before", {"m": 0.4}, settings={"imgsz": 1024, "_note": "base"}),
            measurement("after", {"m": 0.9}, settings={"imgsz": 1024, "_note": "child"}))
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)

    def test_differing_scope_refuses(self):
        rep, _ = self.compare(
            measurement("before", {"m": 0.4}, scope={"samples": 29}),
            measurement("after", {"m": 0.9}, scope={"samples": 30}))
        self.assertIn("scope_differs", [f["code"] for f in rep["findings"]])

    def test_a_waiver_needs_a_reason_and_is_recorded(self):
        rep, code = self.compare(
            measurement("before", {"m": 0.4}, settings={"imgsz": 1024}),
            measurement("after", {"m": 0.9}, settings={"imgsz": 640}),
            waive_setting=["imgsz=both are above the model's native stride"])
        self.assertEqual(code, 0)
        self.assertEqual(rep["verdict"], "warn")
        self.assertIn("imgsz", rep["waived"])


class TheCheckpointsOwnArgsAreTheProtocol(Fixture):
    """run-mechanics.md -> "Baseline measurement (fine-tune only)".

    The free check, and the one that catches a library default standing in for
    the project's protocol. Measured live: validating a yolo26 segmentation
    fine-tune at `overlap_mask=True` (the library default) instead of the
    `False` the weights were trained under moved box mAP50-95 0.9142 -> 0.9027
    and wall 0.9672 -> 0.9445. Both sides carried it, so the DELTA stayed
    honest while every absolute number stopped being comparable to anything
    published -- which is why a delta-only check cannot see this.
    """

    def test_measuring_against_the_library_default_is_caught(self):
        rep, code = self.compare(
            measurement("before", {"m": 0.45}, settings={"overlap_mask": True},
                        trained={"overlap_mask": False}),
            measurement("after", {"m": 0.90}, settings={"overlap_mask": True},
                        trained={"overlap_mask": False}))
        self.assertEqual(rep["verdict"], "fail")
        self.assertEqual(code, 1)
        codes = [f["code"] for f in rep["findings"]]
        self.assertEqual(codes.count("measured_against_trained_args"), 2,
                         "both sides depart from their own weights' protocol")

    def test_consistent_settings_alone_do_not_make_it_pass(self):
        """The two sides agreeing is necessary and not sufficient. This is the
        whole point: the buggy comparison IS internally consistent."""
        rep, _ = self.compare(
            measurement("before", {"m": 0.45}, settings={"overlap_mask": True},
                        trained={"overlap_mask": False}),
            measurement("after", {"m": 0.90}, settings={"overlap_mask": True},
                        trained={"overlap_mask": False}))
        self.assertEqual([], [f for f in rep["findings"] if f["code"] == "settings_differ"])
        self.assertEqual(rep["verdict"], "fail")

    def test_agreement_with_the_weights_passes(self):
        rep, code = self.compare(
            measurement("before", {"m": 0.45}, settings={"overlap_mask": False},
                        trained={"overlap_mask": False, "epochs": 120}),
            measurement("after", {"m": 0.90}, settings={"overlap_mask": False},
                        trained={"overlap_mask": False, "epochs": 200}))
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)

    def test_no_recorded_trained_args_is_silence_not_approval(self):
        """A checkpoint that records nothing cannot confirm the protocol. It also
        must not fabricate a failure -- absent evidence is neither."""
        rep, code = self.compare(
            measurement("before", {"m": 0.45}, settings={"overlap_mask": True}, trained={}),
            measurement("after", {"m": 0.90}, settings={"overlap_mask": True}, trained={}))
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(code, 0)


class ADeltaSaysOnlyWhatItCanSay(Fixture):
    """run-mechanics.md -> "Baseline measurement (fine-tune only)", closing rule."""

    def test_no_declared_direction_means_no_improvement_verdict(self):
        """Guessing direction from a metric's name is how a loss going up gets
        reported as an improvement."""
        rep, _ = self.compare(measurement("before", {"val_loss": 0.4}),
                              measurement("after", {"val_loss": 0.9}))
        self.assertIsNone(rep["deltas"]["val_loss"]["improved"])
        self.assertIsNone(rep["deltas"]["val_loss"]["direction"])

    def test_declared_direction_is_honoured(self):
        out = self.path("output.json")
        with open(out, "w") as fh:
            json.dump({"metrics": {"primary_metric": "val_loss", "direction": "minimize"}}, fh)
        rep, _ = self.compare(measurement("before", {"val_loss": 0.9}),
                              measurement("after", {"val_loss": 0.4}), direction=out)
        self.assertTrue(rep["deltas"]["val_loss"]["improved"])

    def test_a_metric_measured_on_one_side_only_is_absent_not_unchanged(self):
        rep, _ = self.compare(measurement("before", {"a": 1.0, "b": 2.0}),
                              measurement("after", {"a": 1.5}))
        self.assertEqual(rep["metrics_without_a_counterpart"]["only_before"], ["b"])
        self.assertNotIn("b", rep["deltas"])
        self.assertIn("metric_only_before", [f["code"] for f in rep["findings"]])

    def test_no_shared_metric_is_a_refusal(self):
        rep, code = self.compare(measurement("before", {"a": 1.0}),
                                 measurement("after", {"z": 1.0}))
        self.assertEqual(code, 1)
        self.assertIn("no_shared_metric", [f["code"] for f in rep["findings"]])


if __name__ == "__main__":
    unittest.main()
