"""A rig that changed while everything kept working.

Generalized from a real hand-held capture rig, where the failure is documented
in the operator's own notes: a stereo camera was replaced, nothing said so, and
because the factory baseline is the only metric anchor a hand-held rig has,
every measurement taken afterwards was off by a fixed ratio. No error, no failed
capture, no log line. The data reconstructs perfectly and means something else.

That is the bar in CLAUDE.md "Contracts": a record written now and read later by
someone who can no longer verify it. Nobody re-derives, a year on, which
physical camera produced a scene — the only chance to know is a reading taken at
the time.

The three groups below each pin one half of the design that came out of that:

  the classification    `shifts` is watched, `breaks` needs no watching
  the anchor rule       watch a cheap proxy, read the expensive truth live
  the capture asymmetry `check` refuses, `stamp` never does
"""
import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "data-collect/rig.py"


class RigCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.project = self.path("proj")
        self.capture = self.path("capture")
        os.makedirs(self.capture, exist_ok=True)
        self.rig = "pole"

    def declare(self, facts, stamp=None):
        self.write_json(f"proj/rigs/{self.rig}/rig.json", {
            "rig_id": self.rig, "project": self.project, "facts": facts,
            "stamp": stamp or {"path": "_rig", "filename": "rig_stamp.json"},
            "created_at": "2026-07-31T00:00:00+00:00",
            "updated_at": "2026-07-31T00:00:00+00:00"})

    @staticmethod
    def fact(value=None, *, probe=None, on_change="shifts", runtime_only=False,
             evidence="measured on the box 2026-07-27"):
        return {"value": value, "evidence": evidence, "probe": probe,
                "on_change": on_change, "runtime_only": runtime_only}

    def check(self, *extra):
        return run_script(SCRIPT, "check", "--project", self.project,
                          "--rig", self.rig, *extra)

    def stamp(self, *extra):
        return run_script(SCRIPT, "stamp", "--project", self.project,
                          "--rig", self.rig, "--into", self.capture, *extra)

    def stamp_file(self):
        with open(os.path.join(self.capture, "_rig", "rig_stamp.json")) as f:
            return json.load(f)


class ShiftsIsWatchedBreaksIsNot(RigCase):
    """CLAUDE.md -> "Never silently": never let a capture-rig change pass as
    unchanged.

    The classification is the whole design. A change that stops the capture
    reports itself; a change that leaves the capture working and alters what the
    data means reports nothing, ever, and is the only kind a tripwire can help
    with.
    """

    def test_a_changed_shifts_fact_fires_and_refuses(self):
        self.declare({"serial": self.fact("49403954", probe="echo 49999111")})
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertEqual(out["verdict"], "TRIPWIRE")
        self.assertEqual(out["tripwires_fired"], ["serial"])

    def test_the_reading_shows_both_sides_of_the_change(self):
        """A tripwire that says only "something changed" cannot be acted on."""
        self.declare({"serial": self.fact("49403954", probe="echo 49999111")})
        _, out, _ = self.check()
        row = next(f for f in out["facts"] if f["fact"] == "serial")
        self.assertEqual(row["recorded"], "49403954")
        self.assertEqual(row["observed"], "49999111")

    def test_a_changed_breaks_fact_does_not_fire_a_tripwire(self):
        """It is reported, but it needs no watching: a camera that is not there
        fails the capture in seconds. Firing here too would train people to
        ignore the exit code that matters."""
        self.declare({"ip": self.fact("192.168.1.10", probe="echo 192.168.1.99",
                                      on_change="breaks")})
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual(out["tripwires_fired"], [])
        self.assertEqual(out["breaking_changes"], ["ip"])

    def test_an_unchanged_rig_needs_no_override(self):
        """A guard that always fires is a guard nobody reads."""
        self.declare({"serial": self.fact("49403954", probe="echo 49403954")})
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "ok")

    def test_a_shifts_fact_with_no_probe_reads_as_unwatchable(self):
        """The defining property of `shifts` is that its change is invisible.
        An unwatchable one is invisible twice over, so silence here would be the
        worst possible report."""
        self.declare({"lens": self.fact("wide-2.1mm", probe=None)})
        _, out, _ = self.check()
        self.assertEqual(out["unwatchable_shifts"], ["lens"])

    def test_an_unknown_on_change_is_rejected_not_defaulted(self):
        self.declare({"serial": self.fact("x", probe="echo x", on_change="maybe")})
        rc, _, _ = self.check()
        self.assertEqual(rc, 2)


