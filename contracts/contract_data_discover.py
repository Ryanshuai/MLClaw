"""On a handover, "I could not look" is the answer you get for weeks.

`/data-discover` runs when nobody can tell you what data exists, which means it
runs when access has not arrived yet — credentials come after responsibility
does. So the majority state of its findings, early on, is `unreachable`, and the
single thing that would make the skill actively harmful is spelling that `gone`.
Somebody would spend a week chasing data that is fine, and the one dataset that
really did vanish would be indistinguishable in the noise.

That is CLAUDE.md "Never report data you could not look at", in the domain where
it bites hardest: a census at least knows which machines it asked, while a
discovery sweep does not know what it does not know.

The checks below are grouped by what would go wrong if the code drifted: a
failure to look reported as a finding, a lead recorded without the evidence that
makes it interpretable later, and a report that reads as an inventory.
"""
import json
import os
import unittest

from helpers import TempDirCase, run_script

SCRIPT = "data-discover/discover.py"


class DiscoverCase(TempDirCase):
    def setUp(self):
        super().setUp()
        # A workspace, so resources.json sits one level above the project the
        # way it really does.
        self.project = self.path("ws", "proj")
        os.makedirs(self.project, exist_ok=True)
        self.write_json("ws/proj/project.json", {"name": "proj"})

    def resources(self, **blocks):
        self.write_json("ws/resources.json", {
            "aws": {"access_key_id": "", "secret_access_key": "", "region": "",
                    "s3_bucket": ""},
            "servers": {}, "local": {"base_paths": []},
            "outsourcing": {}, **blocks})

    def record(self, path, *extra, on=None, st="doc", ev="a wiki page"):
        args = ["record", "--project", self.project, "--path", path,
                "--source-type", st, "--evidence", ev]
        if on:
            args += ["--on", on]
        return run_script(SCRIPT, *args, *extra)

    def probe(self, *extra):
        return run_script(SCRIPT, "probe", "--project", self.project, *extra)

    def report(self):
        rc, out, err = run_script(SCRIPT, "report", "--project", self.project, "--json")
        self.assertEqual(rc, 0, err)
        return out

    def table(self):
        rc, out, err = run_script(SCRIPT, "report", "--project", self.project)
        self.assertEqual(rc, 0, err)
        return out

    def leads(self):
        return self.read_json("ws/proj/discovery/leads.json")["leads"]


class CouldNotLookIsNeverGone(DiscoverCase):
    """CLAUDE.md -> "Never silently": never report data you could not look at.

    Four statuses exist so that two specific pairs never collapse. `claim` vs
    `verified` is the /ask-human split. `gone` vs `unreachable` is this one, and
    it is the pair that decides whether the skill is useful or actively
    misleading during the weeks when access is still arriving.
    """

    def test_a_local_path_that_is_not_there_is_gone(self):
        """The one place `gone` is a real reading: we could look, and it is not
        there."""
        self.record(self.path("nope", "highway_2024"))
        rc, out, _ = self.probe()
        self.assertEqual(rc, 1, "a `gone` finding is a verdict, exit 1")
        self.assertEqual(out["counts"]["gone"], 1)
        self.assertEqual(self.leads()[0]["status"], "gone")

    def test_a_directory_that_cannot_be_read_is_unreachable_not_gone(self):
        """Permission-denied is the most common way a sweep manufactures a false
        `gone`: the path exists, the data may be fine, and the only thing that
        happened is that we were not allowed to look."""
        d = self.path("locked")
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o000)
        self.addCleanup(os.chmod, d, 0o755)
        if os.access(d, os.R_OK):
            self.skipTest("running as a user that ignores directory permissions")
        self.record(d)
        rc, out, _ = self.probe()
        self.assertEqual(rc, 0, "not being allowed to look is not a finding")
        self.assertEqual(self.leads()[0]["status"], "unreachable")
        self.assertEqual(out["counts"]["gone"], 0)

    def test_an_s3_lead_with_no_credentials_is_unreachable(self):
        """The handover case exactly: the bucket name came off a wiki page and
        there is no key yet. `absent` here would send somebody hunting."""
        self.resources()
        self.record("s3://some-bucket/highway_2024/")
        rc, _out, _ = self.probe()
        self.assertEqual(rc, 0)
        self.assertEqual(self.leads()[0]["status"], "unreachable")

    def test_an_unknown_server_is_unreachable_not_gone(self):
        self.resources()
        self.record("/mnt/nas/highway", on="server:ghost")
        self.probe()
        lead = self.leads()[0]
        self.assertEqual(lead["status"], "unreachable")
        self.assertIn("ghost", lead["probes"][-1]["detail"])

    def test_an_unprobed_lead_is_a_claim_and_not_a_finding(self):
        """Recording is not checking. A lead sitting at `claim` has to be
        visibly outstanding, or a wiki page's assertions read as results."""
        self.record("s3://b/x/")
        self.assertEqual(self.leads()[0]["status"], "claim")
        r = self.report()
        self.assertEqual(len(r["not_checked"]["unprobed_leads"]), 1)
        self.assertEqual(r["verified"], [])


