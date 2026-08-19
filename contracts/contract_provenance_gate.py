"""The provenance gate: a production run may not launch on a value nobody read.

Enforces run-mechanics.md -> "Record integrity", the row beginning "A **production**
run launches only once `provenance.json` has no `blocking` / `guessed` /
`unverified` entry left". Earns a check on the bar in CLAUDE.md -> "Contracts": a
record written now and read later by someone who can no longer verify it.

The direction that matters is a FALSE CLEAR. A refusal costs one message; a clear
costs a `run.json` that states a guess as a measurement, which `/conclude` then
cites and `baseline_delta` subtracts, both correctly.
"""
import json
import os
import subprocess
import tempfile
import unittest

from helpers import REPO_ROOT

GATE = os.path.join(REPO_ROOT, "lifecycle", "scripts", "train-run", "provenance_gate.py")


def project(unresolved, source_mode="inherited", write=True):
    d = tempfile.mkdtemp()
    stage = os.path.join(d, "stages", "training")
    os.makedirs(stage)
    if write:
        with open(os.path.join(stage, "provenance.json"), "w", encoding="utf-8") as f:
            json.dump({"source_mode": source_mode, "unresolved": unresolved}, f)
    return d


def gate(proj, mode, waive=()):
    cmd = ["python3", GATE, "check", "--project", proj, "--mode", mode]
    for k in waive:
        cmd += ["--waive", k]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None)


GUESSED = [{"key": "output.metrics.primary_metric", "status": "guessed",
            "why": "5 metrics logged, none marked primary"}]
ABSENT = [{"key": "config.done_signal", "status": "absent",
           "why": "no done signal is emitted at all"}]


class ProductionRefusesAnUnreadValue(unittest.TestCase):
    """run-mechanics.md -> "Record integrity", the provenance row."""

    def test_guessed_refuses_production(self):
        rc, out = gate(project(GUESSED), "production")
        self.assertEqual(rc, 1)
        self.assertFalse(out["cleared"])
        self.assertEqual(len(out["guessed"]), 1)

    def test_unverified_refuses_production(self):
        """CLAUDE.md: never let somebody's word become a checked fact. A README
        number in a production record becomes one."""
        rc, out = gate(project([{"key": "output.metrics.primary_metric",
                                 "status": "unverified", "why": "from the paper's README"}]),
                       "production")
        self.assertEqual(rc, 1)
        self.assertEqual(len(out["unverified"]), 1)

    def test_blocking_refuses_production(self):
        rc, _ = gate(project([{"key": "config.entry_command", "status": "blocking",
                               "why": "no entry point found"}]), "production")
        self.assertEqual(rc, 1)

    def test_an_unenumerated_status_does_not_fall_through_as_clear(self):
        """A typo'd status must not be a way past the gate."""
        rc, out = gate(project([{"key": "x", "status": "probably-fine", "why": "?"}]),
                       "production")
        self.assertEqual(rc, 1)
        self.assertEqual(len(out["unknown_status"]), 1)

    def test_a_missing_provenance_is_not_a_clear_production(self):
        """Absent provenance and 'nothing was guessed' are different facts."""
        rc, out = gate(project([], write=False), "production")
        self.assertEqual(rc, 1)
        self.assertEqual(out["provenance"], "missing")


class AbsentIsAConclusionNotAGap(unittest.TestCase):
    """run-mechanics.md, the same row: `absent` must never block -- treating it as
    one is how a correct record gets edited to make a gate pass."""

    def test_absent_alone_clears_production(self):
        rc, out = gate(project(ABSENT), "production")
        self.assertEqual(rc, 0)
        self.assertTrue(out["cleared"])
        self.assertEqual(out["absent_not_a_gap"], 1)

    def test_absent_is_not_counted_among_offenders(self):
        rc, out = gate(project(ABSENT + GUESSED), "production")
        self.assertEqual(rc, 1)
        self.assertEqual(out["absent_not_a_gap"], 1)
        self.assertEqual(out["guessed"][0]["key"], "output.metrics.primary_metric")


class DebugIsWhereAGuessBelongs(unittest.TestCase):
    """The gate is the boundary between finding out whether it runs and putting a
    number into the record."""

    def test_debug_clears_with_guesses_and_says_so(self):
        rc, out = gate(project(GUESSED), "debug")
        self.assertEqual(rc, 0)
        self.assertTrue(out["cleared"])
        self.assertIn("note", out)
        self.assertEqual(len(out["guessed"]), 1, "cleared, but still reported")


class AWaiverIsRecordedNotAvoided(unittest.TestCase):
    """`retire.py --waive cited_by_snapshot`'s shape: the loss is stamped, not
    skipped. A waiver outside the record is a flag."""

    def test_waive_clears_and_returns_a_stamp_for_run_json(self):
        rc, out = gate(project(GUESSED), "production",
                       waive=["output.metrics.primary_metric"])
        self.assertEqual(rc, 0)
        self.assertIn("stamp", out)
        w = out["stamp"]["provenance_waived"]
        self.assertEqual(w[0]["key"], "output.metrics.primary_metric")
        self.assertEqual(w[0]["status"], "guessed", "the stamp keeps WHICH kind it was")
        self.assertTrue(w[0]["why"], "and why, or the record cannot be read later")

    def test_waiving_one_key_does_not_clear_another(self):
        two = GUESSED + [{"key": "config.done_signal", "status": "guessed", "why": "inferred"}]
        rc, out = gate(project(two), "production", waive=["output.metrics.primary_metric"])
        self.assertEqual(rc, 1)
        self.assertEqual([i["key"] for i in out["guessed"]], ["config.done_signal"])


class TheGateFailsLoudNotOpen(unittest.TestCase):
    """CLAUDE.md -> "Script Integration": exit 2 = the script broke, fall back;
    exit 1 = it worked and the answer is no. A gate that crashes must not read
    as a pass, so unreadable input is 2 and never 0."""

    def test_unreadable_json_is_exit_2_not_a_clear(self):
        d = project([], write=False)
        with open(os.path.join(d, "stages", "training", "provenance.json"), "w") as f:
            f.write("{not json")
        rc, out = gate(d, "production")
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertIn("fix", out)

    def test_unresolved_of_the_wrong_type_is_exit_2(self):
        d = project([], write=False)
        with open(os.path.join(d, "stages", "training", "provenance.json"), "w") as f:
            json.dump({"unresolved": {"key": "x"}}, f)
        self.assertEqual(gate(d, "production")[0], 2)


class TheSkillActuallyCallsIt(unittest.TestCase):
    """A gate no skill invokes is a file, not a check."""

    def test_train_run_calls_the_gate_in_production_mode(self):
        s = open(os.path.join(REPO_ROOT, "skills", "train-run", "SKILL.md"),
                 encoding="utf-8").read()
        self.assertIn("provenance_gate.py", s)
        self.assertIn("--mode production", s)
        i, j = s.index("**Production mode"), s.index("provenance_gate.py")
        self.assertLess(j - i, 400, "the gate must be the FIRST thing production does")


if __name__ == "__main__":
    unittest.main()
