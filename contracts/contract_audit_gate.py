"""The audit gate: a production run may not launch on data an audit condemned --
and `never audited` is not `clean`.

Enforces run-mechanics.md -> "Record integrity", the row beginning "A **production**
run launches only on data whose latest audit is `clean`", and the fixed keys stated
in `/data-audit` Step 7 (`layers[].verdict`, `audited_at`, `snapshot`).

The direction that matters is a FALSE CLEAR: a refusal costs one message, a clear
costs a trained model whose category ids were silently clamped.
"""
import json
import os
import subprocess
import tempfile
import unittest

from helpers import REPO_ROOT

GATE = os.path.join(REPO_ROOT, "lifecycle", "scripts", "data-audit", "audit_gate.py")
CLEAN = {"integrity": {"verdict": "INFO"}, "compatibility": {"verdict": "WARN"}}


def project(layers=None, judged="260731", cite="260731", audited_at="2026-08-18T00:00:00+00:00",
            audits=True, raw_path=False):
    d = tempfile.mkdtemp()
    st = os.path.join(d, "stages", "training")
    os.makedirs(st)
    entry = ({"location": "local", "path": "/data/boxes"} if raw_path else
             {"location": f"dataset:boxes@{cite}", "path": "",
              "resolve": {"dataset": "boxes", "snapshot": cite}})
    with open(os.path.join(st, "input.json"), "w", encoding="utf-8") as f:
        json.dump({"candidates": {"items": {"train": [entry]}}}, f)
    if audits:
        ad = os.path.join(d, "datasets", "boxes", "audits", "audit_20260818")
        os.makedirs(ad)
        with open(os.path.join(ad, "audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audited_at": audited_at, "snapshot": judged,
                       "layers": layers if layers is not None else CLEAN}, f)
    return d


def gate(proj, mode="production", extra=()):
    p = subprocess.run(["python3", GATE, "check", "--project", proj, "--mode", mode, *extra],
                       capture_output=True, text=True, timeout=30)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)


def state(out):
    return out["rulings"][0]["state"]


class CleanDataClears(unittest.TestCase):
    """run-mechanics.md -> "Record integrity", the audit row."""

    def test_clean_clears_production(self):
        rc, out = gate(project())
        self.assertEqual(rc, 0)
        self.assertEqual(state(out), "clean")


class TheFourRefusalsAreDistinct(unittest.TestCase):
    """The row: `never_audited`, `unverifiable`, `stale` and `unreadable` are each
    refusals, not one -- the same three-fact split `census.py` draws."""

    def test_fatal_refuses_and_names_the_layer(self):
        rc, out = gate(project({"integrity": {"verdict": "INFO"},
                                "compatibility": {"verdict": "FATAL"}}))
        self.assertEqual(rc, 1)
        self.assertEqual(state(out), "fatal")
        self.assertEqual(out["rulings"][0]["fatal_layers"], ["compatibility"])

    def test_never_audited_is_not_clean(self):
        rc, out = gate(project(audits=False))
        self.assertEqual(rc, 1)
        self.assertEqual(state(out), "never_audited")

    def test_a_skipped_layer_is_unverifiable_not_a_pass(self):
        """/data-audit Step 7: a SKIP is a recorded field, never an absent one."""
        rc, out = gate(project({"integrity": {"verdict": "SKIP"},
                                "compatibility": {"verdict": "INFO"}}))
        self.assertEqual(rc, 1)
        self.assertEqual(state(out), "unverifiable")
        self.assertIn("integrity", out["rulings"][0]["blind_layers"])

    def test_a_missing_layer_is_as_blind_as_a_skipped_one(self):
        """An audit missing its compatibility section reads identically to one
        that passed it -- so it must not."""
        rc, out = gate(project({"integrity": {"verdict": "INFO"}}))
        self.assertEqual(rc, 1)
        self.assertEqual(state(out), "unverifiable")
        self.assertIn("compatibility", out["rulings"][0]["blind_layers"])

    def test_an_audit_of_another_snapshot_is_stale(self):
        rc, out = gate(project(judged="260601", cite="260731"))
        self.assertEqual(rc, 1)
        self.assertEqual(state(out), "stale")
        self.assertEqual(out["rulings"][0]["judged_snapshot"], "260601")

    def test_an_audit_with_no_verdict_map_is_unreadable_not_clean(self):
        rc, out = gate(project(layers={}))
        self.assertEqual(rc, 1)
        self.assertEqual(state(out), "unreadable")

    def test_fatal_outranks_stale(self):
        """A FATAL about older bytes is still a FATAL; reporting it as merely
        stale is the /repro verdict-ranking bug in a new place."""
        rc, out = gate(project({"compatibility": {"verdict": "FATAL"}},
                               judged="260601", cite="260731"))
        self.assertEqual(state(out), "fatal")


class TheLatestAuditIsTheOneThatRules(unittest.TestCase):
    def test_a_newer_clean_audit_supersedes_an_older_fatal(self):
        d = project({"compatibility": {"verdict": "FATAL"}, "integrity": {"verdict": "INFO"}},
                    audited_at="2026-01-01T00:00:00+00:00")
        newer = os.path.join(d, "datasets", "boxes", "audits", "audit_20260901")
        os.makedirs(newer)
        with open(os.path.join(newer, "audit.json"), "w", encoding="utf-8") as f:
            json.dump({"audited_at": "2026-09-01T00:00:00+00:00",
                       "snapshot": "260731", "layers": CLEAN}, f)
        rc, out = gate(d)
        self.assertEqual(rc, 0)
        self.assertEqual(out["rulings"][0]["audit_id"], "audit_20260901")


class UnresolvedDataIsNotCleanData(unittest.TestCase):
    """A run reading a path rather than a frozen membership set is exactly the case
    no audit can have covered."""

    def test_a_raw_path_candidate_refuses_production(self):
        rc, out = gate(project(raw_path=True))
        self.assertEqual(rc, 1)
        self.assertEqual(out["datasets"], "unresolved")


class DebugClearsAndStillReports(unittest.TestCase):
    def test_debug_clears_a_fatal_but_says_so(self):
        rc, out = gate(project({"compatibility": {"verdict": "FATAL"}}), mode="debug")
        self.assertEqual(rc, 0)
        self.assertIn("note", out)
        self.assertEqual(state(out), "fatal", "cleared, but the ruling still stands in the record")


class AWaiverIsStamped(unittest.TestCase):
    def test_waive_clears_and_returns_a_stamp_naming_the_state(self):
        rc, out = gate(project({"compatibility": {"verdict": "FATAL"}}),
                       extra=("--waive", "boxes"))
        self.assertEqual(rc, 0)
        w = out["stamp"]["audit_waived"][0]
        self.assertEqual(w["dataset"], "boxes")
        self.assertEqual(w["state"], "fatal")
        self.assertTrue(w["why"])


class TheGateFailsLoudNotOpen(unittest.TestCase):
    def test_unreadable_audit_json_is_exit_2(self):
        d = project()
        p = os.path.join(d, "datasets", "boxes", "audits", "audit_20260818", "audit.json")
        open(p, "w").write("{not json")
        self.assertEqual(gate(d)[0], 2)


class TheSkillsActuallyCallIt(unittest.TestCase):
    def test_train_run_and_eval_run_both_gate_production(self):
        for skill in ("train-run", "eval-run"):
            s = open(os.path.join(REPO_ROOT, "skills", skill, "SKILL.md"), encoding="utf-8").read()
            with self.subTest(skill=skill):
                self.assertIn("audit_gate.py", s)
                self.assertIn("--mode production", s)


if __name__ == "__main__":
    unittest.main()