class ALeadCarriesTheEvidenceOrItIsARumour(DiscoverCase):
    """CLAUDE.md -> "Never silently": never let somebody's word become a checked
    fact. Here the word arrives as a path in a document, and the question a
    reader has six months later is which paths came from code that ran and which
    came from a wiki page written from memory — because that is what decides
    which `gone` is worth escalating.
    """

    def test_evidence_is_required(self):
        rc, _out, err = run_script(SCRIPT, "record", "--project", self.project,
                                   "--path", "/x", "--source-type", "doc")
        self.assertNotEqual(rc, 0, "a lead with no evidence must not be recordable")

    def test_the_source_type_is_recorded_and_survives_probing(self):
        self.record("/x", st="code", ev="train.py:44 DATA_ROOT")
        self.probe()
        lead = self.leads()[0]
        self.assertEqual(lead["source_type"], "code")
        self.assertEqual(lead["evidence"], "train.py:44 DATA_ROOT")

    def test_a_probe_appends_rather_than_replacing(self):
        """Access arrives late, so the interesting record is the sequence: this
        was unreachable in July and verified in August. Overwriting would lose
        the only evidence that the gap existed."""
        self.record(self.path("nope"))
        self.probe()
        self.probe("--all")
        self.assertEqual(len(self.leads()[0]["probes"]), 2)

    def test_a_duplicate_path_is_refused_not_silently_added(self):
        """Two leads for one place get probed twice and reported twice, turning
        one location into two findings and inflating every count."""
        self.record("/x")
        rc, out, _ = self.record("/x")
        self.assertEqual(rc, 1)
        self.assertIn("already recorded", out["refused"])
        self.assertEqual(len(self.leads()), 1)
        rc, _, _ = self.record("/x", "--again")
        self.assertEqual(rc, 0, "--again is the deliberate path")