class TheAnchorIsReadNeverStored(RigCase):
    """CLAUDE.md -> "Never silently": a value read once and stored stops tracking
    the hardware it describes.

    Watch a cheap proxy, read the expensive truth. A stereo baseline is the
    metric anchor for everything downstream; stored as config it drifts from the
    hardware silently, which is the fake-metric bug one layer below the model.
    """

    def test_a_runtime_only_fact_carrying_a_value_is_refused(self):
        self.declare({"baseline_mm": self.fact("119.87", probe="echo 120.44",
                                               runtime_only=True)})
        rc, out, _ = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("baseline_mm", out["refused"])

    def test_a_runtime_only_fact_is_read_not_compared(self):
        self.declare({"baseline_mm": self.fact(None, probe="echo 120.44",
                                               runtime_only=True)})
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual(out["runtime_readings"], {"baseline_mm": "120.44"})
        self.assertEqual(out["tripwires_fired"], [])

    def test_the_live_reading_reaches_the_capture_tree(self):
        """The reading is the product. If it does not land in the data, the
        session has no anchor and nothing later can supply one."""
        self.declare({"baseline_mm": self.fact(None, probe="echo 120.44",
                                               runtime_only=True)})
        self.stamp("--session", "260731")
        self.assertEqual(self.stamp_file()["runtime_readings"],
                         {"baseline_mm": "120.44"})


class NotProbedIsNotUnchanged(RigCase):
    """run-mechanics.md -> "Record integrity": extraction failure and absence are
    different facts and must not become the same value.

    A probe that was skipped, and a probe that ran and matched, are the same
    empty tripwire list unless the difference is carried explicitly.
    """

    def test_no_probe_records_not_checked(self):
        self.declare({"serial": self.fact("49403954", probe="echo 49999111")})
        rc, out, _ = self.check("--no-probe")
        self.assertEqual(out["not_checked"], ["serial"])
        self.assertEqual(out["verdict"], "incomplete")
        self.assertEqual(out["tripwires_fired"], [], "must not claim a match either")

    def test_a_failing_probe_is_a_finding_not_a_crash(self):
        self.declare({"serial": self.fact("49403954", probe="exit 3")})
        rc, out, _ = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual(out["probe_failures"], ["serial"])
        self.assertEqual(out["verdict"], "incomplete")


class CaptureIsNeverBlocked(RigCase):
    """CLAUDE.md -> "Script Integration": exit 1 = worked, the answer is no.

    `check` refuses; `stamp` deliberately does not. An operator is standing in
    front of frames that cannot be re-shot, and the alternative to capturing
    with a changed rig is not capturing at all. The change is recorded, not
    obeyed — but the record has to reach the data, or the concession buys
    nothing.
    """

    def test_stamp_writes_even_when_a_tripwire_fired(self):
        self.declare({"serial": self.fact("49403954", probe="echo 49999111")})
        self.assertEqual(self.check()[0], 1)
        rc, out, err = self.stamp("--session", "260731")
        self.assertEqual(rc, 0, f"stamp must not block a capture: {err}")
        self.assertEqual(out["verdict"], "TRIPWIRE")

    def test_the_fired_tripwire_travels_in_the_stamp(self):
        """Inside the capture tree, so a reader of the data alone — after an
        rsync to a machine that never heard of this project — still sees it."""
        self.declare({"serial": self.fact("49403954", probe="echo 49999111")})
        self.stamp("--session", "260731")
        rec = self.stamp_file()
        self.assertEqual(rec["verdict"], "TRIPWIRE")
        self.assertEqual(rec["tripwires_fired"], ["serial"])
        self.assertEqual(rec["session"], "260731")

    def test_a_second_stamp_does_not_silently_replace_the_first(self):
        """A capture tree describing two different rig readings cannot say which
        one produced its data."""
        self.declare({"serial": self.fact("49403954", probe="echo 49403954")})
        self.assertEqual(self.stamp()[0], 0)
        self.assertEqual(self.stamp()[0], 1)
        self.assertEqual(self.stamp("--overwrite")[0], 0)

    def test_a_missing_capture_root_breaks_rather_than_refusing(self):
        """Exit 2, so the fallback rule applies: this is the script being unable
        to work, not a check saying no."""
        self.declare({"serial": self.fact("x", probe="echo x")})
        rc, _, _ = run_script(SCRIPT, "stamp", "--project", self.project,
                              "--rig", self.rig, "--into", self.path("nope"))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