class ASweepNeverReadsAsAnInventory(DiscoverCase):
    """CLAUDE.md -> "Never silently": a count from a partial reading is a lower
    bound and must be said as one.

    A discovery report is worse than a partial census in one respect: a census
    knows which machines it failed to ask, while a sweep cannot know about data
    nobody wrote down. Handing somebody a findings list on day three of a
    handover, with the caveats underneath, is how a missing dataset is
    discovered in month four.
    """

    def test_the_report_states_it_is_not_exhaustive(self):
        self.record("/x")
        r = self.report()
        self.assertFalse(r["exhaustive"])
        self.assertTrue(r["why_not_exhaustive"])

    def test_an_unmeasured_size_prints_as_a_dash_and_never_as_zero(self):
        """The table's `—` carries three different facts — nobody looked, we
        could not look, or the walk ran out of budget — and the row's status says
        which. A `0` would collapse all three into "there is no data here",
        which is the one conclusion none of them supports.
        """
        self.record("s3://b/x/")                       # recorded, never probed
        t = self.table()
        self.assertIn("—", t)
        self.assertNotIn(" 0 B", t)
        self.assertIn("LOWER BOUND", t,
                      "a total with anything unmeasured must say it is a floor")

    def test_a_measured_total_is_flagged_as_a_lower_bound_in_json_too(self):
        os.makedirs(self.path("real"), exist_ok=True)
        with open(self.path("real", "a.bin"), "wb") as fh:
            fh.write(b"x" * 2048)
        self.record(self.path("real"))
        self.record("s3://b/never-probed/", "--again")
        self.probe("--id", "lead_0001")
        m = self.report()["measured"]
        self.assertEqual(m["bytes"], 2048)
        self.assertEqual(m["files"], 1)
        self.assertTrue(m["is_lower_bound"])

    def test_gaps_are_keyed_above_the_findings(self):
        """Order, not merely presence: `not_checked` precedes the result keys so
        that reading top to bottom reaches the caveat first."""
        self.record("/x")
        keys = list(self.report())
        self.assertLess(keys.index("not_checked"), keys.index("verified"))
        self.assertLess(keys.index("not_checked"), keys.index("counts"))

    def test_every_source_says_whether_it_is_usable_and_why_not(self):
        """`sources` exists to make "what did you not check" answerable at all.
        Without a list of what could have been checked, a findings list is
        unfalsifiable — it looks identical after one probe and after twenty.

        Asserted structurally rather than on a count, because whether *this*
        machine has AWS credentials is not a property of MLClaw.
        """
        self.resources(servers={"box": {"host": "10.0.0.5"}, "nohost": {}})
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["sources"])
        for s in out["sources"]:
            self.assertIn("usable", s, f"{s} does not say whether it is usable")
            if not s["usable"]:
                self.assertTrue(s.get("why"), f"{s['source']} is blocked with no reason")
        blocked = [s for s in out["sources"] if not s["usable"]]
        self.assertEqual(out["counts"]["blocked"], len(blocked))
        if blocked:
            self.assertIn("UNREACHABLE", out["note"],
                          "a blocked source must be said to yield UNREACHABLE, "
                          "not nothing")

    def test_a_missing_resources_file_is_a_blocked_source_not_an_error(self):
        """Day one of a handover: nothing is registered yet. That is the normal
        state this skill runs in, so it must report rather than fail."""
        rc, out, err = run_script(SCRIPT, "sources", "--project", self.project)
        self.assertEqual(rc, 0, err)
        self.assertTrue(any(s["source"] == "resources.json" and not s["usable"]
                            for s in out["sources"]))

    def test_a_vendor_is_never_probed(self):
        """An outsourcing party can hold the only copy of a batch, and no
        listing will ever reveal it. They are a source of answers, so the report
        routes to /ask-human instead of pretending a probe could settle it."""
        self.resources(outsourcing={"vendor-a": {"name": "Vendor A"}})
        _rc, out, _ = run_script(SCRIPT, "sources", "--project", self.project)
        row = next(s for s in out["sources"] if s["source"] == "outsourcing:vendor-a")
        self.assertFalse(row["usable"])
        self.assertEqual(row["fix"], "/ask-human")

    def test_it_never_declares_a_dataset(self):
        """`identity.unit_glob`'s depth decides every unit id and a wrong depth
        yields zero units with no error. A guessed contract is worse than none,
        so discovery hands over a verified path and stops."""
        os.makedirs(self.path("real", "260731", "s000"), exist_ok=True)
        self.record(self.path("real"))
        self.probe()
        self.assertEqual(self.leads()[0]["status"], "verified")
        self.assertFalse(os.path.isdir(os.path.join(self.project, "datasets")),
                         "discovery must not create a dataset declaration")
        self.assertIn("/data-check", self.report()["next"])


if __name__ == "__main__":
    unittest.main()
